"""taskrun 命令行接口。

用法（对齐 runitor 的 `runitor [-flags] -- command` 风格）：

    taskrun -- echo hi
    taskrun --slug db-backup --every 2h24m -- /script/backup
    taskrun --slug backup --at "0 2,14 * * *" -- /script/backup
    taskrun --json -- pytest -q

也支持更贴近日常的子命令：

    taskrun run "echo hi"
    taskrun check "grep -q error log" --expect-exit-code 1
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .reporter import Ping, PingType, Reporter
from .runner import RunStatus, run, run_many
from .scheduler import CronSchedule, Scheduler, format_duration, parse_duration


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="taskrun",
        description="执行命令、判定成败并上报心跳",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  taskrun -- echo hi\n"
            "  taskrun --slug db-backup --every 2h24m -- /script/backup\n"
            "  taskrun check 'grep -c error log' --min 1"
        ),
    )
    p.add_argument("--slug", help="任务标识，用于心跳记录")
    p.add_argument("--every", help="按间隔循环执行，如 2h24m、90s")
    p.add_argument("--at", dest="cron", help="按 cron 表达式循环执行，如 '0 2 * * *'")
    p.add_argument("--timeout", type=float, help="单次执行超时秒数")
    p.add_argument("--max-runs", type=int, help="最多执行几次后退出")
    p.add_argument("--ping-url", help="心跳上报地址，留空则写本地文件")
    p.add_argument("--ping-file", help="心跳记录文件，默认 ~/.taskrun/pings.jsonl")
    p.add_argument("--no-output-in-ping", action="store_true",
                   help="心跳里不带命令输出（输出可能含敏感信息时用）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    p.add_argument("command", nargs=argparse.REMAINDER, help="要执行的命令（-- 之后）")
    return p


def _sub_parser(name: str) -> argparse.ArgumentParser:
    """构造子命令解析器。

    单独构造而不是挂在主 parser 上：主 parser 的 REMAINDER 位置参数会
    抢先把子命令名吃掉，argparse 随后报「invalid choice」。
    """
    p = argparse.ArgumentParser(prog=f"taskrun {name}")
    p.add_argument("cmd", help="要执行的命令")
    p.add_argument("--timeout", type=float, help="超时秒数")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    if name == "check":
        p.add_argument("--expect-exit-code", type=int, default=0)
        p.add_argument("--contains", help="期望 stdout 包含的子串")
        p.add_argument("--not-contains", help="期望 stdout 不包含的子串")
    return p


def _strip_separator(parts: list[str]) -> list[str]:
    """去掉 argparse.REMAINDER 里的前导 `--`。"""
    out = list(parts)
    if out and out[0] == "--":
        out = out[1:]
    return out


def _emit(payload: dict[str, Any], *, as_json: bool, text: str) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(text)


def _run_subcommand(name: str, args: argparse.Namespace) -> int:
    """处理 run / check 子命令。"""
    result = run(args.cmd, timeout=args.timeout)

    if name == "check":
        # check：校验退出码与输出子串
        problems: list[str] = []
        expect_code = getattr(args, "expect_exit_code", None)
        if expect_code is not None and result.exit_code != expect_code:
            problems.append(
                f"退出码期望 {expect_code}，实际 {result.exit_code}"
            )
        contains = getattr(args, "contains", None)
        if contains and contains not in result.stdout:
            problems.append(f"stdout 未包含 {contains!r}")
        not_contains = getattr(args, "not_contains", None)
        if not_contains and not_contains in result.stdout:
            problems.append(f"stdout 不应包含 {not_contains!r}")

        passed = not problems
        payload = {"passed": passed, "problems": problems, **result.to_dict()}
        text = ("✓ 通过" if passed else "✗ 失败：" + "；".join(problems))
        _emit(payload, as_json=args.json, text=text)
        return 0 if passed else 1

    _emit(result.to_dict(), as_json=args.json,
          text=result.stdout.rstrip() or result.verdict)
    return 0 if result.ok else 1


SUBCOMMANDS = ("run", "check")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # 手动拆分子命令：-- 之后的命令也是位置参数，argparse 无法区分
    # 「run 是子命令」还是「run 是要执行的命令」，只在首参精确命中时才当子命令。
    sub: str | None = None
    rest = argv
    if argv and argv[0] in SUBCOMMANDS:
        sub = argv[0]
        rest = argv[1:]

    if sub:
        args = _sub_parser(sub).parse_args(rest)
        return _run_subcommand(sub, args)

    args = build_parser().parse_args(rest)

    command = _strip_separator(args.command)
    if not command:
        build_parser().print_help()
        return 1

    # --every 与 --at 互斥
    if args.every and args.cron:
        print("错误：--every 与 --at 不能同时使用", file=sys.stderr)
        return 1

    reporter = None
    if args.slug:
        reporter = Reporter(
            args.slug,
            url=args.ping_url,
            log_path=args.ping_file,
            send_output=not args.no_output_in_ping,
        )

    # 单次执行（默认）
    if not args.every and not args.cron:
        if reporter:
            run_id = reporter.next_run_id()
            reporter.report_start(run_id)
        result = run(command, timeout=args.timeout)
        if reporter:
            reporter.report_result(result, run_id=run_id or "")
        _emit(result.to_dict(), as_json=args.json,
              text=result.stdout.rstrip() or result.verdict)
        # 原样透传子命令的退出码，方便在 shell 脚本里判断具体失败原因；
        # 超时（exit_code 为负）统一用 124，与 GNU timeout 的约定一致。
        if result.ok:
            return 0
        if result.status is RunStatus.TIMEOUT:
            return 124
        return result.exit_code if result.exit_code and result.exit_code > 0 else 1

    # 循环执行
    try:
        every = parse_duration(args.every) if args.every else None
        cron = CronSchedule.parse(args.cron) if args.cron else None
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    scheduler = Scheduler(
        command,
        every=every,
        cron=cron,
        timeout=args.timeout,
        reporter=reporter,
        max_runs=args.max_runs,
    )

    nxt = scheduler.next_run_at()
    if nxt and not args.json:
        label = (f"every {format_duration(every)}" if every
                 else f"cron {args.cron!r}")
        print(f"调度已启动：{label}，下次 {nxt.isoformat()}", file=sys.stderr)

    try:
        history = scheduler.run_forever()
    except KeyboardInterrupt:  # pragma: no cover - 交互中断
        history = scheduler.history

    if args.json:
        print(json.dumps({
            "total_runs": len(history),
            "results": [h.to_dict() for h in history],
        }, ensure_ascii=False, indent=2, default=str))
    else:
        failed = [h for h in history if not h.result.ok]
        print(f"共执行 {len(history)} 次，失败 {len(failed)} 次")
    return 1 if any(not h.result.ok for h in history) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
