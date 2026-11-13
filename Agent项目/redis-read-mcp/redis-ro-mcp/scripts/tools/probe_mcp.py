#!/usr/bin/env python3
"""MCP 协议层探测（人工按需）：拉起 stdio 子进程 → initialize → tools/list → tools/call。

为什么单独有这个脚本：单元自检只能证明"函数能跑"，证明不了"客户端按 MCP 协议
接进来能用"。这里走真实 JSON-RPC 帧，能抓到 tools/list 缺失、content 结构不对、
启动即崩等只在协议层暴露的问题。HTTP 部署后请改用 scripts/checks/verify.sh。

用法（venv 解释器，必须已注入密钥环境变量）：
    set -a; . /etc/redis-ro-mcp/redis-ro-mcp.env; set +a
    PYTHONPATH=src .venv/bin/python scripts/tools/probe_mcp.py
    PYTHONPATH=src .venv/bin/python scripts/tools/probe_mcp.py --tool key_detail --args '{"env":"test","key":"some:key"}'

退出码：0 成功；1 失败。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # <包根>/scripts/tools/probe_mcp.py
EXPECTED_TOOLS = 11


def rpc(proc, payload: dict, want_id: int, timeout: float = 25.0) -> dict:
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()
    end = time.time() + timeout
    while time.time() < end:
        line = proc.stdout.readline()
        if not line:
            return {"error": "EOF from server"}
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("id") == want_id:
            return msg
    return {"error": "timeout waiting id=%s" % want_id}


def main() -> int:
    ap = argparse.ArgumentParser(description="redis-ro-mcp 协议层探测（stdio）")
    ap.add_argument("--config", default=str(ROOT / "config" / "environments.yaml"))
    ap.add_argument("--tool", default="list_environments", help="要试调的工具名")
    ap.add_argument("--args", default="{}", help="该工具的入参 JSON")
    args = ap.parse_args()

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env.setdefault("REDIS_MCP_CONFIG", args.config)
    proc = subprocess.Popen(
        [sys.executable, "-m", "redis_ro_mcp.server", "--transport", "stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1, env=env, cwd=str(ROOT),
    )
    ok = False
    try:
        init = rpc(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "probe", "version": "1.0.0"}},
        }, 1)
        if "result" not in init:
            print("❌ initialize 失败: %s" % json.dumps(init, ensure_ascii=False)[:300])
            return 1
        server_info = (init.get("result") or {}).get("serverInfo") or {}
        print("✅ initialize: name=%s version=%s protocol=%s"
              % (server_info.get("name"), server_info.get("version"),
                 (init.get("result") or {}).get("protocolVersion")))

        proc.stdin.write(json.dumps({"jsonrpc": "2.0",
                                     "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()

        listed = rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                            "params": {}}, 2)
        tools = [t.get("name") for t in (listed.get("result") or {}).get("tools", [])]
        if not tools:
            print("❌ tools/list 为空: %s" % json.dumps(listed, ensure_ascii=False)[:300])
            return 1
        print("✅ tools/list: %d 个 —— %s" % (len(tools), ", ".join(sorted(tools))))
        if len(tools) != EXPECTED_TOOLS:
            print("⚠️  期望 %d 个工具，实际 %d" % (EXPECTED_TOOLS, len(tools)))

        call = rpc(proc, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": args.tool, "arguments": json.loads(args.args)},
        }, 3, timeout=60)
        result = call.get("result") or {}
        if call.get("error"):
            print("❌ tools/call(%s) 协议错误: %s"
                  % (args.tool, json.dumps(call["error"], ensure_ascii=False)[:300]))
            return 1
        content = result.get("content") or []
        text = content[0].get("text", "") if content else ""
        print("✅ tools/call(%s): isError=%s bytes=%d"
              % (args.tool, result.get("isError"), len(text)))
        print("   " + " ".join(text.split())[:400])
        ok = True
    finally:
        try:
            proc.kill()
        except Exception:                            # noqa: BLE001
            pass
        err = (proc.stderr.read() or "").strip() if proc.stderr else ""
        if err:
            print("   stderr: " + err[-600:])

    print()
    print("结论：%s" % ("✅ 协议层可用" if ok else "❌ 协议层不可用"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
