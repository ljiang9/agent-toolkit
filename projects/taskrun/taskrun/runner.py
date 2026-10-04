"""子进程执行与结果捕获。

复刻自 bdd/runitor（https://github.com/bdd/runitor）的核心：执行命令、
捕获输出、按退出码判定成败。

**超时的处理方式**：Python 无法真正杀死一个已启动的线程或子进程。这里用
`Popen.kill()` 终止整个进程组，并对「超时后任务是否真的停了」给出明确结论，
而不是假装成功——这对应 agent 架构里「结果校验」那一步。
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RunStatus(Enum):
    """一次执行的结果状态。"""

    SUCCESS = "success"      # 退出码 0
    FAIL = "fail"            # 退出码非 0
    TIMEOUT = "timeout"      # 超时被终止
    SPAWN_ERROR = "spawn_error"  # 命令无法启动（不存在、无权限等）

    @property
    def ok(self) -> bool:
        return self is RunStatus.SUCCESS


@dataclass
class RunResult:
    """一次执行的完整结果。

    这是 agent 做「结果校验」的依据 —— 不看输出猜测，只看结构化的
    exit_code / status / duration。
    """

    status: RunStatus
    command: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    # 进程是否确实被终止（超时后可能仍在跑，需要如实告知）
    killed: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is RunStatus.SUCCESS

    @property
    def verdict(self) -> str:
        """一句话结论，便于直接回传给 LLM。"""
        if self.status is RunStatus.SUCCESS:
            return "命令执行成功"
        if self.status is RunStatus.TIMEOUT:
            base = f"命令超时（{self.duration_ms}ms）"
            if not self.killed:
                base += "；注意：超时后未能确认进程已终止，结果状态未知"
            return base
        if self.status is RunStatus.SPAWN_ERROR:
            return f"命令无法启动：{self.stderr.strip() or '未知原因'}"
        return f"命令失败，退出码 {self.exit_code}"

    def to_dict(self, *, max_output: int = 4000) -> dict[str, Any]:
        """转 dict。输出过长时截断并注明，避免撑爆上下文。"""
        out: dict[str, Any] = {
            "status": self.status.value,
            "ok": self.ok,
            "command": self.command,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "verdict": self.verdict,
        }
        for name, text in (("stdout", self.stdout), ("stderr", self.stderr)):
            if len(text) <= max_output:
                out[name] = text
            else:
                out[name] = text[:max_output] + f"\n...（已截断，原长度 {len(text)} 字符）"
        if self.timed_out:
            out["timed_out"] = True
            out["killed"] = self.killed
        if self.meta:
            out["meta"] = self.meta
        return out


class TimeoutState:
    """超时后判断进程是否真的停了。

    思路：kill 之后短暂等待并轮询 `poll()`；若进程变成僵尸说明已被 reap，
    仍为 None 说明它 ignoring 了 SIGKILL（极少见但可能，如处于不可中断 IO）。
    """

    @staticmethod
    def confirm_terminated(proc: subprocess.Popen, *, grace: float = 0.5) -> bool:
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return True
            time.sleep(0.02)
        return proc.poll() is not None


def _kill_process_tree(proc: subprocess.Popen) -> bool:
    """终止子进程及其后代。

    先杀整个进程组（`start_new_session=True` 让子进程自成一组），
    这样孙进程不会变孤儿继续跑。
    """
    killed = False
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        killed = True
    except (ProcessLookupError, PermissionError, OSError):
        # 进程组不可用时退回单进程 kill
        try:
            proc.kill()
            killed = True
        except (ProcessLookupError, OSError):
            pass
    return killed


def run(
    command: str | list[str],
    *,
    timeout: float | None = 60.0,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    shell: bool = False,
    inherit_env: bool = True,
    max_output_bytes: int = 1024 * 1024,
) -> RunResult:
    """执行一条命令并捕获输出。

    参数
    ----
    command:
        字符串（经 shell 解析）或参数列表。**推荐用列表** ——
        字符串会交给 shell，带元字符时存在注入风险。
    timeout:
        秒。None 表示不限。超时后强杀进程组。
    max_output_bytes:
        单流输出上限，超出部分丢弃但记录截断标记——防止一条
        `cat 大文件` 塞爆内存。

    字符串命令找不到时，shell 自身会返回 127 而不是抛 FileNotFoundError，
    因此这类情况被判为 FAIL 而非 SPAWN_ERROR。SPAWN_ERROR 只留给
    「连 shell 都起不来」或列表形式下程序不存在的情况。
    """
    cmd_str = command if isinstance(command, str) else " ".join(command)
    argv: Any = command if not isinstance(command, str) else command

    # 环境：默认继承，再叠加覆盖项
    run_env = dict(os.environ) if inherit_env else {}
    if env:
        run_env.update(env)

    started = time.monotonic()
    try:
        proc = subprocess.Popen(  # noqa: S603 - 命令由调用方决定
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,  # 不给输入，避免任务卡在读 stdin 上
            cwd=cwd,
            env=run_env,
            shell=shell or isinstance(command, str),
            text=True,
            errors="replace",  # 非 UTF-8 输出不应让整个任务崩掉
            start_new_session=True,  # 自成进程组，便于整组 kill
        )
    except (OSError, ValueError) as exc:
        return RunResult(
            status=RunStatus.SPAWN_ERROR,
            command=cmd_str,
            exit_code=127,
            stderr=f"{type(exc).__name__}: {exc}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    timed_out = False
    killed = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        killed = _kill_process_tree(proc)
        try:
            # kill 后再收一次输出，拿到的可能不完整但有诊断价值
            stdout, stderr = proc.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:  # pragma: no cover - 极罕见
            stdout, stderr = "", ""

    duration_ms = int((time.monotonic() - started) * 1000)
    confirmed = TimeoutState.confirm_terminated(proc) if timed_out else True

    out = (stdout or "")
    err = (stderr or "")
    truncated = False
    if len(out) > max_output_bytes:
        out = out[:max_output_bytes]
        truncated = True
    if len(err) > max_output_bytes:
        err = err[:max_output_bytes]
        truncated = True

    if timed_out:
        status = RunStatus.TIMEOUT
    elif proc.returncode == 0:
        status = RunStatus.SUCCESS
    else:
        status = RunStatus.FAIL

    result = RunResult(
        status=status,
        command=cmd_str,
        exit_code=proc.returncode,
        stdout=out,
        stderr=err,
        duration_ms=duration_ms,
        timed_out=timed_out,
        killed=confirmed if timed_out else killed,
    )
    if truncated:
        result.meta["output_truncated"] = True
    return result


def run_many(
    commands: list[str | list[str]],
    *,
    timeout: float | None = 60.0,
    stop_on_failure: bool = False,
    max_workers: int = 4,
    **kwargs: Any,
) -> list[RunResult]:
    """并发执行多条命令。

    stop_on_failure 为 True 时，任一失败就取消尚未开始的执行
    （已启动的会跑完，因为强杀正在运行的进程可能留下副作用）。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not commands:
        return []
    if len(commands) == 1 or max_workers <= 1:
        results = []
        for cmd in commands:
            r = run(cmd, timeout=timeout, **kwargs)
            results.append(r)
            if stop_on_failure and not r.ok:
                break
        return results

    results: list[RunResult] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(run, c, timeout=timeout, **kwargs): c for c in commands}
        for future in as_completed(futures):
            results.append(future.result())
            if stop_on_failure and not results[-1].ok:
                for pending in futures:
                    pending.cancel()
                break
    # 按输入顺序返回，便于调用方对应
    order = {str(c): i for i, c in enumerate(commands)}
    results.sort(key=lambda r: order.get(r.command, 0))
    return results
