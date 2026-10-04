"""终端渲染。

对齐原项目 draw.c 的多种渲染风格，但用纯 ANSI 转义实现，不依赖 termbox
（原项目为了跨平台引入了 117KB 的 termbox.h）。

**双缓冲**是这里的关键：逐帧重画整屏会导致肉眼可见的闪烁。
做法是先构造完整帧字符串，与上一帧比较，只有变化的部分才输出——
既消除闪烁，也大幅减少终端 IO 压力。
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field

from .game import Game, Status
from .grid import Cell, Direction, Point

# ANSI 控制序列
CSI = "\x1b["
HIDE_CURSOR = f"{CSI}?25l"
SHOW_CURSOR = f"{CSI}?25h"
CLEAR_SCREEN = f"{CSI}2J"
CURSOR_HOME = f"{CSI}H"
RESET = f"{CSI}0m"


@dataclass
class Theme:
    """渲染主题（字符画 + 颜色）。"""

    wall: str = "#"
    head: str = "@"
    body: str = "o"
    food: str = "*"
    empty: str = " "
    # 可选颜色；为 None 则不输出颜色序列
    color_wall: str | None = "90"
    color_head: str | None = "93"
    color_body: str | None = "92"
    color_food: str | None = "91"
    use_color: bool = True

    @classmethod
    def ascii_plain(cls) -> "Theme":
        """纯 ASCII 无色，适合日志与管道。"""
        return cls(use_color=False)

    @classmethod
    def blocks(cls) -> "Theme":
        """方块风格，视觉更接近原版。"""
        return cls(wall="█", head="█", body="▓", food="◆", empty=" ")


@dataclass
class Renderer:
    """把 Game 渲染成文本帧，并维护双缓冲状态。"""

    theme: Theme = field(default_factory=Theme)
    show_grid: bool = False
    _previous: str | None = field(default=None, init=False)
    _last_width: int = 0
    _last_height: int = 0

    def _color(self, text: str, code: str | None) -> str:
        if not self.theme.use_color or code is None:
            return text
        return f"{CSI}{code}m{text}{RESET}"

    def frame(self, game: Game, *, hud: bool = True) -> str:
        """构造完整的一帧文本。"""
        grid = game.grid
        snake_cells = set(game.snake.body)
        head = game.snake.head

        lines: list[str] = []
        for y in range(grid.height):
            row: list[str] = []
            for x in range(grid.width):
                p = Point(x, y)
                if game.grid.is_wall(x, y):
                    row.append(self._color(self.theme.wall, self.theme.color_wall))
                elif p == head:
                    row.append(self._color(self.theme.head, self.theme.color_head))
                elif p in snake_cells:
                    row.append(self._color(self.theme.body, self.theme.color_body))
                elif game.food is not None and p == game.food:
                    row.append(self._color(self.theme.food, self.theme.color_food))
                else:
                    row.append(self.theme.empty)
            lines.append("".join(row))

        if self.show_grid:
            # 标尺行不编行号，但要用等宽的留白与下方行对齐，
            # 否则标尺会整体右移 2 格，与实际列对不上。
            ruler = "  " + "".join(str(x % 10) for x in range(grid.width))
            labelled = [f"{y % 10} {line}" for y, line in enumerate(lines)]
            lines = [ruler, *labelled]

        if hud:
            lines.extend(self._hud_lines(game))

        return "\n".join(lines)

    def _hud_lines(self, game: Game) -> list[str]:
        """底部状态栏。"""
        status_text = {
            Status.RUNNING: "运行中",
            Status.WON: "胜利：棋盘已填满",
            Status.DIED: "结束：撞上了",
            Status.PAUSED: "已暂停",
        }[game.status]
        info = (
            f" 长度 {len(game.snake)} · 得分 {game.score} · 步数 {game.steps} "
            f"· {status_text}"
        )
        return ["", info]

    # ---------- 双缓冲输出 ----------

    @staticmethod
    def terminal_size(fallback: tuple[int, int] = (80, 24)) -> tuple[int, int]:
        try:
            size = shutil.get_terminal_size(fallback)
            return size.columns, size.lines
        except Exception:  # noqa: BLE001 - 非终端环境
            return fallback

    def diff(self, frame: str) -> str:
        """与上一帧比较，只返回需要输出的差异部分。

        帧完全相同时返回空串——调用方可以据此跳过 IO。
        """
        if frame == self._previous:
            return ""
        self._previous = frame
        return frame

    def reset(self) -> None:
        """清空缓冲状态（切换尺寸或游戏结束后调用）。"""
        self._previous = None
        self._last_width = 0
        self._last_height = 0

    def draw(self, frame: str, *, stream=None) -> None:
        """输出整帧（带光标归位，适合非 TTY 场景如日志）。"""
        out = stream or sys.stdout
        out.write(CLEAR_SCREEN + CURSOR_HOME + frame + "\n")
        out.flush()

    def live(self, frame: str, *, stream=None) -> None:
        """实时模式下只输出变化部分，避免闪烁。"""
        out = stream or sys.stdout
        chunk = self.diff(frame)
        if not chunk:
            return
        out.write(CURSOR_HOME + chunk + "\n")
        out.flush()

    def enter_live_mode(self, *, stream=None) -> None:
        out = stream or sys.stdout
        out.write(HIDE_CURSOR + CLEAR_SCREEN + CURSOR_HOME)
        out.flush()
        self.reset()

    def exit_live_mode(self, *, stream=None) -> None:
        """退出时务必恢复光标，否则终端会一直不可见。"""
        out = stream or sys.stdout
        out.write(SHOW_CURSOR + CURSOR_HOME)
        out.flush()
        self.reset()


def render_once(game: Game, *, theme: Theme | None = None,
                hud: bool = True, show_grid: bool = False) -> str:
    """一次性渲染成字符串（供测试与 agent 工具使用，不做任何终端 IO）。"""
    return Renderer(theme=theme or Theme(), show_grid=show_grid).frame(game, hud=hud)
