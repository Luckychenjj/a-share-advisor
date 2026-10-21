from __future__ import annotations

import argparse
import json
from pathlib import Path

from xiao_a.auto import AutoAnalyzer
from xiao_a.data.router import QuoteRouter
from xiao_a.data.tencent import TencentQuoteProvider
from xiao_a.service import evaluate_payload, jsonable


def _print(value) -> None:
    print(json.dumps(jsonable(value), ensure_ascii=False, indent=2))


def _quote(symbol: str) -> int:
    result = QuoteRouter([TencentQuoteProvider()]).fetch_quote(symbol)
    _print(result)
    return 0 if result.quote is not None else 2


def _evaluate(path: Path) -> int:
    raw = json.loads(path.read_text(encoding="utf-8"))
    _print(evaluate_payload(raw))
    return 0


def _auto(symbol: str) -> int:
    _print(AutoAnalyzer().analyze(symbol))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="小A顾问：可审计的A股决策核心")
    subparsers = parser.add_subparsers(dest="command", required=True)
    quote = subparsers.add_parser("quote", help="读取带来源与质量状态的实时行情")
    quote.add_argument("symbol", help="例如 600519、SZ.000001、920982")
    evaluate = subparsers.add_parser("evaluate", help="评估一个JSON市场快照")
    evaluate.add_argument("snapshot", type=Path)
    auto = subparsers.add_parser("auto", help="自动读取行情、日线和基准后分析")
    auto.add_argument("symbol", help="例如 600519、SZ.000001、920982")
    dashboard = subparsers.add_parser("dashboard", help="打开本地可视化界面")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--no-browser", action="store_true")
    cloud = subparsers.add_parser("cloud", help="启动需要认证的分析服务")
    cloud.add_argument("--host", default="127.0.0.1")
    cloud.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    if args.command == "quote":
        return _quote(args.symbol)
    if args.command == "evaluate":
        return _evaluate(args.snapshot)
    if args.command == "auto":
        return _auto(args.symbol)
    if args.command == "cloud":
        from xiao_a.cloud import run_cloud
        return run_cloud(host=args.host, port=args.port)
    from xiao_a.dashboard import run_dashboard
    return run_dashboard(port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())
