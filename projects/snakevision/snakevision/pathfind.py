"""路径搜索：A* 与 BFS。

复刻自原项目 autopilot.c —— 它的 short_path 函数指针可在 a_star_search /
breadth_first_search 之间切换，这里保持同样的可替换设计。

原项目的 is_unblocked 有个关键细节：checkBody 参数决定是否把蛇身当障碍。
本模块把它显式化为 blocked 参数，因为「找路时看不见自己」和「找路时避开自己」
是两种完全不同的语义，混在一起容易出隐蔽 bug。
"""

from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass, field

from .grid import Direction, Grid, Point

# 四方向移动代价均为 1；斜向不参与（保持与原项目一致的四邻域）
ORTHOGONAL: tuple[tuple[int, int], ...] = (
    (0, -1),
    (0, 1),
    (1, 0),
    (-1, 0),
)


@dataclass(order=True)
class _Node:
    """优先队列元素。用 order=True 让 heapq 直接按 f 值排序。"""

    f: float
    counter: int  # 打破同 f 值的平局，保证结果可复现
    g: int = field(compare=False)
    point: Point = field(compare=False)
    parent: "Point | None" = field(compare=False, default=None)


def _neighbors(grid: Grid, point: Point, blocked: set[Point]) -> list[Point]:
    """四邻域中可通行的格子。

    注意：blocked 里存的是 Point 对象，查询时必须构造 Point 再比较。
    若图省事写成 `(nx, ny) in blocked`（元组 vs Point），判断会恒为 False，
    blocked 就被静默忽略——这类 bug 很难靠读代码发现，必须靠测试兜住。
    """
    out = []
    for dx, dy in ORTHOGONAL:
        nx, ny = point.x + dx, point.y + dy
        if grid.is_wall(nx, ny):
            continue
        if Point(nx, ny) in blocked:
            continue
        out.append(Point(nx, ny))
    return out


def bfs_path(
    grid: Grid,
    start: Point,
    goal: Point,
    blocked: set[Point] | None = None,
) -> list[Point]:
    """广度优先搜索，返回从 start 到 goal 的完整路径（含终点，不含起点）。

    无解时返回空列表。BFS 保证在四邻域、单位代价下图上的最短性。

    >>> g = Grid(5, 5)
    >>> path = bfs_path(g, Point(0, 0), Point(3, 0))
    >>> len(path), path[-1]
    (3, Point(3, 0))
    >>> bfs_path(g, Point(0, 0), Point(0, 0))
    [Point(0, 0)]
    """
    blocked = blocked or set()
    if grid.is_wall(goal.x, goal.y) or goal in blocked:
        return []
    # 起点本身不可通行时直接判无解。若不查起点，搜索会从 blocked 的起点
    # 出发继续扩散，等于把 blocked 降格成「只约束落脚点」，语义不对。
    if grid.is_wall(start.x, start.y) or start in blocked:
        return []
    if start == goal:
        return [start]

    came_from: dict[Point, Point] = {}
    queue: deque[Point] = deque([start])
    seen: set[Point] = {start}

    while queue:
        current = queue.popleft()
        if current == goal:
            return _reconstruct(came_from, current)
        for nxt in _neighbors(grid, current, blocked):
            if nxt in seen:
                continue
            seen.add(nxt)
            came_from[nxt] = current
            queue.append(nxt)
    return []


def astar_path(
    grid: Grid,
    start: Point,
    goal: Point,
    blocked: set[Point] | None = None,
    *,
    weighted: bool = False,
) -> list[Point]:
    """A* 搜索。

    参数
    ----
    weighted:
        为 True 时用曼哈顿距离 × 1.2 作为启发值（原项目的 try-hard 行为）。
        ×1.2 而非 ×1 是为了在四邻域图上**略微高估**启发，
        换取「更快找到路径、偶尔不是最短路」的取舍——原项目用它做兜底尝试。
    """
    blocked = blocked or set()
    if grid.is_wall(goal.x, goal.y) or goal in blocked:
        return []
    if grid.is_wall(start.x, start.y) or start in blocked:
        return []
    if start == goal:
        return [start]

    def h(p: Point) -> int:
        d = p.manhattan(goal)
        return int(d * 1.2) if weighted else d

    counter = 0
    open_heap: list[_Node] = [_Node(f=h(start), counter=counter, g=0, point=start)]
    came_from: dict[Point, Point] = {}
    best_g: dict[Point, int] = {start: 0}
    closed: set[Point] = set()

    while open_heap:
        node = heapq.heappop(open_heap)
        if node.point in closed:
            continue
        closed.add(node.point)

        if node.point == goal:
            return _reconstruct(came_from, node.point)

        for nxt in _neighbors(grid, node.point, blocked):
            tentative_g = node.g + 1
            if tentative_g >= best_g.get(nxt, 1 << 30):
                continue
            best_g[nxt] = tentative_g
            came_from[nxt] = node.point
            counter += 1
            heapq.heappush(
                open_heap,
                _Node(f=tentative_g + h(nxt), counter=counter,
                      g=tentative_g, point=nxt, parent=node.point),
            )
    return []


def _reconstruct(came_from: dict[Point, Point], current: Point) -> list[Point]:
    """沿 came_from 回溯出从起点到 current 的路径。"""
    path = [current]
    while current in came_from:
        current = came_from[current]
        path.append(current)
    path.reverse()
    return path[1:]  # 去掉起点


# 可替换的寻路器：对应原项目的 set_short_path_algorithm
SEARCH_ALGORITHMS = {
    "bfs": bfs_path,
    "astar": astar_path,
    "astar-weighted": lambda g, s, t, b=None: astar_path(g, s, t, b, weighted=True),
}


def find_path(
    grid: Grid,
    start: Point,
    goal: Point,
    blocked: set[Point] | None = None,
    *,
    algorithm: str = "astar",
) -> list[Point]:
    """按名字选择寻路算法。找不到时依次降级尝试，保证尽量有结果。"""
    fn = SEARCH_ALGORITHMS.get(algorithm)
    if fn is not None:
        path = fn(grid, start, goal, blocked)
        if path or start == goal:
            return path

    # 降级：A* 找不到就换 BFS 兜底（两者在四邻域单位代价下等价）
    for name in ("astar", "bfs"):
        if name == algorithm:
            continue
        path = SEARCH_ALGORITHMS[name](grid, start, goal, blocked)
        if path:
            return path
    return []
