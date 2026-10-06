#!/usr/bin/env bash
# 审计快照端到端示例：
#   证书 + 单条/批次测量 → 生成快照（左闭右开窗）→ 翻页读取明细
#   → 更正把记录移出窗口 / 撤销证书 → 再生成快照对比 → 验证旧快照冻结不变
#   → 非法时间窗拒绝、未知快照 404
# 用法: BASE=http://localhost:8000 ./examples/audit_snapshot_demo.sh
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

D="AUDIT-DEMO-$RANDOM"
W0="2026-01-01T00:00:00+08:00"
W1="2027-01-01T00:00:00+08:00"

echo "### 1. 登记证书"
post /certificates \
  "{\"device_id\":\"$D\",\"certificate_no\":\"CERT-AUDIT-0001\",\"valid_from\":\"$W0\",\"valid_to\":\"$W1\"}" \
  -H "Idempotency-Key: audit-demo-reg-$RANDOM"

echo "### 2. 单条写入两条测量（一条在窗口起点——左闭；另一条在窗口内）"
post /measurements \
  "{\"device_id\":\"$D\",\"measured_at\":\"$W0\",\"certificate_no\":\"CERT-AUDIT-0001\"}"
post /measurements \
  "{\"device_id\":\"$D\",\"measured_at\":\"2026-06-15T10:30:00+08:00\",\"certificate_no\":\"CERT-AUDIT-0001\"}"

echo "### 3. 批次再写入一条（单条与批次均纳入快照）"
post /measurement-batches \
  "{\"batch_key\":\"audit-demo-batch-$RANDOM\",\"records\":[
      {\"device_id\":\"$D\",\"measured_at\":\"2026-06-16T09:00:00+08:00\",\"certificate_no\":\"CERT-AUDIT-0001\"}]}"

echo "### 4. 生成审计快照（时间窗左闭右开，时刻带时区）"
echo "==> POST /audit-snapshots"
SNAP_JSON=$(curl -sS -X POST "$API/audit-snapshots" -H 'Content-Type: application/json' \
  -d "{\"device_id\":\"$D\",\"window_start\":\"$W0\",\"window_end\":\"$W1\"}")
echo "$SNAP_JSON" | eval "$PPRINT"
SID=$(printf '%s' "$SNAP_JSON" | sed -n 's/.*"snapshot_id":\([0-9]*\).*/\1/p')
echo "snapshot_id = $SID"; echo

echo "### 5. 凭快照标识读取快照头（有效/无效总数）"
get "/audit-snapshots/$SID"

echo "### 6. 首页明细（每页 2 条，按测量编号升序）"
get "/audit-snapshots/$SID/items?limit=2"

echo "### 7. 翻页：以上一页最后一条 measurement_id 作为游标继续"
echo "==> GET /audit-snapshots/$SID/items?limit=2&after_measurement_id=<上一页末条编号>"
echo "（响应中的 next_after_measurement_id 即下一页游标，为 null 表示已到末页）"
echo

echo "### 8. 非法请求：缺时区 → 422；起点不早于终点 → 422（均不留快照）"
post /audit-snapshots \
  "{\"device_id\":\"$D\",\"window_start\":\"2026-01-01T00:00:00\",\"window_end\":\"$W1\"}"
post /audit-snapshots \
  "{\"device_id\":\"$D\",\"window_start\":\"$W1\",\"window_end\":\"$W0\"}"

echo "### 9. 未知快照标识 → 404 SNAPSHOT_NOT_FOUND"
get "/audit-snapshots/99999999"

echo "### 10. 撤销证书后再次生成快照：同一窗口此次 valid=0/invalid=3，原因冻结撤销信息"
CERT_ID=$(curl -sS "$API/certificates?device_id=$D" | sed -n 's/.*"id":\([0-9]*\).*/\1/p' | head -1)
post "/certificates/$CERT_ID/revoke" '{"reason":"audit demo revoke: drift found"}'
post /audit-snapshots \
  "{\"device_id\":\"$D\",\"window_start\":\"$W0\",\"window_end\":\"$W1\"}"

echo "### 11. 回看撤销前的旧快照 $SID：计数与明细仍为 valid=3，冻结不变"
get "/audit-snapshots/$SID"
get "/audit-snapshots/$SID/items"
