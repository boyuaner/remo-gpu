#!/usr/bin/env python3
"""
gpu_watch.py
-------------
一个用于汇总 .ssh/config 中所有可用主机 GPU 使用情况的命令行工具。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import glob
import math
import os
import platform
import shlex
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

box = None
Console = None
Group = None
Live = None
Panel = None
ProgressBar = None
Table = None
Text = None
RICH_AVAILABLE = False
TextualAppBase = None
ComposeResultType = None
TextualHeader = None
TextualFooter = None
TextualStatic = None
TextualScroll = None
TEXTUAL_AVAILABLE = False


def ensure_rich() -> bool:
    """动态加载 rich 相关模块，用于可选 TUI。"""
    global box, Console, Group, Live, Panel, ProgressBar, Table, Text, RICH_AVAILABLE  # pylint: disable=global-statement
    if RICH_AVAILABLE:
        return True
    try:
        from rich import box as rich_box  # type: ignore
        from rich.console import Console as RichConsole, Group as RichGroup  # type: ignore
        from rich.live import Live as RichLive  # type: ignore
        from rich.panel import Panel as RichPanel  # type: ignore
        from rich.progress_bar import ProgressBar as RichProgressBar  # type: ignore
        from rich.table import Table as RichTable  # type: ignore
        from rich.text import Text as RichText  # type: ignore
    except ImportError:
        return False

    box = rich_box
    Console = RichConsole
    Group = RichGroup
    Live = RichLive
    Panel = RichPanel
    ProgressBar = RichProgressBar
    Table = RichTable
    Text = RichText
    RICH_AVAILABLE = True
    return True


def ensure_textual() -> bool:
    """动态加载 textual 相关模块，提供可滚动 TUI。"""
    global TextualAppBase, ComposeResultType, TextualHeader, TextualFooter, TextualStatic, TextualScroll, TEXTUAL_AVAILABLE  # pylint: disable=global-statement
    if TEXTUAL_AVAILABLE:
        return True
    try:
        from textual.app import App as TextualApp  # type: ignore
        from textual.app import ComposeResult as TextualComposeResult  # type: ignore
        from textual.containers import VerticalScroll  # type: ignore
        from textual.widgets import Footer, Header, Static  # type: ignore
    except ImportError:
        return False

    TextualAppBase = TextualApp
    ComposeResultType = TextualComposeResult
    TextualHeader = Header
    TextualFooter = Footer
    TextualStatic = Static
    TextualScroll = VerticalScroll
    TEXTUAL_AVAILABLE = True
    return True

REMOTE_GPU_CMD = (
    "nvidia-smi --query-gpu=index,name,temperature.gpu,utilization.gpu,"
    "memory.used,memory.total --format=csv,noheader,nounits"
)


@dataclass
class GPUStat:
    index: str
    name: str
    temperature: str
    utilization: str
    mem_used: str
    mem_total: str


@dataclass
class HostResult:
    host: str
    gpus: List[GPUStat]
    error: Optional[str] = None
    connection_error: bool = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="显示 .ssh/config 中所有主机的 GPU 使用情况"
    )
    parser.add_argument(
        "--config-path",
        default=os.path.expanduser("~/.ssh/config"),
        help="SSH 配置文件路径（默认为 ~/.ssh/config）",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="刷新间隔（秒，默认 5s）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="ssh 命令超时时间（秒，默认 10s）",
    )
    parser.add_argument(
        "--hosts",
        nargs="*",
        help="只监控特定主机（使用 Host 别名）",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="并发 ssh 数（默认 min(总主机, 8)）",
    )
    parser.add_argument(
        "--interval-once",
        action="store_true",
        help="只运行一次后退出（调试用）",
    )
    parser.add_argument(
        "--identity-file",
        help="传给 ssh 的密钥文件路径（等价于 ssh -i）",
    )
    parser.add_argument(
        "--ssh-option",
        action="append",
        default=[],
        help="额外传给 ssh 的原样参数，可重复",
    )
    parser.add_argument(
        "--remote-command",
        default=REMOTE_GPU_CMD,
        help="自定义远程 GPU 查询命令（默认使用 nvidia-smi 查询）",
    )
    parser.add_argument(
        "--ui",
        choices=["plain", "rich", "textual"],
        default="plain",
        help="输出模式：plain 纯文本，rich 彩色进度条，textual 可滚动 TUI",
    )
    parser.add_argument(
        "--no-clear",
        action="store_true",
        help="禁用控制台清屏，方便滚动查看历史记录",
    )
    return parser.parse_args()


def expand_config_includes(
    base_path: str, visited: Optional[set[str]] = None
) -> List[str]:
    visited = visited or set()
    expanded_paths: List[str] = []
    path_obj = Path(os.path.expanduser(base_path)).resolve()
    if not path_obj.exists():
        return expanded_paths
    real_path = str(path_obj)
    if real_path in visited:
        return expanded_paths
    visited.add(real_path)
    expanded_paths.append(real_path)

    include_paths = _extract_include_paths(real_path)
    for inc in include_paths:
        for child in glob.glob(os.path.expanduser(inc)):
            expanded_paths.extend(expand_config_includes(child, visited))
    return expanded_paths


def _extract_include_paths(config_path: str) -> List[str]:
    includes: List[str] = []
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            for raw in fh:
                stripped = raw.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                parts = stripped.split(None, 1)
                if len(parts) == 2 and parts[0].lower() == "include":
                    includes.append(parts[1])
    except OSError:
        pass
    return includes


def parse_ssh_hosts(config_path: str) -> List[str]:
    all_paths = expand_config_includes(config_path)
    hosts: Dict[str, None] = {}
    for path in all_paths:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                current_hosts: List[str] = []
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    lower = line.lower()
                    if lower.startswith("match "):
                        current_hosts = []
                        continue
                    if lower.startswith("host "):
                        names = line.split()[1:]
                        current_hosts = [
                            name
                            for name in names
                            if "*" not in name and "?" not in name
                        ]
                        for name in current_hosts:
                            hosts[name] = None
                        continue
                    if "=" in line:
                        key, value = [item.strip() for item in line.split("=", 1)]
                        lower_key = key.lower()
                    else:
                        parts = line.split(None, 1)
                        if len(parts) != 2:
                            continue
                        lower_key = parts[0].lower()
                        value = parts[1]

                    if lower_key == "host" and value:
                        names = [
                            name
                            for name in value.split()
                            if "*" not in name and "?" not in name
                        ]
                        current_hosts = names
                        for name in names:
                            hosts[name] = None
        except OSError:
            continue
    return list(hosts.keys())


def build_ssh_args(args: argparse.Namespace) -> List[str]:
    ssh_args: List[str] = []
    if args.identity_file:
        ssh_args += ["-i", args.identity_file]
    for opt in args.ssh_option:
        if not opt:
            continue
        ssh_args.extend(shlex.split(opt))
    return ssh_args


async def gather_gpu_stats(
    hosts: Sequence[str],
    remote_command: str,
    ssh_args: Sequence[str],
    concurrency: int,
    timeout: float,
) -> List[HostResult]:
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _fetch(host: str) -> HostResult:
        async with semaphore:
            return await fetch_host_gpu(host, remote_command, ssh_args, timeout)

    tasks = [_fetch(host) for host in hosts]
    return await asyncio.gather(*tasks)


async def fetch_host_gpu(
    host: str, remote_command: str, ssh_args: Sequence[str], timeout: float
) -> HostResult:
    if not shutil.which("ssh"):
        return HostResult(host, [], error="本地未找到 ssh 命令", connection_error=True)

    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", *ssh_args, host, remote_command]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        return HostResult(host, [], error=f"超时（>{timeout:.0f}s）", connection_error=True)
    except FileNotFoundError:
        return HostResult(host, [], error="未找到 ssh 可执行文件", connection_error=True)
    except Exception as exc:  # pylint: disable=broad-except
        return HostResult(host, [], error=f"执行失败: {exc}", connection_error=True)

    if proc.returncode != 0:
        stderr = stderr_bytes.decode("utf-8", errors="ignore").strip()
        stderr = stderr or f"ssh 返回码 {proc.returncode}"
        conn_err = _is_connection_error(stderr)
        return HostResult(host, [], error=stderr, connection_error=conn_err)

    stdout = stdout_bytes.decode("utf-8", errors="ignore").strip()
    gpus = parse_nvidia_smi_output(stdout)
    if not gpus:
        return HostResult(host, [], error="未检测到 GPU 或 nvidia-smi 无输出")
    return HostResult(host, gpus)


def parse_nvidia_smi_output(raw: str) -> List[GPUStat]:
    if not raw:
        return []
    reader = csv.reader(line for line in raw.splitlines() if line.strip())
    stats: List[GPUStat] = []
    for columns in reader:
        cols = [col.strip() for col in columns]
        if len(cols) < 6:
            continue
        stats.append(
            GPUStat(
                index=cols[0],
                name=cols[1],
                temperature=cols[2],
                utilization=cols[3],
                mem_used=cols[4],
                mem_total=cols[5],
            )
        )
    return stats


def _is_connection_error(message: str) -> bool:
    lowered = message.lower()
    keywords = [
        "permission denied",
        "connection timed out",
        "connection refused",
        "no route to host",
        "could not resolve",
        "connection reset",
        "kex_exchange_identification",
        "operation timed out",
        "ssh:",
        "unknown host",
    ]
    return any(keyword in lowered for keyword in keywords)


def format_table(results: Sequence[HostResult]) -> str:
    headers = ["Host", "GPU", "Name", "Util%", "Memory (MiB)", "Temp (°C)", "Status"]
    rows: List[List[str]] = []
    for res in results:
        if res.error:
            rows.append([res.host, "-", "-", "-", "-", "-", res.error])
            continue
        if not res.gpus:
            rows.append([res.host, "-", "-", "-", "-", "-", "No GPU"])
            continue
        for idx, gpu in enumerate(res.gpus):
            host_label = res.host if idx == 0 else ""
            mem_usage = f"{gpu.mem_used}/{gpu.mem_total}"
            rows.append(
                [
                    host_label,
                    gpu.index,
                    gpu.name,
                    f"{gpu.utilization}%",
                    mem_usage,
                    f"{gpu.temperature}°C",
                    "OK",
                ]
            )

    widths = [len(header) for header in headers]
    for row in rows:
        for i, col in enumerate(row):
            widths[i] = max(widths[i], len(col))

    def _format_row(columns: Sequence[str]) -> str:
        return "  ".join(col.ljust(widths[idx]) for idx, col in enumerate(columns))

    lines = [
        _format_row(headers),
        _format_row(["-" * width for width in widths]),
    ]
    lines.extend(_format_row(row) for row in rows)
    return "\n".join(lines)


def clear_screen() -> None:
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")


def _parse_float(value: str) -> Optional[float]:
    try:
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return None
        return number
    except (TypeError, ValueError):
        return None


def _progress_bar(completed: float, total: float) -> ProgressBar:
    total = max(int(total), 1)
    completed = max(0, min(int(round(completed)), total))
    return ProgressBar(total=total, completed=completed, width=16)


def _util_bar(percent: float) -> ProgressBar:
    percent = max(0, min(int(round(percent)), 100))
    return ProgressBar(total=100, completed=percent, width=12)


def _memory_cell(used: float, total: float):
    grid = Table.grid(padding=(0, 0))
    grid.add_row(_progress_bar(used, total))
    grid.add_row(Text(f"{int(used)}/{int(total)} MiB", style="dim"))
    return grid


def build_rich_table(results: Sequence[HostResult]) -> Table:
    table = Table(
        "Host",
        "GPU",
        "Name",
        "Util",
        "Memory",
        "Temp",
        "Status",
        box=box.SIMPLE_HEAVY,
        expand=True,
    )
    for res in results:
        if res.error:
            table.add_row(
                res.host,
                "-",
                "-",
                "-",
                "-",
                "-",
                Text(res.error, style="bold red"),
            )
            continue
        if not res.gpus:
            table.add_row(res.host, "-", "-", "-", "-", "-", Text("No GPU", style="dim"))
            continue
        for idx, gpu in enumerate(res.gpus):
            host_label = res.host if idx == 0 else ""
            util_val = _parse_float(gpu.utilization) or 0.0
            mem_used = _parse_float(gpu.mem_used) or 0.0
            mem_total = _parse_float(gpu.mem_total) or max(mem_used, 1.0)
            temp = gpu.temperature or "-"
            table.add_row(
                host_label,
                f"#{gpu.index}",
                gpu.name,
                _util_bar(util_val),
                _memory_cell(mem_used, mem_total),
                f"{temp}°C",
                Text("OK", style="green"),
            )
    return table


def build_unreachable_table(unreachable: Dict[str, str]) -> Table:
    table = Table("Host", "原因", box=box.SIMPLE_HEAVY, expand=True)
    for host, reason in unreachable.items():
        table.add_row(host, reason)
    return table


def format_unreachable_text(unreachable: Dict[str, str]) -> str:
    if not unreachable:
        return "所有主机可达"
    lines = [f"{host}: {reason}" for host, reason in unreachable.items()]
    return "不可连接主机（已跳过）:\n" + "\n".join(lines)


def build_host_panel(result: HostResult) -> Panel:
    if not RICH_AVAILABLE and not ensure_rich():
        raise RuntimeError("Rich 组件未加载，无法构建卡片")
    if result.error:
        body = Text(result.error, style="bold red")
    elif not result.gpus:
        body = Text("No GPU", style="dim")
    else:
        table = Table(
            "GPU",
            "Name",
            "Util",
            "Memory",
            "Temp",
            "Status",
            box=box.MINIMAL_DOUBLE_HEAD,
            expand=True,
        )
        for gpu in result.gpus:
            util_val = _parse_float(gpu.utilization) or 0.0
            mem_used = _parse_float(gpu.mem_used) or 0.0
            mem_total = _parse_float(gpu.mem_total) or max(mem_used, 1.0)
            temp = gpu.temperature or "-"
            table.add_row(
                f"#{gpu.index}",
                gpu.name,
                _util_bar(util_val),
                _memory_cell(mem_used, mem_total),
                f"{temp}°C",
                Text("OK", style="green"),
            )
        body = table
    title = f"{result.host}"
    return Panel(body, title=title, border_style="cyan", padding=(0, 1))


async def monitor_loop_rich(
    args: argparse.Namespace,
    hosts: List[str],
    ssh_args: List[str],
    concurrency: int,
    initial_results: Optional[List[HostResult]] = None,
    unreachable: Optional[Dict[str, str]] = None,
) -> None:
    console = Console()
    title = f"GPU Watch · 本地 {platform.node()}"
    placeholder = Panel(
        Text("正在拉取 GPU 数据...", style="dim"),
        title=title,
        border_style="cyan",
    )

    pending_results = list(initial_results) if initial_results else None

    try:
        with Live(placeholder, console=console, refresh_per_second=4) as live:
            while True:
                start = time.time()
                if pending_results is not None:
                    results = pending_results
                    pending_results = None
                else:
                    results = await gather_gpu_stats(
                        hosts,
                        args.remote_command,
                        ssh_args,
                        concurrency,
                        args.timeout,
                    )
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                subtitle = Text(
                    f"{timestamp}  • 监控 {len(hosts)} 台  • 并发 {concurrency}"
                    + (f" • 不可达 {len(unreachable)}" if unreachable else ""),
                    style="dim",
                )
                main_table = build_rich_table(results)
                body = main_table
                if unreachable:
                    offline_table = build_unreachable_table(unreachable)
                    body = Group(
                        main_table,
                        Panel(
                            offline_table,
                            title="不可连接主机",
                            border_style="red",
                            padding=(0, 1),
                        ),
                    )
                panel = Panel(
                    body,
                    title=title,
                    subtitle=subtitle,
                    border_style="cyan",
                    padding=(1, 1),
                )
                live.update(panel)
                if args.interval_once:
                    break
                elapsed = time.time() - start
                delay = max(0.0, args.interval - elapsed)
                await asyncio.sleep(delay)
    except KeyboardInterrupt:
        console.print("\n[bold yellow]收到中断，退出 Rich UI[/]")


async def monitor_loop_textual(
    args: argparse.Namespace,
    hosts: List[str],
    ssh_args: List[str],
    concurrency: int,
    initial_results: Optional[List[HostResult]],
    unreachable: Dict[str, str],
) -> None:
    if not ensure_textual():
        print("textual 库未安装，无法启用滚动 UI。请运行 pip install textual", file=sys.stderr)
        sys.exit(1)
    if not ensure_rich():
        print("rich 库未安装，Textual UI 依赖 rich 组件。请运行 pip install rich", file=sys.stderr)
        sys.exit(1)

    class HostCard(TextualStatic):  # type: ignore[misc]
        def __init__(self, host: str) -> None:
            super().__init__("", classes="host-card")
            self.host = host

        def set_result(self, result: HostResult) -> None:
            self.update(build_host_panel(result))

    class GPUWatchApp(TextualAppBase):  # type: ignore[misc]
        CSS = """
        Screen {
            layout: vertical;
        }
        #summary {
            padding: 0 1;
        }
        #offline {
            padding: 0 1;
            color: $warning;
        }
        #cards {
            height: 1fr;
            padding: 0 1;
        }
        .host-card {
            margin-bottom: 1;
        }
        """

        BINDINGS = [
            ("q", "quit", "退出"),
            ("r", "refresh", "刷新"),
        ]

        def __init__(
            self,
            initial_snapshot: Optional[List[HostResult]],
            offline_hosts: Dict[str, str],
        ) -> None:
            super().__init__()
            self._auto_task: Optional[asyncio.Task] = None
            self._summary: Optional[Any] = None
            self._offline: Optional[Any] = None
            self._cards_container: Optional[Any] = None
            self._pending_snapshot: Optional[List[HostResult]] = (
                list(initial_snapshot) if initial_snapshot else None
            )
            self._offline_hosts = offline_hosts

        def compose(self) -> "ComposeResultType":  # type: ignore[override]
            yield TextualHeader(show_clock=True)
            yield TextualStatic("初始化中...", id="summary")
            yield TextualStatic("", id="offline")
            container = TextualScroll(id="cards")
            yield container
            yield TextualFooter()

        async def on_mount(self) -> None:
            self._summary = self.query_one("#summary", TextualStatic)
            self._offline = self.query_one("#offline", TextualStatic)
            self._cards_container = self.query_one("#cards", TextualScroll)
            if self._offline:
                self._offline.update(format_unreachable_text(self._offline_hosts))
            await self.refresh_data()
            if args.interval_once:
                self.call_later(self.exit)
                return
            self._auto_task = asyncio.create_task(self.auto_refresh())

        async def on_unmount(self) -> None:
            if self._auto_task:
                self._auto_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._auto_task

        async def auto_refresh(self) -> None:
            while True:
                await asyncio.sleep(args.interval)
                await self.refresh_data()

        async def action_refresh(self) -> None:
            await self.refresh_data()

        async def refresh_data(self) -> None:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            if self._summary:
                self._summary.update(f"[bold]刷新中…[/] {timestamp}")
            if self._pending_snapshot is not None:
                results = self._pending_snapshot
                self._pending_snapshot = None
            else:
                results = await gather_gpu_stats(
                    hosts,
                    args.remote_command,
                    ssh_args,
                    concurrency,
                    args.timeout,
                )
            if self._cards_container:
                existing_children = list(self._cards_container.children)
                for child in existing_children:
                    await child.remove()
                cards: List[HostCard] = []
                for res in results:
                    card = HostCard(res.host)
                    card.set_result(res)
                    cards.append(card)
                if cards:
                    await self._cards_container.mount(*cards)
            if self._summary:
                self._summary.update(
                    f"{timestamp} · 监控 {len(hosts)} 台 · 并发 {concurrency}"
                )

    app = GPUWatchApp(initial_results, unreachable)
    await app.run_async()


async def monitor_loop_plain(
    args: argparse.Namespace,
    hosts: List[str],
    ssh_args: List[str],
    concurrency: int,
    initial_results: Optional[List[HostResult]] = None,
    unreachable: Optional[Dict[str, str]] = None,
) -> None:
    pending_results = list(initial_results) if initial_results else None
    try:
        while True:
            start = time.time()
            if pending_results is not None:
                results = pending_results
                pending_results = None
            else:
                results = await gather_gpu_stats(
                    hosts,
                    args.remote_command,
                    ssh_args,
                    concurrency,
                    args.timeout,
                )
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            hostname = platform.node()
            if not args.no_clear:
                clear_screen()
            else:
                print("\n" + "=" * 60)
            print(f"[{timestamp}] 本地: {hostname}  监控主机数: {len(hosts)}")
            print(format_table(results))
            if unreachable:
                print("\n不可连接主机（已跳过）：")
                for host, reason in unreachable.items():
                    print(f" - {host}: {reason}")
            if args.interval_once:
                break
            elapsed = time.time() - start
            delay = max(0.0, args.interval - elapsed)
            await asyncio.sleep(delay)
    except KeyboardInterrupt:
        print("\n收到中断，退出。")


async def monitor_loop(args: argparse.Namespace) -> None:
    hosts = parse_ssh_hosts(args.config_path)
    if not hosts:
        print(f"未在 {args.config_path} 中找到任何 Host 条目", file=sys.stderr)
        sys.exit(1)

    if args.hosts:
        filtered = [host for host in hosts if host in args.hosts]
        missing = set(args.hosts) - set(filtered)
        if missing:
            print(f"下列主机未在配置中找到: {', '.join(sorted(missing))}", file=sys.stderr)
        hosts = filtered
    if not hosts:
        print("没有需要监控的主机，退出。", file=sys.stderr)
        sys.exit(1)

    concurrency = args.concurrency or min(len(hosts), 8)
    ssh_args = build_ssh_args(args)

    initial_results_all = await gather_gpu_stats(
        hosts,
        args.remote_command,
        ssh_args,
        concurrency,
        args.timeout,
    )
    unreachable: Dict[str, str] = {}
    reachable_results: List[HostResult] = []
    for res in initial_results_all:
        if res.connection_error:
            unreachable[res.host] = res.error or "连接失败"
        else:
            reachable_results.append(res)

    hosts = [res.host for res in reachable_results]
    if not hosts:
        print("所有主机均不可连接：", file=sys.stderr)
        for host, reason in unreachable.items():
            print(f" - {host}: {reason}", file=sys.stderr)
        sys.exit(1)

    if args.ui == "rich":
        if not ensure_rich():
            print("rich 库未安装，无法启用 rich UI。请运行 pip install rich", file=sys.stderr)
            sys.exit(1)
        await monitor_loop_rich(
            args, hosts, ssh_args, concurrency, reachable_results, unreachable
        )
    elif args.ui == "textual":
        await monitor_loop_textual(
            args, hosts, ssh_args, concurrency, reachable_results, unreachable
        )
    else:
        await monitor_loop_plain(
            args, hosts, ssh_args, concurrency, reachable_results, unreachable
        )


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(monitor_loop(args))
    except RuntimeError as exc:
        # asyncio.run 在某些嵌套事件循环环境（如 IPython）中会抛错
        print(f"运行失败: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()


