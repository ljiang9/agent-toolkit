"""taskrun —— 子进程执行、结果判定与心跳上报。

复刻自 bdd/runitor（https://github.com/bdd/runitor，0BSD），补上零依赖的
Cron 解析、agent 工具接入与更完整的结构化结果。

核心契约：**执行 → 判定 → 上报** 三步形成可重试闭环。
超时不会被伪装成成功，而是如实报告「超时且进程是否确实终止」。

>>> from taskrun import run
>>> r = run("echo hi")
>>> r.ok, r.stdout.strip()
(True, 'hi')
>>> run("exit 3").exit_code
3
"""

from __future__ import annotations

from .reporter import Ping, PingType, Reporter, load_pings
from .runner import (
    RunResult,
    RunStatus,
    TimeoutState,
    run,
    run_many,
)
from .scheduler import (
    CronSchedule,
    ScheduledRun,
    Scheduler,
    format_duration,
    parse_duration,
)

__version__ = "0.1.0"

__all__ = [
    "CronSchedule",
    "Ping",
    "PingType",
    "Reporter",
    "RunResult",
    "RunStatus",
    "ScheduledRun",
    "Scheduler",
    "TimeoutState",
    "format_duration",
    "load_pings",
    "parse_duration",
    "run",
    "run_many",
    "__version__",
]
