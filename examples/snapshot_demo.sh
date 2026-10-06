#!/usr/bin/env bash
# 合规审计快照端到端示例：
#   登记证书 -> 单条/批次写入 -> 非法时间窗拒绝(422 不留快照) ->
#   更正把记录移出窗口 -> 生成快照(空窗计数为零) -> 凭标识稳定翻页 ->
#   撤销证书后再生成(明细冻结为 invalid) -> 旧快照保持不变 -> 未知标识 404。
# 用法: BASE=http://localhost:8000 ./examples/snapshot_demo.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
API="$BASE/api/v1"

if command -v jq >/dev/null 2>&1; then PPRINT="jq ."; else PPRINT="cat"; fi

post() { # post <path> <json-body> [extra curl args]
  local path="$1" body="$2"; shift 2
  echo "==> POST $path"
  curl -sS -X POST "$API$path" -H 'Content-Type: application/json' "$@" -d "$body" | eval "$PPRINT"
  echo
}

get() { # get <path>
  echo "==> GET $1"
  curl -sS "$API$1" | eval "$PPRINT"
  echo
}

echo "### 0. 健康检查"
curl -sS "$BASE/health" | eval "$PPRINT"; echo

echo "### 1. 登记两张不相邻的有效证书（C1 覆盖 2026 年，C2 覆盖 2027 年）"
post /certificates \
  '{"device_id":"SNAP-D1","certificate_no":"SNAP-C1","valid_from":"2026-01-01T00:00:00+08:00","valid_to":"2027-01-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: snap-demo-c1'
post /certificates \
  '{"device_id":"SNAP-D1","certificate_no":"SNAP-C2","valid_from":"2027-01-01T00:00:00+08:00","valid_to":"2028-01-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: snap-demo-c2'

echo "### 2. 单条写入两条 + 批次写入一条（批次记录同样纳入快照）"
post /measurements \
  '{"device_id":"SNAP-D1","measured_at":"2026-06-15T10:30:00+08:00","certificate_no":"SNAP-C1"}'
post /measurements \
  '{"device_id":"SNAP-D1","measured_at":"2026-09-01T08:00:00+09:00","certificate_no":"SNAP-C1"}'
post /measurement-batches \
  '{"batch_key":"snap-demo-batch-1","records":[
      {"device_id":"SNAP-D1","measured_at":"2026-03-01T12:00:00+08:00","certificate_no":"SNAP-C1"}]}'

echo "### 3. 更正第 1 条测量：最新版本时刻移到 2027 年（同一测量的旧版本仍在历史链中）"
M1=$(curl -sS "$API/measurements?device_id=SNAP-D1&limit=1" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["id"])')
post "/measurements/$M1/corrections" \
  '{"base_version":1,"measured_at":"2027-06-01T09:00:00+08:00","certificate_no":"SNAP-C2","reason":"measurement rescheduled per lab review"}'

echo "### 4. 非法时间窗：缺时区 / 起点不早于终点 -> 422，且不会产生任何快照"
post /audit-snapshots \
  '{"device_id":"SNAP-D1","window_from":"2026-01-01T00:00:00","window_to":"2027-01-01T00:00:00+08:00"}'
post /audit-snapshots \
  '{"device_id":"SNAP-D1","window_from":"2027-01-01T00:00:00+08:00","window_to":"2026-01-01T00:00:00+08:00"}'

echo "### 5. 生成快照：时间窗 [2026-01-01T00+08, 2027-01-01T00+08)，左闭右开"
echo "    第 1 条已更正到 2027 年（最新版本时刻出窗）-> 不计入；窗内共 2 条且全部有效"
RESP=$(curl -sS -X POST "$API/audit-snapshots" -H 'Content-Type: application/json' \
  -d '{"device_id":"SNAP-D1","window_from":"2026-01-01T00:00:00+08:00","window_to":"2027-01-01T00:00:00+08:00"}')
echo "$RESP" | eval "$PPRINT"; echo
SID=$(echo "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["snapshot_id"])')
echo "    快照标识 snapshot_id = $SID"

echo "### 6. 凭标识按测量编号稳定分页（limit=1，逐页翻完）"
get "/audit-snapshots/$SID/items?limit=1&offset=0"
get "/audit-snapshots/$SID/items?limit=1&offset=1"
get "/audit-snapshots/$SID/items?limit=1&offset=2"

echo "### 7. 空窗：设备无任何最新版本时刻落在 2030 年窗口内 -> total/valid/invalid 均为 0"
post /audit-snapshots \
  '{"device_id":"SNAP-D1","window_from":"2030-01-01T00:00:00+08:00","window_to":"2031-01-01T00:00:00+08:00"}'

echo "### 8. 撤销 SNAP-C1 后重新生成同窗口快照：窗内 2 条冻结为无效，原因含撤销时刻"
CID=$(curl -sS "$API/certificates?device_id=SNAP-D1" \
  | python3 -c 'import sys,json;print(next(c["id"] for c in json.load(sys.stdin) if c["certificate_no"]=="SNAP-C1"))')
post "/certificates/$CID/revoke" '{"reason":"drift found during compliance review"}'
RESP2=$(curl -sS -X POST "$API/audit-snapshots" -H 'Content-Type: application/json' \
  -d '{"device_id":"SNAP-D1","window_from":"2026-01-01T00:00:00+08:00","window_to":"2027-01-01T00:00:00+08:00"}')
echo "$RESP2" | eval "$PPRINT"; echo
SID2=$(echo "$RESP2" | python3 -c 'import sys,json;print(json.load(sys.stdin)["snapshot_id"])')
get "/audit-snapshots/$SID2/items?limit=100"

echo "### 9. 旧快照不可变：撤销之后读取步骤 5 的快照，仍是 total=2 且全部 valid"
get "/audit-snapshots/$SID"
get "/audit-snapshots/$SID/items?limit=100"

echo "### 10. 未知快照标识 -> 404 SNAPSHOT_NOT_FOUND（摘要与翻页接口一致）"
get "/audit-snapshots/deadbeefdeadbeefdeadbeefdeadbeef"
get "/audit-snapshots/deadbeefdeadbeefdeadbeefdeadbeef/items"
