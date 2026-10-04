"""snakevision 命令行接口。

三种模式：
    autopilot     自动寻路游玩，默认模式
    screensaver   屏保模式：死亡后自动重开，循环播放
    manual        手动控制（方向键 / wasd）

用法示例：
    snakevision -m autopilot -s 30 -y 15
    snakevision -m screensaver --try-hard
    snakevision --once              # 只跑一帧就退出（便于脚本/管道）
    snakevision --benchmark 200     # 跑 200 步并输出统计，不渲染
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from .autopilot import plan_direction, run_autopilot
from .game import Game, Status
from .grid import Direction
from .render import Renderer, Theme


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="snakevision",
        description="终端贪吃蛇：自动寻路 + 存活率保障，可当屏保",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  snakevision -m autopilot -s 30 -y 15\n"
            "  snakevision --benchmark 500 --try-hard\n"
            "  snakevision --once --style blocks --show-grid"
        ),
    )
    p.add_argument("-m", "--mode", default="autopilot",
                   choices=["autopilot", "screensaver", "manual"],
                   help="运行模式，默认 autopilot")
    p.add_argument("-s", "--size", type=int, default=30, help="网格宽度，默认 30")
    p.add_argument("-y", "--height", type=int, default=15, help="网格高度，默认 15")
    p.add_argument("-l", "--length", type=int, default=3, help="蛇的初始长度，默认 3")
    p.add_argument("-t", "--try-hard", action="store_true",
                   help="多步模拟选最安全的动作（更慢但存活更久）")
    p.add_argument("-z", "--seed", type=int, default=None,
                   help="随机种子，传整数可让整局完全可复现")
    p.add_argument("--once", action="store_true", help="只渲染一帧后退出")
    p.add_argument("--benchmark", type=int, metavar="N", default=None,
                   help="跑 N 步并输出统计，不做实时渲染")
    p.add_argument("--style", default="ascii",
                   choices=["plain", "ascii", "blocks"], help="渲染风格")
    p.add_argument("--no-color", action="store_true", help="禁用颜色")
    p.add_argument("--show-grid", action="store_true", help="显示坐标轴")
    p.add_argument("-f", "--fps", type=float, default=8.0, help="自动模式帧率，默认 8")
    return p


def _make_theme(args: argparse.Namespace) -> Theme:
    themes = {
        "plain": Theme.ascii_plain(),
        "ascii": Theme(),
        "blocks": Theme.blocks(),
    }
    theme = themes[args.style]
    if args.no_color:
        theme.use_color = False
    return theme


def _report(game: Game) -> dict[str, object]:
    return {
        "status": game.status.value,
        "score": game.score,
        "steps": game.steps,
        "length": len(game.snake),
    }


def run_once(args: argparse.Namespace) -> int:
    """只渲染一帧，用于管道与截图。"""
    game = Game.create(args.size, args.height, snake_length=args.length, seed=args.seed)
    if args.benchmark:
        run_autopilot(game, max_steps=args.benchmark, try_hard=args.try_hard)
    else:
        game.step(plan_direction(game, try_hard=args.try_hard))
    renderer = Renderer(theme=_make_theme(args), show_grid=args.show_grid)
    print(renderer.frame(game))
    return 0


def run_benchmark(args: argparse.Namespace) -> int:
    """跑 N 步，输出统计而不实时渲染。"""
    game = Game.create(args.size, args.height, snake_length=args.length, seed=args.seed)
    started = time.perf_counter()
    run_autopilot(game, max_steps=args.benchmark, try_hard=args.try_hard)
    elapsed = time.perf_counter() - started

    report = _report(game)
    report.update({
        "elapsed_seconds": round(elapsed, 3),
        "steps_per_second": round(game.steps / elapsed, 1) if elapsed > 0 else None,
        "grid": f"{args.size}x{args.height}",
        "try_hard": args.try_hard,
        "seed": args.seed,
    })
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def run_autopilot_mode(args: argparse.Namespace) -> int:
    """实时自动游玩。"""
    renderer = Renderer(theme=_make_theme(args), show_grid=args.show_grid)
    game = Game.create(args.size, args.height, snake_length=args.length, seed=args.seed)

    renderer.enter_live_mode()
    try:
        while True:
            if game.status is not Status.RUNNING:
                break
            game.step(plan_direction(game, try_hard=args.try_hard))
            renderer.live(renderer.frame(game))
            time.sleep(1.0 / max(args.fps, 0.1))
    except KeyboardInterrupt:
        pass
    finally:
        # 务必恢复光标，否则终端会一直不可见
        renderer.exit_live_mode()

    print(json.dumps(_report(game), ensure_ascii=False))
    return 0


def run_screensaver(args: argparse.Namespace) -> int:
    """屏保模式：死亡后自动重开，循环播放。"""
    renderer = Renderer(theme=_make_theme(args), show_grid=args.show_grid)
    renderer.enter_live_mode()
    delay = 1.0 / max(args.fps, 0.1)
    try:
        while True:
            game = Game.create(args.size, args.height,
                               snake_length=args.length, seed=args.seed)
            while game.status is Status.RUNNING:
                game.step(plan_direction(game, try_hard=args.try_hard))
                renderer.live(renderer.frame(game))
                time.sleep(delay)
            # 停一下让人看清死亡局面，然后重开
            time.sleep(1.5)
            game = Game.create(args.size, args.height,
                               snake_length=args.length, seed=args.seed)
    except KeyboardInterrupt:
        pass
    finally:
        renderer.exit_live_mode()
    return 0


def run_manual(args: argparse.Namespace) -> int:
    """手动控制：读单字符按键。"""
    import termios
    import tty

    renderer = Renderer(theme=_make_theme(args), show_grid=args.show_grid)
    game = Game.create(args.size, args.height, snake_length=args.length, seed=args.seed)

    keymap = {
        "w": Direction.NORTH, "k": Direction.NORTH,
        "s": Direction.SOUTH, "j": Direction.SOUTH,
        "d": Direction.EAST, "l": Direction.EAST,
        "a": Direction.WEST, "h": Direction.WEST,
    }

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    renderer.enter_live_mode()
    try:
        tty.setcbreak(fd)
        while game.status is Status.RUNNING:
            renderer.live(renderer.frame(game))
            char = sys.stdin.read(1)
            if char == "q":
                break
            if char in keymap:
                game.step(keymap[char])
            else:
                game.step()  # 没按键则沿当前方向走
    except (KeyboardInterrupt, termios.error):
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        renderer.exit_live_mode()

    print(json.dumps(_report(game), ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.benchmark:
        return run_benchmark(args)
    if args.once:
        return run_once(args)
    if args.mode == "manual":
        # 非 TTY 环境（管道/重定向）下手动模式无法读按键，退回单帧
        if not sys.stdin.isatty():
            return run_once(args)
        return run_manual(args)
    if args.mode == "screensaver":
        if not sys.stdout.isatty():
            return run_once(args)
        return run_screensaver(args)
    if not sys.stdout.isatty():
        return run_once(args)
    return run_autopilot_mode(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
