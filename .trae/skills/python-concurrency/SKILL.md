---
name: "python-concurrency"
description: "Python 并发编程技能：threading、multiprocessing、concurrent.futures、asyncio 的选型与代码模板。当需要并行/并发执行任务、处理 I/O 等待、CPU 密集计算、异步 IO、线程安全共享状态、后台任务调度时调用。"
---

# Python 并发编程

自包含的并发/并行指南：threading、multiprocessing、concurrent.futures、asyncio。涵盖选型决策、线程安全模式、GIL 影响。

## 何时调用

- 需要 I/O 并发（网络请求、文件读写、数据库查询并行）
- 需要 CPU 并行（数值计算、图像处理、数据转换）
- 需要异步 IO（高并发网络服务、流式处理）
- 需要线程/进程间通信与状态同步
- 需要后台任务调度、工作队列
- 遇到 GIL、线程安全、死锁等并发问题

## 选型决策

| 场景 | 推荐方案 | 理由 |
|------|---------|------|
| I/O 密集（网络/磁盘等待） | `threading` 或 `asyncio` | 等待时释放 GIL，可并发 |
| CPU 密集（纯计算） | `multiprocessing` | 绕过 GIL，真并行 |
| 混合（I/O + 少量计算） | `concurrent.futures.ThreadPoolExecutor` | 简单统一接口 |
| 高并发网络服务 | `asyncio` | 单线程协程，开销最低 |
| 需要共享大量内存 | `multiprocessing.shared_memory`（3.8+） | 避免进程间序列化开销 |
| 简单并行映射 | `concurrent.futures` | `map`/`as_completed` 最简 |

核心原则：
- **I/O bound → threading 或 asyncio**（GIL 在 I/O 等待时释放）。
- **CPU bound → multiprocessing**（GIL 阻止多线程真并行）。
- **优先 `concurrent.futures`**：统一线程/进程池接口，比裸 `Thread`/`Process` 易管理。
- **进程间通信避免大对象序列化**：用 `Queue` 传小消息，用 `shared_memory` 传大数组。

## threading：I/O 并发

### Thread + Lock 基础

```python
from __future__ import annotations

import threading
import time
from typing import List


def fetch_url(url: str, results: List[str], lock: threading.Lock) -> None:
    """模拟网络请求（I/O 等待时释放 GIL，其他线程可运行）。"""
    time.sleep(0.5)  # 模拟网络延迟
    with lock:  # 保护共享列表的写入
        results.append(f"完成: {url}")


def main() -> List[str]:
    """并发抓取多个 URL。"""
    urls = [f"https://api.example.com/{i}" for i in range(10)]
    results: List[str] = []
    lock = threading.Lock()
    threads = [threading.Thread(target=fetch_url, args=(url, results, lock)) for url in urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results
```

### Queue：线程间安全通信

```python
from __future__ import annotations

import threading
import queue
from typing import Any


def producer(q: "queue.Queue[Any]", items: list) -> None:
    """生产者：向队列放入任务。"""
    for item in items:
        q.put(item)
    q.put(None)  # 哨兵表示结束


def consumer(q: "queue.Queue[Any]", results: list) -> None:
    """消费者：从队列取任务并处理。"""
    while True:
        item = q.get()
        if item is None:  # 收到哨兵，退出
            q.task_done()
            break
        # 处理 item...
        results.append(str(item))
        q.task_done()


def run_pipeline(items: list) -> list:
    """生产者-消费者管道。"""
    q: "queue.Queue[Any]" = queue.Queue(maxsize=10)
    results: list = []
    cons = threading.Thread(target=consumer, args=(q, results))
    cons.start()
    producer(q, items)
    cons.join()
    return results
```

### Event / Condition / Semaphore

```python
from __future__ import annotations

import threading


# Event：一次性信号通知
def waiter(event: threading.Event, name: str) -> None:
    """等待事件触发后执行。"""
    event.wait()  # 阻塞直到 event.set()
    print(f"{name} 收到信号")


def event_demo() -> None:
    """Event 信号通知示例。"""
    event = threading.Event()
    t = threading.Thread(target=waiter, args=(event, "worker"))
    t.start()
    # ... 准备工作
    event.set()  # 通知所有等待者
    t.join()


# Condition：等待特定条件成立
class BoundedBuffer:
    """有界缓冲区：满时阻塞生产者，空时阻塞消费者。"""

    def __init__(self, capacity: int) -> None:
        """初始化缓冲区。"""
        self._buffer: list = []
        self._capacity = capacity
        self._cond = threading.Condition()

    def put(self, item: object) -> None:
        """放入元素，满时等待。"""
        with self._cond:
            while len(self._buffer) >= self._capacity:
                self._cond.wait()
            self._buffer.append(item)
            self._cond.notify()  # 通知一个等待的消费者

    def get(self) -> object:
        """取出元素，空时等待。"""
        with self._cond:
            while not self._buffer:
                self._cond.wait()
            item = self._buffer.pop(0)
            self._cond.notify()  # 通知一个等待的生产者
            return item


# Semaphore：限制并发数
def semaphore_demo(urls: list, max_concurrent: int = 5) -> None:
    """限制同时执行的线程数。"""
    sem = threading.Semaphore(max_concurrent)

    def fetch(url: str) -> None:
        """受限并发的抓取。"""
        with sem:  # 超过 max_concurrent 的线程在此阻塞
            # ... 执行抓取
            pass

    threads = [threading.Thread(target=fetch, args=(url,)) for url in urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
```

要点：
- `Lock`：互斥，保护共享可变状态；`with lock:` 自动释放。
- `RLock`：可重入锁，同一线程可多次 acquire（用于递归调用）。
- `Event`：一次性信号通知（启动/停止）。
- `Condition`：等待复杂条件（生产者-消费者、有界缓冲）。
- `Semaphore`：限制并发数（连接池、限流）。
- `Queue`：线程间安全传数据，内置锁，首选通信方式。

## concurrent.futures：统一池接口

推荐用法：比裸 Thread/Process 更简洁，自动管理生命周期。

### ThreadPoolExecutor（I/O 密集）

```python
from __future__ import annotations

import concurrent.futures
from typing import List


def fetch(url: str) -> str:
    """模拟网络请求，返回结果。"""
    import time
    time.sleep(0.3)
    return f"resp: {url}"


def concurrent_fetch(urls: List[str], max_workers: int = 8) -> List[str]:
    """线程池并发抓取，按提交顺序返回结果。"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        # map 保持输入顺序
        return list(pool.map(fetch, urls))


def concurrent_fetch_first_done(urls: List[str]) -> List[str]:
    """线程池并发抓取，谁先完成谁先处理（适合超时容错）。"""
    results: List[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        future_to_url = {pool.submit(fetch, url): url for url in urls}
        for future in concurrent.futures.as_completed(future_to_url, timeout=10):
            url = future_to_url[future]
            try:
                results.append(future.result())
            except (OSError, ValueError) as exc:
                # 单个失败不影响整体
                results.append(f"失败 {url}: {exc}")
    return results
```

### ProcessPoolExecutor（CPU 密集）

```python
from __future__ import annotations

import concurrent.futures
from typing import List


def heavy_compute(n: int) -> int:
    """CPU 密集计算（绕过 GIL，真并行）。"""
    total = 0
    for i in range(n):
        total += i * i
    return total


def parallel_compute(sizes: List[int]) -> List[int]:
    """进程池并行计算。"""
    # max_workers 通常设为 CPU 核心数
    with concurrent.futures.ProcessPoolExecutor() as pool:
        return list(pool.map(heavy_compute, sizes))


# 注意：传入 ProcessPoolExecutor 的函数和参数必须可 pickle
# lambda、闭包、嵌套函数无法跨进程传递
```

要点：
- `submit` + `as_completed`：谁先完成谁先处理，支持单任务超时。
- `map`：保持输入顺序，简单场景首选。
- `ThreadPoolExecutor`：I/O 密集，max_workers 可远超 CPU 核数。
- `ProcessPoolExecutor`：CPU 密集，max_workers 默认 = CPU 核心数。
- `with` 块退出时自动 `shutdown(wait=True)`，等待所有任务完成。
- 进程池函数须可 pickle（禁用 lambda/闭包/嵌套函数）。

## multiprocessing：CPU 并行

### Process + Queue

```python
from __future__ import annotations

import multiprocessing
from typing import Any


def worker(task: dict, result_q: "multiprocessing.Queue[Any]") -> None:
    """子进程工作函数，结果放入队列。"""
    # CPU 密集计算...
    result_q.put({"task_id": task["id"], "output": task["value"] * 2})


def run_multiprocessing(tasks: list) -> list:
    """多进程处理任务列表。"""
    result_q: "multiprocessing.Queue[Any]" = multiprocessing.Queue()
    procs = [multiprocessing.Process(target=worker, args=(t, result_q)) for t in tasks]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    # 收集结果
    results = []
    while not result_q.empty():
        results.append(result_q.get())
    return results
```

### Pool（批量并行映射）

```python
from __future__ import annotations

import multiprocessing


def process_item(x: int) -> int:
    """处理单个数据项。"""
    return x ** 2


def parallel_map(data: list) -> list:
    """Pool 批量并行映射。"""
    with multiprocessing.Pool(processes=multiprocessing.cpu_count()) as pool:
        return pool.map(process_item, data)
```

### shared_memory（3.8+，大数组共享）

```python
from __future__ import annotations

import multiprocessing.shared_memory as shm
import numpy as np  # 需安装 numpy


def worker_on_shared(shm_name: str, shape: tuple) -> None:
    """子进程通过共享内存访问大数组（零拷贝）。"""
    existing = shm.SharedMemory(name=shm_name)
    arr = np.ndarray(shape, dtype=np.float64, buffer=existing.buf)
    # 就地修改共享数组
    arr *= 2
    existing.close()


def shared_memory_demo() -> None:
    """主进程创建共享内存，子进程直接读写。"""
    data = np.ones((1000, 1000), dtype=np.float64)
    shared = shm.SharedMemory(create=True, size=data.nbytes)
    shared_arr = np.ndarray(data.shape, dtype=np.float64, buffer=shared.buf)
    shared_arr[:] = data  # 复制到共享内存

    p = multiprocessing.Process(target=worker_on_shared, args=(shared.name, data.shape))
    p.start()
    p.join()
    # shared_arr 现在已被子进程修改（值翻倍）
    shared.close()
    shared.unlink()  # 释放共享内存（仅创建者 unlink）
```

要点：
- `multiprocessing.Queue`/`Pipe`：进程间通信，数据经 pickle 序列化。
- `shared_memory`：大数组零拷贝共享，避免序列化开销。
- `Pool`：批量并行，`map`/`starmap`/`imap` 按需取用。
- 子进程须显式 `join()`；`if __name__ == "__main__":` 守卫（Windows 必须）。
- 共享内存用完须 `unlink()`，仅创建者负责释放。

## asyncio：异步 IO

### 基础：async/await

```python
from __future__ import annotations

import asyncio
from typing import List


async def fetch(url: str) -> str:
    """模拟异步网络请求。"""
    await asyncio.sleep(0.3)  # 非阻塞等待
    return f"resp: {url}"


async def fetch_all(urls: List[str]) -> List[str]:
    """并发抓取所有 URL（gather 保持顺序）。"""
    results = await asyncio.gather(*(fetch(url) for url in urls))
    return list(results)


# 运行：asyncio.run() 创建事件循环并执行
def run() -> List[str]:
    """同步入口调用异步函数。"""
    urls = [f"https://api.example.com/{i}" for i in range(10)]
    return asyncio.run(fetch_all(urls))
```

### create_task + 超时控制

```python
from __future__ import annotations

import asyncio


async def fetch_with_timeout(url: str, timeout: float = 5.0) -> str:
    """带超时的异步请求。"""
    try:
        return await asyncio.wait_for(fetch(url), timeout=timeout)
    except asyncio.TimeoutError:
        return f"超时: {url}"


async def fetch_first_success(urls: list, timeout: float = 5.0) -> str:
    """谁先成功就返回谁（取消其余任务）。"""
    tasks = [asyncio.create_task(fetch_with_timeout(url, timeout)) for url in urls]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:  # 取消未完成的任务
        t.cancel()
    for t in done:
        if not t.cancelled() and t.exception() is None:
            return t.result()
    raise RuntimeError("全部失败")


async def fetch(url: str) -> str:
    """模拟异步请求。"""
    await asyncio.sleep(0.3)
    return f"resp: {url}"
```

### asyncio.Lock / Queue / Semaphore

```python
from __future__ import annotations

import asyncio
from typing import Any


async def producer(q: "asyncio.Queue[Any]", items: list) -> None:
    """异步生产者。"""
    for item in items:
        await q.put(item)
    await q.put(None)  # 哨兵


async def consumer(q: "asyncio.Queue[Any]", results: list) -> None:
    """异步消费者。"""
    while True:
        item = await q.get()
        if item is None:
            q.task_done()
            break
        await asyncio.sleep(0.1)  # 模拟异步处理
        results.append(item)
        q.task_done()


async def run_async_pipeline(items: list) -> list:
    """异步生产者-消费者管道。"""
    q: "asyncio.Queue[Any]" = asyncio.Queue(maxsize=10)
    results: list = []
    await asyncio.gather(producer(q, items), consumer(q, results))
    return results


# Semaphore：限制并发请求数
async def limited_fetch(urls: list, max_concurrent: int = 10) -> list:
    """限制同时进行的请求数。"""
    sem = asyncio.Semaphore(max_concurrent)

    async def guarded(url: str) -> str:
        """受信号量保护的请求。"""
        async with sem:
            return await fetch(url)

    return await asyncio.gather(*(guarded(url) for url in urls))


async def fetch(url: str) -> str:
    """模拟异步请求。"""
    await asyncio.sleep(0.3)
    return f"resp: {url}"
```

### run_in_executor：在异步中跑同步/阻塞代码

```python
from __future__ import annotations

import asyncio
import time


def blocking_io(path: str) -> str:
    """同步阻塞 I/O（如传统数据库驱动）。"""
    time.sleep(1)
    return f"读取完成: {path}"


async def mixed_async() -> str:
    """异步函数中调用同步阻塞函数（不阻塞事件循环）。"""
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, blocking_io, "/data/file.txt")
    return result
```

要点：
- `asyncio.run()`：入口，自动创建/关闭事件循环；不可在已有循环内调用。
- `gather`：并发等待多个协程，保持顺序；`return_exceptions=True` 不抛异常。
- `create_task`：调度协程并发执行；`asyncio.wait` 控制完成策略。
- `wait_for`：单任务超时；`asyncio.wait`：多任务完成策略。
- `Lock`/`Queue`/`Semaphore`：异步版同步原语，`async with` / `await get()`。
- `run_in_executor`：异步中跑阻塞代码（传统库适配）。
- 禁止在异步函数中直接 `time.sleep()` 或同步 I/O（阻塞整个事件循环）。

## 线程安全模式

### 共享可变状态保护

```python
from __future__ import annotations

import threading
from typing import Any


class ThreadSafeCounter:
    """线程安全计数器（Lock 保护）。"""

    def __init__(self, initial: int = 0) -> None:
        """初始化计数器。"""
        self._value = initial
        self._lock = threading.Lock()

    def increment(self, n: int = 1) -> int:
        """原子递增，返回新值。"""
        with self._lock:
            self._value += n
            return self._value

    @property
    def value(self) -> int:
        """读取当前值。"""
        with self._lock:
            return self._value


class ThreadSafeDict:
    """线程安全字典（RLock 允许同线程递归加锁）。"""

    def __init__(self) -> None:
        """初始化。"""
        self._data: dict[str, Any] = {}
        self._lock = threading.RLock()

    def get_or_create(self, key: str, factory) -> Any:
        """获取或创建值（RLock 允许 factory 内部再次加锁）。"""
        with self._lock:
            if key not in self._data:
                self._data[key] = factory()
            return self._data[key]
```

### 进程全局状态序列化

```python
from __future__ import annotations

import os
import threading

_env_lock = threading.RLock()


def safe_setenv(key: str, value: str) -> None:
    """线程安全地设置环境变量（os.environ 是进程全局状态）。"""
    with _env_lock:
        os.environ[key] = value
```

要点：
- 共享可变状态必须加锁；`with lock:` 确保异常时也释放。
- `Lock`：互斥；`RLock`：同线程可重入（递归调用、嵌套加锁）。
- 进程全局状态（`os.environ`/`os.chdir`）用 `RLock` 序列化。
- 优先用 `Queue` 传数据而非共享变量，减少锁竞争。
- 锁粒度尽量小：只保护临界区，不在锁内做 I/O。

## 常见陷阱

1. **GIL 误区**：CPU 密集多线程不会加速（GIL 串行执行字节码）。改用 `multiprocessing`。
2. **守护线程 + 资源泄漏**：守护线程被强杀时不执行 `finally`。关键清理用非守护线程。
3. **死锁**：锁嵌套顺序不一致导致死锁。统一加锁顺序，或用 `RLock`/`Queue` 替代。
4. **`asyncio.run` 嵌套**：在已有事件循环内调用 `asyncio.run()` 会报错。用 `await` 或 `nest_asyncio`。
5. **阻塞事件循环**：异步函数中 `time.sleep()`/同步 I/O 阻塞所有协程。用 `run_in_executor`。
6. **进程池传不可 pickle 对象**：lambda/闭包/打开的文件句柄无法跨进程。用顶层函数。
7. **`Queue` 不 `task_done`**：`Queue.join()` 会永久阻塞。每个 `get` 后必须 `task_done()`。
8. **共享内存不 unlink**：`SharedMemory` 泄漏导致内存耗尽。仅创建者 `unlink()`。
9. **线程数过多**：I/O 密集也不是越多越好（上下文切换开销）。用 `Semaphore` 或池限制。
10. **裸 `except` 吞异常**：线程/进程内异常被吞，静默失败。至少 `logger.warning` 记录。
