#!/usr/bin/env bash
# ============================================================================
# mongodb-ro-mcp 部署后只读复核（PASS/FAIL 结论，可直接贴验收记录）
#
# 只做 HTTP 探测 + 读审计文件，**不连数据库、不写任何东西、不需要令牌值可读**。
#
# 用法：
#   bash scripts/checks/verify.sh
#   PORT=8040 bash scripts/checks/verify.sh
#
# 退出码：0 全部 PASS；非 0 = 有 FAIL 项数。
# ============================================================================
set -uo pipefail

PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PORT="${PORT:-8040}"
BASE="http://127.0.0.1:${PORT}"
ENV_FILE="${ENV_FILE:-/etc/mongodb-ro-mcp/mongodb-ro-mcp.env}"
LOGS_DIR="${LOGS_DIR:-$PKG_DIR/logs}"

FAILED=0
pass() { echo "  [PASS] $1 —— $2"; }
fail() { echo "  [FAIL] $1 —— $2"; FAILED=$((FAILED + 1)); }
head1() { echo; echo "== $* =="; }

TOKEN=""
if [ -f "$ENV_FILE" ]; then
  TOKEN="$(grep -E '^GATEWAY_TOKEN=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true)"
fi

head1 "1. 服务与版本"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE/healthz" || true)"
if [ "$code" = "200" ]; then
  body="$(curl -s --max-time 5 "$BASE/healthz" || true)"
  ver="$(printf '%s' "$body" | sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' | head -1)"
  pass "/healthz 200" "version=${ver:-未知}"
  case "$body" in
    *'"identity"'*) pass "healthz 回显 identity 段" "可见 agent_id_headers / trusted_proxies" ;;
    *) fail "healthz 回显 identity 段" "响应里没有 identity 字段（配置可能未生效）" ;;
  esac
else
  fail "/healthz 200" "${code:-000}（服务没起？systemctl status mongodb-ro-mcp）"
fi

head1 "2. 准入令牌三态（唯一一层鉴权）"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST "$BASE/mcp/" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  --data-binary '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' || true)"
[ "$code" = "403" ] && pass "无令牌 → 403" "403" || fail "无令牌 → 403" "${code:-000}"

code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST "$BASE/mcp/" \
  -H 'X-MONGODB-RO-MCP-TOKEN: bad-token' -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data-binary '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' || true)"
[ "$code" = "403" ] && pass "错令牌 → 403" "403" || fail "错令牌 → 403" "${code:-000}"

if [ -z "$TOKEN" ]; then
  fail "正确令牌 → 非 403" "取不到 $ENV_FILE 里的 GATEWAY_TOKEN，跳过"
else
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST "$BASE/mcp/" \
    -H "X-MONGODB-RO-MCP-TOKEN: $TOKEN" -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    --data-binary '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"verify","version":"1.0"}}}' || true)"
  case "$code" in
    403 | 000) fail "正确令牌 → 非 403" "${code:-000}" ;;
    *)         pass "正确令牌 → 非 403" "HTTP $code" ;;
  esac
fi

head1 "3. 审计日志（含网关透传身份）"
today="$LOGS_DIR/audit-mongo-$(date +%F).jsonl"
latest="$(ls -1t "$LOGS_DIR"/audit-mongo-*.jsonl 2>/dev/null | head -1 || true)"
if [ -n "$latest" ]; then
  pass "审计文件存在" "$latest"
  lines="$(wc -l < "$latest" 2>/dev/null | tr -d ' ')"
  pass "审计行数" "${lines:-0}"
  if grep -q '"event": *"auth_denied"' "$latest" 2>/dev/null || \
     grep -q '"event":"auth_denied"' "$latest" 2>/dev/null; then
    pass "鉴权拒绝留痕" "auth_denied 已记录（上面两次 403 的痕迹）"
  else
    fail "鉴权拒绝留痕" "没有 auth_denied 记录（是否 audit.log_http_errors=false？）"
  fi
  if [ -f "$today" ]; then
    echo "  最近 3 行审计（人工核对 client_ip / agent_id / identity_trusted）："
    tail -3 "$today" | sed 's/^/    /'
  fi
else
  fail "审计文件存在" "$LOGS_DIR 下没有 audit-mongo-*.jsonl"
fi

head1 "4. 只读防线（静态断言，不连库）"
if grep -q '\$out' "$PKG_DIR/src/mongodb_ro_mcp/mongo_guard.py" 2>/dev/null \
   && grep -q '\$merge' "$PKG_DIR/src/mongodb_ro_mcp/mongo_guard.py" 2>/dev/null; then
  pass "禁止 \$out / \$merge" "算子黑名单在 guard 里"
else
  fail "禁止 \$out / \$merge" "guard 里没找到黑名单（不要上线）"
fi
if grep -q 'execute_command\|run_js\|db.command' "$PKG_DIR/src/mongodb_ro_mcp/server.py" 2>/dev/null; then
  if grep -q 'execute_command' "$PKG_DIR/src/mongodb_ro_mcp/server.py" 2>/dev/null; then
    fail "无通用命令执行入口" "server.py 里出现 execute_command"
  else
    pass "无通用命令执行入口" "server.py 只有内部固定命令调用（collStats/isMaster/explain 等）"
  fi
else
  pass "无通用命令执行入口" "未发现通用命令调用"
fi
if grep -q 'compare_digest' "$PKG_DIR/src/mongodb_ro_mcp/auth.py" 2>/dev/null; then
  pass "令牌常量时间比较" "hmac.compare_digest"
else
  fail "令牌常量时间比较" "auth.py 里没有 compare_digest"
fi

echo
if [ "$FAILED" -gt 0 ]; then
  echo "结论：❌ $FAILED 项未通过"
else
  echo "结论：✅ 全部通过"
fi
exit "$FAILED"
