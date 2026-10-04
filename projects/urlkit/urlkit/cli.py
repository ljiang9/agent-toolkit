"""urlkit 命令行接口。

对齐 trurl 的命令行用法：
    urlkit https://curl.se --set host=example.com
    urlkit --url https://curl.se/we/../are.html --set port=8080
    urlkit "https://curl.se/we/are.html" --get path
    urlkit --url https://curl.se/hello --append path=you
    urlkit "https://curl.se?search=hey&utm_source=x" --qtrim "utm_*"
    cat urls.txt | urlkit --url-file -

也支持更贴近日常使用的子命令形式：
    urlkit parse https://curl.se/we/are.html
    urlkit normalize https://e.com/?b=2&a=1
    urlkit fingerprint https://e.com/?b=2&a=1
    urlkit check http://127.0.0.1/

注意：子命令必须放在 URL 之前（`urlkit parse <url>`），因为 URL 是位置参数，
若先给 URL 则 argparse 无法判断它是子命令名还是 URL。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from typing import Any

from .core import (
    UrlkitError,
    append_component,
    fingerprint,
    get_component,
    iter_batched,
    normalize,
    parse_url,
    redirect,
    set_component,
)
from .safety import check_url_safety

SUBCOMMANDS = ("parse", "normalize", "fingerprint", "check", "batch")


def build_parser() -> argparse.ArgumentParser:
    """构造解析器。

    子命令与 URL 都用位置参数，但把子命令名标为 choices，argparse 会在
    第一个位置参数不是合法子命令名时自动回退到「URL」角色——避免了
    「URL 被当成子命令名而报错」以及「子命令被当成 URL」两个方向的问题。
    """
    p = argparse.ArgumentParser(
        prog="urlkit",
        description="URL 结构化编解码工具：解析、改写、归一化、去重指纹与安全检查",
        epilog=(
            "示例：\n"
            "  urlkit https://curl.se --set host=example.com\n"
            "  urlkit https://curl.se/we/are.html --get path\n"
            "  urlkit parse https://curl.se/we/are.html\n"
            "  urlkit check http://127.0.0.1/"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("url", nargs="?", help="要处理的 URL")
    p.add_argument("--url", dest="url_opt", help="要处理的 URL（与位置参数等价）")
    p.add_argument("--url-file", metavar="FILE",
                   help="从文件批量读取 URL，传 - 表示 stdin")

    # trurl 风格的操作参数
    p.add_argument("--get", dest="get", metavar="COMPONENT",
                   help="读取指定组件并输出")
    p.add_argument("--set", dest="set_", metavar="COMPONENT=VALUE",
                   action="append", help="设置组件，可重复")
    p.add_argument("--append", dest="append_", metavar="COMPONENT=VALUE",
                   action="append", help="追加到组件（path/query/fragment），可重复")
    p.add_argument("--redirect", metavar="PATH", help="把路径替换为指定值")
    p.add_argument("--qtrim", metavar="PATTERN", action="append",
                   help="删除匹配的查询参数，支持 * 通配，可重复")
    p.add_argument("--sort-query", action="store_true", help="对查询参数排序")
    p.add_argument("--default-port", action="store_true", help="显式输出默认端口")
    p.add_argument("--accept-space", action="store_true", help="自动编码未转义空格")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    return p


def _resolve_url(args: argparse.Namespace) -> str | None:
    return args.url_opt or args.url


def _apply_operations(url: str, args: argparse.Namespace) -> str:
    """按固定顺序执行 set / append / redirect / qtrim / sort 操作。

    顺序固定为 set → append → redirect → qtrim → sort，
    这样多条操作组合时结果可预期（例如先 redirect 再 qtrim）。
    """
    if args.set_:
        for item in args.set_:
            comp, sep, value = item.partition("=")
            if not sep:
                raise UrlkitError(f"--set 需要 COMPONENT=VALUE 格式，收到 {item!r}")
            url = set_component(url, comp, value)

    if args.append_:
        for item in args.append_:
            comp, sep, value = item.partition("=")
            if not sep:
                raise UrlkitError(f"--append 需要 COMPONENT=VALUE 格式，收到 {item!r}")
            url = append_component(url, comp, value)

    if args.redirect:
        url = redirect(url, args.redirect)

    if args.qtrim:
        parts = parse_url(url, accept_space=True)
        parts.trim_query(args.qtrim)
        url = parts.compose()

    if args.sort_query:
        parts = parse_url(url, accept_space=True)
        parts.sort_query()
        url = parts.compose()

    if args.default_port:
        parts = parse_url(url, accept_space=True)
        url = parts.compose(default_port=True)

    return url


def _emit(payload: Any, *, as_json: bool, plain_url: bool) -> None:
    """输出结果。单一 dict 且只有 url 键时默认裸输出，便于管道使用。"""
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif plain_url and isinstance(payload, dict) and set(payload) == {"url"}:
        print(payload["url"])
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # 手动拆分子命令：URL 也是位置参数，argparse 无法自动区分，
    # 因此只在首参精确命中子命令名时才当作子命令。
    cmd: str | None = None
    if argv and argv[0] in SUBCOMMANDS:
        cmd = argv[0]
        argv = argv[1:]

    parser = build_parser()
    args = parser.parse_args(argv)
    accept_space = True if args.accept_space else None

    # batch 模式：从 stdin 流式处理
    if cmd == "batch":
        return _run_batch(args, parser)

    url = _resolve_url(args)
    if not url:
        if args.url_file:
            return _run_file(args, parser)
        parser.print_help()
        return 1

    try:
        if cmd:
            # 子命令路径：先施加通用操作，再按子命令语义取结果
            worked = _apply_operations(url, args)
            if cmd == "parse":
                payload = parse_url(worked, accept_space=True).to_dict()
            elif cmd == "normalize":
                payload = {"normalized": normalize(worked, sort_query=args.sort_query)}
            elif cmd == "fingerprint":
                payload = {"fingerprint": fingerprint(worked)}
            elif cmd == "check":
                payload = check_url_safety(worked)
            else:
                raise UrlkitError(f"未知子命令 {cmd!r}")
            plain = False
        else:
            worked = _apply_operations(url, args)
            if args.get:
                # --get 打印裸值而非 JSON，便于管道直接使用
                # （对齐 trurl 的 --get '{path}' 行为）
                value = get_component(worked, args.get)
                if not args.json:
                    print("" if value is None else value)
                    return 0
                payload = {"component": args.get, "value": value}
            else:
                payload = {"url": worked}
                plain = True
    except UrlkitError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"内部错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    _emit(payload, as_json=args.json, plain_url=plain)
    return 0


def _run_file(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """--url-file 模式：逐行读取并处理。"""
    if args.url_file == "-":
        return _process_lines(args, sys.stdin)

    try:
        with open(args.url_file, encoding="utf-8") as fh:
            return _process_lines(args, fh)
    except OSError as exc:
        print(f"错误：无法读取文件 {args.url_file!r}：{exc}", file=sys.stderr)
        return 1


def _process_lines(args: argparse.Namespace, lines: Iterable[str]) -> int:
    """逐行处理：单行失败不中断整体，最终以退出码反映失败情况。"""
    out: list[Any] = []
    errors = 0
    for raw in iter_batched(iter(lines)):
        try:
            worked = _apply_operations(raw, args)
            if args.get:
                out.append({"input": raw, args.get: get_component(worked, args.get)})
            else:
                out.append(worked)
        except UrlkitError as exc:
            errors += 1
            print(f"跳过 {raw!r}：{exc}", file=sys.stderr)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for item in out:
            print(item if isinstance(item, str) else json.dumps(item, ensure_ascii=False))
    # 全部失败才算命令失败；部分成功仍视为成功
    return 1 if errors and not out else 0


def _run_batch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """batch 子命令：对 stdin 中每行 URL 施加相同操作，输出 JSON 数组。"""
    results: list[dict[str, Any]] = []
    errors = 0
    for raw in iter_batched(iter(sys.stdin)):
        try:
            worked = _apply_operations(raw, args)
            entry: dict[str, Any] = {"input": raw, "url": worked}
            if args.get:
                entry[args.get] = get_component(worked, args.get)
            results.append(entry)
        except UrlkitError as exc:
            errors += 1
            results.append({"input": raw, "error": str(exc)})
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 1 if errors and not results else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
