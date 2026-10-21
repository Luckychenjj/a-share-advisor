# Third-party notices

The bundled `src/xiao_a/web/interact.min.js` is interact.js 1.10.27, distributed under the MIT license. Its license is included in `src/xiao_a/web/INTERACT_LICENSE.txt`.

The Windows Flutter client uses `leanflutter/window_manager` 0.5.2 (MIT), `r-mzy47/candlesticks` 3.0.1 (BSD-3-Clause), and `AscenZhong/kline_chart` 1.0.3 (MIT). Original license text is preserved in `client/licenses/`. These packages supply window controls and interactive OHLCV charts; they do not replace the advisor's analysis or account core. Flutter's generated license registry is accessible from the client's settings.

`bdlukaa/fluent_ui` and `cjjapan/financial_chart` were reviewed for interface and chart options but are not bundled. The client uses Flutter Material controls with a desktop layout.

This project was designed after reviewing these open-source projects:

- `simonlin1212/a-stock-data`, Apache License 2.0
- `ZhuLinsen/daily_stock_analysis`, MIT License
- `TauricResearch/TradingAgents`, Apache License 2.0
- `fleetinglife/levistock`, MIT License (reviewed for the CLS market-emotion endpoint and field mapping)
- `er6y/stockquant`, license as published upstream (reviewed for the Eastmoney company-announcement endpoint)
- `2529huang/astrbot_plugin_fund_analyzer`, license as published upstream (reviewed for the Eastmoney daily fund-flow endpoint and fallback pattern)
- `zhangxiangliang/stock-api`, license as published upstream (reviewed for the Tencent Smartbox stock-search endpoint)

No repository was copied wholesale. The CLS adapter is a dependency-free reimplementation of the public endpoint contract, with independent normalization and error handling. If individual upstream modules are added later, their original copyright notices and license text must be preserved alongside the copied or modified source.
