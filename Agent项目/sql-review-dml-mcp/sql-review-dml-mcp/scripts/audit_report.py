"""审计日志小统计：不上 Grafana 也能看趋势。

用法：
    PYTHONPATH=src python3 scripts/audit_report.py                    # 默认最近 7 天
    PYTHONPATH=src python3 scripts/audit_report.py --since 30d
    PYTHONPATH=src python3 scripts/audit_report.py --logs /opt/mcpservers/sql-review-ddl-mcp/logs
    PYTHONPATH=src python3 scripts/audit_report.py --since 30d --json

只读，不改任何文件。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from sql_review_dml.audit import iter_records  # noqa: E402  (项目内模块)


def parse_since(text: str) -> int:
    t = (text or "").strip().lower().rstrip("d")
    if t in ("today", "1"):
        return 1
    try:
        return max(1, int(t))
    except ValueError:
        return 7


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="audit-report", description="审计日志统计")
    ap.add_argument("--logs", default=os.path.join(ROOT, "logs"), help="审计日志目录")
    ap.add_argument("--since", default="7d", help="最近 N 天，如 7d / 30d / today")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    days = parse_since(args.since)
    recs = list(iter_records(args.logs, days=days))
    if not recs:
        print(f"[INFO] {args.logs} 里最近 {days} 天没有审计记录")
        return 0

    events = Counter(r.get("event") or "-" for r in recs)
    outcomes = Counter(r.get("outcome") or "-" for r in recs)
    tools = Counter(r.get("tool") or "-" for r in recs if r.get("event") == "tool_call")
    codes = Counter((r.get("reject") or {}).get("code") for r in recs if r.get("reject"))
    texts = {(r.get("reject") or {}).get("code"): (r.get("reject") or {}).get("text")
             for r in recs if r.get("reject")}
    ips = Counter(r.get("client_ip") or "-" for r in recs)
    stds = Counter((r.get("standard") or {}).get("version") or "-"
                   for r in recs if r.get("standard"))
    rejected = [r for r in recs
                if r.get("outcome") in ("denied", "invalid_request", "rejected", "error")]
    passed = [r for r in recs if r.get("outcome") == "pass"]
    reviewed = [r for r in recs if r.get("event") == "tool_call" and r.get("outcome") in
                ("pass", "fix_required", "need_more_info", "rejected")]

    data = {
        "window_days": days,
        "logs": args.logs,
        "total": len(recs),
        "events": dict(events),
        "outcomes": dict(outcomes),
        "tools": dict(tools.most_common()),
        "reject_codes": dict(codes.most_common()),
        "reject_text": {k: texts.get(k) for k in codes},
        "client_ips": dict(ips.most_common(10)),
        "standard_versions": dict(stds),
        "review_calls": len(reviewed),
        "pass_rate": round(len(passed) / len(reviewed), 3) if reviewed else None,
        "rejection_total": len(rejected),
    }

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    def table(title, counter, note=""):
        items = list(counter.items()) if hasattr(counter, "items") else list(counter)
        print(f"\n{title}{('（' + note + '）') if note else ''}")
        if not items:
            print("  - 无")
            return
        width = max(len(str(k)) for k, _ in items)
        for k, v in items:
            print(f"  {str(k):<{width}}  {v}")

    print("=" * 72)
    print(f"审计统计：最近 {days} 天　目录 {args.logs}")
    print("=" * 72)
    print(f"记录总数 {len(recs)}　审核调用 {len(reviewed)}　"
          f"通过率 {data['pass_rate'] if data['pass_rate'] is not None else '-'}"
          f"　被拒/异常 {len(rejected)}")
    table("按事件类型", events)
    table("按结论（outcome）", outcomes)
    table("按工具调用量", tools.most_common())
    table("拒绝原因 TOP", codes.most_common(), "可直接统计，不用读自由文本")
    if codes:
        print("\n拒绝原因原文（每个码对应的中文说明）")
        for code, cnt in codes.most_common():
            print(f"  [{cnt:>4}] {code}: {texts.get(code)}")
    table("来源 IP TOP10", ips.most_common(10))
    table("规范版本分布", stds)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
