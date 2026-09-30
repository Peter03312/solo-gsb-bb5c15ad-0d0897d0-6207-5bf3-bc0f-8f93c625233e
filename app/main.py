"""考古地层关系核验服务 HTTP API。"""
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import db, service, simulation
from .derivation import RELATION_LABELS
from .errors import ApiError
from .models import BatchIn, SimBatchIn, SimulationIn, SnapshotIn, StratumIn


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_pool()
    yield


app = FastAPI(
    title="考古地层关系核验服务",
    version="1.1.0",
    description=(
        "登记遗址地层与带唯一证据编号的“同一期 / 早于”断言；"
        "同一期可传递，早于沿同一期与早于路径推导；"
        "批次携带修订号、按整批最终关系核验，矛盾整批拒绝；"
        "支持关系查询（含支撑证据路径）与不可变定版快照；"
        "支持推演：固定基线的模拟修订，推演内批次核验，"
        "并入时把相对基线的净变更应用于最新正式状态重新核验后原子提交。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def index():
    return {
        "service": "考古地层关系核验服务",
        "docs": "/docs",
        "relations": RELATION_LABELS,
    }


# ------------------------------------------------------------------ 地层
@app.post("/strata", status_code=201, tags=["strata"])
def create_stratum(body: StratumIn):
    """登记遗址地层。"""
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM strata WHERE stratum_id = %s", (body.stratum_id,))
            if cur.fetchone() is not None:
                raise ApiError(409, "STRATUM_EXISTS", f"地层已登记: {body.stratum_id}", {"stratum_id": body.stratum_id})
            cur.execute(
                "INSERT INTO strata (stratum_id, site) VALUES (%s, %s)",
                (body.stratum_id, body.site),
            )
    return {"stratum_id": body.stratum_id, "site": body.site}


@app.get("/strata", tags=["strata"])
def list_strata():
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT stratum_id, site, created_at FROM strata ORDER BY stratum_id")
            return [
                {"stratum_id": r[0], "site": r[1], "created_at": r[2].isoformat()}
                for r in cur.fetchall()
            ]


# ------------------------------------------------------------------ 断言批次
@app.post("/batches", tags=["batches"])
def post_batch(batch: BatchIn):
    """提交一批断言新增/撤回。整批核验，成功则原子递增修订号。"""
    return service.apply_batch(batch)


@app.get("/revision", tags=["batches"])
def get_revision():
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE")
            return {"revision": cur.fetchone()[0]}


@app.get("/assertions", tags=["batches"])
def list_assertions(status: Optional[str] = Query(default=None, pattern="^(active|retracted)$")):
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            sql = (
                "SELECT evidence_id, stratum_a, stratum_b, relation, status, added_revision, retracted_revision "
                "FROM assertions"
            )
            params = ()
            if status:
                sql += " WHERE status = %s"
                params = (status,)
            sql += " ORDER BY evidence_id"
            cur.execute(sql, params)
            return [
                {
                    "evidence_id": r[0],
                    "stratum_a": r[1],
                    "stratum_b": r[2],
                    "relation": r[3],
                    "status": r[4],
                    "added_revision": r[5],
                    "retracted_revision": r[6],
                }
                for r in cur.fetchall()
            ]


# ------------------------------------------------------------------ 关系查询
@app.get("/relationship", tags=["query"])
def get_relationship(a: str = Query(description="地层编号"), b: str = Query(description="地层编号")):
    """查询两地层当前关系：同一期 / 早于 / 晚于 / 未定，附支撑证据路径。"""
    return service.query_relationship(a, b)


# ------------------------------------------------------------------ 推演（模拟修订）
@app.post("/simulations", status_code=201, tags=["simulations"])
def post_simulation(body: SimulationIn):
    """创建推演：固定当前正式修订号、已登记地层与生效断言；不改动正式数据。"""
    return simulation.create_simulation(body)


@app.get("/simulations", tags=["simulations"])
def get_simulations():
    """推演列表。"""
    return simulation.list_simulations()


@app.get("/simulations/{sim_id}", tags=["simulations"])
def get_simulation_detail(sim_id: str):
    """推演详情：基线/推演修订号、固定地层、推演内断言及相对基线的净变更。"""
    return simulation.get_simulation(sim_id)


@app.get("/simulations/{sim_id}/assertions", tags=["simulations"])
def get_simulation_assertions(
    sim_id: str, status: Optional[str] = Query(default=None, pattern="^(active|retracted)$")
):
    """推演内断言列表（含 baseline / sim 来源标记）。"""
    return simulation.list_sim_assertions(sim_id, status)


@app.post("/simulations/{sim_id}/batches", tags=["simulations"])
def post_simulation_batch(sim_id: str, batch: SimBatchIn):
    """推演内提交一批新增/撤回；携带推演修订号，沿用整批核验语义。"""
    return simulation.apply_sim_batch(sim_id, batch)


@app.get("/simulations/{sim_id}/relationship", tags=["simulations"])
def get_simulation_relationship(
    sim_id: str, a: str = Query(description="地层编号"), b: str = Query(description="地层编号")
):
    """查询推演内两地层关系及支撑证据路径（证据标注 baseline/sim 来源）。"""
    return simulation.query_sim_relationship(sim_id, a, b)


@app.post("/simulations/{sim_id}/merge", tags=["simulations"])
def post_simulation_merge(sim_id: str):
    """把推演净变更并入最新正式状态并重新核验；通过则作为一次正式批次原子提交并关闭推演。"""
    return simulation.merge_simulation(sim_id)


# ------------------------------------------------------------------ 定版
@app.post("/snapshots", status_code=201, tags=["snapshots"])
def post_snapshot(body: SnapshotIn):
    """定版：保存当前修订号下全部关系结论的不可变快照。"""
    return service.create_snapshot(body.label)


@app.get("/snapshots", tags=["snapshots"])
def list_snapshots():
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version, revision, label, created_at FROM snapshots ORDER BY version")
            return [
                {"version": r[0], "revision": r[1], "label": r[2], "created_at": r[3].isoformat()}
                for r in cur.fetchall()
            ]


@app.get("/snapshots/{version}", tags=["snapshots"])
def get_snapshot(version: int):
    return service.get_snapshot(version)


@app.get("/snapshots/{version}/relationship", tags=["snapshots"])
def get_snapshot_relationship(version: int, a: str = Query(...), b: str = Query(...)):
    """查询历史版本结论；后续撤回不影响旧版。"""
    return service.query_snapshot_relationship(version, a, b)
