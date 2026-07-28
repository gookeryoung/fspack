# iter-98: 测试 fixture 共享化

## 需求清单

- [x] 审查 tests/ 下 30 个测试文件，识别重复的 fixture / 桩类 / 守卫函数
- [x] 提取重复 ≥ 2 处且完全一致的符号到共享位置
- [x] 全套门禁通过（ruff / pyrefly / pytest / coverage ≥ 95%）

## 迭代目标

对应 req-47 阶段 3「CI 与跨平台」的 iter-98 测试 fixture 共享化项。原
目标"审查 tests/ 下 30 个测试文件，识别重复的 fixture（tmp_path 包装、
mock subprocess、样本项目构造等）提取到 `tests/conftest.py`。减少测试
代码重复，提升新测试编写效率"。

## 重复识别结论

经 grep 全量扫描 tests/ 目录，识别出以下重复模式：

| 符号 | 类型 | 重复次数 | 一致性 | 提取决策 |
|------|------|---------|--------|---------|
| `_MIRROR` | 常量 | 2 处（test_runtime / test_offline_mode） | 完全一致 | ✅ 提取为 `mirror` fixture |
| `_Completed` | 桩类 | 5 处模块级 + 7 处嵌套 | 模块级完全一致；嵌套带额外字段 | ✅ 模块级 5 处合并为 `CompletedStub`；嵌套保留本地 |
| `_FakeResp` | 桩类 | 2 处模块级 + 3 处嵌套 | 模块级一致（带 block_size）；嵌套简化版 | ✅ 模块级 2 处合并为 `FakeResp`；嵌套保留本地 |
| `_fail_urlopen` / `fail_urlopen` | 守卫函数 | 1 处模块级 + 4 处嵌套 | 完全一致 | ✅ 模块级 1 处提取为 `fail_urlopen`；嵌套 4 处替换为导入版 |
| `_make_info` | 工厂函数 | 2 处（test_installer / test_linux_installer） | 签名差异大（app_type 参数） | ❌ 不提取 |
| `_make_tar` / `_make_zip` / `_copy_example` / `_make_dist` | 工厂函数 | 各 1 处 | — | ❌ 不提取（仅 1 处） |
| `sample_project` / `sample_wheel` 等 | fixture | 1 处（test_perf_baseline） | — | ❌ 不提取（仅 1 处） |

**决策原则**：遵循 rule-01「三处相似才考虑提取，不过早抽象」，仅提取
重复 ≥ 2 处且完全一致的符号；带额外字段的嵌套桩（如 test_runner.py
中带 `args` 的 `_Completed`）保留本地定义，避免为统一接口而牺牲语义清晰度。

## 改动文件清单

### 新增

- `tests/conftest.py`
  - `mirror` fixture：返回 `MirrorConfig(name="t", python_base="https://x/py", pypi_index="https://x/s")`
  - 替换 test_runtime.py 与 test_offline_mode.py 的 `_MIRROR` 模块常量
- `tests/_stubs.py`
  - `CompletedStub` 类：`returncode`/`stdout`/`stderr` 三属性，替换 5 处 `_Completed`
  - `FakeResp` 类：支持分块 `read(n)` 的 urlopen 响应桩，替换 2 处模块级 `_FakeResp`
  - `fail_urlopen` 函数：离线模式守卫，替换 1 处模块级 `_fail_urlopen` + 4 处嵌套 `fail_urlopen`

### 修改

- `tests/test_runtime.py`
  - 删除本地 `_MIRROR` 常量、`_FakeResp` 类定义
  - 新增 `from tests._stubs import FakeResp`
  - 8 个测试函数加 `mirror: MirrorConfig` 参数（用 fixture 替换常量）
- `tests/test_net.py`
  - 删除本地 `_FakeResp` 类定义（含 `import io`）
  - 新增 `from tests._stubs import FakeResp`
- `tests/test_offline_mode.py`
  - 删除本地 `_MIRROR` 常量、`_Completed` 类定义
  - 新增 `from tests._stubs import CompletedStub, fail_urlopen`
  - 4 处嵌套 `fail_urlopen` 定义删除，直接用导入版
  - 3 个测试函数加 `mirror: MirrorConfig` 参数
  - 嵌套 `_FakeResp`（简化版，无 block_size）保留本地定义
- `tests/test_wheels.py`
  - 删除本地 `_Completed` 类定义
  - 新增 `from tests._stubs import CompletedStub`
- `tests/test_loader.py`
  - 删除本地 `_Completed` 类定义
  - 新增 `from tests._stubs import CompletedStub`
- `tests/test_installer.py`
  - 删除本地 `_Completed` 类定义
  - 新增 `from tests._stubs import CompletedStub`
- `tests/test_linux_installer.py`
  - 删除本地 `_Completed` 类定义
  - 新增 `from tests._stubs import CompletedStub`
- `tests/test_offline_integration.py`
  - 删除本地 `_fail_urlopen` 模块级函数
  - 新增 `from tests._stubs import fail_urlopen`

## 关键决策与依据

### conftest.py vs 单独模块

`tests/__init__.py` 存在（空文件），tests 是包。pytest 用 prepend import mode，
将项目根加入 sys.path 而非 tests/。因此：

- **fixture** 放 `conftest.py`：pytest 自动发现，无需导入
- **桩类与守卫函数** 放 `tests/_stubs.py`：测试文件用 `from tests._stubs import XxxStub`
  显式导入（包内导入，路径稳定）

不直接从 conftest.py 导入非 fixture 符号（pytest 不推荐），也不删除
`tests/__init__.py`（避免引入未知风险）。

### 模块级桩 vs 嵌套桩

- **模块级桩**（跨多文件重复）：提取到 `_stubs.py`
- **嵌套桩**（单文件内、带上下文额外字段）：保留本地

例如 `test_runner.py` 的 7 处嵌套 `_Completed` 带 `args` 属性用于断言命令行参数，
与模块级 `CompletedStub` 接口不同，强行合并会引入 `Optional[list[str]]` 字段
污染通用桩。同理 `test_offline_mode.py` 的嵌套 `_FakeResp` 是简化版（无 block_size，
首次 read 返回 DATA 后返回空），行为与 `FakeResp` 不同，保留本地。

### fixture vs 常量

`_MIRROR` 原是模块常量。改为 `mirror` fixture 后：

- 优点：每次测试独立实例，避免可变状态共享；pytest fixture 生态原生支持
- 代价：测试函数需加 `mirror: MirrorConfig` 参数

由于 `MirrorConfig` 是 `@dataclass(frozen=True)`，本质不可变，fixture 与常量
等价。选 fixture 是为与 pytest 最佳实践一致。

## 代码实现情况

### tests/_stubs.py 核心结构

```python
class CompletedStub:
    """subprocess.run 成功返回值桩."""
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeResp:
    """urlopen 响应桩，支持分块 read(n)."""
    def __init__(self, data: bytes, block_size: int = 64) -> None: ...
    def read(self, n: int = -1) -> bytes: ...
    def __enter__(self) -> FakeResp: ...
    def __exit__(self, *a: object) -> bool: ...


def fail_urlopen(*a: object, **kw: object) -> object:
    """离线模式守卫：被 mock 的 urlopen 不应被调用."""
    raise AssertionError("离线模式不应触发网络请求")
```

### tests/conftest.py fixture

```python
@pytest.fixture
def mirror() -> MirrorConfig:
    """测试用 MirrorConfig 常量 fixture."""
    return MirrorConfig(name="t", python_base="https://x/py", pypi_index="https://x/s")
```

### 测试文件使用示例

```python
# 旧：模块常量 + 本地桩
_MIRROR = MirrorConfig(name="t", python_base="https://x/py", pypi_index="https://x/s")

class _Completed:
    returncode = 0
    stdout = ""
    stderr = ""

def test_xxx(tmp_path, monkeypatch):
    download_embed("3.11.9", _MIRROR, tmp_path / "cache")

# 新：fixture + 共享桩
from tests._stubs import CompletedStub

def test_xxx(tmp_path, monkeypatch, mirror: MirrorConfig):
    download_embed("3.11.9", mirror, tmp_path / "cache")
```

## 整合优化情况

- 7 个测试文件去除本地桩定义，统一从 `tests._stubs` 导入，减少 ~80 行重复代码
- `mirror` fixture 替换 2 处 `_MIRROR` 常量，统一镜像配置入口
- 4 处嵌套 `fail_urlopen` 守卫函数替换为导入版，消除"同函数不同名"困扰
- 嵌套桩保留本地，维护语义清晰度，不为合并而合并

## 测试验证结果

- `uv run ruff check src tests` — All checks passed
- `uv run ruff format --check src tests` — 100 files already formatted
- `uv run pyrefly check` — 0 errors (7 suppressed, 7 warnings)
- `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95` —
  1448 passed, 1 skipped, 32 deselected, coverage 97.61%

### 代码量变化

| 文件类别 | 行数变化 |
|---------|---------|
| 新增 conftest.py | +27 |
| 新增 _stubs.py | +73 |
| 7 个测试文件去重 | -80（净减） |
| **总计** | **+20（净增 20 行，换取 7 文件去重）** |

## 遗留事项

- **嵌套桩未提取**：`test_runner.py` 7 处嵌套 `_Completed`（带 `args` 字段）、
  `test_offline_mode.py` 嵌套 `_FakeResp`（简化版）保留本地。未来若嵌套桩
  增多可考虑参数化 `CompletedStub` 支持 `args` 字段
- **`_make_info` 工厂函数**：test_installer.py 与 test_linux_installer.py 各有
  `_make_info`，签名差异（app_type 参数），未提取。未来可参数化提取
- **test_perf_baseline.py fixture**：sample_project/sample_wheel/sample_pyproject_project
  仅 1 处使用，未提取。若后续新增性能测试可复用

## 下一轮计划

iter-99：按 req-47 阶段 3 推进 macOS runtime + loader 支持。

1. `Platform` 枚举新增 `MACOS`；`detect_platform` 识别 Darwin
2. `StandaloneRuntime` 扩展支持 macOS（python-build-standalone 提供 macOS
   x86_64 + arm64 tarball）
3. 新增 `MacLoader`（clang 编译，dlopen libpython3.X.dylib，Mach-O 格式，
   `@executable_path` 解析 runtime 路径）
4. `wheel_platform_tags` 新增 macOS 标签（macosx_11_0_x86_64 / macosx_11_0_arm64）
5. 测试：mock clang 编译，验证 C 源码生成与缓存键
