#!/usr/bin/env python3
"""考古地层关系核验服务 —— 推演（模拟修订）Python 调用示例。

前提：服务已启动（docker compose up --build）。
依赖：pip install requests
运行：python3 examples/client_sim_demo.py
"""
import json
import os
import sys

import requests

BASE = os.getenv("BASE_URL", "http://localhost:8000")


def show(title: str, resp: requests.Response) -> dict:
    print(f"\n== {title} ==  HTTP {resp.status_code}")
    print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
    return resp.json()


def main() -> None:
    # 1. 准备：登记地层 + 正式批次
    for s in ["L1", "L2", "L3", "L4", "L5", "L6"]:
        requests.post(f"{BASE}/strata", json={"stratum_id": s, "site": "甲遗址"})
    r = requests.post(
        f"{BASE}/batches",
        json={
            "expected_revision": 0,
            "operations": [
                {"op": "assert", "evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period"},
                {"op": "assert", "evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than"},
                {"op": "assert", "evidence_id": "EV-003", "stratum_a": "L3", "stratum_b": "L4", "relation": "earlier_than"},
            ],
        },
    )
    show("准备：正式批次 rev=1", r)

    # 2. 创建推演：固定当前修订号/地层/生效断言
    sim = show("创建推演（编号自动生成）", requests.post(f"{BASE}/simulations", json={"label": "证据修订预演"}))
    sid = sim["sim_id"]

    # 3. 推演内查询（沿用推导路径语义）
    show("推演内 L1 vs L3（基线：早于）", requests.get(f"{BASE}/simulations/{sid}/relationship", params={"a": "L1", "b": "L3"}))

    # 4. 推演内批次：新增 L4 早于 L5、撤回 EV-002（携带推演修订号）
    show(
        "推演内批次（sim_revision 0 -> 1）",
        requests.post(
            f"{BASE}/simulations/{sid}/batches",
            json={
                "expected_sim_revision": 0,
                "operations": [
                    {"op": "assert", "evidence_id": "SIM-EV-100", "stratum_a": "L4", "stratum_b": "L5", "relation": "earlier_than"},
                    {"op": "retract", "evidence_id": "EV-002"},
                ],
            },
        ),
    )
    show("推演内 L1 vs L3（撤回后：未定）", requests.get(f"{BASE}/simulations/{sid}/relationship", params={"a": "L1", "b": "L3"}))
    show("正式 L1 vs L3（不受推演影响：仍早于）", requests.get(f"{BASE}/relationship", params={"a": "L1", "b": "L3"}))

    # 5. 推演详情含相对基线的净变更
    show("推演详情（净变更预览）", requests.get(f"{BASE}/simulations/{sid}"))

    # 6. 编号不可复用
    show(
        "复用正式历史编号 EV-001（拒绝）",
        requests.post(
            f"{BASE}/simulations/{sid}/batches",
            json={
                "expected_sim_revision": 1,
                "operations": [
                    {"op": "assert", "evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L5", "relation": "same_period"}
                ],
            },
        ),
    )

    # 7. 成功并入：作为一次正式批次原子提交，rev 1 -> 2，推演关闭
    show("并入推演", requests.post(f"{BASE}/simulations/{sid}/merge"))
    show("正式 L4 vs L5（并入后：早于，证据 SIM-EV-100）", requests.get(f"{BASE}/relationship", params={"a": "L4", "b": "L5"}))

    # 8. 关系冲突并入失败示例（附证据路径，双方不变）
    #    推演仅新增 L6<L1（推演内 L6 为孤立源点，无环，通过）；
    #    正式并行新增 L2<L5 与 L5<L6。并入后叠加 EV-001(L1~L2) 形成闭环：
    #    L1~L2 < L5 < L6 < L1。推演未触碰正式编号，故仅关系冲突、前置条件为空。
    show(
        "创建推演 S-CYC",
        requests.post(f"{BASE}/simulations", json={"sim_id": "S-CYC"}),
    )
    show(
        "S-CYC 新增 L6 早于 L1（推演内无环，通过）",
        requests.post(
            f"{BASE}/simulations/S-CYC/batches",
            json={
                "expected_sim_revision": 0,
                "operations": [
                    {"op": "assert", "evidence_id": "SIM-EV-300", "stratum_a": "L6", "stratum_b": "L1", "relation": "earlier_than"}
                ],
            },
        ),
    )
    rev = requests.get(f"{BASE}/revision").json()["revision"]
    show(
        f"正式并行新增 L2 早于 L5、L5 早于 L6（rev {rev} -> {rev + 1}，并入时成环 L1~L2<L5<L6<L1）",
        requests.post(
            f"{BASE}/batches",
            json={
                "expected_revision": rev,
                "operations": [
                    {"op": "assert", "evidence_id": "EV-400", "stratum_a": "L2", "stratum_b": "L5", "relation": "earlier_than"},
                    {"op": "assert", "evidence_id": "EV-401", "stratum_a": "L5", "stratum_b": "L6", "relation": "earlier_than"},
                ],
            },
        ),
    )
    show("并入 S-CYC（关系冲突，附证据路径，双方不变）", requests.post(f"{BASE}/simulations/S-CYC/merge"))


if __name__ == "__main__":
    try:
        main()
    except requests.ConnectionError:
        sys.exit(f"无法连接 {BASE}，请先启动服务：docker compose up --build")
