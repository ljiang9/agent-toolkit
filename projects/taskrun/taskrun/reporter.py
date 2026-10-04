"""心跳上报。

复刻自 runitor 的 healthchecks.io 集成，但**不绑定任何外部服务**：
默认写本地 JSON 文件（零依赖、离线可用），也可配置为 HTTP 上报。

上报的四种状态（对齐原项目语义）：
    start    任务开始
    success  成功
    fail     失败
    log      仅记录输出，不作判定

「死信开关」语义：定时任务若没有按时上报 start，就说明它根本没跑起来 ——
这比「跑了但失败」更值得告警。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .runner import RunResult


class PingType(Enum):
    """心跳类型（对齐 runitor 的 ping 语义）。"""

    START = "start"
    SUCCESS = "success"
    FAIL = "fail"
    LOG = "log"

    @classmethod
    def for_result(cls, result: RunResult) -> "PingType":
        """按执行结果推导应上报的类型。"""
        if result.status.value == "success":
            return cls.SUCCESS
        return cls.FAIL


@dataclass
class Ping:
    """一条心跳记录。"""

    type: PingType
    slug: str
    timestamp: float = field(default_factory=time.time)
    duration_ms: int | None = None
    exit_code: int | None = None
    output: str = ""
    error: str = ""
    run_id: str = ""
    # 关联的完整结果，便于事后复盘
    result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "type": self.type.value,
            "slug": self.slug,
            "timestamp": self.timestamp,
        }
        if self.duration_ms is not None:
            d["duration_ms"] = self.duration_ms
        if self.exit_code is not None:
            d["exit_code"] = self.exit_code
        if self.output:
            d["output"] = self.output
        if self.error:
            d["error"] = self.error
        if self.run_id:
            d["run_id"] = self.run_id
        return d

    @property
    def age_seconds(self) -> float:
        """距今多少秒。"""
        return time.time() - self.timestamp


class Reporter:
    """心跳上报器。

    默认后端是本地 JSON 文件 —— 不发任何网络请求，适合离线与测试。
    传 `url` 则切换到 HTTP 上报（healthchecks.io 或自建端点）。
    """

    def __init__(
        self,
        slug: str,
        *,
        url: str | None = None,
        log_path: str | None = None,
        timeout: float = 5.0,
        api_retries: int = 2,
        send_output: bool = True,
    ) -> None:
        self.slug = slug
        self.url = url
        self.timeout = timeout
        self.api_retries = max(0, api_retries)
        self.send_output = send_output
        # 未指定 url 时回落到本地文件
        self.log_path = log_path or os.path.join(
            os.path.expanduser("~"), ".taskrun", "pings.jsonl"
        )
        self._pings: list[Ping] = []
        self._counter = 0

    # ---------- 生成 run id ----------

    def next_run_id(self) -> str:
        """生成运行标识，用于把 start 与 end 关联起来。

        时间戳 + 序号，保证同一秒内多次运行也不会撞。
        """
        self._counter += 1
        return f"{int(time.time())}-{self._counter}"

    # ---------- 上报 ----------

    def report(self, ping: Ping) -> bool:
        """上报一条心跳。返回是否成功送达。"""
        self._pings.append(ping)
        if self.url:
            return self._send_http(ping)
        return self._write_local(ping)

    def _write_local(self, ping: Ping) -> bool:
        """追加到本地 JSONL 文件。"""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.log_path)), exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(ping.to_dict(), ensure_ascii=False) + "\n")
            return True
        except OSError:
            return False

    def _send_http(self, ping: Ping) -> bool:
        """HTTP 上报，瞬时错误按退避重试。"""
        # healthchecks.io 约定：UUID 或 slug 放在路径里，状态放在最后一段
        url = f"{self.url.rstrip('/')}/{ping.slug}/{ping.type.value}"
        payload = b""
        if self.send_output and (ping.output or ping.error):
            payload = (ping.output or ping.error).encode("utf-8")

        last_error: Exception | None = None
        for attempt in range(self.api_retries + 1):
            try:
                req = urllib.request.Request(url, data=payload or None, method="POST")
                req.add_header("User-Agent", "taskrun/0.1")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return 200 <= resp.status < 300
            except (urllib.error.URLError, OSError) as exc:
                last_error = exc
                if attempt < self.api_retries:
                    # 指数退避，避免把监控端点打垮
                    time.sleep(0.5 * (2 ** attempt))
        if last_error:
            return False
        return False

    # ---------- 便捷方法 ----------

    def report_start(self, run_id: str = "") -> bool:
        """上报开始。定时任务的重点监控对象。"""
        return self.report(Ping(type=PingType.START, slug=self.slug, run_id=run_id))

    def report_result(
        self,
        result: RunResult,
        *,
        run_id: str = "",
        include_output: bool | None = None,
    ) -> bool:
        """按执行结果上报成功/失败。"""
        should_send = self.send_output if include_output is None else include_output
        output = ""
        if should_send:
            output = result.stdout or result.stderr or ""
        return self.report(Ping(
            type=PingType.for_result(result),
            slug=self.slug,
            duration_ms=result.duration_ms,
            exit_code=result.exit_code,
            output=output,
            error="" if result.ok else result.verdict,
            run_id=run_id,
            result=result.to_dict(),
        ))

    def report_log(self, message: str, *, run_id: str = "") -> bool:
        """上报一条纯日志，不作成功失败判定。"""
        return self.report(Ping(
            type=PingType.LOG, slug=self.slug, output=message, run_id=run_id,
        ))

    # ---------- 查询 ----------

    @property
    def pings(self) -> list[Ping]:
        """本次会话内的心跳记录。"""
        return list(self._pings)

    def last(self, ping_type: PingType | None = None) -> Ping | None:
        """最近一条心跳，可按类型过滤。"""
        for ping in reversed(self._pings):
            if ping_type is None or ping.type is ping_type:
                return ping
        return None

    def summary(self) -> dict[str, Any]:
        """健康度摘要 —— 「多久没成功」是最值得关注的指标。"""
        last_success = self.last(PingType.SUCCESS)
        last_any = self.last()
        return {
            "slug": self.slug,
            "total_pings": len(self._pings),
            "counts": {
                t.value: sum(1 for p in self._pings if p.type is t)
                for t in PingType
            },
            "last_ping_age_seconds": (
                round(last_any.age_seconds, 1) if last_any else None
            ),
            "last_success_age_seconds": (
                round(last_success.age_seconds, 1) if last_success else None
            ),
            # 24 小时内成功过就算健康
            "healthy": bool(
                last_success is not None and last_success.age_seconds < 86400
            ),
        }


def load_pings(log_path: str, *, limit: int = 100) -> list[dict[str, Any]]:
    """从本地 JSONL 读回历史心跳。"""
    if not os.path.exists(log_path):
        return []
    out: list[dict[str, Any]] = []
    with open(log_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out[-limit:]
