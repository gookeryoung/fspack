---
name: "python-performance"
description: "Python 性能基线、分析与优化技能：timeit/perf_counter 基线测量、pytest-benchmark 回归门禁、cProfile/py-spy/pyinstrument/line_profiler/memray 分析工具、热点识别、算法/缓存/批量/生成器/数据结构优化模式、内存与 I/O 优化、并发加速。当需要建立性能基线、剖析热点、优化耗时/内存、添加性能回归门禁时调用。"
---

# Python 性能基线、分析与优化

自包含的性能工程指南：测量先行 → 建立基线 → 定位热点 → 针对性优化 → 回归门禁。遵循 rule-11 硬约束：`from __future__ import annotations`、中文 docstring、`subprocess` 禁 `shell=True`。核心原则：**未测量不优化**——凭直觉的"优化"多数是噪声，必须用数据驱动。

## 何时调用

- 需要为函数/模块建立性能基线（响应时间、吞吐量、内存峰值）
- 需要剖析耗时热点（哪个函数占 80% 时间）
- 需要剖析内存占用（泄漏、峰值、对象分布）
- 需要优化算法/数据结构（O(n²) → O(n)、列表查找改字典）
- 需要优化 I/O 密集代码（批量、缓冲、异步）
- 需要优化 CPU 密集代码（向量化、C 扩展、并发）
- 需要在 CI 中添加性能回归门禁（防止性能退化）
- 需要对比优化前后性能（A/B 基准）

## 性能基线建立

**测量先行**：优化前先建立可复现的基线，优化后对比验证。基线须包含：场景描述、输入规模、硬件信息、多次运行统计（min/median/mean/stddev）。

### time.perf_counter：高精度计时

`time.perf_counter()` 是测量代码段耗时的首选（不受系统时钟调整影响，分辨率纳秒级）。避免 `time.time()`（受 NTP 调整影响）和 `time.clock()`（3.8 已移除）。

```python
"""coopie.perf 模块：性能基线测量工具."""

from __future__ import annotations

import statistics
import time
from typing import Callable, TypeVar

T = TypeVar("T")


def measure_time(
    func: Callable[..., T],
    *args: object,
    repeat: int = 10,
    **kwargs: object,
) -> tuple[T, statistics.Statistics]:
    """测量函数执行时间，返回结果与统计信息。

    Args:
        func: 待测函数
        *args: 位置参数
        repeat: 重复次数（剔除首冷的冷启动后取统计）
        **kwargs: 关键字参数

    Returns:
        (最后一次结果, 耗时统计 min/median/mean/stdev)

    """
    timings: list[float] = []
    result: T | None = None
    # 预热一次（缓存对齐、JIT 暖机、页缓存）
    result = func(*args, **kwargs)
    for _ in range(repeat):
        start = time.perf_counter()
        result = func(*args, **kwargs)
        timings.append(time.perf_counter() - start)
    return result, statistics.Statistics(timings)  # type: ignore[arg-type]


def format_stats(timings: list[float]) -> str:
    """格式化耗时统计为可读字符串。"""
    return (
        f"min={min(timings)*1000:.2f}ms "
        f"median={statistics.median(timings)*1000:.2f}ms "
        f"mean={statistics.mean(timings)*1000:.2f}ms "
        f"stdev={statistics.stdev(timings)*1000:.2f}ms "
        f"n={len(timings)}"
    )
```

要点：
- `perf_counter` 分辨率纳秒级，`time.time` 仅微秒级且受 NTP 影响。
- **预热一次**：首次调用有冷启动开销（导入、页缓存、分支预测），剔除后取统计。
- **多次运行取中位数**：单次测量受 GC/调度抖动影响，至少 10 次取 median。
- **报告 stdev**：方差大说明测量噪声高，需增加 repeat 或隔离环境。

### timeit：微基准

`timeit.timeit`/`repeat` 适用于微基准（单行表达式、纯函数），自动禁用 GC（`gc.disable()`）减少干扰。

```python
from __future__ import annotations

import timeit


def benchmark_lookup() -> dict[str, float]:
    """对比列表查找 vs 字典查找的耗时。"""
    data_list = list(range(10_000))
    data_dict = {i: i for i in range(10_000)}
    needle = 9_999  # 列表最坏情况（末尾）

    list_time = min(timeit.repeat(lambda: needle in data_list, number=10_000, repeat=5))
    dict_time = min(timeit.repeat(lambda: needle in data_dict, number=10_000, repeat=5))
    return {"list_lookup_s": list_time, "dict_lookup_s": dict_time, "speedup": list_time / dict_time}
```

要点：
- `number`：每次循环执行次数（小函数设大值，累计可测）。
- `repeat`：重复次数，取 `min`（最小值代表无干扰下的最优）。
- `timeit` 自动禁用 GC，适合纯计算微基准；含 I/O 的代码用 `perf_counter`。

### pytest-benchmark：回归门禁

`pytest-benchmark` 把基准测试纳入 pytest 体系，支持基线持久化、回归对比、CI 门禁。

```python
"""tests/test_perf_lookup.py：查找性能基准测试."""

from __future__ import annotations

import pytest


@pytest.fixture
def data_list() -> list[int]:
    """测试数据：0..9999 列表。"""
    return list(range(10_000))


@pytest.fixture
def data_dict() -> dict[int, int]:
    """测试数据：0..9999 字典。"""
    return {i: i for i in range(10_000)}


def test_dict_lookup_faster_than_list(benchmark, data_dict) -> None:
    """字典查找应快于列表查找（回归门禁）。"""
    needle = 9_999
    result = benchmark(lambda: needle in data_dict)
    assert result is True


def test_list_lookup_worst_case(benchmark, data_list) -> None:
    """列表末尾查找基线（用于对比）。"""
    needle = 9_999
    result = benchmark(lambda: needle in data_list)
    assert result is True
```

CI 中对比基线并设退化阈值：

```bash
# 首次运行，保存基线（提交到仓库或 CI artifact）
uv run pytest tests/test_perf_lookup.py --benchmark-save=baseline

# 后续运行，对比基线，退化超过 10% 失败
uv run pytest tests/test_perf_lookup.py \
    --benchmark-compare \
    --benchmark-compare-fail=mean:10% \
    --benchmark-min-rounds=10 \
    --benchmark-warmup=on
```

要点：
- `--benchmark-save=NAME`：保存到 `.benchmarks/` 目录（应加入 `.gitignore` 或专门管理）。
- `--benchmark-compare`：与上次基线对比，输出表格。
- `--benchmark-compare-fail=mean:10%`：均值退化超 10% 时测试失败（CI 门禁）。
- `--benchmark-min-rounds`/`--benchmark-warmup`：保证测量稳定性。
- 基线须与硬件绑定：CI 与本地基线不可混用（CPU/内存差异巨大）。

## 性能分析工具

### cProfile：确定性 profiler

`cProfile` 是标准库确定性 profiler（记录每次函数调用），适用于定位热点函数。开销中等（约 2-5x 减速）。

```python
"""coopie.analyzer 模块：cProfile 分析封装."""

from __future__ import annotations

import cProfile
import pstats
from io import StringIO
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")


def profile_to_stats(
    func: Callable[..., T],
    *args: object,
    top_n: int = 20,
    sort_key: str = "cumulative",
    **kwargs: object,
) -> str:
    """运行函数并返回 cProfile 统计的可读文本。

    Args:
        func: 待剖析函数
        *args: 位置参数
        top_n: 显示前 N 个热点
        sort_key: 排序键（cumulative/tottime/percall）
        **kwargs: 关键字参数

    Returns:
        pstats 格式的统计文本

    """
    profiler = cProfile.Profile()
    profiler.enable()
    func(*args, **kwargs)
    profiler.disable()

    stream = StringIO()
    stats = pstats.Stats(profiler, stream=stream).sort_stats(sort_key)
    stats.print_stats(top_n)
    return stream.getvalue()


def profile_to_file(
    func: Callable[..., T],
    output: Path,
    *args: object,
    **kwargs: object,
) -> T:
    """运行函数并将 cProfile 数据保存到文件（供 snakeviz/可视化）。"""
    profiler = cProfile.Profile()
    profiler.enable()
    result = func(*args, **kwargs)
    profiler.disable()
    profiler.dump_stats(str(output))
    return result
```

命令行用法：

```bash
# 直接剖析脚本
uv run python -m cProfile -o profile.prof -s cumulative my_script.py

# 用 snakeviz 可视化（pip install snakeviz）
uv run snakeviz profile.prof
```

### pstats 排序键

| 排序键 | 含义 | 适用场景 |
|--------|------|---------|
| `cumulative` | 累计时间（含子调用） | 定位"哪个调用链最耗时" |
| `tottime` | 函数自身时间（不含子调用） | 定位"哪个函数本身最耗时" |
| `percall` | 每次调用平均时间 | 定位"单次昂贵的函数" |
| `ncalls` | 调用次数 | 定位"被频繁调用的函数" |

要点：
- **先看 `cumulative`** 找最耗时的调用链，**再看 `tottime`** 找自身最耗时的函数。
- `ncalls` 异常高（如百万次）提示循环内重复计算，应缓存或批量。
- cProfile 有开销，**绝对耗时不可信**，仅用于相对热点排序；绝对耗时用 `perf_counter`。

### py-spy：采样 profiler（无需改代码）

`py-spy` 是用 Rust 写的采样 profiler，**无需修改代码**、无需重启进程，适用于生产环境剖析。开销极低（约 1-5%）。

```bash
# 安装
uv tool install py-spy

# 采样运行中的进程（按 PID）
py-spy record --pid 12345 --duration 30 --output profile.svg

# 直接运行并采样
py-spy record -- python my_script.py

# 实时查看（类似 top）
py-spy top --pid 12345

# dump 当前调用栈（调试卡死）
py-spy dump --pid 12345
```

生成火焰图（SVG）直接浏览器打开，直观显示调用栈与耗时占比。

### pyinstrument：低开销调用栈采样

`pyinstrument` 是纯 Python 采样 profiler，开销低（约 1%），输出树状调用栈。

```bash
# 安装
uv add --dev pyinstrument

# 命令行
uv run pyinstrument my_script.py

# 代码内
```

```python
from __future__ import annotations

from pyinstrument import Profiler


def run_with_profile(func, *args, **kwargs):
    """在 pyinstrument 剖析下运行函数，输出报告。"""
    profiler = Profiler(interval=0.001)  # 1ms 采样间隔
    profiler.start()
    result = func(*args, **kwargs)
    profiler.stop()
    print(profiler.output_text(unicode=True, color=True))
    return result
```

### line_profiler：逐行剖析

`line_profiler` 用于剖析单个函数的**每一行**耗时，定位函数内部热点。需用 `@profile` 装饰（无导入）。

```bash
# 安装
uv add --dev line_profiler

# 命令行剖析
uv run kernprof -l -v my_script.py
```

```python
"""my_script.py：待剖析脚本."""

from __future__ import annotations


def process_items(items: list[int]) -> list[int]:
    """逐行剖析此函数。"""
    result = []                                    # 装饰后显示每行耗时
    for item in items:                             # 循环本身耗时
        squared = item * item                      # 计算
        result.append(squared)                     # append 耗时
    return result


# kernprof 注入 @profile 装饰器，无需导入
profile(process_items)  # type: ignore[name-defined]
```

### memray：内存剖析

`memray` 是 CPython 内存分析器，追踪每次分配的调用栈，定位泄漏与峰值。

```bash
# 安装
uv add --dev memray

# 运行并采集
uv run memray run my_script.py

# 生成火焰图
uv run memray flamegraph memray-my_script.1234.bin

# 生成统计报告
uv run memray stats memray-my_script.1234.bin
```

代码内用法：

```python
from __future__ import annotations

from memray import FileDestination, Profile


def with_memory_profile(output_path: str, func, *args, **kwargs):
    """在 memray 剖析下运行函数。"""
    with Profile(destination=FileDestination(path=output_path, overwrite=True)):
        return func(*args, **kwargs)
```

## 优化模式

### 1. 算法复杂度优先

**最大收益来自算法本身**：O(n²) → O(n log n) 比任何微优化都重要。

```python
from __future__ import annotations


def find_duplicates_o_n2(items: list[int]) -> list[int]:
    """O(n²) 暴力法（慢）。"""
    duplicates = []
    for i, x in enumerate(items):
        for y in items[i + 1 :]:
            if x == y and x not in duplicates:
                duplicates.append(x)
    return duplicates


def find_duplicates_o_n(items: list[int]) -> list[int]:
    """O(n) 哈希法（快）。"""
    seen: set[int] = set()
    duplicates: set[int] = set()
    for x in items:
        if x in seen:
            duplicates.add(x)
        else:
            seen.add(x)
    return list(duplicates)
```

要点：
- **先分析复杂度**：循环嵌套层数 ≠ 复杂度，看数据结构操作（`in list` 是 O(n)，`in set` 是 O(1)）。
- **用 `set`/`dict` 查找**：成员判断 O(1)，列表 O(n)。
- **排序后双指针**：O(n log n) 替代 O(n²) 暴力匹配。

### 2. 缓存：避免重复计算

`functools.lru_cache` 缓存纯函数结果；`functools.cached_property` 缓存实例属性（rule-11 数据结构章节）。

```python
from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=128)
def fibonacci(n: int) -> int:
    """斐波那契数列（缓存后从 O(2^n) 降到 O(n)）。"""
    if n < 2:
        return n
    return fibonacci(n - 1) + fibonacci(n - 2)


def clear_cache_on_change() -> None:
    """修改缓存源后必须手动清空。"""
    fibonacci.cache_clear()


@lru_cache(maxsize=None)
def read_config(path: Path) -> dict[str, str]:
    """读取配置文件并缓存（path 必须可哈希）。"""
    # 注意：不可哈希参数（list/dict）无法用 lru_cache
    text = path.read_text(encoding="utf-8")
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
```

要点：
- **纯函数才可缓存**：无副作用、相同输入相同输出。
- **参数必须可哈希**：`list`/`dict`/`set` 不可哈希，转 `tuple`/`frozenset`。
- **修改缓存源后清空**：文件改动、依赖更新后调用 `cache_clear()`。
- **`maxsize=None` 无上限**：长期运行可能内存膨胀，需监控 `cache_info()`。

### 3. 批量优于循环单次

循环内多次 I/O/DB/API 调用是性能杀手，改为一次批量。

```python
from __future__ import annotations

import sqlite3


def fetch_users_one_by_one(conn: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
    """N 次 SQL 查询（慢，N 大时延迟线性增长）。"""
    results = []
    for uid in ids:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if row:
            results.append(row)
    return results


def fetch_users_batch(conn: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
    """1 次 SQL 查询（快，利用 IN 子句批量）。"""
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    return conn.execute(
        f"SELECT * FROM users WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
```

适用场景：DB 查询、HTTP API、文件读写、subprocess 调用。

### 4. 生成器：惰性求值省内存

处理大序列时用生成器替代列表，按需产出避免一次性加载。

```python
from __future__ import annotations
from typing import Iterator


def load_lines_eager(path: str) -> list[str]:
    """一次性加载全部（内存爆炸风险）。"""
    with open(path, encoding="utf-8") as f:
        return f.readlines()


def load_lines_lazy(path: str) -> Iterator[str]:
    """生成器逐行产出（内存恒定）。"""
    with open(path, encoding="utf-8") as f:
        yield from f


def count_lines(path: str) -> int:
    """生成器消费：内存恒定，可处理 GB 级文件。"""
    return sum(1 for _ in load_lines_lazy(path))
```

要点：
- 生成器只能迭代一次，需多次消费用 `list()` 物化或 `itertools.tee`。
- 流式处理管道（`map`/`filter`/生成器组合）避免中间列表。

### 5. 字符串构建：join 而非 +=

循环内 `+=` 字符串是 O(n²)（每次创建新字符串），`join` 是 O(n)。

```python
from __future__ import annotations


def build_string_bad(words: list[str]) -> str:
    """O(n²)：每次 += 创建新字符串。"""
    result = ""
    for w in words:
        result += w
    return result


def build_string_good(words: list[str]) -> str:
    """O(n)：join 一次性拼接。"""
    return "".join(words)
```

### 6. 局部变量绑定：LOAD_FAST

函数内频繁访问的全局/内置名绑定到局部变量，CPython 的 `LOAD_FAST` 比 `LOAD_GLOBAL` 快。

```python
from __future__ import annotations

import math


def distance_global(points: list[tuple[float, float]]) -> list[float]:
    """每次循环 LOAD_GLOBAL math.sqrt（慢）。"""
    result = []
    for x, y in points:
        result.append(math.sqrt(x * x + y * y))
    return result


def distance_local(points: list[tuple[float, float]]) -> list[float]:
    """局部绑定 sqrt（快约 15-30%）。"""
    sqrt = math.sqrt
    result = []
    for x, y in points:
        result.append(sqrt(x * x + y * y))
    return result
```

### 7. __slots__：减少实例内存

`__slots__` 禁止 `__dict__`，实例内存减少约 40-50%，属性访问也更快。适用于大量实例的类。

```python
from __future__ import annotations


class PointDict:
    """普通类：每个实例有 __dict__（约 100+ 字节额外开销）。"""
    def __init__(self, x: float, y: float) -> None:
        self.x = x
        self.y = y


class PointSlots:
    """__slots__ 类：无 __dict__，省内存。"""
    __slots__ = ("x", "y")

    def __init__(self, x: float, y: float) -> None:
        self.x = x
        self.y = y


# 大量实例场景：1M 个 PointSlots 比 PointDict 省约 50MB 内存
```

要点：
- `__slots__` 后**不能动态添加属性**（牺牲灵活性换内存）。
- 继承链中所有父类也需声明 `__slots__`，否则子类仍会有 `__dict__`。
- 默认 `__weakref__` 也会消失，需要时显式加入 `__slots__`。

### 8. 推导式优于 map+filter

CPython 对推导式有专门优化，比 `map`+`filter`+`lambda` 快约 10-20%。

```python
from __future__ import annotations


def with_map_filter(nums: list[int]) -> list[int]:
    """map+filter（慢且可读性差）。"""
    return list(map(lambda x: x * x, filter(lambda x: x % 2 == 0, nums)))


def with_comprehension(nums: list[int]) -> list[int]:
    """推导式（快且清晰）。"""
    return [x * x for x in nums if x % 2 == 0]
```

## 内存优化

### 避免深拷贝

`copy.deepcopy` 极慢（递归反射），优先用不可变结构或显式构造。

```python
from __future__ import annotations

import copy
from dataclasses import dataclass


@dataclass(frozen=True)
class Point:
    """不可变值对象：天然无需深拷贝。"""
    x: float
    y: float


def move_point(p: Point, dx: float, dy: float) -> Point:
    """不可变更新：创建新实例而非深拷贝。"""
    return Point(p.x + dx, p.y + dy)


# 反例：copy.deepcopy(point)  # 慢且不必要
```

### 大数值序列用 array/numpy

`list[int]` 每个元素是完整 Python 对象（28 字节），`array.array`/`numpy.ndarray` 是紧凑 C 数组（4-8 字节/元素）。

```python
from __future__ import annotations

import array
import sys


def list_memory(nums: list[int]) -> int:
    """list[int] 内存：每元素约 28 字节（含对象头）。"""
    return sys.getsizeof(nums) + sum(sys.getsizeof(n) for n in nums)


def array_memory(nums: array.array) -> int:
    """array.array 内存：每元素 8 字节（int64）。"""
    return sys.getsizeof(nums)


# 1M 整数：list 约 28MB，array 约 8MB
```

## I/O 优化

### 批量读写

循环内多次 `read`/`write` 改为一次批量。

```python
from __future__ import annotations

from pathlib import Path


def write_lines_bad(path: Path, lines: list[str]) -> None:
    """N 次 write 系统调用（慢）。"""
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")  # 每次系统调用


def write_lines_good(path: Path, lines: list[str]) -> None:
    """1 次 write（快）。"""
    with path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")  # 单次系统调用
```

### mmap：大文件内存映射

`mmap` 将文件映射到虚拟内存，按需分页加载，避免一次性读取大文件。

```python
from __future__ import annotations

import mmap
from pathlib import Path


def search_in_large_file(path: Path, pattern: bytes) -> int:
    """在大文件中搜索字节模式（mmap 避免全量加载）。"""
    with path.open("rb") as f:
        with mmap.mmap(f.fileno(), length=0, access=mmap.ACCESS_READ) as m:
            return m.find(pattern)
```

### 异步 I/O

I/O 密集场景用 `asyncio` 并发多个 I/O 操作（详见 `python-concurrency` SKILL）。

```python
from __future__ import annotations

import asyncio
import aiohttp  # 第三方异步 HTTP


async def fetch_urls(urls: list[str]) -> list[str]:
    """并发抓取多个 URL（I/O 等待时切换任务）。"""
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_one(session, url) for url in urls]
        return await asyncio.gather(*tasks)


async def fetch_one(session: aiohttp.ClientSession, url: str) -> str:
    """抓取单个 URL。"""
    async with session.get(url) as resp:
        return await resp.text()
```

## 并发优化

详见 `python-concurrency` SKILL。选型摘要：

| 场景 | 方案 | 说明 |
|------|------|------|
| I/O 密集（网络/磁盘） | `asyncio` 或 `ThreadPoolExecutor` | GIL 在 I/O 等待时释放 |
| CPU 密集（纯计算） | `ProcessPoolExecutor` 或 `multiprocessing` | 绕过 GIL 真并行 |
| 大量独立任务 | `concurrent.futures` | 统一池接口 |
| 混合（I/O + 计算） | `asyncio` + `run_in_executor` | 异步主体 + 阻塞部分进线程池 |

要点：
- **先串行优化**：并发前先用算法/缓存优化串行版本，并发有协调开销。
- **进程池慎用**：序列化开销可能抵消并行收益，数据量大用 `shared_memory`。
- **GIL 不是万能挡箭牌**：I/O 密集多线程能加速，CPU 密集才必须多进程。

## 回归检测与 CI 门禁

### pytest-benchmark 回归门禁

在 CI 中对比基线，防止性能退化。

```yaml
# .github/workflows/perf.yml：性能回归门禁
name: performance
on:
  push:
    branches: [main]
  pull_request:

jobs:
  benchmark:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v5
      - name: Install uv
        uses: astral-sh/setup-uv@v8.3.2
        with:
          enable-cache: true
          cache-dependency-glob: "uv.lock"
      - run: uv sync --frozen
      - name: 恢复基线
        run: |
          git fetch origin refs/bench/baseline:refs/bench/baseline || true
          git show refs/bench/baseline:.benchmarks/ 2>/dev/null || true
      - name: 运行基准测试
        run: |
          uv run pytest tests/test_perf/ \
            --benchmark-compare \
            --benchmark-compare-fail=mean:10% \
            --benchmark-min-rounds=10
```

### 基线管理策略

- **基线与硬件绑定**：不同 CI runner（CPU/内存）基线不可混用，按 runner 分组保存。
- **基线定期刷新**：每月或大版本后重新建立基线，反映当前性能水平。
- **退化阈值合理**：微基准设 10%，含 I/O 的设 20-30%（噪声大）。
- **报告而非阻塞**：首次引入的基准测试可设 `--benchmark-disable-gc` 观察一周后再启门禁。

## 常见陷阱

1. **未测量就优化**：凭直觉"优化"多数无效甚至变慢。先用 cProfile/perf_counter 定位热点。
2. **微基准噪声**：单次测量受 GC/调度影响，至少 10 次取 median；cProfile 绝对耗时不可信，仅看相对排序。
3. **预热缺失**：首次调用有冷启动（导入、页缓存），基准测试前先预热 1-2 次。
4. **基线硬件混用**：本地基线与 CI 基线不可比，CPU/内存差异巨大；基线须标注硬件。
5. **lru_cache 不可哈希参数**：`list`/`dict` 传给 `@lru_cache` 报 `TypeError`，转 `tuple`/`frozenset`。
6. **缓存源变更未清空**：文件/DB 改动后 `lru_cache` 仍返回旧值，需手动 `cache_clear()`。
7. **循环内 I/O**：N 次 DB/API 调用延迟线性增长，改为一次批量（`IN` 子句、批量接口）。
8. **字符串 +=**：循环内 `+=` 是 O(n²)，用 `"".join(parts)` 一次性拼接。
9. **深拷贝滥用**：`copy.deepcopy` 极慢，优先用不可变值对象（`@dataclass(frozen=True)`）+ 显式构造。
10. **过早并发**：串行版本未优化就上多线程/多进程，协调开销可能抵消收益；先优化算法再考虑并发。
