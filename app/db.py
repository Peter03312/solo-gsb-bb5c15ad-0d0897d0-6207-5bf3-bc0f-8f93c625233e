"""PostgreSQL 连接与模式初始化。"""
import os
import time
from contextlib import contextmanager

import psycopg2
import psycopg2.pool

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://strat:strat@localhost:5432/stratigraphy",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS strata (
    stratum_id  TEXT PRIMARY KEY,
    site        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 断言只增不删：撤回仅置状态，保证历史版本可回溯、证据编号永久唯一
CREATE TABLE IF NOT EXISTS assertions (
    evidence_id        TEXT PRIMARY KEY,
    stratum_a          TEXT NOT NULL REFERENCES strata(stratum_id),
    stratum_b          TEXT NOT NULL REFERENCES strata(stratum_id),
    relation           TEXT NOT NULL CHECK (relation IN ('same_period', 'earlier_than')),
    status             TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retracted')),
    added_revision     INTEGER NOT NULL,
    retracted_revision INTEGER,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    retracted_at       TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS revision_counter (
    id       BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    revision INTEGER NOT NULL DEFAULT 0
);
INSERT INTO revision_counter (id, revision) VALUES (TRUE, 0) ON CONFLICT DO NOTHING;

-- 每次成功批次的审计记录
CREATE TABLE IF NOT EXISTS revisions (
    revision   INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    batch      JSONB NOT NULL
);

-- 定版：不可变关系快照。结论行一旦写入永不修改、永不删除
CREATE TABLE IF NOT EXISTS snapshots (
    version    INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    revision   INTEGER NOT NULL,
    label      TEXT,
    assertions JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS snapshot_conclusions (
    version   INTEGER NOT NULL REFERENCES snapshots(version),
    stratum_a TEXT NOT NULL,
    stratum_b TEXT NOT NULL,
    relation  TEXT NOT NULL CHECK (relation IN ('same_period', 'earlier_than', 'later_than')),
    path      JSONB NOT NULL,
    PRIMARY KEY (version, stratum_a, stratum_b)
);
"""

_pool: "psycopg2.pool.ThreadedConnectionPool | None" = None


def init_pool(retries: int = 30, delay: float = 1.0) -> None:
    """启动时初始化连接池并建表（幂等），等待数据库就绪。"""
    global _pool
    last_err: Exception | None = None
    for _ in range(retries):
        try:
            pool = psycopg2.pool.ThreadedConnectionPool(1, 10, DATABASE_URL)
            conn = pool.getconn()
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute(SCHEMA)
            finally:
                pool.putconn(conn)
            _pool = pool
            return
        except Exception as exc:  # noqa: BLE001 - 启动重试阶段需兜底
            last_err = exc
            time.sleep(delay)
    raise RuntimeError(f"无法连接数据库（{DATABASE_URL}）: {last_err}")


@contextmanager
def db_conn():
    """事务上下文：正常退出提交，异常回滚，连接归还连接池。"""
    assert _pool is not None, "连接池未初始化"
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)
