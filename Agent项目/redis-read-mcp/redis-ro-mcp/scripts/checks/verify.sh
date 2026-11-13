#!/usr/bin/env bash
# ============================================================================
# redis-ro-mcp 部署后只读复核（PASS/FAIL 结论，可直接贴验收记录）
#
# 只做 HTTP 探测 + 读审计文件 + 源码静态断言，
# **不连 Redis、不写任何东西、不需要令牌值可读**。
#
# 用法：
#   bash scripts/checks/verify.sh
#   PORT=8050 bash scripts/checks/verify.sh
#
# 退出码：0 全部 PASS；非 0 = 有 FAIL 项数。
# ============================================================================
set -uo pipefail

PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PORT="${PORT:-8050}"
BASE="http://127.0.0.1:${PORT}"
ENV_FILE="${ENV_FILE:-/etc/redis-ro-mcp/redis-ro-mcp.env}"
LOGS_DIR="${LOGS_DIR:-$PKG_DIR/logs}"
TOKEN_HEADER="X-REDIS-RO-MCP-TOKEN"
SRC="$PKG_DIR/src/redis_ro_mcp"

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
  case "$body" in
    *'"proxy_rejected_commands"'*) pass "healthz 回显只读防线" "可见 allowed / proxy_rejected 命令表" ;;
    *) fail "healthz 回显只读防线" "响应里没有 proxy_rejected_commands（guard 配置可能未生效）" ;;
  esac
  case "$body" in
    *'"password"'*) fail "healthz 不泄露口令" "响应里出现 password 字段" ;;
    *) pass "healthz 不泄露口令" "响应里没有口令字段" ;;
  esac
else
  fail "/healthz 200" "${code:-000}（服务没起？systemctl status redis-ro-mcp）"
fi

head1 "2. 准入令牌三态（唯一一层鉴权）"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST "$BASE/mcp/" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  --data-binary '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' || true)"
[ "$code" = "403" ] && pass "无令牌 → 403" "403" || fail "无令牌 → 403" "${code:-000}"

code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST "$BASE/mcp/" \
  -H "${TOKEN_HEADER}: bad-token" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data-binary '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' || true)"
[ "$code" = "403" ] && pass "错令牌 → 403" "403" || fail "错令牌 → 403" "${code:-000}"

if [ -z "$TOKEN" ]; then
  fail "正确令牌 → 非 403" "取不到 $ENV_FILE 里的 GATEWAY_TOKEN，跳过"
else
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST "$BASE/mcp/" \
    -H "${TOKEN_HEADER}: $TOKEN" -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    --data-binary '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"verify","version":"1.0"}}}' || true)"
  case "$code" in
    403 | 000) fail "正确令牌 → 非 403" "${code:-000}" ;;
    *)         pass "正确令牌 → 非 403" "HTTP $code" ;;
  esac
fi

head1 "3. 审计日志（含网关透传身份）"
latest="$(ls -1t "$LOGS_DIR"/audit-redis-*.jsonl 2>/dev/null | head -1 || true)"
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
  if grep -q '"identity_trusted"' "$latest" 2>/dev/null; then
    pass "审计含身份字段" "identity_trusted / client_ip / agent_id 已落盘"
  else
    fail "审计含身份字段" "审计里没有 identity_trusted"
  fi
  echo "  最近 3 行审计（人工核对 client_ip / agent_id / identity_trusted）："
  tail -3 "$latest" | sed 's/^/    /'
else
  fail "审计文件存在" "$LOGS_DIR 下没有 audit-redis-*.jsonl"
fi

head1 "4. 只读防线（静态断言，不连 Redis）"
# 4.1 无通用命令执行入口（只看"调用形态"，文档里提到这几个词不算）
if grep -qE 'execute_command[[:space:]]*\(|\.eval[[:space:]]*\(|evalsha[[:space:]]*\(' "$SRC"/*.py 2>/dev/null; then
  fail "无通用命令执行入口" "源码里出现 execute_command / eval / evalsha 调用（只读服务的红线）"
else
  pass "无通用命令执行入口" "src/ 下没有 execute_command / eval / evalsha 调用"
fi

# 4.2 键枚举一律 SCAN，绝不 KEYS
if grep -qE '\b(r|client|conn|redis_client)\.keys\(' "$SRC"/*.py 2>/dev/null; then
  fail "不使用 KEYS" "源码里出现 .keys( 调用（KEYS 会阻塞实例且不在白名单）"
else
  pass "不使用 KEYS" "键枚举只走 SCAN"
fi
if grep -q '\.scan(' "$SRC"/server.py 2>/dev/null; then
  pass "使用 SCAN" "server.py 里有 .scan( 调用"
else
  fail "使用 SCAN" "server.py 里没有 .scan( 调用"
fi

# 4.3 写命令零出现
# 只匹配「redis 客户端变量的方法调用」形态，例如 r.set(...) / client.hset(...)；
# 不匹配 contextvars 的 ctx.set(...)、pathlib 的 p.unlink()、list 的 rows.append(...)
# —— 后者是 Python 原生方法，在这个服务里与 Redis 写命令无关。
RECEIVER='\b(r|client|conn|redis_client)\.'
WRITE_METHODS="set setnx setex psetex getset append setrange mset msetnx incr decr incrby decrby incrbyfloat hset hmset hsetnx hincrby hincrbyfloat hdel lpush rpush lpushx rpushx lpop rpop lset linsert ltrim lrem sadd srem spop smove zadd zrem zincrby zremrangebyrank zremrangebyscore zpopmin zpopmax del unlink expire pexpire expireat persist rename renamenx move copy restore flushall flushdb swapdb eval evalsha multi exec watch publish spublish"
hits=""
for m in $WRITE_METHODS; do
  if grep -qE "${RECEIVER}${m}\(" "$SRC"/*.py 2>/dev/null; then
    hits="${hits} ${m}"
  fi
done
if [ -z "$hits" ]; then
  wcount="$(printf '%s\n' $WRITE_METHODS | wc -l | tr -d ' ')"
  pass "源码无写命令调用" "${wcount} 类写方法全部不存在"
else
  fail "源码无写命令调用" "命中:${hits}"
fi

# 4.4 命令白/黑名单确实在 guard 里（改配置不改代码的前提）
if grep -q 'allowed_commands' "$SRC/redis_guard.py" 2>/dev/null \
   && grep -q 'forbidden_commands' "$SRC/redis_guard.py" 2>/dev/null; then
  pass "只读白/黑名单存在" "redis_guard.py 里有 allowed_commands / forbidden_commands"
else
  fail "只读白/黑名单存在" "redis_guard.py 里没找到白/黑名单（不要上线）"
fi

# 4.5 令牌常量时间比较
if grep -q 'compare_digest' "$SRC/auth.py" 2>/dev/null; then
  pass "令牌常量时间比较" "hmac.compare_digest"
else
  fail "令牌常量时间比较" "auth.py 里没有 compare_digest"
fi

# 4.6 身份防伪造留痕
if grep -q 'untrusted-peer' "$SRC/identity.py" 2>/dev/null; then
  pass "身份防伪造留痕" "不可信直连方标 untrusted-peer"
else
  fail "身份防伪造留痕" "identity.py 里没有 untrusted-peer 标记"
fi

echo
if [ "$FAILED" -gt 0 ]; then
  echo "结论：❌ $FAILED 项未通过"
else
  echo "结论：✅ 全部通过"
fi
exit "$FAILED"
