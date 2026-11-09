#!/usr/bin/env bash
# ============================================================================
# db-read-mcp v1.0.0 复核脚本（只读，不改任何配置）
#
# 用途：部署后自证「假鉴权真的下掉了」，可直接贴进验收记录。
#   bash /opt/mcpservers/dba-mcp/scripts/checks/verify.sh
#
# 只查本次范围（假鉴权）。监听面 / 导出能力本版未改动，脚本会把现状如实列出（INFO），
# 不参与 PASS/FAIL 判定 —— 免得把"下一版才做的事"误报成"这次没做"。
# ============================================================================
set -uo pipefail

DEST="${DEST:-/opt/mcpservers/dba-mcp}"
ENV_FILE="${ENV_FILE:-/etc/dba-mcp/dba-mcp.env}"
PORT="${PORT:-8000}"
MOCK_UNIT=dba-mcp-mock-auth

PASS=0; FAIL=0
ok()   { echo "  [PASS] $*"; PASS=$((PASS+1)); }
ng()   { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
info() { echo "  [INFO] $*"; }

echo "=========== db-read-mcp v1.0.0 复核（去假鉴权，$(date '+%F %T')） ==========="

# ---------- 一、假鉴权本体 ----------
echo "一、假鉴权是否真的下掉了"
if systemctl list-unit-files 2>/dev/null | grep -qE "^${MOCK_UNIT}\.service"; then
  ng "${MOCK_UNIT}.service 仍登记在 systemd 里"
else
  ok "systemd 未登记 ${MOCK_UNIT}"
fi
if systemctl is-active --quiet "$MOCK_UNIT" 2>/dev/null; then
  ng "${MOCK_UNIT} 仍在 active"
else
  ok "${MOCK_UNIT} 未运行"
fi
if ss -lntp 2>/dev/null | grep -qE ':9000\b'; then
  ng "9000 端口仍有进程监听：$(ss -lntp 2>/dev/null | grep -E ':9000\b' | tr -s ' ')"
else
  ok "9000 端口无监听"
fi
if [ -f "$DEST/scripts/mock_auth_server.py" ]; then
  ng "假鉴权脚本仍存在：$DEST/scripts/mock_auth_server.py"
else
  ok "假鉴权脚本已删除"
fi
if grep -qE 'verify_url|mock.?auth|9000/api/auth' "$DEST/config/environments.yaml" 2>/dev/null; then
  ng "配置里仍有 verify_url / 假鉴权残留"
else
  ok "配置中无 verify_url / 假鉴权残留"
fi
if grep -rqE 'AUTH_TOKEN_MISSING|AUTH_FAILED|AUTH_SCOPE_DENIED' "$DEST/src" 2>/dev/null; then
  ng "源码里仍有第二层鉴权错误码（AUTH_*）"
else
  ok "源码中已无 AUTH_TOKEN_MISSING / AUTH_FAILED / AUTH_SCOPE_DENIED"
fi
if grep -rqE 'verify_url|build_auth_provider|HttpTokenAuthProvider' "$DEST/src" 2>/dev/null; then
  ng "源码里仍有第二层鉴权实现残留"
else
  ok "源码中已无第二层鉴权实现（provider / verify_url）"
fi

# ---------- 二、准入令牌三态 ----------
echo "二、准入令牌（唯一一层鉴权）"
TOKEN=""
if [ -f "$ENV_FILE" ]; then
  TOKEN="$(grep -E '^GATEWAY_TOKEN=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true)"
fi
[ -n "$TOKEN" ] && ok "env 文件中存在 GATEWAY_TOKEN（未回显）" || ng "env 文件缺少 GATEWAY_TOKEN"

BASE="http://127.0.0.1:${PORT}"
c=000; c="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE/healthz" || true)"
[ "$c" = "200" ] && ok "/healthz 免鉴权返回 200" || ng "/healthz 返回 $c（期望 200）"

c=000; c="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST "$BASE/mcp/" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  --data-binary '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' || true)"
[ "$c" = "403" ] && ok "无令牌调用 /mcp/ -> 403" || ng "无令牌调用 /mcp/ -> $c（期望 403）"

c=000; c="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST "$BASE/mcp/" \
  -H 'X-DB-READ-MCP-TOKEN: bad-token' -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data-binary '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' || true)"
[ "$c" = "403" ] && ok "错令牌调用 /mcp/ -> 403" || ng "错令牌调用 /mcp/ -> $c（期望 403）"

c=000; c="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST "$BASE/mcp/" \
  -H "X-DB-READ-MCP-TOKEN: $TOKEN" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data-binary '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' || true)"
case "$c" in
  403) ng "正确令牌仍被 403 —— 网关会全量失败，优先排查" ;;
  000) ng "正确令牌连不上" ;;
  *)   ok "正确令牌 -> $c（非 403 即为通过）" ;;
esac

c=000; c="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST "$BASE/mcp/" \
  -H "X-DBA-MCP-TOKEN: $TOKEN" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data-binary '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' || true)"
case "$c" in
  403|000) ng "旧头名 X-DBA-MCP-TOKEN -> $c（兼容期应仍可用）" ;;
  *)       ok "旧头名 X-DBA-MCP-TOKEN 仍兼容 -> $c" ;;
esac

# ---------- 三、审计 ----------
echo "三、审计（拒绝可追溯，令牌不落盘）"
DAY="$(date +%F)"
AUDIT="$DEST/logs/audit-$DAY.jsonl"
if [ -f "$AUDIT" ]; then
  ok "今日审计文件存在：logs/audit-$DAY.jsonl"
  if grep -qF "$TOKEN" "$AUDIT" 2>/dev/null; then
    ng "审计中出现准入令牌原文（必须没有）"
  else
    ok "审计中不含准入令牌原文"
  fi
  if grep -q 'gateway_token' "$AUDIT" 2>/dev/null; then
    ok "拒绝事件已写入审计（tool=gateway_token）"
  else
    info "今日审计里还没有拒绝记录（刚部署、或没人试探过）"
  fi
else
  info "今日尚无审计文件（还没有工具调用也会这样）"
fi

# ---------- 四、本版未改动的项：只报现状 ----------
echo "四、本版未改动项（如实列出，不判定）"
LA="$(ss -lntp 2>/dev/null | grep -E ":${PORT}\b" | tr -s ' ' || true)"
[ -n "$LA" ] && info "监听面（本版保持原样）：$LA" || info "监听面：未见 :${PORT} 监听"
if [ -f "$DEST/config/environments.yaml" ]; then
  HOST="$(grep -E '^\s*host:' "$DEST/config/environments.yaml" | head -1 | awk '{print $2}')"
  info "配置 server.host=$HOST（监听收敛属下一版，本版不改）"
fi
info "导出能力保留（/export/... 下载链接仍在），属暴露面议题，下一版处理"

echo "-------------------------------------------------------------"
echo "结论：PASS ${PASS} 项，FAIL ${FAIL} 项"
if [ "$FAIL" -gt 0 ]; then
  echo "❌ 未通过 —— 见上面 [FAIL] 行"
  exit 1
fi
echo "✅ 全部通过：第二层鉴权（假鉴权）已彻底下掉，只剩准入令牌一层"
exit 0
