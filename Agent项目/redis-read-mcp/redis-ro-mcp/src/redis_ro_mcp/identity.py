"""调用方身份：网关透传的「机器 IP + AgentID」→ contextvar → 审计日志。

严格对齐 mongodb-ro-mcp / sql-review-ddl-mcp / sql-review-dml-mcp v1.0.1 的做法
（那套已在 10.0.0.218 上线实测）：

    socket_ip  ← 直连方（正常应为网关 10.0.0.219）
    trusted    ← 直连方是否在 identity.trusted_proxies 之内（支持 IP 与 CIDR）
    client_ip  ← trusted 时取 X-Forwarded-For 的**最左一跳**（最原始客户端），否则取 socket_ip
    agent_id   ← trusted 时取 X-Agent-Id（按配置的头名顺序第一个非空），否则留空

**防伪造**：别人绕过网关直连本服务并自己塞 `X-Agent-Id`/`X-Forwarded-For` 时，
日志里 `agent_id` 恒为空、`identity_trusted=false`、`agent_id_source="untrusted-peer"`，
伪造当场留痕。三件事都要记：谁说的（*_source）、说的是不是可信（identity_trusted）、
直连的是谁（socket_ip）。

**只审计、不授权**：本模块的结论不参与任何放行/拒绝判定，规范明令禁止把身份头
用于授权（授权只看准入令牌 + 网关侧 AgentID 核验）。
"""

from __future__ import annotations

import contextvars
import ipaddress
from typing import Any

# 当前请求的身份（ASGI 中间件设置；stdio 模式下为空字典）
_identity: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "redis_mcp_identity", default={}
)

# 落日志前的长度上限，避免超长头撑爆日志
MAX_FIELD = 128


def clean_ident(value: Any, limit: int = MAX_FIELD) -> str:
    """身份字段落盘前清洗：去掉控制字符（防换行注入）并截断。"""
    text = "".join(ch for ch in str(value or "") if ch.isprintable())
    return text.strip()[:limit]


def peer_trusted(socket_ip: str, trusted: list[str] | None) -> bool:
    """直连方是否可信代理（支持单个 IP 与 CIDR）。"""
    if not socket_ip:
        return False
    try:
        ip = ipaddress.ip_address(socket_ip)
    except ValueError:
        return False
    for item in trusted or ():
        text = str(item or "").strip()
        if not text:
            continue
        try:
            if "/" in text:
                if ip in ipaddress.ip_network(text, strict=False):
                    return True
            elif ip == ipaddress.ip_address(text):
                return True
        except ValueError:
            continue
    return False


def bind(identity: dict[str, Any]) -> None:
    _identity.set(identity or {})


def current() -> dict[str, Any]:
    """取当前请求的身份；无请求上下文（stdio 调试）时返回空字典。"""
    ident = _identity.get() or {}
    return {k: v for k, v in ident.items() if v not in (None, "")}


def resolve(scope: dict, headers: dict[str, str], settings: Any) -> dict[str, Any]:
    """解析本次请求的身份，返回可直接展开进审计的字典。"""
    client = scope.get("client") or ("", 0)
    socket_ip = clean_ident(str(client[0] or ""), 64)
    trusted = peer_trusted(socket_ip, getattr(settings, "trusted_proxies", None))

    ip_name, ip_value = _pick(headers, getattr(settings, "client_ip_headers", None))
    if trusted and ip_value:
        client_ip = clean_ident(ip_value.split(",")[0], 64)     # XFF 最左一跳
        client_ip_source = ip_name
    else:
        client_ip = socket_ip
        client_ip_source = "socket"

    agent_name, agent_value = _pick(headers, getattr(settings, "agent_id_headers", None))
    if trusted and agent_value:
        agent_id = clean_ident(agent_value)
        agent_id_source = agent_name
    else:
        agent_id = ""
        agent_id_source = "untrusted-peer" if agent_value else ""

    return {
        "client_ip": client_ip,
        "client_ip_source": client_ip_source,
        "agent_id": agent_id,
        "agent_id_source": agent_id_source,
        "socket_ip": socket_ip,
        "identity_trusted": trusted,
    }


def _pick(headers: dict[str, str], names: list[str] | None) -> tuple[str, str]:
    for name in names or ():
        value = (headers.get(str(name).lower()) or "").strip()
        if value:
            return str(name), value
    return "", ""


class IdentityCaptureMiddleware:
    """纯 ASGI 中间件：抓取网关注入的身份头，绑定到 contextvar（**不做拦截**）。

    拦截是 TokenMiddleware 的职责，两者分开，避免"身份校验"再长成第二套鉴权。

    入参是 **IdentityConfig**（不是整个 Settings）——传错成 Settings 会让
    `trusted_proxies` 取不到，从而把外层已绑定的正确身份降级成"非可信"。
    没有配置时（例如 SDK 自带 http 服务的兜底路径）只负责清空上下文，
    防止上一次请求的身份串到下一次。
    """

    def __init__(self, app, identity=None) -> None:
        self._app = app
        self._ident = identity

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") == "http" and self._ident is not None:
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            }
            bind(resolve(scope, headers, self._ident))
        else:
            bind({})
        await self._app(scope, receive, send)
