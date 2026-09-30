# 考古地层关系核验服务

基于 **Python (FastAPI) + PostgreSQL** 的考古地层关系核验服务。登记遗址地层与带唯一证据编号的
“同一期 / 早于”断言，自动推导地层间关系，支持批次原子核验、冲突证据路径定位、关系查询与不可变定版快照；
并支持**推演（模拟修订）**：在不改动正式断言的前提下预演证据修订，核验通过后作为一次正式批次原子并入。

## 语义规则

- **同一期（same_period）**：对称且可传递，构成等价类。
- **早于（earlier_than）**：沿“同一期 + 早于”混合路径推导。
  例如 `L1 ~ L2`（同一期）、`L2 < L3`（早于），则 `L1 早于 L3`。
- **硬约束**（对整批应用后的最终关系核验）：
  1. **组内早于禁止**——同一期等价类内部不得存在早于关系；
  2. **早于闭环禁止**——等价类之间的早于关系不得成环。
- **批次语义**：每批新增/撤回须携带当前修订号；按整批应用后的最终关系核验；
  发生矛盾时**整批拒绝**并返回冲突证据路径，原状态不变；全部通过则在同一事务内落库并**原子递增修订号**。
- **路径选取**：多条证据路径时，取证据数最少者；并列时按证据编号序列字典序最小者。
- **定版（快照）**：保存当前修订号下全部关系结论的不可变快照；后续撤回不影响旧版结论。

### 推演（模拟修订）语义

- **创建即固定基线**：创建推演时固定当时的**正式修订号（base_revision）**、**已登记地层**与
  **全部生效断言**；此后正式历史继续推进也不改变任何已有推演的基线，且推演全程不改动正式数据。
- **推演内沿用批次语义**：新增/撤回携带**推演修订号**（`expected_sim_revision`，从 0 开始）；
  按整批应用后的最终关系核验，冲突整批拒绝、推演不变；通过则原子递增推演修订号。
- **证据编号占用规则**：正式历史已用证据编号（含已撤回）在推演内不可复用；
  推演新增编号在本推演内不可复用（撤回后同样不可复用）。
- **推演内查询**：可查询该推演固定地层范围内任意两地层的关系与支撑证据路径
  （`path_detail` 以 `origin=baseline/sim` 标注证据来源）。
- **并入（merge）**：取推演**相对基线的净变更**（推演新增且仍生效的断言 + 基线被撤回的证据编号），
  应用于**最新正式状态**并重新核验：
  - 基线撤回目标已在正式状态撤回 / 不存在 → `BASELINE_RETRACTED_OFFICIAL`；
  - 推演新增编号已被正式历史占用（含已撤回） → `EVIDENCE_TAKEN_OFFICIAL`；
  - 应用后出现组内早于 / 早于闭环 → `MERGE_REJECTED` 的 `conflicts`，附冲突证据路径；
  - 任一前置失败：**正式状态与推演均不变**。
- **并入成功**：净变更作为**一次正式批次原子提交**（插入/撤回断言、递增正式修订号、写批次审计），
  同时关闭推演并记录落地修订号；**净变更为空时只关闭推演，不递增正式修订号**。
- 推演一旦关闭即不可再提交批次或并入。

## 快速开始

```bash
docker compose up --build
```

- API 地址：**http://localhost:8000**
- 交互式接口文档（Swagger UI）：**http://localhost:8000/docs**
- PostgreSQL：`localhost:5432`（容器内服务名 `db`，数据持久化于命名卷 `pgdata`）

首次启动时应用会自动等待数据库就绪并完成表结构初始化，无需手工迁移。

### 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://strat:strat@db:5432/stratigraphy`（compose 内） | 应用连接串 |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `strat` / `strat` / `stratigraphy` | 数据库账号（compose） |
| `APP_PORT` | `8000` | API 对外端口 |
| `DB_PORT` | `5432` | 数据库对外端口 |

本地开发（不用 Docker）：

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://strat:strat@localhost:5432/stratigraphy
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/strata` | 登记地层 `{stratum_id, site?}` |
| GET | `/strata` | 地层列表 |
| POST | `/batches` | 提交一批断言新增/撤回（整批核验，成功原子递增修订号） |
| GET | `/revision` | 当前修订号 |
| GET | `/assertions?status=active` | 断言列表 |
| GET | `/relationship?a=L1&b=L3` | 查询两地层当前关系及支撑证据路径 |
| POST | `/snapshots` | 定版：保存不可变关系快照 `{label?}` |
| GET | `/snapshots` / `/snapshots/{version}` | 版本列表 / 版本详情 |
| GET | `/snapshots/{version}/relationship?a=L1&b=L3` | 查询历史版本结论 |
| POST | `/simulations` | 创建推演：固定当前修订号/地层/生效断言 `{sim_id?, label?}` |
| GET | `/simulations` / `/simulations/{sim_id}` | 推演列表 / 详情（含净变更预览） |
| GET | `/simulations/{sim_id}/assertions?status=active` | 推演内断言（标注 baseline / sim 来源） |
| POST | `/simulations/{sim_id}/batches` | 推演内批次（携带 `expected_sim_revision`） |
| GET | `/simulations/{sim_id}/relationship?a=L1&b=L3` | 查询推演内关系与证据路径 |
| POST | `/simulations/{sim_id}/merge` | 净变更并入最新正式状态并重新核验，成功原子提交并关闭推演 |

### 批次请求格式

```json
{
  "expected_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period"},
    {"op": "assert", "evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than"},
    {"op": "retract", "evidence_id": "EV-009"}
  ]
}
```

- `relation`：`same_period`（同一期）/ `earlier_than`（`stratum_a` 早于 `stratum_b`）。
- 成功响应：`{"revision": 1, "applied": 3}`。
- 冲突响应（HTTP 409，原状态不变）：

```json
{
  "error": {
    "code": "CONFLICT",
    "message": "批次与现有断言矛盾，整批拒绝，原状态不变",
    "details": {
      "conflicts": [
        {"type": "earlier_cycle", "message": "早于关系形成闭环: ...", "evidence_path": ["EV-002", "EV-003", "EV-004"]}
      ]
    }
  }
}
```

### 关系查询响应

```json
{
  "stratum_a": "L1",
  "stratum_b": "L3",
  "relation": "earlier_than",
  "relation_label": "早于",
  "evidence_path": ["EV-001", "EV-002"],
  "path_detail": [
    {"evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period", "status": "active"},
    {"evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than", "status": "active"}
  ],
  "revision": 1
}
```

`relation` 取值：`same_period`（同一期）/ `earlier_than`（早于）/ `later_than`（晚于）/ `undetermined`（未定）。

### 推演请求格式

创建推演（`sim_id` 省略时自动生成 `SIM-001` 形式编号）：

```json
{"sim_id": "S-2026Q3-01", "label": "三季度证据修订预演"}
```

成功响应（固定基线，不改动正式数据）：

```json
{"sim_id": "SIM-001", "base_revision": 1, "sim_revision": 0, "status": "open",
 "label": "...", "strata": 6, "baseline_assertions": 3}
```

推演内批次（操作格式与正式批次完全一致，仅修订号字段不同）：

```json
{
  "expected_sim_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "SIM-EV-100", "stratum_a": "L4", "stratum_b": "L5", "relation": "earlier_than"},
    {"op": "retract", "evidence_id": "EV-002"}
  ]
}
```

成功响应：`{"sim_id": "SIM-001", "base_revision": 1, "sim_revision": 1, "applied": 2}`。

并入成功响应（净变更非空，作为一次正式批次提交，正式修订号 1 -> 2）：

```json
{"sim_id": "SIM-001", "merged": true, "empty": false,
 "revision": 2, "previous_revision": 1, "revision_incremented": true,
 "applied": 2, "added": 1, "retracted": 1, "status": "closed"}
```

净变更为空时：`{"merged": true, "empty": true, "revision": 2, "revision_incremented": false,
"applied": 0, "status": "closed"}`（不递增正式修订号）。

并入失败响应（HTTP 409，正式状态与推演均不变；`preconditions` 与 `conflicts` 各自给出具体原因，
关系冲突附证据路径）：

```json
{
  "error": {
    "code": "MERGE_REJECTED",
    "message": "推演并入失败：前置条件不满足或关系核验冲突，正式状态与推演均不变",
    "details": {
      "sim_id": "S5",
      "base_revision": 4,
      "current_revision": 5,
      "preconditions": [
        {"type": "BASELINE_RETRACTED_OFFICIAL", "evidence_id": "EV-003", "reason": "...", "official_status": "retracted"},
        {"type": "EVIDENCE_TAKEN_OFFICIAL", "evidence_id": "EV-200", "reason": "...", "official_status": "active"}
      ],
      "conflicts": [
        {"type": "earlier_cycle", "message": "早于关系形成闭环: ...", "evidence_path": ["EV-200", "EV-300", "SIM-EV-300", "EV-001"]}
      ]
    }
  }
}
```

### 错误码

| HTTP | code | 含义 |
| --- | --- | --- |
| 400 | `INVALID_STRATUM` | 引用了未登记的地层 |
| 409 | `STALE_REVISION` | 批次携带的修订号过期（响应含当前修订号） |
| 409 | `DUPLICATE_EVIDENCE` | 证据编号重复（含已撤回的编号） |
| 409 | `ASSERTION_NOT_FOUND` | 撤回不存在或已撤回的断言 |
| 409 | `CONFLICT` | 整批核验发现组内早于 / 早于闭环（附冲突证据路径） |
| 409 | `STRATUM_EXISTS` | 地层重复登记 |
| 404 | `SNAPSHOT_NOT_FOUND` | 版本不存在 |
| 404 | `SIMULATION_NOT_FOUND` | 推演不存在 |
| 409 | `SIMULATION_EXISTS` | 推演编号重复 |
| 400 | `INVALID_SIM_ID` | 推演编号含非法字符（仅允许字母、数字、点、下划线、短横线） |
| 409 | `SIMULATION_CLOSED` | 推演已关闭，不可再提交批次或并入 |
| 409 | `MERGE_REJECTED` | 并入失败：基线撤回失效 / 编号被正式占用（`preconditions`）或关系冲突（`conflicts`，附证据路径）；正式状态与推演均不变 |

> 推演内批次复用 `STALE_REVISION`（详情字段为 `current_sim_revision`）、`DUPLICATE_EVIDENCE`
> （正式历史编号与推演内编号均占用）、`ASSERTION_NOT_FOUND`、`CONFLICT`、`INVALID_STRATUM`（引用创建推演时未固定的地层）。

## 调用示例

端到端 curl 演示（覆盖登记、批次、推导查询、冲突拒绝、撤回、定版与旧版不可变）：

```bash
bash examples/demo.sh
```

**本轮新增：推演与并入**端到端 curl 演示（覆盖固定基线、推演内批次核验与关系查询、三种并入失败
及冲突证据路径、空净变更只关闭、成功并入原子提交）：

```bash
bash examples/sim_demo.sh
```

Python 客户端示例：

```bash
pip install requests
python3 examples/client_demo.py
```

## 项目结构

```
app/
  main.py        # FastAPI 路由与错误处理
  service.py     # 正式批次核验、关系查询、定版快照
  simulation.py  # 推演：创建（固定基线）、推演内批次、推演关系查询、净变更并入
  derivation.py  # 推导引擎：并查集等价类、DAG 校验、最优证据路径
  db.py          # 连接池与模式初始化（启动时自动建表）
  models.py      # 请求模型
examples/
  demo.sh        # curl 端到端示例（正式批次 / 关系 / 定版）
  sim_demo.sh    # curl 端到端示例（推演 / 并入）
  client_demo.py # Python 客户端示例
Dockerfile
docker-compose.yml
requirements.txt
```
