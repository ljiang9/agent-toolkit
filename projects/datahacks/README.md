# datahacks

纯标准库实现的命令行数据分析工具集。六个独立子命令，stdin 读入、stdout 输出，
可以在 shell 管道里自由组合。

复刻自 Bitly 的 [data_hacks](https://github.com/bitly/data_hacks)（1,970 star），
保留其直方图输出格式，补上分位数、去重、透视、agent 接入等能力。**零第三方依赖。**

## 快速开始

```bash
pip install -e .

# 从管道读
cat access.log | datahacks histogram --bins 20

# 从文件读
datahacks describe --file metrics.txt --json
```

## 六个子命令

### histogram — 直方图

```bash
$ seq 1 10 | datahacks histogram --bins 5
# NumSamples = 10; Min = 1.0000; Max = 10.0000
# Mean = 5.5000; Variance = 9.1667; SD = 3.0277; Median 5.5000
# each ∎ represents a count of 1
   1.0000 -    2.8000 [     2]: ∎∎ (20.00%)
   2.8000 -    4.6000 [     2]: ∎∎ (20.00%)
   4.6000 -    6.4000 [     2]: ∎∎ (20.00%)
   6.4000 -    8.2000 [     2]: ∎∎ (20.00%)
   8.2000 -   10.0000 [     2]: ∎∎ (20.00%)
```

响应时间这类跨数量级的数据用对数分桶：

```bash
awk '{print $NF}' access.log | datahacks histogram --log --bins 15
```

`--min` / `--max` 可以排除越界样本（并报告被排除的数量）：

```bash
datahacks histogram --min 0 --max 1000    # 只统计 [0, 1000] 内的值
```

条形宽度自动缩放：每个 `∎` 代表多少样本由峰值决定，不会撑爆终端宽度。

### quantile — 分位数

```bash
$ printf '10\n20\n30\n40\n50\n100\n200\n300\n' | datahacks quantile --probs 0.5 0.95
P50	45
P95	265
```

单个百分位（对应原项目的 `ninety_five_percent.py`）：

```bash
awk '{print $NF}' access.log | datahacks quantile --pct 95
```

用线性插值法，与 numpy 默认的 `'linear'` 一致。

### describe — 完整描述统计

一次拿到全貌，适合刚拿到一批陌生数值时先摸个底：

```bash
$ seq 1 5 | datahacks describe
# NumSamples = 5; Min = 1.0000; Max = 5.0000
# Mean = 3.0000; Variance = 2.5000; SD = 1.5811; Median 3.0000
  P25  = 2
  P50  = 3
  P75  = 4
  P95  = 4.8000
  P99  = 4.9600
```

方差用样本方差（除以 n−1），这是统计学惯例。

### frequency — 频次统计

```bash
$ printf 'apple\t3\nbanana\t5\napple\t7\ncherry\t2\n' | datahacks frequency
apple          2 (40.00%)
banana         2 (40.00%)
cherry         1 (20.00%)
```

输入是 `key<分隔符>...` 形式。`--by-value` 按 `key=value` 组合统计共现。

**输出顺序稳定**：计数降序，同计数按键升序。方便多次运行做 diff 对比。

### unique — 去重

```bash
$ printf 'a\nb\na\nc\nb\n' | datahacks unique --count-only
total=5 unique=3 duplicates=2
```

保持**首次出现**的顺序 —— 日志分析里「第一次出现」往往才是你要找的那条。

### sample — 抽样

```bash
$ seq 1 10 | datahacks sample --percentage 30 --seed 42
2
7
4
```

给了 `--seed` 就完全可复现。不给 `--percentage` 和 `--count` 时返回全部。

### md5 / sha1 / sha256 — 哈希

```bash
$ printf 'a\nb\n' | datahacks sha256
ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb  a
3e23e8160039594a33894f6564e1b1348bbd7a0088d42c4acb73eeaed59c009d  b

$ printf 'hello\n' | datahacks sha256 --whole
f3aefe62965a91903610f0e23cc8a69d5b87cea6d28e75489b0d2ca02ed7993c
```

`--whole` 把所有行合成一个哈希，**每行内容后加 `\x00` 分隔符** ——
否则 `["ab","c"]` 和 `["a","bc"]` 拼接后完全相同会撞车。

## 管道组合

这是设计的核心：每个工具只做一件事，组合起来完成复杂分析。

```bash
# 日志去重后统计最常见的错误码
awk '{print $9}' access.log | datahacks unique | datahacks frequency --top 10

# 采样 10% 后看分布
cat big.log | datahacks sample --percentage 10 --seed 42 | datahacks histogram

# 找出 P95 最差的接口
grep " /api/" access.log | awk '{print $7, $NF}' | datahacks frequency
```

## 作为库使用

```python
from datahacks import compute_stats, quantiles_of, render_histogram, frequency, pivot

stats, outside, kept = compute_stats([1.0, 2.0, 3.0, 4.0])
stats.median      # 2.5
stats.variance    # 1.666...（样本方差，n-1）
outside           # 0

quantiles_of([1, 2, 3, 4], [0.25, 0.5, 0.95])   # [1.75, 2.5, 3.85]

print(render_histogram([float(i) for i in range(100)], bins=10))

frequency([("apple", "3"), ("banana", "5"), ("apple", "7")])
# [('apple', 2), ('banana', 1)]

pivot([("a", "x", "1"), ("a", "y", "2"), ("b", "x", "3")])
# [{'row': 'a', 'x': '1', 'y': '2'}, {'row': 'b', 'x': '3', 'y': None}]
```

## 作为 agent 工具

注册 9 个工具：

```python
from agent_core import ToolRegistry, Dispatcher
from datahacks.agent import register

reg = ToolRegistry()
register(reg)
d = Dispatcher(reg)

d.dispatch("data_describe", {"values": [1, 2, 3, 4, 5]})
# {"ok": true, "data": {"count": 5, "mean": 3.0, "quantiles": {"P95": 4.8, ...}}}

d.dispatch("data_percentile", {"values": [10, 20, 30], "percentile": 95})
d.dispatch("data_frequency", {"pairs": [{"key": "a", "value": "1"}, ...]})
```

| 工具 | 用途 |
|---|---|
| `data_histogram` | 文本直方图 + 描述统计 |
| `data_quantiles` | 多分位点（默认 P50/P90/P95/P99） |
| `data_percentile` | 单个百分位（如 P95 响应时间） |
| `data_describe` | 一次拿全：均值/方差/分位数 |
| `data_frequency` | 频次与共现统计 |
| `data_unique` | 去重与唯一值计数 |
| `data_sample` | 抽样（seed 可复现） |
| `data_hash` | md5/sha1/sha256 |
| `data_pivot` | 交叉透视 |

**对 LLM 的容错**：数值数组的元素 schema 声明为 `["number", "string"]`，
所以模型把数字包成字符串发过来（`["1.5", "2"]`）也能正常工作 ——
这类不宽容会让本可成功的调用直接失败。

## 目录结构

```
datahacks/
├── datahacks/
│   ├── __init__.py     # 公开 API
│   ├── io.py           # 管道读写层 + 描述统计 + 分位数
│   ├── stats.py        # 分桶、直方图渲染、频次、透视
│   ├── sample.py       # 抽样、去重、哈希
│   ├── agent.py        # agent 工具注册（9 个）
│   └── cli.py          # 六个子命令
├── tests/
│   └── test_datahacks.py  # 76 个测试
├── pyproject.toml
└── README.md
```

## 测试

```bash
pip install -e ".[dev]"
pytest -v
```

## 依赖

| 类型 | 依赖 |
|---|---|
| 运行时 | **无**（仅标准库） |
| 开发 | pytest, pytest-cov, mypy, ruff |
| 可选 | agent-core（启用 agent 工具注册） |

要求 Python ≥ 3.11。

## 许可

MIT
