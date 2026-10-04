"""datahacks 测试。

重点验证三件事：
1. 统计量计算正确（尤其分位数与样本方差的口径）
2. 边界条件（空输入、单元素、全等值、越界）
3. CLI 管道行为与确定性（同 seed 同结果）
"""

from __future__ import annotations

import io
import os

import pytest

from datahacks import (
    BAR_CHAR,
    DataError,
    compute_stats,
    fmt_number,
    frequency,
    hash_file,
    hash_lines,
    linear_buckets,
    log_buckets,
    percentile,
    pivot,
    quantiles_of,
    read_key_values,
    read_lines,
    read_values,
    render_frequency,
    render_histogram,
    sample_lines,
    unique_count,
    unique_lines,
)


# ---------- 输入解析 ----------

def test_read_values_multiple_per_line() -> None:
    assert read_values(io.StringIO("1 2 3\n4\n")) == [1.0, 2.0, 3.0, 4.0]


def test_read_values_skips_comments_and_blanks() -> None:
    assert read_values(io.StringIO("# c\n\n1\n\n# x\n2\n")) == [1.0, 2.0]


def test_read_values_error_has_line_number() -> None:
    with pytest.raises(DataError, match="第 2 行"):
        read_values(io.StringIO("1\nabc\n"))


def test_read_lines_skips_blanks() -> None:
    assert read_lines(io.StringIO("a\n\nb\n")) == ["a", "b"]


def test_read_key_values() -> None:
    assert read_key_values(io.StringIO("a\t1\nb\t2\n")) == [("a", "1"), ("b", "2")]


def test_read_key_values_error() -> None:
    with pytest.raises(DataError, match="缺少分隔符"):
        read_key_values(io.StringIO("a\t1\nbad\n"))


# ---------- 数字格式化 ----------

def test_fmt_number_strips_trailing_zeros() -> None:
    assert fmt_number(3.0) == "3"
    assert fmt_number(3.14159) == "3.1416"
    assert fmt_number(3.14159, precision=2) == "3.14"


def test_fmt_number_kills_float_noise() -> None:
    """线性插值会算出 264.99999999999994，应显示为 265。"""
    assert fmt_number(264.99999999999994, precision=2) == "265"
    assert fmt_number(265.00000000000006, precision=4) == "265"


# ---------- 描述统计 ----------

def test_compute_stats_basic() -> None:
    stats, outside, kept = compute_stats([1.0, 2.0, 3.0, 4.0])
    assert stats.count == 4
    assert stats.minimum == 1.0
    assert stats.maximum == 4.0
    assert stats.mean == 2.5
    assert stats.median == 2.5
    assert outside == 0
    assert kept == [1.0, 2.0, 3.0, 4.0]


def test_compute_stats_uses_sample_variance() -> None:
    """样本方差除以 n-1（data_hacks 的口径），不是总体方差。"""
    stats, _, _ = compute_stats([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])
    assert stats.variance == pytest.approx(4.571428, rel=1e-4)
    assert stats.stddev == pytest.approx(stats.variance ** 0.5)


def test_compute_stats_median_even_count() -> None:
    stats, _, _ = compute_stats([1.0, 2.0, 3.0, 4.0])
    assert stats.median == 2.5


def test_compute_stats_median_odd_count() -> None:
    stats, _, _ = compute_stats([1.0, 2.0, 3.0])
    assert stats.median == 2.0


def test_compute_stats_single_value() -> None:
    stats, _, _ = compute_stats([5.0])
    assert stats.variance == 0.0
    assert stats.stddev == 0.0
    assert stats.median == 5.0


def test_compute_stats_bounds_exclude_outliers() -> None:
    stats, outside, kept = compute_stats([1.0, 2.0, 3.0, 100.0],
                                         min_bound=0.0, max_bound=10.0)
    assert outside == 1
    assert stats.count == 3
    assert 100.0 not in kept


def test_compute_stats_all_out_of_bounds() -> None:
    with pytest.raises(DataError, match="都落在"):
        compute_stats([100.0, 200.0], min_bound=0.0, max_bound=10.0)


def test_compute_stats_empty_raises() -> None:
    with pytest.raises(DataError, match="没有可统计"):
        compute_stats([])


def test_stats_header_format() -> None:
    stats, _, _ = compute_stats([1.0, 2.0, 3.0])
    header = stats.to_header()
    assert "# NumSamples = 3" in header
    assert "Min = 1.0000" in header
    assert "Median 2.0000" in header


def test_stats_header_reports_outside() -> None:
    stats, outside, _ = compute_stats([1.0, 100.0], max_bound=10.0)
    assert "outside" in stats.to_header(outside=outside)


# ---------- 分位数 ----------

def test_quantiles_linear_interpolation() -> None:
    assert quantiles_of([1, 2, 3, 4], [0.0, 0.5, 1.0]) == [1.0, 2.5, 4.0]


def test_quantiles_out_of_range_returns_none() -> None:
    assert quantiles_of([1, 2, 3], [-0.1, 1.5]) == [None, None]


def test_quantiles_empty_returns_none() -> None:
    assert quantiles_of([], [0.5]) == [None]


def test_percentile_p95() -> None:
    values = [10, 20, 30, 40, 50, 100, 200, 300]
    assert percentile(values, 95) == pytest.approx(265.0)


def test_percentile_invalid_returns_none() -> None:
    assert percentile([1, 2], 150) is None


# ---------- 分桶 ----------

def test_linear_buckets_counts_sum() -> None:
    values = [float(i) for i in range(100)]
    buckets = linear_buckets(values, bins=10)
    assert len(buckets) == 10
    assert sum(b.count for b in buckets) == 100


def test_linear_buckets_all_equal() -> None:
    """所有值相同时不能除零，应退化为单桶。"""
    buckets = linear_buckets([5.0] * 10, bins=5)
    assert len(buckets) == 1
    assert buckets[0].count == 10


def test_linear_buckets_max_value_in_last_bucket() -> None:
    buckets = linear_buckets([0.0, 1.0, 2.0, 3.0, 4.0], bins=4)
    assert buckets[-1].count >= 1


def test_linear_buckets_invalid_bins() -> None:
    with pytest.raises(DataError, match="分桶数"):
        linear_buckets([1.0], bins=0)


def test_log_buckets_rejects_non_positive() -> None:
    with pytest.raises(DataError, match="对数分桶要求"):
        log_buckets([0.0, 1.0, 2.0], bins=3)


def test_log_buckets_counts_sum() -> None:
    values = [float(i) for i in range(1, 100)]
    buckets = log_buckets(values, bins=5)
    assert sum(b.count for b in buckets) == 99


def test_log_buckets_all_equal() -> None:
    assert len(log_buckets([5.0] * 4, bins=3)) == 1


# ---------- 直方图渲染 ----------

def test_render_histogram_matches_original_format() -> None:
    """对齐 data_hacks 的 histogram.py 输出格式。"""
    text = render_histogram([float(i) for i in range(1, 11)], bins=5)
    assert "# NumSamples = 10" in text
    assert "# each" in text and BAR_CHAR in text
    assert "%" in text


def test_render_histogram_respects_bounds() -> None:
    text = render_histogram([1.0, 2.0, 100.0], bins=3, max_bound=10.0)
    assert "outside" in text
    assert "NumSamples = 2" in text


def test_render_histogram_log_scale() -> None:
    text = render_histogram([float(i) for i in range(1, 50)], bins=4, log_scale=True)
    assert "NumSamples" in text


def test_render_histogram_empty_raises() -> None:
    with pytest.raises(DataError, match="没有可绘制"):
        render_histogram([])


def test_render_histogram_scales_bars() -> None:
    """峰值远超 max_bar 时应缩放每字符代表的计数，而不是撑爆宽度。"""
    text = render_histogram([1.0] * 500 + [2.0], bins=2, max_bar=10)
    for line in text.split("\n"):
        if BAR_CHAR in line:
            assert line.count(BAR_CHAR) <= 12, f"条形应被缩放: {line}"


# ---------- 频次 ------------

def test_frequency_sorted_by_count_then_key() -> None:
    pairs = [("b", ""), ("a", ""), ("c", ""), ("a", ""), ("a", ""), ("b", "")]
    counts = frequency(pairs)
    assert counts == [("a", 3), ("b", 2), ("c", 1)]


def test_frequency_tie_broken_by_key() -> None:
    """计数相同时按键升序，保证输出确定。"""
    counts = frequency([("z", ""), ("a", "")])
    assert counts == [("a", 1), ("z", 1)]


def test_frequency_by_value() -> None:
    pairs = [("a", "1"), ("a", "2"), ("b", "1")]
    counts = frequency(pairs, count_by_value=True)
    assert ("a=1", 1) in counts
    assert ("a=2", 1) in counts


def test_render_frequency_aligns() -> None:
    text = render_frequency([("apple", 2), ("fig", 1)])
    assert "apple" in text and "fig" in text


def test_render_frequency_empty() -> None:
    assert render_frequency([]) == "(无数据)"


# ---------- 透视 ----------

def test_pivot_builds_table() -> None:
    rows = pivot([("a", "x", "1"), ("a", "y", "2"), ("b", "x", "3")])
    assert rows == [
        {"row": "a", "x": "1", "y": "2"},
        {"row": "b", "x": "3", "y": None},
    ]


def test_pivot_rows_sorted() -> None:
    rows = pivot([("z", "c", "1"), ("a", "c", "2")])
    assert [r["row"] for r in rows] == ["a", "z"]


# ---------- 去重 ----------

def test_unique_lines_preserves_first_seen_order() -> None:
    assert unique_lines(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]


def test_unique_count() -> None:
    assert unique_count(["a", "b", "a"]) == 2


def test_unique_lines_with_key_func() -> None:
    lines = ["GET /a", "POST /a", "GET /b"]
    assert unique_lines(lines, key_func=lambda s: s.split()[1]) == ["GET /a", "GET /b"]


# ---------- 抽样 ----------

def test_sample_by_percentage() -> None:
    items = [f"l{i}" for i in range(100)]
    picked = sample_lines(items, 30, seed=42)
    assert len(picked) == 30


def test_sample_reproducible_with_seed() -> None:
    items = [f"l{i}" for i in range(50)]
    a = sample_lines(items, 40, seed=7)
    b = sample_lines(items, 40, seed=7)
    assert a == b, "同 seed 必须完全可复现"


def test_sample_different_seed_differs() -> None:
    items = [f"l{i}" for i in range(50)]
    a = sample_lines(items, 40, seed=1)
    b = sample_lines(items, 40, seed=2)
    assert a != b


def test_sample_by_count() -> None:
    assert len(sample_lines([f"l{i}" for i in range(20)], count=5, seed=1)) == 5


def test_sample_boundaries() -> None:
    items = ["a", "b", "c"]
    assert sample_lines(items, 100) == items
    assert sample_lines(items, 0) == []


def test_sample_no_args_returns_all() -> None:
    items = ["a", "b"]
    assert sample_lines(items) == items


def test_sample_invalid_percentage() -> None:
    with pytest.raises(DataError, match="0-100"):
        sample_lines(["a"], 150)


# ---------- 哈希 ----------

def test_hash_lines_known_value() -> None:
    """md5("abc") 是众所周知的固定值。"""
    result = hash_lines(["abc"], algorithm="md5")
    assert list(result.values()) == ["900150983cd24fb0d6963f7d28e17f72"]


def test_hash_lines_strips_newline() -> None:
    """行尾换行不应影响哈希，否则同样的内容会得到不同值。"""
    a = hash_lines(["abc"])
    b = hash_lines(["abc\n"])
    assert list(a.values()) == list(b.values())


def test_hash_lines_whole_mode() -> None:
    result = hash_lines(["a", "b"], per_line=False)
    assert "__all__" in result


def test_hash_lines_whole_avoids_concat_collision() -> None:
    """"ab"+"c" 与 "a"+"bc" 必须得到不同哈希，故用分隔符。"""
    a = hash_lines(["ab", "c"], per_line=False)["__all__"]
    b = hash_lines(["a", "bc"], per_line=False)["__all__"]
    assert a != b


def test_hash_unsupported_algorithm() -> None:
    with pytest.raises(DataError, match="不支持的哈希算法"):
        hash_lines(["a"], algorithm="nope")


@pytest.fixture
def tmp_dir():
    """测试临时目录。

    部分受限环境不允许 pytest 使用系统 temp（报 EEXIST / 权限错误），
    统一落到项目内 .pytest_tmp，让测试结果只反映代码正确性。
    """
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".pytest_tmp")
    os.makedirs(path, exist_ok=True)
    return path


def test_hash_file(tmp_dir) -> None:
    p = os.path.join(tmp_dir, "f.txt")
    with open(p, "w") as fh:
        fh.write("hello")
    assert hash_file(p, algorithm="md5") == "5d41402abc4b2a76b9719d911017c592"


# ---------- agent 集成 ----------

def test_agent_registration() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from datahacks.agent import register

    reg = ToolRegistry()
    register(reg)
    for name in ("data_histogram", "data_quantiles", "data_frequency",
                 "data_unique", "data_sample", "data_hash", "data_percentile",
                 "data_pivot", "data_describe"):
        assert reg.has(name), name

    d = Dispatcher(reg)
    r = d.dispatch("data_quantiles", {"values": [1, 2, 3, 4]})
    assert r.ok
    assert r.data["quantiles"]["P50"] == 2.5


def test_agent_schemas_are_strict() -> None:
    from agent_core import ToolRegistry

    from datahacks.agent import register

    reg = ToolRegistry()
    register(reg)
    for spec in reg.specs():
        s = spec.parameters
        assert s["additionalProperties"] is False, spec.name
        assert set(s["required"]) == set(s["properties"]), spec.name


def test_agent_accepts_string_numbers() -> None:
    """模型常把数字包成字符串发来，应宽松接受而不是报错。"""
    from agent_core import Dispatcher, ToolRegistry

    from datahacks.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)
    r = d.dispatch("data_percentile", {"values": ["1", "2", "3"], "percentile": 50})
    assert r.ok and r.data["value"] == 2


def test_agent_describe_returns_quantiles() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from datahacks.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)
    r = d.dispatch("data_describe", {"values": [1, 2, 3, 4, 5]})
    assert r.ok
    assert r.data["count"] == 5
    assert "P95" in r.data["quantiles"]


def test_agent_unique_count_only() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from datahacks.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)
    r = d.dispatch("data_unique", {"lines": ["a", "b", "a"], "count_only": True})
    assert r.ok
    assert r.data["unique_count"] == 2
    assert r.data["duplicates"] == 1


def test_agent_bad_number_fails_gracefully() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from datahacks.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)
    r = d.dispatch("data_percentile", {"values": ["abc"], "percentile": 50})
    assert r.ok is False


def test_agent_empty_values_fails_gracefully() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from datahacks.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)
    r = d.dispatch("data_histogram", {"values": []})
    assert r.ok is False


# ---------- CLI ----------

def _cli(args: list[str], stdin: str = "") -> int:
    """在子进程中跑 CLI 以真实喂 stdin，monkeypatch sys.stdin 不够可靠。"""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "datahacks.cli", *args],
        input=stdin, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.returncode


def _cli_out(args: list[str], stdin: str = "") -> str:
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "datahacks.cli", *args],
        input=stdin, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_cli_histogram() -> None:
    out = _cli_out(["histogram", "--bins", "3"], "1\n2\n3\n4\n")
    assert "NumSamples = 4" in out


def test_cli_quantile() -> None:
    out = _cli_out(["quantile", "--probs", "0.5", "0.95"], "1\n2\n3\n4\n")
    assert "P50" in out and "P95" in out


def test_cli_quantile_single_pct() -> None:
    out = _cli_out(["quantile", "--pct", "95"], "1\n2\n3\n4\n")
    assert "P95" in out


def test_cli_describe() -> None:
    out = _cli_out(["describe"], "1\n2\n3\n4\n5\n")
    assert "NumSamples = 5" in out
    assert "P99" in out
    assert "PP25" not in out, "不应出现 PP25 这种重复前缀"


def test_cli_frequency() -> None:
    out = _cli_out(["frequency"], "a\t1\nb\t2\na\t3\n")
    assert "a" in out and "b" in out


def test_cli_unique_count_only() -> None:
    out = _cli_out(["unique", "--count-only"], "a\nb\na\n")
    assert "unique=2" in out


def test_cli_sample_reproducible() -> None:
    a = _cli_out(["sample", "--percentage", "50", "--seed", "42"],
                 "\n".join(f"l{i}" for i in range(20)))
    b = _cli_out(["sample", "--percentage", "50", "--seed", "42"],
                 "\n".join(f"l{i}" for i in range(20)))
    assert a == b, "同 seed 的抽样结果必须一致"


def test_cli_sha256_whole() -> None:
    """整块哈希在每行内容后加 \\x00 分隔符，避免拼接撞车。"""
    import hashlib

    out = _cli_out(["sha256", "--whole"], "hello\n").strip()
    # 等价于 sha256(b"hello\\x00")
    assert out == hashlib.sha256(b"hello\x00").hexdigest()
    # 且不应等于裸内容哈希——否则说明分隔符没起作用
    assert out != hashlib.sha256(b"hello").hexdigest()


def test_cli_json_output() -> None:
    import json

    out = _cli_out(["describe", "--json"], "1\n2\n3\n")
    data = json.loads(out)
    assert data["count"] == 3


def test_cli_empty_input_errors() -> None:
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "datahacks.cli", "histogram"],
        input="", capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 1
    assert "输入为空" in proc.stderr


def test_cli_pipeline_chain() -> None:
    """验证管道组合：unique 后接 frequency。"""
    import subprocess
    import sys

    data = "a\nb\na\nc\nb\n"
    p1 = subprocess.run([sys.executable, "-m", "datahacks.cli", "unique"],
                        input=data, capture_output=True, text=True, check=False)
    p2 = subprocess.run([sys.executable, "-m", "datahacks.cli", "frequency"],
                        input=p1.stdout, capture_output=True, text=True, check=False)
    assert p2.returncode == 0
    assert "a" in p2.stdout
