#!/usr/bin/env python3
"""
麦动平衡 · 命令行入口
M-Balance / balance_cli.py

用法
----
# 离线演示（无需 MCP Token，用 examples/demo_mock_data.json）
python3 scripts/balance_cli.py --demo --activity "跑步 40min" --weight 68 --budget 35

# 在线调用真实麦当劳 MCP
export MCD_MCP_TOKEN="你的Token"
python3 scripts/balance_cli.py --activity "力量训练 50min" --weight 70 --budget 40 \
        --store-code <门店编码> --json

# 查看支持的运动与对应 MET
python3 scripts/balance_cli.py --list-activities
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from activity_model import ACTIVITY_TABLE, ActivityEntry, ActivityError, list_activities  # noqa: E402
from mcd_client import MCPError, get_client  # noqa: E402
from pipeline import render_report, run  # noqa: E402


def parse_activities(raw: str) -> list:
    """
    解析形如 "跑步 40min, 力量训练 30" 的运动串。
    支持逗号 / 分号 / 换行分隔。
    也支持 "跑步=40" 或直接 "跑步40分钟"。
    """
    entries = []
    parts = [p.strip() for p in raw.replace("；", ",").replace(";", ",").replace("\n", ",").split(",")]
    for part in parts:
        if not part:
            continue
        text, minutes = _split_one(part)
        entries.append(ActivityEntry(activity=text, minutes=minutes))
    return entries


def _split_one(part: str):
    """从 "跑步 40min" / "跑步=40" / "跑步40分钟" 中拆出名称与分钟数。"""
    import re
    p = part.replace("＝", "=")

    m = re.match(r"^(.+?)\s*[=:：]\s*(\d+(?:\.\d+)?)$", p)
    if m:
        return m.group(1).strip(), float(m.group(2))

    m = re.match(r"^(.+?)\s*(\d+(?:\.\d+)?)\s*(?:分钟|min|minutes|mins|m)?$", p)
    if m and m.group(1).strip():
        return m.group(1).strip(), float(m.group(2))

    raise ActivityError(f"无法解析运动描述：{part!r}。请用「跑步 40min」这样的格式。")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="balance-cli",
        description="麦动平衡 M-Balance · 用运动消耗换取麦当劳加餐额度，并最大化地把它花出去",
    )
    p.add_argument("--activity", "-a", required=False,
                   help='运动记录，如 "跑步 40min, 力量训练 30"')
    p.add_argument("--weight", "-w", type=float, default=65.0, help="体重 kg（默认 65）")
    p.add_argument("--budget", "-b", type=float, default=40.0, help="预算（元，默认 40）")
    p.add_argument("--credit-ratio", type=float, default=0.6,
                   help="消耗折算为额度的系数，默认 0.6（保守）")
    p.add_argument("--dining-mode", choices=["pickup", "delivery"], default="pickup",
                   help="取餐方式，默认到店取餐")
    p.add_argument("--store-code", default="", help="门店编码，留空则自动查询附近门店")
    p.add_argument("--address-code", default="", help="外送场景的收货地址编码")
    p.add_argument("--top-n", type=int, default=3, help="返回方案数，默认 3")
    p.add_argument("--demo", action="store_true", help="离线演示模式，使用本地 mock 数据")
    p.add_argument("--mock-path", default="", help="自定义 mock 数据路径")
    p.add_argument("--json", action="store_true", help="以 JSON 输出，便于程序消费")
    p.add_argument("--list-activities", action="store_true", help="列出支持的运动与 MET 值")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_activities:
        print("支持的运动（MET 值参考《Compendium of Physical Activities》）\n")
        print(f"{'ID':<18}{'MET':>6}  {'说明'}")
        print("-" * 62)
        for item in list_activities():
            print(f"{item['id']:<18}{item['met']:>6.1f}  {item['label']}")
        print("\n提示：口语说法也可直接识别，例如「撸铁」「遛狗」「打羽毛球」。")
        return 0

    if not args.activity:
        print("错误：请用 --activity 指定至少一项运动，例如：", file=sys.stderr)
        print('  python3 scripts/balance_cli.py --demo --activity "跑步 40min"', file=sys.stderr)
        return 2

    try:
        activities = parse_activities(args.activity)
    except ActivityError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        print(f"支持的运动：{', '.join(sorted(ACTIVITY_TABLE))}", file=sys.stderr)
        return 2

    try:
        client = get_client(offline=args.demo, mock_path=args.mock_path or None)
    except MCPError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        print("提示：加 --demo 参数可在无 Token 的情况下体验完整流程。", file=sys.stderr)
        return 3

    if args.demo:
        print("【离线演示模式】数据来自 examples/demo_mock_data.json，非麦当劳实时数据。\n", file=sys.stderr)
    else:
        try:
            client.initialize()
        except MCPError as exc:
            print(f"错误：{exc}\n建议：{exc.hint()}", file=sys.stderr)
            return 3

    try:
        session = run(
            client, activities,
            weight_kg=args.weight, budget=args.budget,
            store_code=args.store_code, address_code=args.address_code,
            dining_mode=args.dining_mode,
            credit_ratio=args.credit_ratio, top_n=args.top_n,
        )
    except MCPError as exc:
        print(f"错误：{exc}\n建议：{exc.hint()}", file=sys.stderr)
        return 3

    if args.json:
        payload = {
            "burn": {
                "total_burn_kcal": session.burn.total_burn_kcal if session.burn else 0,
                "credit_kcal": session.burn.credit_kcal if session.burn else 0,
                "breakdown": session.burn.breakdown if session.burn else [],
            },
            "plans": [p.to_dict() for p in session.plans],
            "warnings": session.warnings,
            "trace": session.trace,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(render_report(session, args.budget, args.dining_mode))

    if args.demo:
        print("\n" + "-" * 56)
        print("本次调用的 MCP Tool（离线模拟）：")
        for line in session.trace:
            print(f"  {line}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
