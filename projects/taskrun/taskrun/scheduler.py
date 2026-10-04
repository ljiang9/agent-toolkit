"""任务调度：间隔与 Cron 两种模式。

复刻自 runitor 的 `-every` 与 `-at` 两种调度方式，并实现一个零依赖的
Cron 表达式解析器（字段：分 时 日 月 周）。

调度器只用标准库：计算下次触发时间，不引入 APScheduler 之类的依赖。
"""

from __future__ import annotations

import re
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from .runner import RunResult, run

# ---------- 间隔解析 ----------

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> float:
    """把 `2h24m`、`90s`、`500ms` 解析成秒。

    >>> parse_duration("2h24m")
    8640.0
    >>> parse_duration("90s")
    90.0
    """
    value = text.strip()
    if not value:
        raise ValueError("时长不能为空")
    total = 0.0
    matched = False
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)", value):
        total += float(amount) * _UNIT_SECONDS[unit]
        matched = True
    if not matched:
        # 纯数字按秒处理
        try:
            return float(value)
        except ValueError:
            raise ValueError(f"无法解析时长 {text!r}；示例：2h24m、90s、500ms") from None
    return total


def format_duration(seconds: float) -> str:
    """秒转回可读时长。

    >>> format_duration(8640)
    '2h24m0s'
    """
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m}m{s}s"
    if m:
        return f"{m}m{s}s"
    return f"{s}s"


# ---------- Cron 解析 ----------

_FIELD_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
_FIELD_NAMES = ["minute", "hour", "day", "month", "weekday"]


@dataclass
class CronSchedule:
    """Cron 表达式。

    支持 `*`、`,`、`-`、`/` 四种语法，覆盖标准 cron 的常见用法。
    """

    expression: str
    minutes: set[int]
    hours: set[int]
    days: set[int]
    months: set[int]
    weekdays: set[int]

    @classmethod
    def parse(cls, expression: str) -> "CronSchedule":
        """解析 5 段 cron 表达式。

        >>> c = CronSchedule.parse("*/15 * * * *")
        >>> sorted(c.minutes)[:4]
        [0, 15, 30, 45]
        >>> CronSchedule.parse("0 2,14 * * *").hours == {2, 14}
        True
        """
        parts = expression.split()
        if len(parts) != 5:
            raise ValueError(
                f"Cron 表达式需要 5 段（分 时 日 月 周），收到 {len(parts)} 段：{expression!r}"
            )
        sets = [_parse_field(p, lo, hi, name)
                for p, (lo, hi), name in zip(parts, _FIELD_RANGES, _FIELD_NAMES)]
        return cls(expression, *sets)

    def matches(self, moment: datetime) -> bool:
        """某个时刻是否命中。

        日与周同时受限时按 cron 惯例取**或**（任一匹配即触发）。
        """
        dow = (moment.weekday() + 1) % 7  # Python 周一=0 -> cron 周日=0
        return (
            moment.minute in self.minutes
            and moment.hour in self.hours
            and moment.month in self.months
            and (moment.day in self.days or dow in self.weekdays)
        )

    def next_after(self, moment: datetime | None = None) -> datetime:
        """下一次触发时间。

        从当前分钟的下一秒开始逐分钟查找；最多往后找 4 年，
        超过说明这个表达式永远不会触发（应报错而非死循环）。
        """
        start = (moment or datetime.now()).replace(second=0, microsecond=0)
        candidate = start + timedelta(minutes=1)
        limit = start + timedelta(days=366 * 4)
        while candidate <= limit:
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        raise ValueError(f"Cron 表达式 {self.expression!r} 在未来 4 年内不会触发")


def _parse_field(text: str, lo: int, hi: int, name: str) -> set[int]:
    """解析单个 cron 字段，支持 `*`、`,`、`-`、`/`。"""
    values: set[int] = set()
    for chunk in text.split(","):
        step = 1
        if "/" in chunk:
            chunk, _, step_text = chunk.partition("/")
            try:
                step = int(step_text)
            except ValueError:
                raise ValueError(f"{name} 字段的步长非法：{step_text!r}") from None
            if step < 1:
                raise ValueError(f"{name} 字段的步长必须为正，收到 {step}")
        if chunk in ("*", ""):
            start, end = lo, hi
        elif "-" in chunk.lstrip("-"):
            start_text, _, end_text = chunk.partition("-")
            try:
                start, end = int(start_text), int(end_text)
            except ValueError:
                raise ValueError(f"{name} 字段区间非法：{chunk!r}") from None
        else:
            try:
                start = end = int(chunk)
            except ValueError:
                raise ValueError(f"{name} 字段值非法：{chunk!r}") from None

        if start < lo or end > hi or start > end:
            raise ValueError(
                f"{name} 字段超出范围 [{lo}, {hi}] 或区间反向：{chunk!r}"
            )
        values.update(range(start, end + 1, step))
    return values


# ---------- 调度器 ----------

@dataclass
class ScheduledRun:
    """一次调度执行的结果。"""

    run_id: str
    result: RunResult
    started_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            **self.result.to_dict(),
        }


class Scheduler:
    """按间隔或 Cron 调度任务。

    与原项目一致，支持两类用法：
    1. **一次性**：跑一次就结束（最常见，配合外部 cron）
    2. **长驻**：`-every 2h` 或 `-at "0 2 * * *"` 循环执行，
       收到 SIGALRM 时立刻触发一次并重置间隔（便于手动强制执行）
    """

    def __init__(
        self,
        command: str | list[str],
        *,
        every: float | None = None,
        cron: CronSchedule | None = None,
        timeout: float | None = None,
        reporter: Any = None,
        on_result: Callable[[ScheduledRun], None] | None = None,
        max_runs: int | None = None,
    ) -> None:
        if (every is None) == (cron is None):
            raise ValueError("every 与 cron 必须且只能指定一个")
        self.command = command
        self.every = every
        self.cron = cron
        self.timeout = timeout
        self.reporter = reporter
        self.on_result = on_result
        self.max_runs = max_runs
        self._stop = False
        self._trigger_now = False
        self.history: list[ScheduledRun] = []

    # ---------- 信号处理 ----------

    def _install_signal_handlers(self) -> None:
        """SIGALRM 触发立即执行，SIGTERM/SIGINT 优雅退出。

        对齐原项目：不必重启进程就能强制跑一次。
        """
        def _alarm(_signum: int, _frame: Any) -> None:
            self._trigger_now = True

        def _term(_signum: int, _frame: Any) -> None:
            self._stop = True

        for sig, handler in (
            (signal.SIGALRM, _alarm),
            (signal.SIGTERM, _term),
            (signal.SIGINT, _term),
        ):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError, AttributeError):
                # 非主线程或平台不支持时跳过——不能因此崩掉
                pass

    def request_stop(self) -> None:
        self._stop = True

    def trigger_now(self) -> None:
        """请求立刻执行一次（不阻塞）。"""
        self._trigger_now = True

    # ---------- 执行 ----------

    def _execute_once(self) -> ScheduledRun:
        run_id = ""
        if self.reporter is not None:
            run_id = self.reporter.next_run_id()
            self.reporter.report_start(run_id)
        started = time.time()
        result = run(self.command, timeout=self.timeout)
        scheduled = ScheduledRun(run_id=run_id, result=result, started_at=started)
        if self.reporter is not None:
            self.reporter.report_result(result, run_id=run_id)
        self.history.append(scheduled)
        if self.on_result is not None:
            self.on_result(scheduled)
        return scheduled

    def run_once(self) -> ScheduledRun:
        """执行一次并返回结果。"""
        return self._execute_once()

    def run_forever(self) -> list[ScheduledRun]:
        """长驻运行，直到收到停止信号或达到 max_runs。"""
        self._install_signal_handlers()
        count = 0
        while not self._stop:
            if self.max_runs is not None and count >= self.max_runs:
                break
            self._execute_once()
            count += 1
            if self.max_runs is not None and count >= self.max_runs:
                break
            # 睡到下次触发，期间可被信号打断
            self._sleep_until_next()
        return self.history

    def _sleep_until_next(self) -> None:
        """睡到下次触发。用短间隔轮询，保证信号能及时响应。"""
        delay = self._seconds_until_next()
        deadline = time.monotonic() + delay
        while not self._stop and time.monotonic() < deadline:
            if self._trigger_now:
                self._trigger_now = False
                return  # 立刻再跑一轮
            # 最多睡 0.5s 一次，保证 SIGALRM 能被及时处理
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
        self._trigger_now = False

    def _seconds_until_next(self) -> float:
        if self.every is not None:
            return max(0.0, self.every)
        assert self.cron is not None
        nxt = self.cron.next_after(datetime.now())
        return max(0.0, (nxt - datetime.now()).total_seconds())

    def next_run_at(self) -> datetime | None:
        """下次触发时间（间隔模式下用 now+every 表示）。"""
        if self.every is not None:
            return datetime.now() + timedelta(seconds=self.every)
        if self.cron is not None:
            return self.cron.next_after(datetime.now())
        return None
