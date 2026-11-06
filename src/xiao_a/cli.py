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
    research = subparsers.add_parser("research", help="只读研究诊断；不结算、不改权重、不下单")
    research_commands = research.add_subparsers(dest="research_command", required=True)
    audit = research_commands.add_parser("audit", help="读取本地预测台账并输出分母、基线和时间外对照")
    audit.add_argument("--audit", type=Path, required=True, help="本地 prediction_audit.jsonl，只读")
    audit.add_argument("--as-of", dest="as_of", help="上海日期 YYYY-MM-DD；只纳入 recorded_at 不晚于该日的事件")
    audit.add_argument("--cohort-annotations", type=Path, help="可选的外部 cohort 注释，不回填台账")
    audit.add_argument("--engine", choices=("auto", "python", "duckdb"), default="auto")
    audit.add_argument("--output", type=Path, help="把 JSON 报告写到新文件；不能覆盖台账")
    backtest = research_commands.add_parser("backtest", help="用显式费率和日线规格跑既有A股约束回测")
    backtest.add_argument("--spec", type=Path, required=True, help="合成或本地日线规格 JSON")
    backtest.add_argument("--output", type=Path)
    candidates = research_commands.add_parser("candidates", help="对照预先固定的候选规则；不写台账、不给全局净收益")
    candidates.add_argument("--audit", type=Path, required=True)
    candidates.add_argument("--as-of", dest="as_of")
    candidates.add_argument("--cohort-annotations", type=Path)
    candidates.add_argument("--execution-spec", type=Path, help="可选的未来成交路径规格；审计快照不能代替它")
    candidates.add_argument("--output", type=Path)
    manifest = research_commands.add_parser("sample-manifest", help="开盘前冻结当日样本，不评估、不写审计")
    manifest.add_argument("--session-date", required=True)
    manifest.add_argument("--study-dir", type=Path)
    manifest.add_argument("--study-id")
    manifest.add_argument("--pool", type=Path, required=True)
    manifest.add_argument("--holdings", type=Path)
    manifest.add_argument("--universe", type=Path, required=True)
    manifest.add_argument("--random-count", type=int, default=18)
    manifest.add_argument("--seed")
    manifest.add_argument("--study-definition", choices=("research-study-v1", "research-study-v2"), default="research-study-v1")
    manifest.add_argument("--index-history", type=Path, help="沪深300价格指数快照；v2 必填，且不能晚于截止日")
    link = research_commands.add_parser("sample-link", help="把同一次评估返回的预测身份追加到侧车")
    link.add_argument("--study-dir", type=Path)
    link.add_argument("--study-id")
    link.add_argument("--session-date", required=True)
    link.add_argument("--response", type=Path, required=True)
    link.add_argument("--audit", type=Path, help="只读预测台账，用预测ID补齐指纹和快照哈希")
    shadow = research_commands.add_parser("shadow-freeze", help="用已保存的分数做前瞻影子，不重算特征")
    shadow.add_argument("--study-dir", type=Path, required=True)
    shadow.add_argument("--session-date", required=True)
    shadow.add_argument("--predictions", type=Path, required=True)
    shadow.add_argument("--lock-timeout-seconds", type=float, default=5)
    evidence = research_commands.add_parser("collect-evidence", help="按预算收集公开证据；缺字段就标不可用")
    evidence.add_argument("--study-dir", type=Path, required=True)
    evidence.add_argument("--session-date", required=True)
    evidence.add_argument("--budget-seconds", type=float, default=300)
    evidence.add_argument("--fees-path", type=Path)
    evidence.add_argument("--fetch", action="store_true")
    evidence.add_argument("--lock-timeout-seconds", type=float, default=5)
    daily = research_commands.add_parser("daily-status", help="读取当日侧车的覆盖和质量计数")
    daily.add_argument("--study-dir", type=Path, required=True)
    daily.add_argument("--session-date", required=True)
    weekly = research_commands.add_parser("weekly-review", help="只读侧车的周回顾；不改生产")
    weekly.add_argument("--study-dir", type=Path, required=True)
    weekly.add_argument("--review-date", required=True)
    weekly.add_argument("--predictions", type=Path)
    relative = research_commands.add_parser("relative-study", help="按已冻结的市场状态读取相对沪深300标签")
    relative.add_argument("--study-dir", type=Path, required=True)
    relative.add_argument("--session-date", required=True)
    relative.add_argument("--labels", type=Path, required=True)
    retrospective = research_commands.add_parser("retrospective-market-bucket", help="只打印回顾性状态，不回写清单")
    retrospective.add_argument("--index-history", type=Path, required=True)
    retrospective.add_argument("--cutoff", required=True)
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
    if args.command == "research":
        from xiao_a.research_diagnostics import run_research_command
        return run_research_command(args)
    from xiao_a.dashboard import run_dashboard
    return run_dashboard(port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())
