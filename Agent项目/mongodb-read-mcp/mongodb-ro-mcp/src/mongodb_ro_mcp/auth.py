"""准入令牌（唯一一层鉴权）。

本模块只做两件事，都很小、都可单独测：

1. `pick_header()` —— 按头名列表顺序取第一个非空值（兼容新旧头名过渡期）；
2. `token_ok()`   —— 常量时间比较（`hmac.compare_digest`），避免时序侧信道。

明确不做的事（与 db-read-mcp v1.0.0 / sql-review-* v1.0.1 保持一致）：
1. 不校验"用户凭证"：调用方身份与环境授权由 AgentID 在网关侧逐请求核验；
2. 不缓存任何鉴权结果：既没有可缓存的对象，也就无从"凭既往成功放行"；
3. 不在缺少令牌时降级放行：config.py 已禁止"无令牌启动"；
4. 不落任何凭证原文：令牌只参与比较，不写日志、不写审计。
"""

from __future__ import annotations

import hmac
from typing import Iterable, Mapping


def pick_header(headers: Mapping[str, str], names: Iterable[str]) -> tuple[str, str]:
    """按 names 顺序取第一个非空请求头，返回 (头名, 值)；取不到返回 ("", "")。"""
    for name in names or ():
        value = (headers.get(str(name).lower()) or "").strip()
        if value:
            return str(name), value
    return "", ""


def token_ok(provided: str, expected: str) -> bool:
    """常量时间比较；任一为空一律不通过。"""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
