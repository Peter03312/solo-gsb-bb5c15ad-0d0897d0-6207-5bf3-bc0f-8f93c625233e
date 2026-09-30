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

-- ------------------------------------------------------------------ 推演（模拟修订，不触碰正式断言）
-- 创建时固定：基线正式修订号、已登记地层快照、生效断言快照
CREATE TABLE IF NOT EXISTS simulations (
    sim_id          TEXT PRIMARY KEY,
    base_revision   INTEGER NOT NULL,                 -- 创建时的正式修订号（固定，不再随正式历史移动）
    sim_revision    INTEGER NOT NULL DEFAULT 0,       -- 推演内部修订号（沿用批次原子递增语义）
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    label           TEXT,
    merged_revision INTEGER,                          -- 并入后落地的正式修订号；净变更为空时不递增，保持 NULL
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at       TIMESTAMPTZ
);

-- 推演内的已登记地层：创建时快照当前正式地层；推演内新增断言只能引用这些地层
CREATE TABLE IF NOT EXISTS sim_strata (
    sim_id     TEXT NOT NULL REFERENCES simulations(sim_id),
    stratum_id TEXT NOT NULL,
    site       TEXT,
    PRIMARY KEY (sim_id, stratum_id)
);

-- 推演内断言只增不删，撤回仅置状态。
-- origin='baseline'：创建时从正式生效断言复制（证据编号=正式编号）；
-- origin='sim'：推演内新增（证据编号仅在本推演命名空间内唯一）。
CREATE TABLE IF NOT EXISTS sim_assertions (
    sim_id              TEXT NOT NULL REFERENCES simulations(sim_id),
    evidence_id         TEXT NOT NULL,
    stratum_a           TEXT NOT NULL,
    stratum_b           TEXT NOT NULL,
    relation            TEXT NOT NULL CHECK (relation IN ('same_period', 'earlier_than')),
    status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retracted')),
    origin              TEXT NOT NULL CHECK (origin IN ('baseline', 'sim')),
    added_sim_revision  INTEGER NOT NULL,
    retracted_sim_revision INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    retracted_at        TIMESTAMPTZ,
    PRIMARY KEY (sim_id, evidence_id)
);

-- 推演内每次成功批次的审计记录
CREATE TABLE IF NOT EXISTS sim_revisions (
    sim_id     TEXT NOT NULL REFERENCES simulations(sim_id),
    sim_revision INTEGER NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    batch      JSONB NOT NULL,
    PRIMARY KEY (sim_id, sim_revision)
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
