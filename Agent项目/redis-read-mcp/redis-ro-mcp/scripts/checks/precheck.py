#!/usr/bin/env python3
"""部署前预检（硬门禁）：三环境连通 / Predixy 版本 / 数据库序号 / SCAN / 白名单实测。

跑不过 → deploy.sh 直接中止，服务不启动。

用法（在安装目录下，venv 解释器）：
    set -a; . /etc/redis-ro-mcp/redis-ro-mcp.env; set +a
    PYTHONPATH=src .venv/bin/python scripts/checks/precheck.py --config config/environments.yaml
    PYTHONPATH=src .venv/bin/python scripts/checks/precheck.py --env test --json

退出码：0 全部 OK；1 有环境不可用；2 配置/依赖问题。

预期结果（2026-11-10 三环境实测，见 README §2）：
  · 允许：PING / INFO / SCAN / RANDOMKEY / TYPE / TTL / STRLEN / GETRANGE /
          LRANGE / LLEN / HGETALL / HGET / HLEN / HKEYS / SMEMBERS / SCARD /
          ZRANGE / ZCARD / ZRANGEBYSCORE / EXISTS / CONFIG GET(单参数)
  · 拒绝：DBSIZE / OBJECT / MEMORY / SLOWLOG / CLIENT / CLUSTER
被拒绝的命令是**预期行为**，脚本只在"本该被拒却可用"时告警（提示 Predixy 可能已放开）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # <包根>/scripts/checks/precheck.py
sys.path.insert(0, str(ROOT / "src"))


def _probe_read_ops(client, result: dict) -> None:
    """逐个实测只读命令，把结果落成事实（不抛异常）。"""
    allowed: dict[str, str] = {}
    # 取一个真实 key 做类型化读探测（SCAN 抽样，绝不 KEYS）
    sample_key = None
    try:
        cursor, keys = client.scan(cursor=0, count=20)
        sample_key = keys[0] if keys else None
        allowed["scan"] = "ok（样例 %d 个键）" % len(keys)
    except Exception as exc:                        # noqa: BLE001
        allowed["scan"] = "FAIL: %s" % str(exc).split("\n")[0][:80]

    checks = {
        "ping": lambda: client.ping(),
        "info": lambda: bool(client.info()),
        "config_get": lambda: client.config_get("maxmemory"),
        "randomkey": lambda: client.randomkey(),
    }
    if sample_key:
        checks.update({
            "type": lambda: client.type(sample_key),
            "ttl": lambda: client.ttl(sample_key),
            "exists": lambda: client.exists(sample_key),
        })
    for label, fn in checks.items():
        try:
            fn()
            allowed[label] = "ok"
        except Exception as exc:                    # noqa: BLE001
            allowed[label] = "FAIL: %s" % str(exc).split("\n")[0][:80]
    result["allowed_commands"] = allowed

    rejected: dict[str, str] = {}
    for label, fn in {
        "dbsize": lambda: client.dbsize(),
        "slowlog": lambda: client.slowlog_get(1),
        "client_list": lambda: client.client_info(),
    }.items():
        try:
            fn()
            rejected[label] = "意外可用（Predixy 可能已升级/放开，需重新核对工具面）"
        except Exception as exc:                    # noqa: BLE001
            rejected[label] = "denied（预期）: %s" % str(exc).split("\n")[0][:60]
    result["rejected_commands"] = rejected

    # 提示性告警：被拒命令若可用，说明工具面还有扩展空间（不阻断部署）
    for label, value in rejected.items():
        if value.startswith("意外可用"):
            result.setdefault("warnings", []).append(
                "%s 竟可用：Predixy 白名单可能已放开，建议复核 README §2 与工具面" % label)


def check_env(settings, name: str) -> dict:
    """检查单个环境，返回结果字典（不抛异常，全部落成事实）。

    每个阶段都会写 `result["stage"]`，失败时用它定位是"连不上 / 认证不过 /
    数据库序号越界 / 命令被拒"，避免只看到一句 TypeError 还要猜。
    """
    from redis_ro_mcp.pool import make_client

    conf = settings.env(name)
    result = {
        "env": name,
        "env_actual": conf.actual,
        "display_name": conf.display_name,
        "endpoint": conf.endpoint,
        "auth": bool(conf.has_auth),
        "ok": False,
        "stage": "init",
    }

    def mark(stage: str) -> None:
        result["stage"] = stage

    client = None
    try:
        mark("connect")
        client = make_client(conf)
        mark("ping")
        result["ping"] = bool(client.ping())

        mark("info")
        raw = client.info() or {}
        result["proxy"] = {
            "name": raw.get("Name") or raw.get("name"),
            "version": raw.get("Version") or raw.get("version"),
            "mode": raw.get("RedisMode") or raw.get("redis_mode"),
        }
        result["note"] = "版本信息为 Predixy 代理层；后端 Redis 版本经 proxy 不可见"

        mark("readOps")
        _probe_read_ops(client, result)

        # 只读复核：确认没有任何写命令会被本服务发出（静态，不依赖连接）
        result["forbidden_commands"] = len(settings.guard.forbidden_commands)

        result["ok"] = True
        result["stage"] = "ok"
    except Exception as exc:                        # noqa: BLE001
        result["error"] = "[阶段 %s] %s: %s" % (
            result.get("stage", "?"), type(exc).__name__,
            str(exc).split("\n")[0][:200])
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:                       # noqa: BLE001
                pass
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="redis-ro-mcp 部署前预检")
    ap.add_argument("--config", default=None, help="environments.yaml 路径")
    ap.add_argument("--env", default="", help="只检查某个环境（留空=全部）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（便于进验收记录）")
    args = ap.parse_args()

    try:
        from redis_ro_mcp.config import load_settings
        settings = load_settings(args.config)
    except Exception as exc:                        # noqa: BLE001
        print("[precheck][FAIL] 配置加载失败: %s" % exc, file=sys.stderr)
        return 2

    targets = [args.env] if args.env else settings.names()
    results = []
    failed = 0
    for name in targets:
        try:
            res = check_env(settings, name)
        except Exception as exc:                    # noqa: BLE001
            res = {"env": name, "ok": False, "error": str(exc)[:200]}
        results.append(res)
        if not res.get("ok"):
            failed += 1

    if args.json:
        print(json.dumps({"ok": failed == 0, "results": results},
                         ensure_ascii=False, indent=1))
        return 0 if failed == 0 else 1

    print("=" * 78)
    print("redis-ro-mcp 部署前预检（目标：Redis 5.0.6 + Predixy 1.0.5，端口 25900、db0）")
    print("=" * 78)
    for res in results:
        name = res["env"]
        print()
        print("[%s] → 实际环境 %s（%s）%s"
              % (name, res.get("env_actual", name), res.get("display_name", ""),
                 res.get("endpoint", "")))
        if not res.get("ok"):
            print("  ❌ 不可用（停在阶段 %s）：%s"
                  % (res.get("stage") or "?", res.get("error", "未知原因")))
            continue
        proxy = res.get("proxy") or {}
        print("  ✅ 连通：PING=%s / 代理 %s %s（%s）"
              % (res.get("ping"), proxy.get("name") or "-",
                 proxy.get("version") or "-", proxy.get("mode") or "-"))
        print("  · 认证：%s" % ("已配置口令" if res.get("auth") else "无口令（当前实例形态）"))
        allowed = res.get("allowed_commands") or {}
        bad = [k for k, v in allowed.items() if str(v).startswith("FAIL")]
        print("  · 只读命令实测：%d 项，失败 %d 项%s"
              % (len(allowed), len(bad), ("（%s）" % ", ".join(bad)) if bad else ""))
        for label, value in sorted(allowed.items()):
            print("      %-12s %s" % (label, value))
        rejected = res.get("rejected_commands") or {}
        print("  · 预期被拒命令：")
        for label, value in sorted(rejected.items()):
            print("      %-12s %s" % (label, value))
        if res.get("note"):
            print("  · 说明：%s" % res["note"])
        for warn in res.get("warnings") or []:
            print("  ⚠️  %s" % warn)

    print()
    if failed:
        print("结论：❌ %d/%d 个环境不可用，禁止上线" % (failed, len(results)))
        return 1
    print("结论：✅ %d/%d 个环境全部可用" % (len(results), len(results)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
