# 只读研究诊断与 A 股约束回测

这条命令只读取本地文件并打印 JSON。它不结算预测、不追加审计事件、不改写历史、不读取账户费率或资产台账，也不修改 `decision.py` / `ensemble.py` 的生产权重。候选规则不会自动晋升。`calibrated_probability_available_for_production` 固定为 `false`。

规则符合分不是概率。方向命中率不是买入事件率，也不是扣费后的净收益。审计标签的 `execution_simulation` 仍是 `not_applied`。私人台账没有随本仓库提供；下面的命令是在你自己的机器上复核分母用的，仓库测试只用合成样本。

## 本地只读复核

在包含本仓库代码的目录执行。把审计路径换成你自己的 `XIAO_A_AUDIT_FILE`，不要把该文件提交或上传。

```bash
PYTHONPATH=src python -m xiao_a.cli research audit \
  --audit "$XIAO_A_AUDIT_FILE" \
  --as-of 2026-11-06 \
  --engine python
```

`--as-of` 使用上海日历日。带时区的 `recorded_at` 先换到 Asia/Shanghai；没有时区的时间戳保留字符串里的日期。晚于截止日的事件不进入本次分母。文件比旧报告更长，只说明后来又追加了事件；只有整份文件的哈希链失败才暂停评分。缺少 `recorded_at` 的事件不能证明落在截止日内，因此不进入切片。

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

非重叠日期块按信息截止日贪心选取：下一块的截止日必须晚于上一块的退出日。块数少于 8 时不提供重采样区间。满 8 块时的区间也只是描述，不能消除金融时序相关。样本来自额外评估、持仓和股票池的混合，不是随机样本。

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

## 测试

```bash
PYTHONPATH=src python -m unittest tests.test_research_diagnostics tests.test_backtest_constraints -v
```

测试只用 `TEST.*` 一类合成代码和虚构价格。

## 本次改动允许清单

- `src/xiao_a/research_diagnostics.py`
- `src/xiao_a/cli.py`
- `src/xiao_a/backtest.py`
- `tests/test_research_diagnostics.py`
- `tests/test_backtest_constraints.py`
- `tests/fixtures/research_backtest_spec.json`
- `docs/RESEARCH_DIAGNOSTICS.md`
- `README.md`
- `pyproject.toml`（只增加不自动安装的 `research` 可选依赖）

未改：`decision.py`、`ensemble.py`、`prediction_audit.py` 的目标定义、指纹和哈希链、仪表盘 API、`requirements-lock.txt`、`start.sh`，以及观察列表、账户费率和资产台账。

## 部署与回退

本变更不合并、不部署，也不更新另一份没有 git 历史的本地运行目录。那份目录里的 `portfolio_store.py`、`screener.py`、`dashboard.py`、`data/market.py`、`web/index.html` 与公开版本不同，不能用本分支覆盖。

服务进程不会导入研究模块。回退就是撤回这个提交；没有数据迁移，也没有需要恢复的生产权重。不要把研究可选依赖写进 `requirements-lock.txt` 或 `start.sh`。本地确认诊断 JSON 之后，仍不能把候选规则或回测结果写成生产策略。
