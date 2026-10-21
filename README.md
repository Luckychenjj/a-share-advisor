# 小A顾问核心层 (Xiao A Advisor)

> **免责声明 / Disclaimer**
>
> 本项目仅供研究学习，不构成任何投资建议。程序输出的结论、评分和“置信度”均为规则计算结果，不代表收益承诺；据此操作的盈亏风险由使用者自行承担。行情数据来自第三方公开接口，可能延迟、缺失或出错，请以交易所和券商为准。
>
> This project is for research and educational purposes only and does **not** constitute investment advice. All outputs, scores and "confidence" values are rule-based calculations, not promises of return. Market data comes from third-party public endpoints and may be delayed, incomplete or wrong. You are solely responsible for any trading decisions and their outcomes.

混合架构开发与当前电脑服务器测试见 [HYBRID_ARCHITECTURE.md](HYBRID_ARCHITECTURE.md)。当前已加入认证分析服务、客户端服务器路由与过期缓存保护；独立应用窗口、安卓客户端与账户跨设备同步仍在后续阶段。

这是“小A顾问”的第一版可运行核心。设计原则不是维护某一套永远正确的理论，而是：

1. 每条数据都带来源、时间和质量状态；
2. 每种理论都是可替换策略，必须输出可验证证据；
3. 规则分与统计概率分开；未经前瞻验证和校准不显示为成功概率；
4. 风险规则独立于大模型，能够否决或降低建议；
5. 大模型以后只做反证、信息补充和解释，不直接绕过风险规则；
6. “历史建议验证”和“组合资金回测”分开，避免把命中率当成实盘收益。

## 当前已经实现

- 沪深京股票、ETF和显式指数代码标准化；
- 顶部证券搜索支持中文名称、六码代码和拼音首字母，候选结果可直接切换首行实时行情；
- 腾讯实时行情适配器，输出来源、抓取时间和陈旧状态；
- 腾讯前复权历史日线与沪深300基准接入，失败时自动切换东方财富日线；
- 财联社市场情绪快照接入，自动计算板块热度、市场广度、市场情绪和炸板风险；失败时回退东方财富板块广度；
- 个股公告、东方财富个股资金流和财联社当日涨停池明细接入；资金流支持 `push2his`/`push2` 重试与切换，缺失时明确标记为不可用；
- 自动生成日/周/月趋势、相对强度、周期位置、龙头强度和流动性特征；
- 多数据源显式优先级、失败切换和错误保留；
- 模型1“周期×趋势”和模型2“龙头×情绪”示例策略；
- 模型3“多因子评分”和模型4“市场状态”策略：趋势、动量、估值、量价、流动性、资金流、市场广度与情绪综合判断；
- 确定性的简化缠论结构策略：分型、笔、中枢和MACD背驰；
- 任意自定义策略的统一接口；
- 确定性策略集成；可接收显式历史表现统计，但当前线上运行不会自动学习或调参；
- 策略冲突惩罚，分歧越大，规则符合分越低；
- 数据陈旧、ST、停牌、仓位集中、回撤、流动性、涨跌停等硬风控；
- 可编辑持仓组合：实时市值、权重、成本盈亏、最大单股仓位和组合级动作覆盖；
- 持仓、成本和现金保存在浏览器本机，行情网站只接收公开股票代码；
- 只读取当时已经结算结果的 point-in-time 决策日志；
- A股日线回测骨架：次日开盘成交、T+1、100股整手、涨跌停封死、手续费、印花税、过户费和滑点；
- 单元测试覆盖关键边界。

## 预测评估、置信度校准与审计

桌面版新增“评估”页面和本地追加式预测台账。当前模型输出的 `confidence` 是规则符合分而非成功概率；详见 [预测评估与受控改进说明](docs/PREDICTION_AUDIT.md)。报告只统计预先定义且到期的前瞻市场事件，策略收益与账户级风险价值在未有可靠成交模拟前会明确显示“尚未验证”。

## 理论如何被替换

所有策略只需实现 `Strategy.evaluate(snapshot)`。模型1、模型2和缠论没有特殊权限，可以被新增、降权、停用或替换。`AdaptiveEnsemble` 支持接收外部历史统计作权重输入，但当前线上自动分析未接入前瞻评估数据或自动调权；策略分歧只调整规则符合分。

## 运行测试

```powershell
cd xiao-a-advisor
python -m unittest discover -s tests -v
```

## 安装

需要 Python 3.11 或更高版本。

```bash
git clone <本仓库地址> xiao-a-advisor
cd xiao-a-advisor
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -e .                # 可选备用数据源：pip install -e ".[fallback-data]"
xiao-a dashboard                # 或 python -m xiao_a.cli dashboard
```

然后打开 `http://127.0.0.1:8765/`。

## 个人数据与隐私

持仓、现金、成交记录和预测台账都只保存在**你自己电脑的本地文件**里，不属于源码，也不应提交到 Git：

- 默认持仓文件为项目下的 `data/portfolio.json`（已被 `.gitignore` 排除）；其他运行数据默认在 `%LOCALAPPDATA%\XiaoAAdvisor\`（Windows）或 `~/.xiao-a-advisor/`；
- `XIAO_A_DATA_DIR`：数据目录；`XIAO_A_PORTFOLIO_FILE`：持仓文件；`XIAO_A_AUDIT_FILE`：预测台账（JSONL）；
- 仓库自带的 `.gitignore` 已排除 `userdata/`、`data/`、`portfolio*.json`、`*.jsonl`、日志和缓存。提交前请再用 `git status` 确认没有个人数据。

测试和网页默认示例中的股票代码与数量仅为演示用的公开代码，与任何真实账户无关。

## 更换电脑继续使用

默认模块布局随网页源码保存在仓库中。新电脑首次打开时会加载仓库默认布局；已有浏览器继续保留本机调整及本机保存的默认布局。“设为默认布局”保存在当前浏览器。持仓等个人数据请自行备份上面所说的本地数据文件，不要放进仓库。

## 直接运行

### 可视化界面（推荐）

Windows 可以直接双击 `start-dashboard.cmd`。浏览器会自动打开 `http://127.0.0.1:8765`，关闭命令窗口或按 `Ctrl+C` 即可停止。

行情模块默认不继承终端中的 `HTTP_PROXY/HTTPS_PROXY`，避免无效开发代理阻断行情。确实需要代理时，请单独设置 `XIAOA_PROXY`。行情请求只包含公开股票代码，不上传账户持仓或交易记录。

也可以在 PowerShell 中运行：

```powershell
cd xiao-a-advisor
$env:PYTHONPATH = "src"
python -m xiao_a.cli dashboard
```

顶部搜索框可输入中文名称、六码代码或拼音首字母；从候选项中选择（或按 Enter）即可切换首行实时行情，并触发该标的的自动分析。下方“我的持仓”支持维护股票、数量、成本和现金，并展示组合市值、仓位权重、盈亏、模型动作以及经过集中度约束后的组合动作。

### 命令行

在源码目录中可先用 `PYTHONPATH=src` 运行；安装为包后可直接使用 `xiao-a`：

```powershell
$env:PYTHONPATH = "src"
python -m xiao_a.cli quote 600519
python -m xiao_a.cli auto 600519
python -m xiao_a.cli evaluate path/to/snapshot.json
```

`quote` 返回实时行情；`auto` 自动读取行情、约360根日线和沪深300基准并完成分析；`evaluate` 评估自定义JSON快照。所有结果保留数据来源、规则权重、原始判断、风险覆盖后的最终动作、规则符合分、警告和否决原因。缺少输入的策略会弃权。

## 重要边界

- 当前已经接入实时行情、历史日线、财联社市场情绪快照、公告、个股资金流和当日涨停池明细；任一补充源失败时会保留警告并让对应特征弃权，不会用猜测值填充。
- `ChanStructureStrategy` 是可重复计算的保守近似，不宣称代表任何唯一或“正统”缠论定义。
- 回测器已经处理基本A股成交约束，但真实接入前仍需补历史成分股、复权、除权除息、公告发布时间和更精细的盘口成交模型。
- `FeeSchedule` 的默认值只是可配置示例，实测时应按券商和回测期间规则设置。

## 来源与许可证说明

架构吸收了以下开源项目的优点，但本目录代码是为“小A顾问”重新组织的独立实现：

- `simonlin1212/a-stock-data`（Apache-2.0）：A股代码路由、数据质量契约、腾讯行情和东财限流思路；
- `ZhuLinsen/daily_stock_analysis`（MIT）：Provider、确定性风险覆盖、阶段护栏和信号后验验证思路；
- `TauricResearch/TradingAgents`（Apache-2.0）：状态编排、point-in-time决策记忆和检查点思路。

详见 `THIRD_PARTY_NOTICES.md`。

## 许可证 / License

本项目代码以 [MIT License](LICENSE) 发布。第三方组件（如 `src/xiao_a/web/interact.min.js`）保留其各自许可证，见 `THIRD_PARTY_NOTICES.md`。

Released under the [MIT License](LICENSE). Bundled third-party components keep their own licenses; see `THIRD_PARTY_NOTICES.md`.
