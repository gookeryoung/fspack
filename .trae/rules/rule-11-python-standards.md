---
name: "rule-11-python-standards"
glob: "*.{py,pyi}"
---

# Python 开发规范

## 工具链（独立配置文件，pyproject.toml 仅含项目元数据）

| 工具 | 配置文件 | 配置要点 |
|------|---------|---------|
| ruff | `ruff.toml` | `line-length=120`，`target-version="py38"` |
| pyrefly | `pyrefly.toml` | `preset="strict"`，`python-version="3.8"` |
| pytest | `pytest.ini` | `asyncio_default_fixture_loop_scope="function"`，marker `slow` |
| coverage | `.coveragerc` | `branch=true`，`fail_under=95`，`concurrency=thread` |
| bump-my-version | `.bumpversion.toml` | `current_version` 同步 `pyproject.toml` 与 `__init__.py` |
| uv | `uv.toml` | `required-version`，国内镜像源（可选） |
| pre-commit | `.pre-commit-config.yaml` | ruff `--fix` + trailing-whitespace + end-of-file-fixer |

验证（每次修改后）：

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run pyrefly check
uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95
```

slow 全量测试（端到端构建+运行，耗时较长）：**每 5 次开发循环至少跑一次**，确保示例项目真实可构建可运行。

```bash
uv run pytest --cov=fspack --cov-fail-under=95
```

详细参考：工具链配置拆分决策与完整示例见 `python-project-structure` SKILL「工具链配置拆分」章节；覆盖率排除规则与 `# pragma: no cover` 见 `python-testing` SKILL「覆盖率」章节。

## 兼容性

- 最低 Python 3.8：用 `from __future__ import annotations` 延迟注解求值；按版本 `typing.List` → 内置泛型(3.9) → `X | Y`(3.10) → `typing.override`(3.12)。
- 版本守卫：`if sys.version_info >= (3, X):` 引入高版本 API；低版本回退加 `# pragma: no cover`。
- 优先标准库；`typing-extensions` 用于 `override`/`TypeVar` 前向兼容（`python_version < '3.13'` 时引入）。新增依赖须审慎。

## 类型注解

- 公共 API 必须有完整类型注解（含返回类型）；私有函数也应有注解。
- `Mapping`/`Sequence` 用于只读参数，`dict`/`list` 用于可变返回。`Any` 仅用于真正动态场景。
- 禁用裸 `# type: ignore`，确需时加规则码（如 `# type: ignore[union-attr]`）。
- 仅类型检查的导入放 `if TYPE_CHECKING:` 块内。
- 类型收窄用 `assert isinstance(x, Y)`；`cast()` 仅用于类型系统无法表达的场景。

## 数据结构

- 配置/描述类用 `@dataclass(frozen=True)`；可变类属性标注 `RUF012` 豁免。
- 缓存：实例级 `functools.cached_property`，参数键控 `lru_cache`；不可哈希参数 try/except 回退；修改缓存源后手动清空。
- 接口用 `abc.ABC` + `@abstractmethod`；状态/标志值用 `enum.Enum`（`UPPER_SNAKE`），禁止裸字符串/魔术数字。
- 可变类实现 `__repr__`（含关键字段）。

## 模块与导入

- 单一职责；导入顺序（ruff isort）：`__future__` → 标准库 → 第三方 → 本地，各组间空行。
- 惰性导入仅用于打破循环依赖（函数体内导入并注释）。
- 定义 `__all__` 显式声明导出符号（位置仅次于 `__future__`）。禁用 `from x import *`；避免 `utils.py`/`helpers.py`。

## 函数设计

- 模块级函数优于 Mixin；纯函数直接放模块级（慎用静态方法）。参数 ≤ 5 个，超出用 dataclass 封装。
- 异常范围要窄：只捕获预期异常（如 `(TypeError, ValueError, KeyError, AttributeError)`），**禁止** `except Exception`；捕获后至少 `logger.warning`。
- 可变默认参数用 `None` 哨兵或 `field(default_factory=list)`。

## 异常处理

- 自定义异常继承公共基类，按场景分类；`raise NewError(...) from exc` 保留因果链。
- 不吞异常：捕获后必须处理（记录/包装/重抛），禁止空 `except: pass`。第三方回调异常仅记录，不影响主流程。

## 并发

- `os.environ`/`os.chdir` 等进程全局状态用 `threading.RLock` 序列化。循环内多次 I/O 改为批量一次；按组限流用 `Semaphore`。

## 测试

- 覆盖率 ≥ 95%（branch），不得下降。
- 公共 API 优先通过公共接口测试；故障注入可临时访问私有属性（docstring 注明）。
- 命名 `test_<对象>_<场景>`；原生 `assert`，禁用 `self.assertEqual`；`pytest.raises` 必填 `match=`。
- Mock 优先级：`monkeypatch` > 内联 stub > `unittest.mock` > `pytest-mock`。禁用 `@patch` 装饰器、`mock.patch.object` 上下文、`pytest-mock` 的 `mocker` fixture。
- fixture 优先 `tmp_path`/`monkeypatch`/`capsys`；autouse 仅全局必需时用。耗时测试加 `@pytest.mark.slow`；`tests/**` 忽略 `ARG001`/`ARG002`。

## 代码风格

- 行宽 120；ruff 默认双引号；末尾单 `\n`、无尾随空格。
- 公共 API 必须有中文 docstring；使用中文打印和日志。
- 命名：`snake_case` 函数/变量，`PascalCase` 类，`UPPER_SNAKE` 常量，`_` 前缀私有。不用 emoji。

## Pythonic 风格

- 单例用 `is`，值用 `==`；EAFP 优于 LBYL；`if items:` 优于 `if len(items) > 0:`。
- 字符串首选 f-string（`%` 仅用于 logging 延迟格式化）；推导式优于 `map`+`filter`（> 2 层拆显式循环）。
- `enumerate` 替代 `range(len())`；`zip` 并行迭代（3.10+ `strict=True`）；解包优于索引；海象运算符不滥用。

## 日志

- 每模块 `logging.getLogger(__name__)`，禁用 `print` 调试残留。
- `extra={...}` 传字段；延迟格式化用 `%`；级别：DEBUG 诊断 / INFO 关键流程 / WARNING 可恢复 / ERROR 需介入。禁止日志密码/密钥，脱敏后记录。

## 路径与资源

- 优先 `pathlib.Path`（ruff `PTH` 强制），禁止字符串拼接路径；边界 `str` 立即包装。
- 文件/锁/连接用 `with` 或 `contextlib.contextmanager`；多资源用 `ExitStack`。循环内多次 acquire/release 改为批量一次。

## 安全

- 禁用 `eval`/`exec`（用 `ast.literal_eval`）；`subprocess` 禁用 `shell=True`（优先 `list[str]`）。
- 凭证放 `.env`/环境变量，`.gitignore` 须含 `.env`；日志脱敏。`uv lock` 后审阅新增依赖避免已知 CVE。

## 性能

- 循环内查询缓存或预构建映射；`has(k)` + `get(k)` 改为单次 `get(k)` + `KeyError` 回退。
- 入口校验一次，下游不重复。生命周期事件 emit 完整，不留死分支（`# pragma: no cover` 应激活或删除）。

## Git 与提交

- 任务完成后自动 `git add`（按文件名）+ `git commit`（遵循 `rule-09-git提交规则.md` 风格）+ `git push`（分支已跟踪远程时；新分支跳过并在总结说明）。
- 破坏性命令的暂停边界遵循 `rule-01-开发流程.md` 暂停条件：`force-push`、工作区有改动时的 `reset --hard`、`git clean -f`/`-fd`/`-fx` 需暂停；`git clean -fX`、`git stash`/`stash pop`、`git revert`、工作区干净时的 `reset --hard` 可自主执行。
- staging 按文件名添加，不用 `git add -A`/`git add .`。

## 详细参考

本规则为硬约束简表，各领域详细模式与代码模板见对应 SKILL（调用指引见 `rule-03-触发场景.md`）：

| 章节 | 对应 SKILL |
|------|-----------|
| 工具链 | `python-project-structure`（工具链配置拆分）、`python-testing`（覆盖率） |
| 数据结构 | `python-class-design` |
| 并发 | `python-concurrency` |
| 测试 | `python-testing` |
| 日志 | `python-logging` |
| 路径与资源 | `python-file-io` |
| 安全（subprocess） | `python-subprocess` |
| 性能 | `python-performance` |
| 项目骨架 | `python-project-structure` |
| 配置管理 | `python-config` |
| CLI 入口 | `python-cli` |
