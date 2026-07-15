---
name: "python-file-io"
description: "Python 文件与路径处理技能：pathlib 路径操作、文本二进制读写、流式分块、临时文件、JSON/CSV/pickle、原子写入、上下文管理。当需要读写文件、遍历目录、处理大文件、管理临时文件、序列化数据、确保写入原子性时调用。"
---

# Python 文件与路径处理

自包含的文件 IO 指南：pathlib 路径操作、读写模式、流式处理、临时文件、序列化、原子写入。所有路径操作优先用 `pathlib.Path`（ruff `PTH` 强制），禁止字符串拼接路径。

## 何时调用

- 需要读写文件（文本/二进制/JSON/CSV）
- 需要遍历目录、匹配文件模式（glob）
- 需要处理大文件（流式分块读取）
- 需要创建临时文件/目录
- 需要原子写入（避免半写文件）
- 需要批量重命名/移动/复制文件
- 需要序列化/反序列化数据（JSON/CSV/pickle）

## pathlib：路径操作首选

禁止 `os.path.join`/`"a" + "/b"`；一律 `Path`。

```python
from __future__ import annotations

from pathlib import Path


# --- 构造与拼接 ---
root = Path("/data/project")
config_path = root / "config" / "settings.json"  # / 运算符拼接
output_dir = Path("output") / "reports"

# --- 路径属性 ---
path = Path("/data/config/settings.json")
path.name          # "settings.json"  文件名（含扩展名）
path.stem          # "settings"       文件名（不含扩展名）
path.suffix        # ".json"          扩展名
path.parent        # Path("/data/config")  父目录
path.parents[0]    # Path("/data/config")
path.parents[1]    # Path("/data")
path.parts         # ('/', 'data', 'config', 'settings.json')

# --- 存在性与类型 ---
path.exists()      # bool
path.is_file()     # bool
path.is_dir()      # bool
path.is_symlink()  # bool

# --- 创建/删除 ---
Path("output/logs").mkdir(parents=True, exist_ok=True)  # 递归创建，已存在不报错
path.touch()       # 创建空文件（已存在则更新时间戳）
path.unlink()      # 删除文件（不存在抛 FileNotFoundError；missing_ok=True 不抛）
path.unlink(missing_ok=True)  # 3.8+，不存在不报错
Path("old_dir").rmdir()       # 删除空目录

# --- 遍历目录 ---
for entry in Path("src").iterdir():       # 一级直接子项
    print(entry.name)

for py_file in Path("src").rglob("*.py"): # 递归匹配
    print(py_file)

for match in Path(".").glob("**/*.test"): # ** 需递归
    print(match)
```

要点：
- 路径拼接用 `/` 运算符，不用字符串 `+` 或 `os.path.join`。
- `mkdir(parents=True, exist_ok=True)` 等价 `mkdir -p`。
- `rglob` 递归匹配，`glob("**/*")` 同效。
- 边界 `str` 立即包装 `Path(s)`；函数签名用 `Path` 类型。

## 读写文件

### 简单读写

```python
from __future__ import annotations

from pathlib import Path


def read_config(path: Path) -> str:
    """读取文本文件，显式指定编码（禁止依赖系统默认）。"""
    return path.read_text(encoding="utf-8")


def write_config(path: Path, content: str) -> None:
    """写入文本文件。"""
    path.write_text(content, encoding="utf-8")


def read_binary(path: Path) -> bytes:
    """读取二进制文件。"""
    return path.read_bytes()


def write_binary(path: Path, data: bytes) -> None:
    """写入二进制文件。"""
    path.write_bytes(data)
```

### 流式读写（大文件）

`read_text()`/`read_bytes()` 一次性加载到内存；大文件用 `open()` 逐行/分块。

```python
from __future__ import annotations

from pathlib import Path
from typing import Iterator


def count_lines(path: Path) -> int:
    """逐行统计行数（不加载全文到内存）。"""
    count = 0
    with path.open(encoding="utf-8") as f:
        for line in f:  # 逐行迭代，内存友好
            count += 1
    return count


def iter_lines(path: Path) -> Iterator[str]:
    """生成器：逐行产出（流式处理大日志）。"""
    with path.open(encoding="utf-8") as f:
        for line in f:
            yield line.rstrip("\n")


def read_chunks(path: Path, chunk_size: int = 8192) -> Iterator[bytes]:
    """分块读取二进制文件（处理超大文件）。"""
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            yield chunk


def copy_large(src: Path, dst: Path, chunk_size: int = 65536) -> int:
    """分块复制大文件，返回写入字节数。"""
    total = 0
    with src.open("rb") as fin, dst.open("wb") as fout:
        while True:
            chunk = fin.read(chunk_size)
            if not chunk:
                break
            total += fout.write(chunk)
    return total
```

要点：
- 大文件禁止 `read()`/`read_text()` 全量加载，用 `for line in f` 或 `read(chunk_size)`。
- 显式指定 `encoding="utf-8"`，不依赖系统默认（Windows 默认 GBK）。
- 文件操作一律 `with` 语句，确保异常时也关闭。
- `open()` 默认 `mode="r"`（文本读）；二进制用 `"rb"`/`"wb"`。

### 追加与多种模式

```python
from pathlib import Path


def append_log(path: Path, message: str) -> None:
    """追加日志行（不覆盖已有内容）。"""
    with path.open("a", encoding="utf-8") as f:
        f.write(message + "\n")


# 常用模式：
# "r"  读（默认），文件不存在抛错
# "w"  写（覆盖），文件不存在则创建
# "a"  追加，文件不存在则创建
# "x"  排他创建，文件已存在抛 FileExistsError
# "rb" / "wb" / "ab"  二进制模式
# "r+" 读写（不截断）
```

## 上下文管理：多资源

### ExitStack 管理多个文件

```python
from __future__ import annotations

import contextlib
from pathlib import Path


def merge_files(paths: list[Path], output: Path) -> None:
    """合并多个文件到一个输出（ExitStack 管理可变数量资源）。"""
    with contextlib.ExitStack() as stack:
        files = [stack.enter_context(p.open(encoding="utf-8")) for p in paths]
        out = stack.enter_context(output.open("w", encoding="utf-8"))
        for f in files:
            for line in f:
                out.write(line)
```

### 自定义上下文管理器

```python
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import IO


@contextlib.contextmanager
def atomic_write(path: Path, encoding: str = "utf-8") -> "contextlib._GeneratorContextManager[IO]":
    """原子写入：先写临时文件，成功后原子替换。

    用法：
        with atomic_write(Path("config.json")) as f:
            f.write(json.dumps(data))
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding=encoding) as f:
            yield f
        tmp.replace(path)  # 原子替换（同文件系统内 rename 是原子的）
    except Exception:
        tmp.unlink(missing_ok=True)  # 失败时清理临时文件
        raise
```

要点：
- 多个资源用 `ExitStack`，避免深层嵌套 `with`。
- `@contextlib.contextmanager` + 生成器：简化自定义上下文管理器。
- `Path.replace(target)`：原子移动/重命名（同文件系统内）。

## 临时文件

```python
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import IO


def process_with_temp(data: bytes) -> bytes:
    """使用临时文件处理数据（自动清理）。"""
    # NamedTemporaryFile：有文件名的临时文件
    with tempfile.NamedTemporaryFile(suffix=".dat", delete=True) as f:
        f.write(data)
        f.flush()  # 刷到磁盘（不关闭）
        # ... 外部程序可通过 f.name 访问
        f.seek(0)
        return f.read()


def batch_process(items: list[str]) -> list[str]:
    """使用临时目录批量处理。"""
    results = []
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        for i, item in enumerate(items):
            (tmp / f"input_{i}.txt").write_text(item, encoding="utf-8")
            # ... 处理...
            output = (tmp / f"output_{i}.txt").read_text(encoding="utf-8")
            results.append(output)
    # 退出 with 后临时目录自动删除
    return results


# 需要持久临时路径时
def get_temp_path(prefix: str = "app_") -> Path:
    """获取临时文件路径（需手动清理）。"""
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=".tmp")
    import os
    os.close(fd)  # mkstemp 返回的 fd 需手动关闭
    return Path(name)
```

要点：
- `NamedTemporaryFile(delete=True)`：退出 `with` 自动删除。
- `TemporaryDirectory()`：临时目录，退出自动递归删除。
- `mkstemp`/`mkdtemp`：需手动清理（持久场景用）。
- 临时文件用于：中间结果、外部程序接口、避免内存爆炸。

## 序列化

### JSON

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def save_json(path: Path, data: Any, indent: int = 2) -> None:
    """保存为 JSON 文件（UTF-8，ensure_ascii=False 保留中文）。"""
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)


def load_json(path: Path) -> Any:
    """加载 JSON 文件。"""
    with path.open(encoding="utf-8") as f:
        return json.load(f)


# 处理不可直接序列化的类型（datetime/Path 等）
class CustomEncoder(json.JSONEncoder):
    """自定义 JSON 编码器，处理 Path/datetime 等类型。"""

    def default(self, o: Any) -> Any:
        """转换不支持的类型为可序列化值。"""
        from datetime import datetime
        from pathlib import Path
        if isinstance(o, Path):
            return str(o)
        if isinstance(o, datetime):
            return o.isoformat()
        return super().default(o)


# 使用：json.dump(data, f, cls=CustomEncoder, ensure_ascii=False)
```

### CSV

```python
from __future__ import annotations

import csv
from pathlib import Path
from typing import Sequence


def write_csv(path: Path, rows: Sequence[Sequence[str]], headers: Sequence[str]) -> None:
    """写入 CSV 文件。"""
    with path.open("w", encoding="utf-8", newline="") as f:  # newline="" 防止 Windows 空行
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    """读取 CSV 为字典列表（首行作表头）。"""
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))
```

### pickle（谨慎用）

```python
from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any


def save_pickle(path: Path, obj: Any) -> None:
    """序列化对象到 pickle 文件。

    警告：pickle 可执行任意代码，仅用于信任的内部数据，
    禁止反序列化不受信任来源的 pickle 文件。
    """
    with path.open("wb") as f:
        pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)


def load_pickle(path: Path) -> Any:
    """从 pickle 文件加载对象（仅信任来源）。"""
    with path.open("rb") as f:
        return pickle.load(f)
```

要点：
- **JSON**：跨语言、可读、安全 → 首选。
- **CSV**：表格数据，`newline=""` 防 Windows 空行。
- **pickle**：Python 专有、可序列化任意对象、**不安全**（可执行代码）→ 仅信任数据。
- `ensure_ascii=False`：JSON 保留中文，不用 `\uXXXX` 转义。
- `json` 不支持 `Path`/`datetime`，需自定义编码器或先转 `str`/`isoformat`。

## 原子写入

避免写到一半崩溃产生半写文件。

```python
from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import IO


@contextlib.contextmanager
def atomic_write(path: Path, mode: str = "w", encoding: str = "utf-8"):
    """原子写入上下文管理器。

    写入临时文件，成功后原子替换目标。
    崩溃时不会损坏已有文件（临时文件残留，下次清理）。
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        f = tmp.open(mode, encoding=encoding if "b" not in mode else None)
        yield f
        f.flush()
        os.fsync(f.fileno())  # 强制刷盘（防断电丢数据）
        f.close()
        tmp.replace(path)  # 原子替换
    except BaseException:
        try:
            f.close()
        except Exception:
            pass
        tmp.unlink(missing_ok=True)
        raise


# 使用
def save_config_safely(path: Path, config: dict) -> None:
    """安全保存配置（原子写入）。"""
    import json
    with atomic_write(path) as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
```

要点：
- 写临时文件 → `fsync` → `replace`：三步保证原子性。
- `Path.replace()` 在同文件系统内是原子操作（rename 系统调用）。
- 临时文件与目标须在同一文件系统（否则 replace 退化为复制+删除）。
- 高频写入场景可省略 `fsync`（性能优先），关键数据须 `fsync`。

## 批量文件操作

```python
from __future__ import annotations

import shutil
from pathlib import Path


def batch_rename(src_dir: Path, pattern: str, suffix: str) -> list[Path]:
    """批量重命名匹配文件，返回新路径列表。"""
    renamed = []
    for f in src_dir.glob(pattern):
        new_name = f.stem + suffix
        new_path = f.with_name(new_name)
        f.rename(new_path)
        renamed.append(new_path)
    return renamed


def copy_tree(src: Path, dst: Path) -> None:
    """递归复制目录树。"""
    shutil.copytree(src, dst, dirs_exist_ok=True)  # 3.8+ dirs_exist_ok


def clean_old_files(dir_path: Path, pattern: str = "*.log", keep: int = 10) -> int:
    """保留最新 N 个匹配文件，删除其余，返回删除数。"""
    files = sorted(dir_path.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    deleted = 0
    for f in files[keep:]:
        f.unlink()
        deleted += 1
    return deleted


def walk_with_depth(root: Path, max_depth: int = 3) -> dict[int, list[Path]]:
    """按深度分组遍历目录树。"""
    result: dict[int, list[Path]] = {}
    for path in root.rglob("*"):
        depth = len(path.relative_to(root).parts)
        if depth > max_depth:
            continue
        result.setdefault(depth, []).append(path)
    return result
```

要点：
- `shutil.copytree(dirs_exist_ok=True)`：目标已存在不报错（3.8+）。
- 批量操作先收集路径列表再执行，避免边遍历边修改。
- `stat().st_mtime` 按修改时间排序；`stat().st_size` 按大小排序。

## 编码处理

```python
from __future__ import annotations

from pathlib import Path


def detect_encoding(path: Path) -> str:
    """检测文件编码（需 chardet；无则默认 utf-8）。"""
    try:
        import chardet
    except ImportError:
        return "utf-8"
    raw = path.read_bytes()[:4096]  # 读前 4KB 检测
    result = chardet.detect(raw)
    return result.get("encoding") or "utf-8"


def safe_read(path: Path, fallback_encodings: list[str] | None = None) -> str:
    """容错读取：尝试多种编码。"""
    encodings = fallback_encodings or ["utf-8", "gbk", "latin-1"]
    for enc in encodings:
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    # 最后用 utf-8 + errors="replace" 兜底
    return path.read_text(encoding="utf-8", errors="replace")
```

要点：
- 写入一律 `encoding="utf-8"`；读取也显式指定。
- 处理遗留 GBK 文件：依次尝试 `["utf-8", "gbk", "latin-1"]`。
- `errors="replace"`/`"ignore"`：兜底处理不可解码字节（数据有损）。
- `chardet`：检测未知编码（需安装，非标准库）。

## 常见陷阱

1. **字符串拼接路径**：`"/data" + "/" + "file"` 跨平台不兼容。用 `Path("/data") / "file"`。
2. **不指定编码**：Windows 默认 GBK，Linux 默认 UTF-8，跨平台乱码。显式 `encoding="utf-8"`。
3. **大文件 `read_text()`**：内存爆炸。用 `for line in open()` 流式读取。
4. **不 `with`**：异常时文件未关闭，资源泄漏/锁文件。一律 `with`。
5. **半写文件**：写到一半崩溃，已有文件被损坏。用原子写入（临时文件 + replace）。
6. **CSV 不加 `newline=""`**：Windows 产生多余空行。`open(newline="")`。
7. **pickle 反序列化不受信数据**：可执行任意代码（RCE）。仅用 JSON 或信任来源。
8. **边遍历边修改目录**：`glob` 迭代中删除文件导致行为未定义。先收集列表再操作。
9. **临时文件不清理**：`mkstemp` 需手动删除；`NamedTemporaryFile(delete=True)` 自动清理。
10. **跨文件系统 replace**：`Path.replace()` 跨文件系统退化为复制+删除，非原子。临时文件须同文件系统。
