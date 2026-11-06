# 只读研究诊断与 A 股约束回测

这条命令只读取本地文件并打印 JSON。它不结算预测、不追加审计事件、不改写历史，也不修改 `decision.py` / `ensemble.py` 的生产权重。候选规则不会自动晋升。`calibrated_probability_available_for_production` 固定为 `false`。审计诊断本身不读账户费率。只有单独的执行规格明确声明费率的历史适用性时，才会在本机读取用户给出的费率文件，并且仍然不形成全局净收益。

规则符合分不是概率。方向命中率不是买入事件率，也不是扣费后的净收益。审计标签的 `execution_simulation` 仍是 `not_applied`。私人台账没有随本仓库提供；下面的命令是在你自己的机器上复核分母用的，仓库测试只用合成样本。

## 本地只读复核

在包含本仓库代码的目录执行。把审计路径换成你自己的 `XIAO_A_AUDIT_FILE`，不要把该文件提交或上传。

```bash
PYTHONPATH=src python -m xiao_a.cli research audit \
  --audit "$XIAO_A_AUDIT_FILE" \
  --as-of 2026-11-06 \
  --engine python
```

`--as-of` 使用上海日历日。带时区的 `recorded_at` 先换到 Asia/Shanghai；没有时区的时间戳保留字符串里的日期。`recorded_at` 只决定这份文件里当时看得到哪些事件。预测自己的 `as_of` 和 `information_cutoff` 也都必须不晚于报告日；只是事件写得早、决策日更晚的记录不进入评分。五个交易日窗口的退出日晚于报告日时，标签也不使用，即使结算事件的写入时间更早。文件比旧报告更长，只说明后来又追加了事件；只有整份文件的哈希链失败才暂停评分。缺少 `recorded_at` 的事件不能证明落在截止日内，因此不进入切片。

可选的 cohort 注释是另一个文件，不会写回台账。没有注释、也没有完整 `research_cohort` 的旧记录保持 `legacy_unspecified`，不会被回填成持仓或股票池。

```bash
PYTHONPATH=src python -m xiao_a.cli research audit \
  --audit "$XIAO_A_AUDIT_FILE" \
  --cohort-annotations ./cohort-annotations.json \
  --engine python \
  --output ./research-report.json
```

注释格式是对象数组，或每行一个对象的 `.jsonl`。每条需要 `prediction_id`、`cohort_bucket`、`cohort_provenance`。同一 ID 的注释冲突时记为歧义，不猜一个来源。`--output` 不能指向审计文件，也不能写成 `.jsonl`。

报告里的 `denominator_ladder` 按这个顺序解释行数：

| 名称 | 含义 |
| --- | --- |
| `slice_prediction_created` | 切片内的新建预测条数。`prediction_repeated` 不是新预测 |
| `formal_mode` | `mode` 明确为 formal |
| `legacy_mode_unspecified` | 没有 mode 的旧记录，不回填进正式分母 |
| `excluded_duplicate_symbol_as_of_date` | 同一标的、同一 `as_of` 上海日期只保留 `sequence` 最早的一条 |
| `formal_research_rows` | 去重后的正式研究行 |
| `research_resolvable_v1` | 能对上 `forward_positive_return_5_sessions_v1` 的行：信息截止日之后的下一交易日开盘，到第五个交易日收盘，收益大于零 |
| `direction_nonzero_rule_score` | 与生产报告相同的方向口径：规则符合分绝对值大于 `1e-12` |

`as_of` 与 `information_cutoff` 落在同一天不会被剔除，并单独计数。标签和收益符号矛盾、目标 ID 对不上、或同一预测有多条结算时，记为歧义或未匹配，不进入方向分母。

方向对照使用同一批非零规则分：模型命中数、常数看空命中数、常数看多和多数类。下跌样本占多数时，常数看空的命中率可以更高；这不是交易优势，也不是净收益。`model_action`、`account_action` 和 `position_aware_action` 的买入事件率分开列出。

子模型表同时给出浮点分数条数和方向样本条数。接近零的分数会离开方向样本，缺失分数不会被当成零。缠论分数少于集成分数时，先看这三项，再决定是不是另一套标签。

非重叠日期块按信息截止日贪心选取，退出日用结算里的第五个交易日，不是日历加五天。下一块的截止日必须晚于上一块退出日；截止日正好等于退出日仍然算重叠。接近零的规则分不进入方向分母，也不拿来凑块数。

生产监测要先有 8 个参考块，再加两个各 8 块的观察窗口，合计 24 块，才会开始方向比较。研究诊断使用同一个门槛。只有 8 块时仍然是证据不足，也不给重采样区间。满 24 块时的区间也只是描述，不能消除金融时序相关，更不能当成预测改进。样本来自额外评估、持仓和股票池的混合，不是随机样本。

时间外比较把信息截止日的后 30% 当作测试段。参照段里退出日不严格早于测试起点的行会被五日窗口清洗掉。比较的是预先固定的符号规则和两个常数规则，以及台账里已经出现的子模型符号。这里不搜索阈值，不挑选胜者，也不宣称预测改进。

## 约束回测

成交模拟仍以 `AShareBacktester` 为准：信号在下一根日线开盘成交，买入当日不可卖，100 股整手，封死涨跌停和停牌拒绝成交，禁止卖空。滑点买在开盘价上加、卖在开盘价上减。费用必须写在规格里，默认示例费率不会被当成你的账户费率。

VectorBT 和 QuantStats 没有接入。它们不能替代上述成交约束。DuckDB 只在显式 `--engine duckdb`，或 `--engine auto` 且文件达到 1 MiB 时，于内存中核对事件类型计数；Python 仍是权威结果，不会写出 sidecar 数据库。`start.sh` 和 `requirements-lock.txt` 不安装这个可选包。

合成规格：

```bash
PYTHONPATH=src python -m xiao_a.cli research backtest \
  --spec tests/fixtures/research_backtest_spec.json
```

自己的日线规格放在仓库外。规格里出现 `account_fees_path`、`assets_ledger` 或 `portfolio_path` 会被拒绝。日线必须已经按交易日递增。这个结果是单标的研究模拟，不能回写成审计结算，也不能从方向命中率推出来。

## 预先固定的候选对照

两个候选在看结果之前就固定，使用生产代码里已有的动作门槛：分数大于等于 0.25 为买入，小于等于 -0.20 为减仓，小于等于 -0.55 为卖出。不在结果上搜索新门槛。

- `ensemble_baseline` 只用保存的 `rule_score`。
- `multi_factor_alone` 只用保存的 `submodel_scores.multi_factor`。缺失或接近零就弃权，不用成分重新计算。
- `consensus_only` 要求至少两个非零参与者同号。缺失和接近零的分数不投票。买入仍要最弱的参与者达到 0.25。意见不一致就弃权。

常数看多、常数看空和不发信号在同一批标的和日期上对照。方向命中率和买入事件率分母分开。卖出信号不会开空。

状态分组只读 `strategy_signals.market_regime.metadata.regime`，以及 `available_inputs` 和 `invalidation_active`。没有这份元数据就是缺失，不用后来的价格或分数反推。买点分组是探索性描述，不挑选胜者。多因子之所以被拿来对照，是因为这份研究已经知道它；旧切片不是前瞻确认。配置指纹只覆盖规则文本，不覆盖本次文件里的截止日期。

全局可执行净收益保持不可用。结算端点价、预测前的快照日线、把后来快照拼出的覆盖率、预测当时的涨跌停价，以及未经核实的停牌/ST/涨跌停标志，都不能当成成交路径，也不能把标签收益当成净收益。

```bash
PYTHONPATH=src python -m xiao_a.cli research candidates \
  --audit "$XIAO_A_AUDIT_FILE" \
  --as-of 2026-11-06
```

不要把审计文件、费率文件或日线规格提交到仓库。

## 每日前瞻样本

这些命令只写研究目录里的侧车，不调用自动评估，不结算，不改审计哈希链，也不创建定时任务。固定池来自调用方给出的股票列表文件，程序不把数量写成 35。全市场名单没有已接入的公开完整列表适配器；`--universe` 必须是本地快照。快照过期会标明 `stale_list_reused`，并且不声称名单完整或随机抽样无偏。ST、退市、零价和停牌只在快照里明确知道时才排除。

```bash
PYTHONPATH=src python -m xiao_a.cli research sample-manifest \
  --session-date 2026-11-07 \
  --study-dir ./study \
  --pool watchlists/inst-counter-2026-10-21/symbols.txt \
  --holdings ./portfolio.json \
  --universe ./a-share-snapshot.json \
  --random-count 18
```

退出码 0 表示清单已创建或内容相同；退出码 2 表示清单已冻结却被换成另一套成员。同一天再跑不会重抽随机样本。

评估仍由现有调用方逐只执行。自动评估响应通常只有预测 ID。链接命令可以用 `--audit` 只读台账里同一 ID 的 `prediction_created`，补上规范指纹和 `input_sha256`。代码或截止日期对不上就是 `ambiguous`，不会按代码加日期去猜。同一 ID 如果指纹或快照引用变了，不能当作原链接未变化。同一代码同一天有两个预测 ID 时，影子分数两边都不计，避免悄悄二选一。

研究目录不要放在可打包的 `study/` 下面。默认是 `/workspace/xiao-a-research/<study-id>`，仓库只忽略这个具体路径。

证据采集会跨日期继续未完成的五日窗口，并把原始返回的 OHLC 留在版本化快照里。请求参数 `none` 不会被写成“未复权”。单次请求超过剩余预算时，子进程会被终止。外层还可以用系统 `timeout`：

```bash
timeout 300 python -m xiao_a.cli research collect-evidence \
  --study-id demo --session-date 2026-11-07 --budget-seconds 240 --fetch
```

```bash
PYTHONPATH=src python -m xiao_a.cli research sample-link \
  --study-id demo \
  --session-date 2026-11-07 \
  --response ./one-eval.json \
  --audit "$XIAO_A_AUDIT_FILE"
```

影子命令只读清单、链接和已保存的预测记录，不重新拉行情、不重算因子。配置指纹、代码版本和 `prospective_shadow_start` 第一次写入后不变。分数侧车不含结算标签。映射不确定就标 `ambiguous`。

```bash
PYTHONPATH=src python -m xiao_a.cli research shadow-freeze \
  --study-dir ./study \
  --session-date 2026-11-07 \
  --predictions ./saved-predictions.jsonl
```

证据命令默认不访问网络。加上 `--fetch` 才调用现有公开日线适配器，并且只为清单内的代码请求有限根数。适配器给出的涨跌停比例、零成交量停牌推断和前复权价格都不会被当成交易所限价、核实停牌或官方复权因子。部分结果会留下，但 `usable_for_executable_backtest` 仍是 0。`--budget-seconds` 默认 300，在每只代码开始前检查；到点就写 `stopped_budget` 并正常结束，下次从缓存继续。锁超时退出码是 2。

```bash
PYTHONPATH=src python -m xiao_a.cli research collect-evidence \
  --study-dir ./study \
  --session-date 2026-11-07 \
  --budget-seconds 300 \
  --fees-path ./account_fees.json \
  --fetch
```

费率文件只在本机读取。研究输出不复制费率数字，也不回写账户。`historically_applicable` 不是 true 时，费率不能用于可执行回测。

```bash
PYTHONPATH=src python -m xiao_a.cli research daily-status \
  --study-dir ./study --session-date 2026-11-07
PYTHONPATH=src python -m xiao_a.cli research weekly-review \
  --study-dir ./study --review-date 2026-11-14 \
  --predictions ./saved-predictions.jsonl
```

周回顾在非重叠日期块少于 24 时不给方向命中率。概率不可用。单因子假设保持未应用。不要把 `study/` 提交进仓库。

## 相对沪深300与市场状态

这两项只属于 `research-study-v2`。已有的 v1 清单不会被改写。过去的交易日也不能补成前瞻研究。规则在抽样前固定：沪深300收盘高于包含当日的20日简单均线，并且5日收盘收益大于0，为强；两者都严格小于为弱；其余已核实的情况为震荡，相等算震荡。指数历史不足、来源未核实，或快照里有截止日之后的K线，就拒绝或记为不可用，不会当成震荡，也不从 `market_regime` 分数推断。

相对目标仍是下一交易日开盘到第五个交易日收盘，再用同一日期的沪深300价格指数开盘和收盘相减。股票端必须有已核实的公司行为可比收益。指数必须是 `SH.000300` 价格指数，不能用 ETF 代替。缺任一端点则相对结果不可用。超额收益不是个股阿尔法证明。样本外多数类只看更早的参照段；全样本多数类只是命名过的描述上限。每个市场状态自己计算24块门槛。

```bash
PYTHONPATH=src python -m xiao_a.cli research sample-manifest \
  --session-date 2026-11-07 \
  --study-dir ./study-v2 \
  --pool ./pool.txt \
  --universe ./a-share-snapshot.json \
  --study-definition research-study-v2 \
  --index-history ./csi300-price-index.json
```

指数快照如果来源不完整，命令以退出码 2 拒绝，不会生成清单。回顾性状态只打印，不写回清单：

```bash
PYTHONPATH=src python -m xiao_a.cli research retrospective-market-bucket \
  --index-history ./csi300-price-index.json \
  --cutoff 2026-10-31
```

## 测试

```bash
PYTHONPATH=src python -m unittest tests.test_research_diagnostics tests.test_research_candidates tests.test_backtest_constraints -v
```

测试只用 `TEST.*` 一类合成代码和虚构价格。

## 本次改动允许清单

- `src/xiao_a/research_diagnostics.py`
- `src/xiao_a/research_candidates.py`
- `src/xiao_a/research_daily.py`
- `src/xiao_a/research_study_extensions.py`
- `src/xiao_a/cli.py`
- `src/xiao_a/backtest.py`
- `tests/test_research_diagnostics.py`
- `tests/test_research_candidates.py`
- `tests/test_research_daily.py`
- `tests/test_research_study_extensions.py`
- `tests/test_backtest_constraints.py`
- `tests/fixtures/research_backtest_spec.json`
- `docs/RESEARCH_DIAGNOSTICS.md`
- `README.md`
- `pyproject.toml`（只增加不自动安装的 `research` 可选依赖）

未改：`decision.py`、`ensemble.py`、`prediction_audit.py` 的目标定义、指纹和哈希链、仪表盘 API、`requirements-lock.txt`、`start.sh`，以及观察列表、账户费率和资产台账。

## 部署与回退

本变更不合并、不部署，也不更新另一份没有 git 历史的本地运行目录。那份目录里的 `portfolio_store.py`、`screener.py`、`dashboard.py`、`data/market.py`、`web/index.html` 与公开版本不同，不能用本分支覆盖。

服务进程不会导入研究模块。回退就是撤回这个提交；没有数据迁移，也没有需要恢复的生产权重。不要把研究可选依赖写进 `requirements-lock.txt` 或 `start.sh`。本地确认诊断 JSON 之后，仍不能把候选规则或回测结果写成生产策略。
