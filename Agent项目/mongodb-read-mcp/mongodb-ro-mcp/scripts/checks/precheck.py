#!/usr/bin/env python3
"""部署前预检（硬门禁）：三环境连通 / 版本 / 账号 / 只读权限。

跑不过 → deploy.sh 直接中止，服务不启动。

用法（在安装目录下，venv 解释器）：
    set -a; . /etc/mongodb-ro-mcp/mongodb-ro-mcp.env; set +a
    PYTHONPATH=src .venv/bin/python scripts/checks/precheck.py --config config/environments.yaml
    PYTHONPATH=src .venv/bin/python scripts/checks/precheck.py --env test --json

退出码：0 全部 OK；1 有环境不可用；2 配置/依赖问题。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # <包根>/scripts/checks/precheck.py
sys.path.insert(0, str(ROOT / "src"))

# 只读账号不应带的写类角色（命中即告警；admin 库的只读监控角色不在其中）
WRITE_ROLES = {
    "root", "readWrite", "readWriteAnyDatabase", "dbOwner", "dbAdmin",
    "dbAdminAnyDatabase", "userAdmin", "userAdminAnyDatabase",
    "clusterAdmin", "clusterManager", "hostManager", "restore",
}


def check_env(settings, name: str) -> dict:
    """检查单个环境，返回结果字典（不抛异常，全部落成事实）。

    每个阶段都会写 `result["stage"]`，失败时用它定位是"连不上 / 认证不过 /
    权限不足 / 取列表崩了"，避免只看到一句 TypeError 还要猜。
    """
    from pymongo import MongoClient

    conf = settings.env(name)
    result = {
        "env": name,
        "env_actual": conf.actual,
        "display_name": conf.display_name,
        "uri_hosts": conf.uri.split("@")[-1].split("/")[0],   # 不含账号口令
        "ok": False,
        "stage": "init",
    }

    def mark(stage: str) -> None:
        result["stage"] = stage

    client = None
    try:
        mark("connect")
        client = MongoClient(conf.uri, serverSelectionTimeoutMS=conf.connect_timeout_ms,
                             appname="mongodb-ro-mcp-precheck")
        mark("isMaster")
        hello = client.admin.command("isMaster")
        mark("serverInfo")
        info = client.server_info()
        result.update(
            reachable=True,
            version=info.get("version"),
            setName=hello.get("setName"),
            primary=hello.get("primary"),
            maxWireVersion=hello.get("maxWireVersion"),
            ismaster=hello.get("ismaster"),
        )
        if int(hello.get("maxWireVersion") or 0) < 7:
            result.setdefault("warnings", []).append(
                "wire version %s 低于 7，pymongo 4.6 仍可连，但请确认不是连错实例"
                % hello.get("maxWireVersion")
            )

        # list_databases() 在 pymongo 4.x 返回 CommandCursor（惰性游标），
        # 必须 list() 物化后才能 len()/多次遍历 —— 这里踩过一次 TypeError。
        mark("listDatabases")
        dbs = list(client.list_databases())
        result["database_count"] = len(dbs)
        result["databases"] = [d.get("name") for d in dbs][:20]

        # 读权限抽查：collStats 需要一个真实存在的库（跳过空实例）
        mark("readProbe")
        readable = None
        for item in dbs:
            dname = item.get("name")
            if dname in ("admin", "local", "config"):
                continue
            try:
                names = client[dname].list_collection_names()
                if names:
                    client[dname].command("collStats", names[0])   # 真正读一次元数据
                    readable = dname
                    break
            except Exception:                       # noqa: BLE001
                continue
        result["read_probe"] = readable or "未抽查（无业务库或无权限）"

        # 账号角色：能读到就核对是不是真的只读，读不到只告警不阻断
        mark("connectionStatus")
        try:
            status = client.admin.command("connectionStatus")
            users = (status.get("authInfo") or {}).get("authenticatedUsers") or []
            roles = sorted({str(r.get("role")) for u in users for r in (u.get("roles") or [])})
            result["authenticated_users"] = [u.get("user") for u in users]
            result["roles"] = roles
            bad = sorted(set(roles) & WRITE_ROLES)
            if bad:
                result.setdefault("warnings", []).append(
                    "账号带写类角色 %s —— 只读服务不应使用可写账号，请换只读账号" % bad
                )
        except Exception as exc:                     # noqa: BLE001
            result["roles"] = "未取到（%s）" % str(exc).split("\n")[0][:80]

        # 巡检三工具所需权限（clusterMonitor）：拿不到只告警
        for probe in ("serverStatus", "replSetGetStatus"):
            try:
                client.admin.command(probe)
                result[probe] = "ok"
            except Exception as exc:                 # noqa: BLE001
                result[probe] = "denied"
                result.setdefault("warnings", []).append(
                    "%s 权限不足：巡检工具（server_status / replica_status）不可用；"
                    "需要 admin 库 clusterMonitor 只读监控角色（%s）"
                    % (probe, str(exc).split("\n")[0][:60])
                )

        result["ok"] = True
        result["stage"] = "ok"
    except Exception as exc:                         # noqa: BLE001
        result["error"] = "[阶段 %s] %s: %s" % (
            result.get("stage", "?"), type(exc).__name__,
            str(exc).split("\n")[0][:200])
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:                        # noqa: BLE001
                pass
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="mongodb-ro-mcp 部署前预检")
    ap.add_argument("--config", default=None, help="environments.yaml 路径")
    ap.add_argument("--env", default="", help="只检查某个环境（留空=全部）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（便于进验收记录）")
    args = ap.parse_args()

    try:
        from mongodb_ro_mcp.config import ConfigError, load_settings
        settings = load_settings(args.config)
    except Exception as exc:                         # noqa: BLE001
        print("[precheck][FAIL] 配置加载失败: %s" % exc, file=sys.stderr)
        return 2

    targets = [args.env] if args.env else settings.names()
    results = []
    failed = 0
    for name in targets:
        try:
            res = check_env(settings, name)
        except Exception as exc:                     # noqa: BLE001
            res = {"env": name, "ok": False, "error": str(exc)[:200]}
        results.append(res)
        if not res.get("ok"):
            failed += 1

    if args.json:
        print(json.dumps({"ok": failed == 0, "results": results},
                         ensure_ascii=False, indent=1))
        return 0 if failed == 0 else 1

    print("=" * 78)
    print("mongodb-ro-mcp 部署前预检（目标实例 MongoDB 4.0 / wire version 7）")
    print("=" * 78)
    for res in results:
        name = res["env"]
        print()
        print("[%s] → 实际环境 %s（%s）"
              % (name, res.get("env_actual", name), res.get("display_name", "")))
        if not res.get("ok"):
            print("  ❌ 不可用（停在阶段 %s）：%s"
                  % (res.get("stage") or "?", res.get("error", "未知原因")))
            continue
        print("  ✅ 连通：版本 %s / 副本集 %s / 主节点 %s / wire %s"
              % (res.get("version"), res.get("setName") or "-",
                 res.get("primary") or "-", res.get("maxWireVersion")))
        print("  ✅ 可见库 %d 个：%s" % (res.get("database_count", 0),
                                       ", ".join(res.get("databases") or []) or "-"))
        print("  · 读权限抽查：%s" % res.get("read_probe"))
        print("  · 账号：%s" % (res.get("authenticated_users") or "未取到"))
        print("  · 角色：%s" % (res.get("roles") or "未取到"))
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
