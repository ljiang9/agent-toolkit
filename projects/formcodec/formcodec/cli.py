"""formcodec 命令行接口。

三个子命令：
    encode   JSON 对象 -> 表单键值对 / query 串
    decode   表单键值对 -> JSON（可指定字段类型转换）
    path     解析表单键路径语法

encode 的输入可用 --file 读 JSON，也可从 stdin 读：

    echo '{"name":"joey","tags":["a","b"]}' | formcodec encode
    formcodec decode --query 'name=joey&age=3' --cast age=int
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .codec import CodecError, decode_value
from .core import form_to_query_string, query_string_to_form, unflatten
from .path import KeyPath, PathError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="formcodec",
        description="结构体与表单/查询参数的双向编解码",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  echo '{\"name\":\"joey\",\"tags\":[\"a\"]}' | formcodec encode\n"
            "  formcodec decode --query 'name=joey&age=3' --cast age=int\n"
            "  formcodec path 'Address[0].Name'"
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("encode", help="JSON 对象编码成表单键值对")
    sp.add_argument("--file", help="输入 JSON 文件，省略则从 stdin 读")
    sp.add_argument("--query-string", action="store_true",
                    help="输出 URL query 串而非键值对列表")

    sp = sub.add_parser("decode", help="表单键值对解码成 JSON")
    sp.add_argument("--query", help="query 串或 form body，如 'a=1&b=2'")
    sp.add_argument("--file", help="含键值对的 JSON 文件")
    sp.add_argument("--cast", action="append", metavar="FIELD=TYPE",
                    help="字段类型转换，如 age=int，可重复")
    sp.add_argument("--raw", action="store_true",
                    help="保留原始键名，不展开成嵌套结构")

    sp = sub.add_parser("path", help="解析表单键路径")
    sp.add_argument("path", help="如 Address[0].Name 或 m[key]")

    return p


def _cast_map(items: list[str] | None) -> dict[str, Any]:
    """把 `--cast age=int` 解析成 {"age": int}。"""
    mapping = {
        "int": int, "integer": int, "float": float, "number": float,
        "bool": bool, "boolean": bool, "str": str, "string": str,
        "list": list, "array": list,
    }
    out: dict[str, Any] = {}
    for item in items or []:
        field, sep, type_name = item.partition("=")
        if not sep:
            raise CodecError(f"--cast 需要 FIELD=TYPE 格式，收到 {item!r}")
        key = type_name.strip().lower()
        if key not in mapping:
            raise CodecError(
                f"未知类型 {type_name!r}；可用：{sorted(mapping)}"
            )
        out[field.strip()] = mapping[key]
    return out


def _load_json(args: argparse.Namespace) -> Any:
    """从 --file 或 stdin 读 JSON。"""
    if args.file:
        try:
            with open(args.file, encoding="utf-8") as fh:
                return json.load(fh)
        except OSError as exc:
            raise CodecError(f"无法读取文件 {args.file!r}：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise CodecError(f"{args.file} 不是合法 JSON：{exc}") from exc
    text = sys.stdin.read().strip()
    if not text:
        raise CodecError("输入为空：请提供 --file 或通过 stdin 传入 JSON")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CodecError(f"stdin 不是合法 JSON：{exc}") from exc


def _load_pairs(args: argparse.Namespace) -> list[tuple[str, Any]]:
    """从 --query / --file / stdin 读键值对。"""
    if args.query:
        return query_string_to_form(args.query)
    if args.file:
        try:
            with open(args.file, encoding="utf-8") as fh:
                data = json.load(fh)
        except OSError as exc:
            raise CodecError(f"无法读取文件 {args.file!r}：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise CodecError(f"{args.file} 不是合法 JSON：{exc}") from exc
        if isinstance(data, dict):
            return list(data.items())
        if isinstance(data, list):
            return [(str(k), v) for k, v in data]
        raise CodecError("文件内容应为对象或 [[key, value], ...] 数组")
    text = sys.stdin.read().strip()
    if not text:
        raise CodecError("输入为空：请提供 --query / --file 或通过 stdin 传入")
    return query_string_to_form(text)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.command == "encode":
            data = _load_json(args)
            from .core import encode_form

            pairs = encode_form(data)
            if args.query_string:
                print(form_to_query_string(pairs))
            else:
                for k, v in pairs:
                    print(f"{k}={v}")
            return 0

        if args.command == "decode":
            pairs = _load_pairs(args)
            casts = _cast_map(args.cast)
            result: dict[str, Any] = (
                dict(pairs) if args.raw else unflatten(pairs)
            )
            # 类型转换只作用于顶层平铺键（嵌套结构由调用方自行处理）
            for field, tp in casts.items():
                if field in result and not isinstance(result[field], (dict, list)):
                    result[field] = decode_value(str(result[field]), tp, path=field)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0

        if args.command == "path":
            parsed = KeyPath.parse(args.path)
            print(json.dumps({
                "raw": parsed.raw,
                "root": parsed.root,
                "terminal": parsed.terminal,
                "indices": parsed.indices,
                "keys": parsed.keys,
                "depth": parsed.depth,
                "has_index": parsed.has_index,
                "fields": [t.name for t in parsed.tokens if t.kind == "field"],
            }, ensure_ascii=False, indent=2))
            return 0

    except (CodecError, PathError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"内部错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
