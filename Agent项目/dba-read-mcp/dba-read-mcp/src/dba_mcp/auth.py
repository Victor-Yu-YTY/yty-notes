"""请求身份与准入令牌（v0.3.0：唯一一层鉴权）。

## 本版做了什么

按《MCP 统一鉴权方案》v1.1 §1.3「不应引入第三层鉴权」，v0.3.0 把原来的
**第二层"用户凭证校验"整层移除**：

| 原 v0.2.x | 现在 |
| --- | --- |
| 第二层"用户凭证校验"：`auth.mode = http`，`verify_url` 指向本机一个 mock 鉴权服务 | 删除。不再向任何地址转发调用方凭证 |
| `HttpTokenAuthProvider` / `StaticTokenAuthProvider` / `NoopAuthProvider` / `CachingAuthProvider` | 删除。不存在"可以关掉鉴权"的路径 |
| `_gate()` 里校验 `dba:read:{env}` scope，不足返回 `AUTH_SCOPE_DENIED` | 删除。环境授权由 AgentID 临时凭证的 `mysql_scope` 编组承担，MCP 不重复判定 |
| `Authorization: Bearer <token>` 抓取 | 删除。MCP 不再收调用方凭证 |

保留并统一的只有：**准入令牌**（证明请求来自受管入口 / 网关），
缺失或错误一律 HTTP 403，且发生在 MCP 协议处理之前。

## 调用方身份：只用于审计

网关可以在转发时注入下列请求头。本服务**只把它们写进审计日志**，
不参与任何放行/拒绝判定（规范要求：不得用于授权判定）。

| 请求头 | 含义 |
| --- | --- |
| `X-Agent-Id` | AgentID 核验返回的 `subject.user_id` |
| `X-Agent-Name` | AgentID 核验返回的 `agent_name` |
| `X-Request-Id` | 网关生成的调用流水号，用于把"结论"和"证据"绑在一起 |

## 明确不做的事

1. 不缓存任何鉴权结果（既没有可缓存的对象，也就无从"凭既往成功放行"）；
2. 不落任何凭证原文（令牌只参与比较，不写日志、不写审计）；
3. 不在缺少令牌时降级放行——配置层已禁止"无令牌启动"。
"""

from __future__ import annotations

import contextvars
import hmac
from typing import Iterable, Mapping

# 当前请求的审计用身份（由 ASGI 中间件设置；只读，不参与授权）
_identity: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "dba_mcp_identity", default={}
)

# 审计里记录的身份字段上限，避免超长头撑爆日志
_MAX_FIELD = 128


def bind_identity(agent_id: str = "", agent_name: str = "", request_id: str = "") -> None:
    _identity.set(
        {
            "agent_id": (agent_id or "")[:_MAX_FIELD],
            "agent_name": (agent_name or "")[:_MAX_FIELD],
            "request_id": (request_id or "")[:_MAX_FIELD],
        }
    )


def current_identity() -> dict[str, str]:
    """取当前请求的身份信息；无请求上下文（如 stdio 调试）时返回空字典。"""
    ident = _identity.get()
    return {k: v for k, v in ident.items() if v}


class IdentityCaptureMiddleware:
    """纯 ASGI 中间件：抓取网关注入的身份头，绑定到 contextvar（仅供审计）。

    注意：这里**不做拦截**。拦截由 _GatewayTokenMiddleware 在协议层之前完成，
    两者职责分开，避免"身份校验"再次长成第二套鉴权。
    """

    def __init__(self, app) -> None:
        self._app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") == "http":
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            }
            bind_identity(
                agent_id=(headers.get("x-agent-id") or "").strip(),
                agent_name=(headers.get("x-agent-name") or "").strip(),
                request_id=(headers.get("x-request-id") or "").strip(),
            )
        elif scope.get("type") != "http":
            bind_identity()
        await self._app(scope, receive, send)


def pick_header(headers: Mapping[str, str], names: Iterable[str]) -> tuple[str, str]:
    """按 headers 名顺序取第一个非空值，返回 (头名, 值)。取不到返回 ("", "")。"""
    for name in names:
        value = (headers.get(name) or "").strip()
        if value:
            return name, value
    return "", ""


def token_ok(provided: str, expected: str) -> bool:
    """常量时间比较，避免时序侧信道；任一为空一律不通过。"""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)
