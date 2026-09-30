#!/usr/bin/env bash
# 考古地层关系核验服务 —— 推演（模拟修订）端到端调用示例
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
# 覆盖：创建推演固定基线、推演内批次核验与关系查询、
#       三种并入失败（基线撤回失效 / 编号被正式占用 / 关系冲突附路径）、
#       净变更为空只关闭、成功并入作为一次正式批次原子提交。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }

echo "== 0. 准备：登记地层 + 正式批次（rev 0 -> 1） =="
for s in L1 L2 L3 L4 L5 L6; do
  curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
    -d "{\"stratum_id\": \"$s\", \"site\": \"甲遗址\"}" >/dev/null || true
done
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period"},
    {"op": "assert", "evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than"},
    {"op": "assert", "evidence_id": "EV-003", "stratum_a": "L3", "stratum_b": "L4", "relation": "earlier_than"}
  ]}' | pp

echo; echo "== 1. 创建推演（固定当前修订号=1、已登记地层、生效断言；不指定编号则自动生成 SIM-001） =="
curl -s -X POST "$BASE/simulations" -H 'Content-Type: application/json' \
  -d '{"label": "证据修订推演"}' | pp

echo; echo "== 2. 查询推演内关系（沿用推导/路径语义，证据标注 baseline 来源） =="
curl -s "$BASE/simulations/SIM-001/relationship?a=L1&b=L3" | pp

echo; echo "== 3. 推演内批次（携带推演修订号 0）：新增 L4 早于 L5，撤回 EV-002 =="
curl -s -X POST "$BASE/simulations/SIM-001/batches" -H 'Content-Type: application/json' -d '{
  "expected_sim_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "SIM-EV-100", "stratum_a": "L4", "stratum_b": "L5", "relation": "earlier_than"},
    {"op": "retract", "evidence_id": "EV-002"}
  ]}' | pp

echo; echo "== 4. 推演内重新查询：EV-002 撤回后 L1 vs L3 未定；L4 vs L5 走推演新增证据 =="
curl -s "$BASE/simulations/SIM-001/relationship?a=L1&b=L3" | pp
curl -s "$BASE/simulations/SIM-001/relationship?a=L4&b=L5" | pp

echo; echo "== 5. 正式状态不受推演影响（L1 vs L3 仍为早于，正式修订号仍为 1） =="
curl -s "$BASE/relationship?a=L1&b=L3" | pp
curl -s "$BASE/revision" | pp

echo; echo "== 6. 推演内错误语义（与正式批次一致） =="
echo "-- 6a. 推演修订号过期（STALE_REVISION，当前为 1） --"
curl -s -X POST "$BASE/simulations/SIM-001/batches" -H 'Content-Type: application/json' -d '{
  "expected_sim_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "X1", "stratum_a": "L1", "stratum_b": "L5", "relation": "same_period"}
  ]}' | pp
echo "-- 6b. 复用正式历史已用编号 EV-001（DUPLICATE_EVIDENCE） --"
curl -s -X POST "$BASE/simulations/SIM-001/batches" -H 'Content-Type: application/json' -d '{
  "expected_sim_revision": 1,
  "operations": [
    {"op": "assert", "evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L5", "relation": "same_period"}
  ]}' | pp
echo "-- 6c. 复用推演内已用编号 SIM-EV-100（DUPLICATE_EVIDENCE，含已撤回不可复用） --"
curl -s -X POST "$BASE/simulations/SIM-001/batches" -H 'Content-Type: application/json' -d '{
  "expected_sim_revision": 1,
  "operations": [
    {"op": "assert", "evidence_id": "SIM-EV-100", "stratum_a": "L1", "stratum_b": "L5", "relation": "same_period"}
  ]}' | pp
echo "-- 6d. 整批核验冲突（L1 早于 L2 构成组内早于，整批拒绝、推演不变） --"
curl -s -X POST "$BASE/simulations/SIM-001/batches" -H 'Content-Type: application/json' -d '{
  "expected_sim_revision": 1,
  "operations": [
    {"op": "assert", "evidence_id": "SIM-EV-101", "stratum_a": "L1", "stratum_b": "L2", "relation": "earlier_than"}
  ]}' | pp

echo; echo "== 7. 推演详情：含相对基线的净变更预览（added / retracted） =="
curl -s "$BASE/simulations/SIM-001" | pp

echo; echo "== 8. 成功并入：净变更应用于最新正式状态并重新核验，rev 1 -> 2，推演关闭 =="
curl -s -X POST "$BASE/simulations/SIM-001/merge" | pp
echo "-- 并入后正式关系：L1 vs L3 未定；L4 早于 L5（SIM-EV-100 成为正式证据） --"
curl -s "$BASE/relationship?a=L1&b=L3" | pp
curl -s "$BASE/relationship?a=L4&b=L5" | pp

echo; echo "== 9. 净变更为空：创建空推演直接并入，只关闭推演，不递增正式修订号 =="
curl -s -X POST "$BASE/simulations" -H 'Content-Type: application/json' -d '{"sim_id": "S-EMPTY"}' | pp
curl -s -X POST "$BASE/simulations/S-EMPTY/merge" | pp
curl -s "$BASE/revision" | pp

echo; echo "== 10. 并入失败一：基线撤回目标已在正式状态撤回 =="
# S3 在 rev=2 基线上撤回 EV-003；正式批次先行撤回 EV-003（rev 2 -> 3）后再并入
curl -s -X POST "$BASE/simulations" -H 'Content-Type: application/json' -d '{"sim_id": "S3"}' | pp
curl -s -X POST "$BASE/simulations/S3/batches" -H 'Content-Type: application/json' -d '{
  "expected_sim_revision": 0,
  "operations": [{"op": "retract", "evidence_id": "EV-003"}]
}' | pp
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 2,
  "operations": [{"op": "retract", "evidence_id": "EV-003"}]
}' | pp
curl -s -X POST "$BASE/simulations/S3/merge" | pp

echo; echo "== 11. 并入失败二：推演新增编号已被正式占用 =="
# S4 在 rev=3 基线上新增 EV-200；正式批次先用 EV-200（rev 3 -> 4）后再并入
curl -s -X POST "$BASE/simulations" -H 'Content-Type: application/json' -d '{"sim_id": "S4"}' | pp
curl -s -X POST "$BASE/simulations/S4/batches" -H 'Content-Type: application/json' -d '{
  "expected_sim_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "EV-200", "stratum_a": "L5", "stratum_b": "L6", "relation": "earlier_than"}
  ]}' | pp
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 3,
  "operations": [
    {"op": "assert", "evidence_id": "EV-200", "stratum_a": "L2", "stratum_b": "L5", "relation": "earlier_than"}
  ]}' | pp
curl -s -X POST "$BASE/simulations/S4/merge" | pp

echo; echo "== 12. 并入失败三：关系冲突（正式并行新增 L5 早于 L6，与推演 L6 早于 L1 成环，附证据路径） =="
curl -s -X POST "$BASE/simulations" -H 'Content-Type: application/json' -d '{"sim_id": "S5"}' | pp
curl -s -X POST "$BASE/simulations/S5/batches" -H 'Content-Type: application/json' -d '{
  "expected_sim_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "SIM-EV-300", "stratum_a": "L6", "stratum_b": "L1", "relation": "earlier_than"}
  ]}' | pp
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 4,
  "operations": [
    {"op": "assert", "evidence_id": "EV-300", "stratum_a": "L5", "stratum_b": "L6", "relation": "earlier_than"}
  ]}' | pp
curl -s -X POST "$BASE/simulations/S5/merge" | pp
echo "-- 拒绝后：正式修订号仍为 5，S5 仍 open（正式状态与推演均不变） --"
curl -s "$BASE/revision" | pp
curl -s "$BASE/simulations/S5" | pp

echo; echo "== 13. 推演列表 =="
curl -s "$BASE/simulations" | pp

echo; echo "全部推演示例执行完毕。"
