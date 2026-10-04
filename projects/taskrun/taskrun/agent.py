"""taskrun 的 agent 工具接入层。

这是整个仓库里最贴合 agent 架构的一个：「执行 → 判定 → 上报」构成
可重试的闭环，agent 拿到的是结构化的成败结论，而不是一坨日志。
"""

from __future__ import annotations

import shlex
from typing import Any

from agent_core import ToolRegistry

from .reporter import Ping, PingType, Reporter
from .runner import RunResult, RunStatus, run, run_many
from .scheduler import CronSchedule, parse_duration


def register(registry: ToolRegistry) -> None:
    """把 taskrun 的能力注册进工具注册表。"""

    @registry.tool(
        name="taskrun_run",
        description=(
            "执行一条 shell 命令并返回结构化结果：退出码、stdout、stderr、"
            "耗时、明确的成功失败判定与一句话结论。命令超时会被强制终止。"
            "当需要真正跑一条命令来验证某件事（编译、测试、git 状态、"
            "文件是否存在）时使用，而不是凭记忆猜测结果。"
            "注意：命令由你决定后执行，请只运行用户明确要求的命令。"
        ),
        properties={
            "command": {
                "type": "string",
                "description": "要执行的命令行，例如 'pytest -q' 或 'git status --short'",
            },
            "timeout": {
                "type": ["number", "null"],
                "default": 60.0,
                "description": "超时秒数，默认 60；传 null 表示不限时",
            },
            "cwd": {
                "type": ["string", "null"],
                "default": None,
                "description": "工作目录，省略则用当前目录",
            },
        },
        retryable=True,
        timeout=300.0,
    )
    def taskrun_run(
        command: str,
        timeout: float | None = 60.0,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        if not command.strip():
            raise ValueError("command 不能为空")
        return run(command, timeout=timeout, cwd=cwd).to_dict()

    @registry.tool(
        name="taskrun_run_many",
        description=(
            "并发执行多条命令，全部返回各自的结果。适合同时检查多个条件"
            "（比如同时跑测试和 lint）。结果顺序与输入顺序一致。"
            "当多个检查彼此独立、可并行时用它，比串行快很多。"
        ),
        properties={
            "commands": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要执行的命令列表",
            },
            "timeout": {
                "type": ["number", "null"],
                "default": 60.0,
                "description": "每条命令的超时秒数",
            },
            "stop_on_failure": {
                "type": ["boolean", "null"],
                "default": False,
                "description": "任一失败时是否取消尚未开始的执行",
            },
        },
        retryable=True,
        timeout=300.0,
    )
    def taskrun_run_many(
        commands: list[str],
        timeout: float | None = 60.0,
        stop_on_failure: bool | None = None,
    ) -> dict[str, Any]:
        results = run_many(
            commands,
            timeout=timeout,
            stop_on_failure=bool(stop_on_failure),
        )
        failed = [r for r in results if not r.ok]
        return {
            "total": len(results),
            "failed": len(failed),
            "all_ok": not failed,
            "results": [r.to_dict() for r in results],
        }

    @registry.tool(
        name="taskrun_check",
        description=(
            "检查一个断言是否成立：命令退出码是否等于期望值，或输出是否"
            "包含/不包含某个子串。返回是否通过、具体哪里不满足，以及实际输出。"
            "当你想验证某个具体事实（某文件存在、某段文本出现、"
            "命令退出码是 1）时使用，比手动读输出更可靠。"
        ),
        properties={
            "command": {"type": "string", "description": "要执行的命令"},
            "expect_exit_code": {
                "type": ["integer", "null"],
                "default": 0,
                "description": "期望的退出码，传 null 表示不检查退出码",
            },
            "expect_stdout_contains": {
                "type": ["string", "null"],
                "default": None,
                "description": "期望 stdout 包含的子串",
            },
            "expect_stdout_not_contains": {
                "type": ["string", "null"],
                "default": None,
                "description": "期望 stdout 不包含的子串",
            },
        },
        retryable=True,
        timeout=300.0,
    )
    def taskrun_check(
        command: str,
        expect_exit_code: int | None = 0,
        expect_stdout_contains: str | None = None,
        expect_stdout_not_contains: str | None = None,
    ) -> dict[str, Any]:
        result = run(command, timeout=60.0)
        problems: list[str] = []

        if expect_exit_code is not None and result.exit_code != expect_exit_code:
            problems.append(
                f"退出码期望 {expect_exit_code}，实际 {result.exit_code}"
            )
        if expect_stdout_contains and expect_stdout_contains not in result.stdout:
            problems.append(f"stdout 未包含 {expect_stdout_contains!r}")
        if (
            expect_stdout_not_contains
            and expect_stdout_not_contains in result.stdout
        ):
            problems.append(
                f"stdout 不应包含 {expect_stdout_not_contains!r}，但出现了"
            )

        return {"passed": not problems, "problems": problems, **result.to_dict()}

    @registry.tool(
        name="taskrun_heartbeat",
        description=(
            "上报一次任务心跳并返回健康度摘要。用于定时任务监控："
            "「多久没有成功过了」是最值得关注的指标——定时任务没按时跑，"
            "往往比跑了但失败更严重。当你执行完一个周期性的维护、"
            "同步或清理任务后调用它来记录状态。"
        ),
        properties={
            "slug": {"type": "string", "description": "任务标识，如 db-backup"},
            "outcome": {
                "type": "string",
                "enum": ["success", "failure"],
                "default": "success",
                "description": "本次执行结果",
            },
            "detail": {
                "type": ["string", "null"],
                "default": None,
                "description": "补充说明，如输出摘要或错误信息",
            },
            "log_path": {
                "type": ["string", "null"],
                "default": None,
                "description": "心跳记录文件路径，默认 ~/.taskrun/pings.jsonl",
            },
        },
    )
    def taskrun_heartbeat(
        slug: str,
        outcome: str = "success",
        detail: str | None = None,
        log_path: str | None = None,
    ) -> dict[str, Any]:
        reporter = Reporter(slug, log_path=log_path)
        ping_type = PingType.SUCCESS if outcome == "success" else PingType.FAIL
        reporter.report(
            Ping(type=ping_type, slug=slug, output=detail or "")
        )
        return reporter.summary()

    @registry.tool(
        name="taskrun_schedule_info",
        description=(
            "解析调度表达式（间隔如 2h24m，或 5 段 cron）并算出下次触发时间，"
            "不实际执行任何命令。当需要确认一个定时配置是否合理、"
            "或回答「这个任务下次什么时候跑」时使用。"
        ),
        properties={
            "expression": {
                "type": "string",
                "description": "间隔（2h24m、90s）或 5 段 cron 表达式（0 2 * * *）",
            },
        },
    )
    def taskrun_schedule_info(expression: str) -> dict[str, Any]:
        from datetime import datetime, timedelta

        text = expression.strip()
        if len(text.split()) == 5:
            cron = CronSchedule.parse(text)
            nxt = cron.next_after(datetime.now())
            return {"kind": "cron", "expression": text, "next_run_at": nxt.isoformat()}
        seconds = parse_duration(text)
        nxt = datetime.now() + timedelta(seconds=seconds)
        return {
            "kind": "interval",
            "expression": text,
            "seconds": seconds,
            "next_run_at": nxt.isoformat(),
        }

    @registry.tool(
        name="taskrun_split_command",
        description=(
            "把命令行字符串按 shell 规则拆分为参数列表（处理引号与转义）。"
            "当需要把一条命令拆成结构化参数、或确认引号是否正确闭合时使用。"
        ),
        properties={
            "command": {"type": "string", "description": "命令行字符串"},
        },
    )
    def taskrun_split_command(command: str) -> dict[str, Any]:
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            return {"valid": False, "error": str(exc), "argv": []}
        return {"valid": True, "argv": parts, "count": len(parts)}


__all__ = ["register"]
