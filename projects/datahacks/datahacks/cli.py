"""datahacks 命令行接口。

设计原则（对齐原项目）：每个工具是一个独立子命令，
stdin 读入、stdout 输出，可以在 shell 管道里自由组合。

    cat access.log | datahacks histogram --bins 20
    cat data | datahacks quantile --probs 0.5 0.95 0.99
    awk '{print $9}' access.log | datahacks frequency
    cat urls.txt | datahacks unique
    cat data | datahacks sample --percentage 10 --seed 42
    cat data | datahacks md5

也支持直接给文件（--file），不必每次 cat。
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from .io import (
    DataError,
    compute_stats,
    fmt_number,
    quantiles_of,
    read_key_values,
    read_lines,
    write_json,
)
from .sample import hash_lines, percentile, sample_lines, unique_count, unique_lines
from .stats import frequency, pivot, render_frequency, render_histogram


def _load_lines(args: argparse.Namespace) -> list[str]:
    """按 --file 或 stdin 读取行。"""
    if args.file:
        try:
            with open(args.file, encoding="utf-8") as fh:
                return read_lines(fh)
        except OSError as exc:
            raise DataError(f"无法读取文件 {args.file!r}：{exc}") from exc
    return read_lines(sys.stdin)


def _load_numbers(args: argparse.Namespace) -> list[float]:
    """读取数值。复用 _load_lines 再逐 token 解析，保证注释/空行规则一致。"""
    from .io import read_values

    if args.file:
        try:
            with open(args.file, encoding="utf-8") as fh:
                values = read_values(fh)
        except OSError as exc:
            raise DataError(f"无法读取文件 {args.file!r}：{exc}") from exc
    else:
        values = read_values(sys.stdin)

    if not values:
        raise DataError(
            "输入为空" + (f"（文件 {args.file!r} 可能不含数值）" if args.file else "")
        )
    return values


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="datahacks",
        description="纯标准库实现的命令行数据分析工具集",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  cat data | datahacks histogram --bins 20\n"
            "  awk '{print $NF}' log | datahacks quantile --probs 0.5 0.95 0.99\n"
            "  cat data | datahacks frequency --top 10\n"
            "  cat urls | datahacks unique\n"
            "  cat big.log | datahacks sample --percentage 10 --seed 42"
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--file", help="输入文件，省略则从 stdin 读")
        sp.add_argument("--json", action="store_true", help="以 JSON 输出")

    sp = sub.add_parser("histogram", help="数值直方图")
    add_common(sp)
    sp.add_argument("--bins", type=int, default=10, help="分桶数，默认 10")
    sp.add_argument("-l", "--log", action="store_true", help="对数分桶（要求值为正）")
    sp.add_argument("--min", dest="min_bound", type=float, help="下界，之外的值不计入")
    sp.add_argument("--max", dest="max_bound", type=float, help="上界，之外的值不计入")
    sp.add_argument("--max-bar", type=int, default=40, help="条形最大宽度")

    sp = sub.add_parser("quantile", help="分位数")
    add_common(sp)
    sp.add_argument("--probs", type=float, nargs="+",
                    default=[0.5, 0.9, 0.95, 0.99], help="分位点 0-1")
    sp.add_argument("--pct", type=float, help="单个百分位 0-100，如 95")

    sp = sub.add_parser("describe", help="完整描述统计")
    add_common(sp)

    sp = sub.add_parser("frequency", help="频次统计")
    add_common(sp)
    sp.add_argument("-d", "--delimiter", default="\t", help="字段分隔符，默认制表符")
    sp.add_argument("--by-value", action="store_true", help="按 key=value 组合统计")
    sp.add_argument("--top", type=int, default=20, help="只显示前 N 项")

    sp = sub.add_parser("unique", help="去重")
    add_common(sp)
    sp.add_argument("--count-only", action="store_true", help="只输出唯一值数量")

    sp = sub.add_parser("sample", help="抽样")
    add_common(sp)
    sp.add_argument("--percentage", type=float, help="抽样比例 0-100")
    sp.add_argument("--count", type=int, help="抽取条数")
    sp.add_argument("--seed", type=int, help="随机种子")

    for name, help_text in (("md5", "MD5 哈希"), ("sha1", "SHA1 哈希"),
                            ("sha256", "SHA256 哈希")):
        sp = sub.add_parser(name, help=help_text)
        add_common(sp)
        sp.add_argument("--whole", action="store_true",
                        help="整块哈希（所有行合成一个）而非逐行")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        payload = _dispatch(args)
    except DataError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 1

    if args.json:
        write_json(payload)
    else:
        print(payload["_text"])
    return 0


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    """按子命令执行，返回 {"_text": 展示文本, ...数据}。"""
    cmd = args.command

    if cmd == "histogram":
        values = _load_numbers(args)
        text = render_histogram(
            values, bins=args.bins, log_scale=args.log,
            max_bar=args.max_bar, min_bound=args.min_bound, max_bound=args.max_bound,
        )
        return {"_text": text}

    if cmd == "quantile":
        values = _load_numbers(args)
        if args.pct is not None:
            value = percentile(values, args.pct)
            return {
                "_text": f"P{args.pct:g} = {fmt_number(value or 0)}",
                "percentile": args.pct,
                "value": value,
            }
        qs = quantiles_of(values, args.probs)
        lines = [f"P{p * 100:g}\t{fmt_number(q) if q is not None else 'N/A'}"
                 for p, q in zip(args.probs, qs, strict=False)]
        return {
            "_text": "\n".join(lines),
            "quantiles": {
                f"P{p * 100:g}": (fmt_number(q) if q is not None else None)
                for p, q in zip(args.probs, qs, strict=False)
            },
        }

    if cmd == "describe":
        values = _load_numbers(args)
        stats, outside, _ = compute_stats(values)
        qs = quantiles_of(values, [0.25, 0.5, 0.75, 0.95, 0.99])
        data = {**stats.to_dict(), "outside_bounds": outside,
                "quantiles": dict(zip(["P25", "P50", "P75", "P95", "P99"], qs, strict=False))}
        lines = [stats.to_header(outside=outside)]
        lines += [f"  {k:<4} = {fmt_number(v or 0)}"
                  for k, v in data["quantiles"].items()]
        return {"_text": "\n".join(lines), **data}

    if cmd == "frequency":
        lines = _load_lines(args)
        pairs: list[tuple[str, str]] = []
        for line in lines:
            if args.delimiter in line:
                k, _, v = line.partition(args.delimiter)
                pairs.append((k.strip(), v.strip()))
            else:
                pairs.append((line.strip(), ""))
        counts = frequency(pairs, count_by_value=args.by_value)
        top = counts[: args.top] if args.top > 0 else counts
        text = render_frequency(top)
        return {
            "_text": text,
            "total_unique": len(counts),
            "counts": [{"key": k, "count": c} for k, c in top],
        }

    if cmd == "unique":
        lines = _load_lines(args)
        if args.count_only:
            n = unique_count(lines)
            return {
                "_text": f"total={len(lines)} unique={n} duplicates={len(lines) - n}",
                "total": len(lines), "unique_count": n,
                "duplicates": len(lines) - n,
            }
        uniq = unique_lines(lines)
        return {
            "_text": "\n".join(uniq),
            "total": len(lines), "unique_count": len(uniq),
            "duplicates": len(lines) - len(uniq),
        }

    if cmd == "sample":
        lines = _load_lines(args)
        picked = sample_lines(lines, args.percentage, count=args.count, seed=args.seed)
        return {
            "_text": "\n".join(picked),
            "input_count": len(lines), "sampled_count": len(picked),
        }

    if cmd in ("md5", "sha1", "sha256"):
        lines = _load_lines(args)
        # --whole 为真时合成一个哈希，否则逐行各算一个
        hashes = hash_lines(lines, algorithm=cmd, per_line=not args.whole)
        if args.whole:
            # 整块模式只输出裸哈希值，便于 `HASH=$(... sha256 --whole)` 直接取用
            digest = hashes["__all__"]
            return {"_text": digest, "hash": digest, "algorithm": cmd}
        return {
            "_text": "\n".join(f"{h}  {line}" for line, h in hashes.items()),
            "hashes": hashes, "algorithm": cmd,
        }

    raise DataError(f"未知子命令 {cmd!r}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
