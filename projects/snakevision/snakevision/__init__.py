"""snakevision —— 会自己玩的终端贪吃蛇，兼网格寻路演示器。

复刻自 AngelJumbo 的 sssnake（https://github.com/AngelJumbo/sssnake），
用纯 Python 重写，去掉了原项目 117KB 的 termbox 依赖，改为纯 ANSI 渲染。

相比原项目的关键改进：贪心寻路必然把自己逼进死角，所以加了三级安全策略
（贴尾 → 保命 → 多步模拟），实测存活步数显著高于朴素 BFS。

>>> from snakevision import Game, run_autopilot
>>> g = Game.create(15, 10, seed=42)
>>> g = run_autopilot(g, max_steps=200)
>>> g.steps > 0
True
"""

from __future__ import annotations

from .autopilot import (
    path_to_food,
    plan_direction,
    run_autopilot,
    safe_directions,
    survival_probability,
)
from .game import Game, Snake, Status
from .grid import Cell, Direction, Grid, Point
from .pathfind import astar_path, bfs_path, find_path
from .render import Renderer, Theme, render_once

__version__ = "0.1.0"

__all__ = [
    "Cell",
    "Direction",
    "Game",
    "Grid",
    "Point",
    "Renderer",
    "Snake",
    "Status",
    "Theme",
    "astar_path",
    "bfs_path",
    "find_path",
    "path_to_food",
    "plan_direction",
    "render_once",
    "run_autopilot",
    "safe_directions",
    "survival_probability",
    "__version__",
]
