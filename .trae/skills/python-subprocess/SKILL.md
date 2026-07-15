---
name: "python-subprocess"
description: "Python 子进程技能：subprocess.run/Popen、命令构造、输出捕获、流式处理、超时管理、管道链式、环境与工作目录、安全准则、GUI 集成、测试替身。当需要调用外部命令、运行可执行文件、捕获命令输出、实时读取子进程输出、构建命令管道、处理超时与取消、在 QThread 中运行子进程时调用。"
---

# Python 子进程

自包含的外部命令执行指南：`subprocess.run`、`Popen`、命令构造、输出捕获、流式读取、超时管理、管道链式。遵循 rule-11 安全要求：禁用 `shell=True`（优先 `list[str]` 形式），`subprocess.run` 必须 `check=True`（ruff `PLW1510`），编码显式 `encoding="utf-8"`。

## 何时调用

- 需要调用外部可执行文件（git、ffmpeg、imagemagick、编译器、CLI 工具）
- 需要捕获命令输出（stdout/stderr/退出码）
- 需要实时逐行读取子进程输出（流式日志、进度条）
- 需要构建命令管道（多进程流水线、stdout 链接到下一个 stdin）
- 需要超时控制与子进程清理（避免僵尸进程、长时间挂起）
- 需要自定义子进程环境变量或工作目录（`env`、`cwd`）
- 需要在 GUI 中后台运行命令（QThread Worker + 信号转发）

## subprocess.run 基础

`subprocess.run` 是同步阻塞的高级 API，适用于一次性执行并捕获结果。

```python
from __future__ import annotations

import subprocess
from pathlib import Path


def git_branches(repo: Path) -> list[str]:
    """列出 git 仓库所有本地分支名（check=True 抛异常，encoding 显式 UTF-8）。"""
    result = subprocess.run(
        ["git", "branch", "--list", "--format=%(refname:short)"],
        cwd=repo,
        check=True,                  # 非零退出码抛 CalledProcessError
        capture_output=True,         # 捕获 stdout/stderr
        text=True,                   # 返回 str 而非 bytes
        encoding="utf-8",            # 显式编码，Windows 默认 GBK 会乱码
        timeout=10,                  # 10 秒超时抛 TimeoutExpired
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]
```

要点：
- `check=True` 强制要求（ruff `PLW1510`）：非零退出码自动抛 `CalledProcessError`，禁止忽略失败。
- `capture_output=True` 等价 `stdout=PIPE, stderr=PIPE`；`text=True` + `encoding="utf-8"` 解码为 str。
- `timeout` 参数：超时抛 `TimeoutExpired`，子进程会被 kill。
- `CompletedProcess` 字段：`args`/`returncode`/`stdout`/`stderr`。静默执行用 `DEVNULL`。

## 命令构造

命令必须以 `list[str]` 形式传递，每个参数独立元素；禁止 `shell=True`（rule-11 安全要求）。

```python
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path


def convert_video(src: Path, dst: Path, crf: int = 23) -> None:
    """调用 ffmpeg 转码视频（list[str] 形式，shlex.join 仅用于日志显示）。"""
    cmd = [
        "ffmpeg", "-y", "-i", str(src),     # 输入文件
        "-c:v", "libx264", "-crf", str(crf),  # 数字也需转 str
        "-c:a", "aac", str(dst),
    ]
    print(f"执行命令: {shlex.join(cmd)}")   # 仅日志显示
    subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8")
```

要点：
- `list[str]` 形式：参数直接传给 `execv`，不经 shell 解析，天然防注入。
- 数字参数必须 `str(crf)` 显式转字符串；`Path` 对象需 `str(p)` 转换。
- `shlex.join(cmd)`：仅用于日志/调试显示，不可作为执行输入。

## 输出捕获

```python
from __future__ import annotations

import subprocess
from typing import Iterator


def run_and_capture(cmd: list[str]) -> tuple[str, str]:
    """运行命令并返回 (stdout, stderr)。"""
    r = subprocess.run(
        cmd, check=True, capture_output=True, text=True, encoding="utf-8",
    )
    return r.stdout, r.stderr


def run_merge_output(cmd: list[str]) -> str:
    """合并 stdout 与 stderr 到一个流（按时间顺序查看）。"""
    r = subprocess.run(
        cmd, check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,       # stderr 重定向到 stdout
        text=True, encoding="utf-8",
    )
    return r.stdout


def iter_command_output(cmd: list[str], chunk_size: int = 8192) -> Iterator[bytes]:
    """生成器：分块产出子进程 stdout（大输出流式，不爆内存）。"""
    with subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) as proc:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(chunk_size)
            if not chunk:
                break
            yield chunk
        ret = proc.wait()
        if ret != 0:
            err = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            raise subprocess.CalledProcessError(ret, cmd, output=None, stderr=err)
```

要点：
- `capture_output=True` 与 `stdout=PIPE` 互斥；合并输出用 `stderr=subprocess.STDOUT`。
- 大输出禁止 `communicate()`（一次性读全），用 `Popen` + `read(chunk)`。
- `Popen` 默认不抛 `CalledProcessError`，需手动检查 `returncode`。

## Popen 流式处理

实时逐行读取子进程输出（长时间命令的进度反馈）。`readline()` 无数据时阻塞；非阻塞读用 `select`（Unix）或线程轮询，Windows 用 `asyncio`。

```python
from __future__ import annotations

import subprocess
from typing import Iterator


def stream_lines(cmd: list[str]) -> Iterator[str]:
    """生成器：逐行产出子进程 stdout（实时流式）。"""
    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,        # 合并到 stdout
        text=True, encoding="utf-8",
        bufsize=1,                       # 行缓冲（text 模式才有效）
    ) as proc:
        assert proc.stdout is not None
        for line in proc.stdout:         # 逐行迭代，实时产出
            yield line.rstrip("\n")
        proc.wait()
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd)
```

要点：
- `bufsize=1` 行缓冲：仅在 `text=True` 时生效；二进制模式用 `-1`（默认全缓冲）。
- `for line in proc.stdout` 逐行迭代，实时产出，内存友好。
- `Popen` 不自动 `wait()`，需显式调用或在 `with` 退出时自动 wait。

## 超时管理

`subprocess.run` 的 `timeout` 与 `Popen.communicate(timeout=)` 都会在超时后 kill 子进程；`Popen` 自身需手动清理。

```python
from __future__ import annotations

import subprocess


def popen_with_timeout(cmd: list[str], timeout: float = 30) -> str:
    """Popen 带超时：超时后必须手动 kill + communicate 回收（run 自动 kill）。"""
    with subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    ) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
            if proc.returncode != 0:
                raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)
            return out
        except subprocess.TimeoutExpired:
            proc.kill()                  # 发 SIGKILL（强制终止）
            proc.communicate()           # 必须再 communicate 回收僵尸
            raise


def graceful_terminate(proc: subprocess.Popen, grace: float = 5) -> int:
    """优雅终止：SIGTERM → 等待 grace 秒 → SIGKILL 回退（SIGTERM 允许子进程清理资源）。"""
    proc.terminate()                         # 等价 SIGTERM
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()                          # SIGKILL，强制
        proc.wait()
    return proc.returncode if proc.returncode is not None else -1
```

要点：
- `subprocess.run(timeout=)` 超时自动 kill 并回收；`Popen` 超时后**必须** `kill()` + `communicate()` 回收僵尸。
- `terminate()` = SIGTERM（优雅）；`kill()` = SIGKILL（强制）。优先 `terminate()` 回退。

## 管道与链式

用 `Popen` 将前一个进程的 `stdout` 连接到下一个的 `stdin`。

```python
from __future__ import annotations

import subprocess


def pipeline_two_stage(stage1: list[str], stage2: list[str]) -> str:
    """两阶段管道：等价 `cmd1 | cmd2`。"""
    p1 = subprocess.Popen(
        stage1, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )
    assert p1.stdout is not None
    p2 = subprocess.Popen(
        stage2, stdin=p1.stdout,           # p1 的 stdout 作为 p2 的 stdin
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )
    p1.stdout.close()                       # 允许 p1 在 stdout 关闭时收到 SIGPIPE
    out, err = p2.communicate()
    p1.wait()
    if p1.returncode != 0:
        raise subprocess.CalledProcessError(p1.returncode, stage1)
    if p2.returncode != 0:
        raise subprocess.CalledProcessError(p2.returncode, stage2, out, err)
    return out
```

要点：
- `stdin=p1.stdout`：将上游 `stdout` 接到下游 `stdin`；关闭 `p1.stdout` 避免 `SIGPIPE` 死锁。
- 必须按顺序 `communicate()` 下游再 `wait()` 上游。
- 多级管道同理扩展：`p2.stdout → p3.stdin`，逐级连接。
- shell 管道符 `cmd1 | cmd2` **禁用**（需 `shell=True`）；改用 `Popen` 链式。

## 环境与工作目录

```python
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Mapping


def run_with_custom_env(cmd: list[str], extra_env: Mapping[str, str]) -> str:
    """运行命令并附加环境变量（env={**os.environ, ...} 不污染原 os.environ）。"""
    env = {**os.environ, **extra_env}        # 合并，不修改原 os.environ
    r = subprocess.run(
        cmd, check=True, capture_output=True, text=True,
        encoding="utf-8", env=env,
    )
    return r.stdout


def git_with_config(cmd: list[str], configs: dict[str, str]) -> str:
    """通过 GIT_CONFIG_PARAMETERS 环境变量传递 git 配置（避免 -c 参数膨胀）。"""
    params = " ".join(f"'{k}={v}'" for k, v in configs.items())
    env = {**os.environ, "GIT_CONFIG_PARAMETERS": params}
    r = subprocess.run(
        cmd, check=True, capture_output=True, text=True,
        encoding="utf-8", env=env,
    )
    return r.stdout


def git_status(repo: Path) -> str:
    """在指定目录下执行 git status（cwd 参数指定子进程工作目录）。"""
    r = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,                           # str 或 Path
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    return r.stdout
```

要点：
- `env={**os.environ, "KEY": "value"}`：基于当前环境**复制**后修改，**禁止**直接修改 `os.environ`。
- `cwd` 参数：指定子进程工作目录（`str` 或 `Path`）。不传 `env` 则继承父进程所有环境变量。
- 凭证（token、密码）**禁止**放在命令行参数（进程列表可见），用环境变量传递。
- `GIT_CONFIG_PARAMETERS`：批量传 git 配置，避免命令行 `-c` 参数膨胀。

## 安全准则

rule-11 强制：禁用 `shell=True`，参数来自用户输入时必须用 `list[str]` 形式或 `shlex.quote`。

```python
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


# 反面（禁用）：subprocess.run(f"ls {user_input}", shell=True)  # 命令注入！
# user_input = "; rm -rf /" 会执行 rm


# 正面：list[str] 形式
def safe_ls(directory: str) -> list[str]:
    """安全列目录：list[str] 形式天然防注入。"""
    r = subprocess.run(
        ["ls", directory], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return r.stdout.splitlines()


# 用户输入路径校验：白名单正则 + resolve() + 父目录校验
SAFE_PATH_RE = re.compile(r"^[a-zA-Z0-9_\-./]+$")


def validate_user_path(path: str) -> Path:
    """校验用户输入的路径，只允许字母数字与有限符号。"""
    if not SAFE_PATH_RE.match(path):
        raise ValueError(f"非法路径字符: {path!r}")
    p = Path(path).resolve()
    allowed_root = Path("/data/uploads").resolve()
    if allowed_root not in p.parents and p != allowed_root:
        raise ValueError(f"路径越权: {p}")
    return p


# 凭证通过环境变量传递（不暴露在命令行）
def call_api_with_token(endpoint: str, token: str) -> str:
    """API token 通过环境变量传递，不暴露在命令行（进程列表可见）。"""
    env = {**os.environ, "API_TOKEN": token}
    r = subprocess.run(
        ["curl", "-s", "-H", "Authorization: Bearer $API_TOKEN", endpoint],
        check=True, capture_output=True, text=True,
        encoding="utf-8", env=env,
    )
    return r.stdout
```

要点：
- **禁用 `shell=True`**（rule-11 硬约束）：`list[str]` 形式直接传 `execv`，不经 shell，天然防注入。
- 用户输入路径：白名单正则 + `resolve()` + 父目录校验，防止越权。
- 凭证（token/密码）**禁止**传命令行参数（`ps` 可见），用环境变量。
- `shlex.quote()`/`shlex.join()`：仅在必须拼字符串（日志显示）时用，**不**用于执行。

## GUI 集成

在 PySide 中运行子进程须用 QThread Worker 模式（参考 `gui-pyside` SKILL）：Worker 在后台线程 `run` 子进程，通过信号将输出转发到主线程更新 UI。

```python
from __future__ import annotations

import subprocess
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal


class CommandWorker(QObject):
    """在 QThread 中运行子进程，逐行通过信号转发输出到主线程。"""

    output_line = Signal(str)       # 每行 stdout 触发
    finished = Signal(int)          # 进程结束，参数为退出码

    def __init__(self, cmd: list[str], cwd: Optional[str] = None) -> None:
        super().__init__()
        self._cmd = cmd
        self._cwd = cwd
        self._proc: Optional[subprocess.Popen] = None
        self._cancelled = False

    def run(self) -> None:
        """线程入口：启动子进程并实时转发输出。"""
        try:
            self._proc = subprocess.Popen(
                self._cmd, cwd=self._cwd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", bufsize=1,  # 行缓冲
            )
            assert self._proc.stdout is not None
            for line in self._proc.stdout:       # 实时逐行读
                if self._cancelled:
                    break
                self.output_line.emit(line.rstrip("\n"))
            self._proc.wait()
            self.finished.emit(self._proc.returncode or 0)
        except Exception as e:
            self.output_line.emit(f"执行异常: {e}")
            self.finished.emit(-1)

    def cancel(self) -> None:
        """取消执行：设置标志并终止子进程。"""
        self._cancelled = True
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()               # 优雅 SIGTERM
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()                # 强制 SIGKILL
                self._proc.wait()
# 主线程使用：worker.moveToThread(thread); thread.started.connect(worker.run)
# worker.output_line.connect(ui.append_output); worker.finished.connect(thread.quit)
```

要点：
- **禁止在主线程运行 `subprocess.run`**：会阻塞 UI 事件循环。
- `Signal` 跨线程自动排队（`Qt.QueuedConnection`），主线程槽函数线程安全。
- 取消：`_cancelled` 标志 + `terminate()`/`kill()` 双保险。
- 进度估计：根据行数/已知总行数比例，或解析子进程特定输出（如 ffmpeg `frame=`）。

## 测试子进程

不依赖真实外部命令，用 `monkeypatch` 替换 `subprocess.run` 或伪造 `Popen`。

```python
"""fspack 子进程测试."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest


def test_git_branches_parses_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """git_branches 应正确解析分支名列表。"""
    from fspack.vcs import git_branches

    fake_completed = subprocess.CompletedProcess(
        args=["git", "branch"], returncode=0,
        stdout="main\ndevelop\nfeature/x\n", stderr="",
    )

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        assert cmd[:2] == ["git", "branch"]       # 校验调用参数
        assert kwargs.get("check") is True
        assert kwargs.get("encoding") == "utf-8"
        return fake_completed

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert git_branches(tmp_path) == ["main", "develop", "feature/x"]


class FakePopen:
    """伪造 Popen，支持 stdout 迭代与 wait()。"""

    def __init__(self, lines: list[str], returncode: int = 0) -> None:
        self.stdout = iter(lines)               # 支持逐行迭代
        self.stderr = iter([""])
        self.returncode = returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15                   # SIGTERM

    def kill(self) -> None:
        self.returncode = -9                    # SIGKILL

    def __enter__(self) -> "FakePopen": return self
    def __exit__(self, *args: Any) -> None: pass


def test_stream_lines_yields_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """stream_lines 应逐行产出 stdout。"""
    from fspack.runner import stream_lines

    monkeypatch.setattr(
        subprocess, "Popen",
        lambda cmd, **kw: FakePopen(["line1", "line2", "line3"], returncode=0),
    )
    assert list(stream_lines(["echo", "test"])) == ["line1", "line2", "line3"]
```

要点：
- `monkeypatch.setattr(subprocess, "run", fake_run)`：替换 `subprocess.run`，测试结束自动还原。
- `FakePopen`：实现 `stdout`/`wait()`/`terminate()`/`kill()` 接口，覆盖流式与取消路径。
- 禁止依赖真实外部命令（git/ffmpeg）：CI 环境可能未安装。`capsys` 不能捕获子进程输出，须用 `capture_output`。
- 校验 `check=True`/`encoding`：fake_run 中断言 kwargs 防止回归。

## 常见陷阱

1. **`shell=True` 命令注入**：`subprocess.run(f"ls {user}", shell=True)` 中 `user="; rm -rf /"` 会执行 rm。禁用 `shell=True`，用 `list[str]` 形式（rule-11 硬约束）。
2. **缺 `check=True`**：非零退出码被静默忽略，错误隐藏。ruff `PLW1510` 强制 `check=True`（rule-11 工具链）。
3. **不指定 `encoding`**：Windows 默认 GBK 解码 UTF-8 输出乱码。显式 `encoding="utf-8"`。
4. **大输出 `communicate()`**：一次性读全到内存，输出过 GB 时 OOM。用 `Popen` + `readline()`/`read(chunk)`。
5. **`Popen` 超时不回收**：`communicate(timeout=)` 超时后子进程仍在跑，必须 `kill()` + `wait()` 回收僵尸。
6. **管道死锁**：上游 `stdout` 未 close，下游提前退出后上游阻塞写。`p1.stdout.close()` 在父进程关闭。
7. **`bufsize=1` 在二进制无效**：行缓冲仅 `text=True` 生效；二进制需手动 `readline()`。
8. **凭证放命令行参数**：`["curl", "-H", f"Authorization: Bearer {token}"]` 在 `ps` 中可见。用环境变量传递。
9. **修改 `os.environ` 污染全局**：`os.environ["KEY"] = "x"` 影响后续所有子进程。用 `env={**os.environ, ...}` 副本。
10. **GUI 主线程 `subprocess.run`**：阻塞事件循环，UI 卡死。用 QThread Worker + `Popen` 流式 + 信号转发。
