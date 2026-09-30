"""推演（模拟修订）业务逻辑：创建、推演内批次、关系查询、并入正式状态。

核心约束：
- 创建推演时固定当前正式修订号（base_revision）、已登记地层、生效断言；
  后续正式批次不会改变任何已有推演的基线。
- 推演内沿用正式批次的新增/撤回/整批核验语义，但携带的是推演修订号。
- 正式历史已用证据编号不可复用（创建时基线内的正式编号即被占用）；
  推演新增编号在本推演内不可复用（撤回后也不可复用）。
- 并入时取推演相对基线的净变更应用于最新正式状态并重新核验；
  基线目标已在正式状态撤回、编号已被正式占用或关系冲突时，整体拒绝且双方不变；
  通过则作为一次正式批次原子提交并关闭推演；净变更为空时只关闭推演，不递增正式修订号。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from psycopg2.extras import Json

from .db import db_conn
from .derivation import Assertion, Derivation, RELATION_LABELS
from .errors import ApiError
from .models import AssertOp, SimBatchIn, SimulationIn

_SIM_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


# ------------------------------------------------------------------ 通用工具
def _sim_row(cur, sim_id: str, for_update: bool = False) -> Tuple:
    sql = (
        "SELECT sim_id, base_revision, sim_revision, status, label, merged_revision "
        "FROM simulations WHERE sim_id = %s"
    )
    if for_update:
        sql += " FOR UPDATE"
    cur.execute(sql, (sim_id,))
    row = cur.fetchone()
    if row is None:
        raise ApiError(404, "SIMULATION_NOT_FOUND", f"推演不存在: {sim_id}", {"sim_id": sim_id})
    return row


def _require_open(row: Tuple) -> None:
    if row[3] != "open":
        raise ApiError(
            409,
            "SIMULATION_CLOSED",
            f"推演已关闭，不可再操作: {row[0]}",
            {"sim_id": row[0], "status": row[3], "merged_revision": row[5]},
        )


def _load_sim_active(cur, sim_id: str) -> List[Assertion]:
    cur.execute(
        "SELECT evidence_id, stratum_a, stratum_b, relation "
        "FROM sim_assertions WHERE sim_id = %s AND status = 'active' ORDER BY evidence_id",
        (sim_id,),
    )
    return [Assertion(evidence_id=r[0], a=r[1], b=r[2], relation=r[3]) for r in cur.fetchall()]


def _ensure_sim_strata(cur, sim_id: str, *stratum_ids: str) -> None:
    for s in stratum_ids:
        cur.execute(
            "SELECT 1 FROM sim_strata WHERE sim_id = %s AND stratum_id = %s",
            (sim_id, s),
        )
        if cur.fetchone() is None:
            raise ApiError(
                400,
                "INVALID_STRATUM",
                f"地层未登记（创建推演时已固定，推演内不可新增地层）: {s}",
                {"sim_id": sim_id, "stratum_id": s},
            )


def _sim_path_details(cur, sim_id: str, path: Optional[List[str]]) -> List[Dict[str, Any]]:
    """把证据编号序列展开为可读推导链（取自推演内断言视图）。"""
    if not path:
        return []
    cur.execute(
        "SELECT evidence_id, stratum_a, stratum_b, relation, status, origin "
        "FROM sim_assertions WHERE sim_id = %s AND evidence_id = ANY(%s)",
        (sim_id, list(path)),
    )
    rows = {r[0]: r for r in cur.fetchall()}
    return [
        {
            "evidence_id": eid,
            "stratum_a": rows[eid][1],
            "stratum_b": rows[eid][2],
            "relation": rows[eid][3],
            "status": rows[eid][4],
            "origin": rows[eid][5],
        }
        for eid in path
        if eid in rows
    ]


def _sim_relation_payload(cur, sim_id: str, a: str, b: str) -> Dict[str, Any]:
    derivation = Derivation(_load_sim_active(cur, sim_id))
    relation, path = derivation.relation(a, b)
    return {
        "stratum_a": a,
        "stratum_b": b,
        "relation": relation,
        "relation_label": RELATION_LABELS[relation],
        "evidence_path": path,
        "path_detail": _sim_path_details(cur, sim_id, path),
    }


# ------------------------------------------------------------------ 创建推演
def create_simulation(body: SimulationIn) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            # 锁定正式修订计数器，基线与某一正式修订严格对应
            cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE FOR UPDATE")
            base_revision = cur.fetchone()[0]

            sim_id = body.sim_id
            if sim_id is not None:
                if not _SIM_ID_RE.match(sim_id):
                    raise ApiError(
                        400,
                        "INVALID_SIM_ID",
                        "推演编号仅允许字母、数字、点、下划线、短横线",
                        {"sim_id": sim_id},
                    )
                cur.execute("SELECT 1 FROM simulations WHERE sim_id = %s", (sim_id,))
                if cur.fetchone() is not None:
                    raise ApiError(
                        409, "SIMULATION_EXISTS", f"推演编号已存在: {sim_id}", {"sim_id": sim_id}
                    )
            else:
                sim_id = _allocate_sim_id(cur)

            cur.execute(
                "INSERT INTO simulations (sim_id, base_revision, label) VALUES (%s, %s, %s)",
                (sim_id, base_revision, body.label),
            )

            # 固定已登记地层
            cur.execute("INSERT INTO sim_strata (sim_id, stratum_id, site) SELECT %s, stratum_id, site FROM strata", (sim_id,))

            # 固定生效断言（正式证据编号随基线复制，在推演内被视为已占用）
            cur.execute(
                "INSERT INTO sim_assertions "
                "(sim_id, evidence_id, stratum_a, stratum_b, relation, status, origin, added_sim_revision) "
                "SELECT %s, evidence_id, stratum_a, stratum_b, relation, 'active', 'baseline', 0 "
                "FROM assertions WHERE status = 'active'",
                (sim_id,),
            )
            cur.execute("SELECT count(*) FROM sim_strata WHERE sim_id = %s", (sim_id,))
            strata_count = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM sim_assertions WHERE sim_id = %s AND origin = 'baseline'",
                (sim_id,),
            )
            assertion_count = cur.fetchone()[0]
            return {
                "sim_id": sim_id,
                "base_revision": base_revision,
                "sim_revision": 0,
                "status": "open",
                "label": body.label,
                "strata": strata_count,
                "baseline_assertions": assertion_count,
            }


def _allocate_sim_id(cur) -> str:
    cur.execute("SELECT sim_id FROM simulations WHERE sim_id LIKE 'SIM-%'")
    used = {r[0] for r in cur.fetchall()}
    n = 1
    while True:
        candidate = f"SIM-{n:03d}"
        if candidate not in used:
            return candidate
        n += 1


# ------------------------------------------------------------------ 推演详情/列表
def list_simulations() -> List[Dict[str, Any]]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sim_id, base_revision, sim_revision, status, label, merged_revision, created_at, closed_at "
                "FROM simulations ORDER BY created_at, sim_id"
            )
            return [
                {
                    "sim_id": r[0],
                    "base_revision": r[1],
                    "sim_revision": r[2],
                    "status": r[3],
                    "label": r[4],
                    "merged_revision": r[5],
                    "created_at": r[6].isoformat(),
                    "closed_at": r[7].isoformat() if r[7] else None,
                }
                for r in cur.fetchall()
            ]


def get_simulation(sim_id: str) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            row = _sim_row(cur, sim_id)
            cur.execute("SELECT stratum_id, site FROM sim_strata WHERE sim_id = %s ORDER BY stratum_id", (sim_id,))
            strata = [{"stratum_id": r[0], "site": r[1]} for r in cur.fetchall()]
            cur.execute(
                "SELECT evidence_id, stratum_a, stratum_b, relation, status, origin, "
                "added_sim_revision, retracted_sim_revision "
                "FROM sim_assertions WHERE sim_id = %s ORDER BY evidence_id",
                (sim_id,),
            )
            assertions = [
                {
                    "evidence_id": r[0],
                    "stratum_a": r[1],
                    "stratum_b": r[2],
                    "relation": r[3],
                    "status": r[4],
                    "origin": r[5],
                    "added_sim_revision": r[6],
                    "retracted_sim_revision": r[7],
                }
                for r in cur.fetchall()
            ]
            return {
                "sim_id": row[0],
                "base_revision": row[1],
                "sim_revision": row[2],
                "status": row[3],
                "label": row[4],
                "merged_revision": row[5],
                "strata": strata,
                "assertions": assertions,
                "net_changes": _net_changes(cur, sim_id),
            }


def list_sim_assertions(sim_id: str, status_filter: Optional[str]) -> List[Dict[str, Any]]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            _sim_row(cur, sim_id)
            sql = (
                "SELECT evidence_id, stratum_a, stratum_b, relation, status, origin, "
                "added_sim_revision, retracted_sim_revision "
                "FROM sim_assertions WHERE sim_id = %s"
            )
            params: Tuple[Any, ...] = (sim_id,)
            if status_filter:
                sql += " AND status = %s"
                params = (sim_id, status_filter)
            sql += " ORDER BY evidence_id"
            cur.execute(sql, params)
            return [
                {
                    "evidence_id": r[0],
                    "stratum_a": r[1],
                    "stratum_b": r[2],
                    "relation": r[3],
                    "status": r[4],
                    "origin": r[5],
                    "added_sim_revision": r[6],
                    "retracted_sim_revision": r[7],
                }
                for r in cur.fetchall()
            ]


def _net_changes(cur, sim_id: str) -> Dict[str, Any]:
    """推演相对基线的净变更：
    - added：推演内新增且当前仍生效的断言（基线撤回后再增的同编号也归入此类）；
    - retracted：基线生效、推演内撤回的证据编号。
    """
    cur.execute(
        "SELECT evidence_id, stratum_a, stratum_b, relation FROM sim_assertions "
        "WHERE sim_id = %s AND origin = 'sim' AND status = 'active' ORDER BY evidence_id",
        (sim_id,),
    )
    added = [
        {"evidence_id": r[0], "stratum_a": r[1], "stratum_b": r[2], "relation": r[3]}
        for r in cur.fetchall()
    ]
    cur.execute(
        "SELECT evidence_id FROM sim_assertions "
        "WHERE sim_id = %s AND origin = 'baseline' AND status = 'retracted' ORDER BY evidence_id",
        (sim_id,),
    )
    retracted = [r[0] for r in cur.fetchall()]
    return {"added": added, "retracted": retracted, "total": len(added) + len(retracted)}


# ------------------------------------------------------------------ 推演内批次
def apply_sim_batch(sim_id: str, batch: SimBatchIn) -> Dict[str, Any]:
    """推演内提交一批新增/撤回。语义与正式批次一致：
    - 携带推演修订号，过期整批拒绝；
    - 仅可引用创建时固定的已登记地层；
    - 正式历史编号与推演内编号（含已撤回）均不可复用；
    - 整批应用后重新核验，组内早于/早于闭环整批拒绝、推演状态不变；
    - 通过则原子递增推演修订号并记审计。
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            row = _sim_row(cur, sim_id, for_update=True)
            _require_open(row)
            current_sim_revision = row[2]
            if batch.expected_sim_revision != current_sim_revision:
                raise ApiError(
                    409,
                    "STALE_REVISION",
                    f"推演修订号过期：请求携带 {batch.expected_sim_revision}，当前为 {current_sim_revision}",
                    {"sim_id": sim_id, "current_sim_revision": current_sim_revision},
                )
            new_sim_revision = current_sim_revision + 1

            for op in batch.operations:
                if isinstance(op, AssertOp):
                    _ensure_sim_strata(cur, sim_id, op.stratum_a, op.stratum_b)
                    # 推演内编号唯一：正式基线编号 + 推演内已有编号（含已撤回）均占用
                    cur.execute(
                        "SELECT 1 FROM sim_assertions WHERE sim_id = %s AND evidence_id = %s",
                        (sim_id, op.evidence_id),
                    )
                    if cur.fetchone() is not None:
                        raise ApiError(
                            409,
                            "DUPLICATE_EVIDENCE",
                            f"证据编号已存在（含正式历史编号与推演内已撤回编号）: {op.evidence_id}",
                            {"sim_id": sim_id, "evidence_id": op.evidence_id},
                        )
                    cur.execute(
                        "INSERT INTO sim_assertions "
                        "(sim_id, evidence_id, stratum_a, stratum_b, relation, status, origin, added_sim_revision) "
                        "VALUES (%s, %s, %s, %s, %s, 'active', 'sim', %s)",
                        (
                            sim_id,
                            op.evidence_id,
                            op.stratum_a,
                            op.stratum_b,
                            op.relation,
                            new_sim_revision,
                        ),
                    )
                else:  # RetractOp
                    cur.execute(
                        "SELECT status FROM sim_assertions "
                        "WHERE sim_id = %s AND evidence_id = %s FOR UPDATE",
                        (sim_id, op.evidence_id),
                    )
                    r = cur.fetchone()
                    if r is None:
                        raise ApiError(
                            409,
                            "ASSERTION_NOT_FOUND",
                            f"撤回失败：证据编号在推演内不存在: {op.evidence_id}",
                            {"sim_id": sim_id, "evidence_id": op.evidence_id},
                        )
                    if r[0] != "active":
                        raise ApiError(
                            409,
                            "ASSERTION_NOT_FOUND",
                            f"撤回失败：证据 {op.evidence_id} 已处于撤回状态",
                            {"sim_id": sim_id, "evidence_id": op.evidence_id},
                        )
                    cur.execute(
                        "UPDATE sim_assertions SET status = 'retracted', "
                        "retracted_sim_revision = %s, retracted_at = now() "
                        "WHERE sim_id = %s AND evidence_id = %s",
                        (new_sim_revision, sim_id, op.evidence_id),
                    )

            # 对推演内整批应用后的最终关系核验
            conflicts = Derivation(_load_sim_active(cur, sim_id)).validate()
            if conflicts:
                raise ApiError(
                    409,
                    "CONFLICT",
                    "推演批次与现有断言矛盾，整批拒绝，推演状态不变",
                    {"sim_id": sim_id, "conflicts": conflicts},
                )

            cur.execute(
                "UPDATE simulations SET sim_revision = %s WHERE sim_id = %s",
                (new_sim_revision, sim_id),
            )
            cur.execute(
                "INSERT INTO sim_revisions (sim_id, sim_revision, batch) VALUES (%s, %s, %s)",
                (sim_id, new_sim_revision, Json(batch.model_dump())),
            )
            return {
                "sim_id": sim_id,
                "base_revision": row[1],
                "sim_revision": new_sim_revision,
                "applied": len(batch.operations),
            }


# ------------------------------------------------------------------ 推演内关系查询
def query_sim_relationship(sim_id: str, a: str, b: str) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            row = _sim_row(cur, sim_id)
            _ensure_sim_strata(cur, sim_id, a, b)
            payload = _sim_relation_payload(cur, sim_id, a, b)
            payload.update(
                {
                    "sim_id": sim_id,
                    "base_revision": row[1],
                    "sim_revision": row[2],
                }
            )
            return payload


# ------------------------------------------------------------------ 并入正式状态
def merge_simulation(sim_id: str) -> Dict[str, Any]:
    """把推演相对基线的净变更并入最新正式状态：
    1. 基线撤回目标若已在正式状态撤回/不存在 -> BASELINE_RETRACTED_OFFICIAL；
    2. 推演新增编号若已被正式占用 -> EVIDENCE_TAKEN_OFFICIAL；
    3. 把净变更应用到最新正式生效断言后重新核验，冲突 -> MERGE_REJECTED 的 conflicts（附证据路径）；
    任一前置失败：正式状态与推演均不变。
    通过：作为一次正式批次原子提交并关闭推演；净变更为空只关闭推演，不递增正式修订号。
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            # 先锁推演（行锁顺序：simulations -> revision_counter，全局一致避免死锁）
            sim = _sim_row(cur, sim_id, for_update=True)
            _require_open(sim)
            base_revision, sim_revision = sim[1], sim[2]

            cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE FOR UPDATE")
            current_revision = cur.fetchone()[0]

            net = _net_changes(cur, sim_id)
            added = net["added"]
            retracted = net["retracted"]

            # --- 前置校验 1：基线撤回目标是否已在正式状态撤回（含正式编号已彻底不存在）
            cur.execute(
                "SELECT evidence_id, status FROM assertions WHERE evidence_id = ANY(%s)",
                (retracted,),
            )
            retract_rows = {r[0]: r[1] for r in cur.fetchall()}
            stale_retracts: List[Dict[str, Any]] = []
            for eid in retracted:
                status = retract_rows.get(eid)
                if status is None:
                    stale_retracts.append(
                        {
                            "type": "BASELINE_RETRACTED_OFFICIAL",
                            "evidence_id": eid,
                            "reason": "基线证据在正式状态已不存在，无法按推演撤回",
                        }
                    )
                elif status != "active":
                    stale_retracts.append(
                        {
                            "type": "BASELINE_RETRACTED_OFFICIAL",
                            "evidence_id": eid,
                            "reason": "基线证据已在正式状态撤回，推演撤回目标失效",
                            "official_status": status,
                        }
                    )

            # --- 前置校验 2：推演新增编号是否已被正式占用（含已撤回，正式编号永久唯一）
            cur.execute(
                "SELECT evidence_id, status FROM assertions WHERE evidence_id = ANY(%s)",
                ([x["evidence_id"] for x in added],),
            )
            taken_rows = cur.fetchall()
            taken = [
                {
                    "type": "EVIDENCE_TAKEN_OFFICIAL",
                    "evidence_id": r[0],
                    "reason": "证据编号已被正式历史占用（含已撤回），无法并入",
                    "official_status": r[1],
                }
                for r in taken_rows
            ]
            blocked_add_ids = {r[0] for r in taken_rows}

            # --- 在最新正式状态之上叠加净变更，重新核验关系
            cur.execute(
                "SELECT evidence_id, stratum_a, stratum_b, relation "
                "FROM assertions WHERE status = 'active'"
            )
            retract_set = set(retracted)
            candidate = [
                Assertion(evidence_id=r[0], a=r[1], b=r[2], relation=r[3])
                for r in cur.fetchall()
                if r[0] not in retract_set
            ]
            for x in added:
                if x["evidence_id"] not in blocked_add_ids:
                    candidate.append(
                        Assertion(
                            evidence_id=x["evidence_id"],
                            a=x["stratum_a"],
                            b=x["stratum_b"],
                            relation=x["relation"],
                        )
                    )
            conflicts = Derivation(candidate).validate()
            preconditions = stale_retracts + taken

            if preconditions or conflicts:
                raise ApiError(
                    409,
                    "MERGE_REJECTED",
                    "推演并入失败：前置条件不满足或关系核验冲突，正式状态与推演均不变",
                    {
                        "sim_id": sim_id,
                        "base_revision": base_revision,
                        "current_revision": current_revision,
                        "preconditions": preconditions,
                        "conflicts": conflicts,
                    },
                )

            # --- 净变更为空：只关闭推演，不递增正式修订号
            if not added and not retracted:
                cur.execute(
                    "UPDATE simulations SET status = 'closed', closed_at = now() WHERE sim_id = %s",
                    (sim_id,),
                )
                return {
                    "sim_id": sim_id,
                    "merged": True,
                    "empty": True,
                    "revision": current_revision,
                    "revision_incremented": False,
                    "applied": 0,
                    "status": "closed",
                }

            # --- 作为一次正式批次原子提交
            new_revision = current_revision + 1
            for x in added:
                cur.execute(
                    "INSERT INTO assertions (evidence_id, stratum_a, stratum_b, relation, status, added_revision) "
                    "VALUES (%s, %s, %s, %s, 'active', %s)",
                    (x["evidence_id"], x["stratum_a"], x["stratum_b"], x["relation"], new_revision),
                )
            for eid in retracted:
                cur.execute(
                    "UPDATE assertions SET status = 'retracted', retracted_revision = %s, retracted_at = now() "
                    "WHERE evidence_id = %s AND status = 'active'",
                    (new_revision, eid),
                )

            cur.execute("UPDATE revision_counter SET revision = %s WHERE id = TRUE", (new_revision,))
            cur.execute(
                "INSERT INTO revisions (revision, batch) VALUES (%s, %s)",
                (
                    new_revision,
                    Json(
                        {
                            "source": "simulation_merge",
                            "sim_id": sim_id,
                            "base_revision": base_revision,
                            "sim_revision": sim_revision,
                            "operations": [
                                {
                                    "op": "assert",
                                    "evidence_id": x["evidence_id"],
                                    "stratum_a": x["stratum_a"],
                                    "stratum_b": x["stratum_b"],
                                    "relation": x["relation"],
                                }
                                for x in added
                            ]
                            + [{"op": "retract", "evidence_id": eid} for eid in retracted],
                        }
                    ),
                ),
            )
            cur.execute(
                "UPDATE simulations SET status = 'closed', merged_revision = %s, closed_at = now() "
                "WHERE sim_id = %s",
                (new_revision, sim_id),
            )
            return {
                "sim_id": sim_id,
                "merged": True,
                "empty": False,
                "revision": new_revision,
                "previous_revision": current_revision,
                "revision_incremented": True,
                "applied": len(added) + len(retracted),
                "added": len(added),
                "retracted": len(retracted),
                "status": "closed",
            }
