"""网格与坐标基础。

对齐原项目 xymap.h / structs.h 的设计：网格用二维数组表示，格子有类型标记，
坐标以 (x, y) 表示，原点可配置（蛇头出生在中心或左上）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Cell(Enum):
    """格子类型。"""

    EMPTY = " "
    WALL = "#"
    SNAKE = "S"
    FOOD = "@"
    HEAD = "H"


class Direction(Enum):
    """四个基本方向。"""

    NORTH = (0, -1)
    SOUTH = (0, 1)
    EAST = (1, 0)
    WEST = (-1, 0)

    @property
    def delta(self) -> tuple[int, int]:
        return self.value

    @property
    def opposite(self) -> "Direction":
        return _OPPOSITE[self]

    def rotate_clockwise(self) -> "Direction":
        """顺时针转 90°，用于沙盘模式的边界转向策略。"""
        return _ROTATE_CW[self]


_OPPOSITE = {
    Direction.NORTH: Direction.SOUTH,
    Direction.SOUTH: Direction.NORTH,
    Direction.EAST: Direction.WEST,
    Direction.WEST: Direction.EAST,
}

_ROTATE_CW = {
    Direction.NORTH: Direction.EAST,
    Direction.EAST: Direction.SOUTH,
    Direction.SOUTH: Direction.WEST,
    Direction.WEST: Direction.NORTH,
}


@dataclass(frozen=True)
class Point:
    """坐标点。"""

    x: int
    y: int

    def __add__(self, other: "Point") -> "Point":
        return Point(self.x + other.x, self.y + other.y)

    def move(self, direction: Direction) -> "Point":
        dx, dy = direction.delta
        return Point(self.x + dx, self.y + dy)

    def manhattan(self, other: "Point") -> int:
        return abs(self.x - other.x) + abs(self.y - other.y)


class Grid:
    """带边界的二维网格。

    边界视为墙，因此路径搜索不必单独判断越界——这与原项目 is_valid/is_unblocked
    的检查合并到一处，减少出错面。
    """

    def __init__(self, width: int, height: int) -> None:
        if width < 3 or height < 3:
            raise ValueError(f"网格尺寸过小：{width}x{height}，至少需要 3x3")
        self.width = width
        self.height = height
        # 用 0/1 表示墙，比存字符串省内存也更快
        self._walls: set[tuple[int, int]] = set()

    # ---------- 边界与墙 ----------

    def in_bounds(self, x: int, y: int) -> bool:
        """坐标是否在网格内（不含墙）。"""
        return 0 <= x < self.width and 0 <= y < self.height

    def is_wall(self, x: int, y: int) -> bool:
        """越界一律视为墙。"""
        if not self.in_bounds(x, y):
            return True
        return (x, y) in self._walls

    def add_wall(self, x: int, y: int) -> None:
        if self.in_bounds(x, y):
            self._walls.add((x, y))

    def remove_wall(self, x: int, y: int) -> None:
        self._walls.discard((x, y))

    def add_border_walls(self) -> None:
        """显式画出四周的墙（用 WALL 字符可见，内部逻辑本就把越界当墙）。"""
        for x in range(self.width):
            self.add_wall(x, 0)
            self.add_wall(x, self.height - 1)
        for y in range(self.height):
            self.add_wall(0, y)
            self.add_wall(self.width - 1, y)

    def wall_positions(self) -> set[tuple[int, int]]:
        """返回所有显式墙的坐标（快照还原时用）。"""
        return set(self._walls)

    # ---------- 查询 ----------

    def free_cells(self) -> list[Point]:
        """所有非墙格子。"""
        out = []
        for y in range(self.height):
            for x in range(self.width):
                if not self.is_wall(x, y):
                    out.append(Point(x, y))
        return out

    def __repr__(self) -> str:
        return f"Grid({self.width}x{self.height}, walls={len(self._walls)})"
