# 迭代 23：slim 重构为包，类继承形式按包分发

## 迭代目标

将单文件 `src/fspack/slim.py` 重构为 `src/fspack/slim/` 包，采用类继承形式
让用户能够按不同包配置精简规则。参考 fspacker `packers.libspec` 设计：

- 抽象基类 `SlimSpec` 定义 4 个核心接口（match/normalize_submodule/
  expand_closure/classify_entry）
- 注册表 `register_spec`/`get_spec` 按 wheel 归一化包名分发到对应 spec 子类
- `QtSlimSpec`（PySide2/PySide6/PyQt5/PyQt6 共享）+ `DefaultSlimSpec`（兜底）
- 新增包精简规则只需继承 `SlimSpec` 并注册，无需修改分发逻辑

## 需求清单

- [x] 把 slim.py 重构为 slim/ 包（base.py/default.py/qt.py/__init__.py）
- [x] 抽象基类 SlimSpec + 注册表机制
- [x] QtSlimSpec：Qt 库白名单 + 模块依赖闭包
- [x] DefaultSlimSpec：非 Qt 库兜底
- [x] slim_unpack 与 classify_entry 按 spec 注册表分发
- [x] 向后兼容：`from fspack.slim import _qt_module_closure` 等仍可用
- [x] 向后兼容：`fspack.slim.zipfile.ZipFile` 仍可被 monkeypatch
- [x] 测试覆盖 spec 类、注册表分发、stage 回调
- [x] 门禁通过（ruff/pyrefly/pytest，覆盖率 98.30%）

## 改动文件清单

### 核心实现

- `src/fspack/slim/base.py`（新增）：`SlimSpec` 抽象基类、`register_spec`/
  `get_spec` 注册表、`classify_entry`/`slim_unpack` 入口函数、`_detect_top_pkg`/
  `_full_unpack`/`_slim_extract` 解压实现、`override` 装饰器兼容导入
- `src/fspack/slim/default.py`（新增）：`DefaultSlimSpec`——非 Qt 库兜底规则
- `src/fspack/slim/qt.py`（新增）：`QtSlimSpec` + `_normalize_qt_sub`/
  `_qt_dll_submodule`/`_qt_module_closure` + Qt 模块依赖映射
- `src/fspack/slim/__init__.py`（新增）：导出公共 API、按指定顺序注册内置 spec
- `src/fspack/slim.py`（删除）：原单文件，逻辑拆分到上述 4 个文件

### 配置

- `pyproject.toml`：新增 `typing-extensions>=4.0; python_version < '3.13'`
  依赖（rule-11 允许，用于 `@override` 装饰器兼容 Python 3.8-3.12）

### 测试

- `tests/test_slim.py`：新增 `TestDefaultSlimSpec`（8 个，覆盖默认规则全分支）、
  `TestSlimSpecRegistry`（5 个，覆盖注册表分发与自定义 spec 注册）、
  `TestQtSubdirSharedFallback`（1 个，Qt 子目录兜底）、
  `TestSlimUnpackStageCallback`（2 个，stage 回调与 None 边界）

## 关键决策与依据

### 1. 类继承 + 注册表分发（参考 fspacker libspec）

原 `slim.py` 用 `is_qt`/`is_abi_pkg` 标志在 `classify_entry` 中分支处理，
扩展新包规则需修改 `classify_entry`。重构为 `SlimSpec` 抽象基类 + 注册表后：

- 每种包（Qt、默认、未来的 numpy/pygame 等）独立一个 spec 子类
- `get_spec(whl_pkg)` 按注册顺序匹配，首个 `match()` 命中的 spec 生效
- 新增规则只写新文件，不改分发逻辑（OCP）

参考 fspacker `ChildLibSpecPacker`（PySide2Packer/PygamePacker/MatplotlibSpecPacker
等）的 `PATTERNS`/`EXCLUDES` 模型，但 fspack 的精简机制更动态（基于子模块
import + 闭包推导），无需 PATTERNS 白名单。

### 2. 注册顺序不依赖 import 顺序

首版用 `@register_spec` 装饰器在模块导入时自动注册，但 ruff isort 会重排
`from fspack.slim import default` 到 `qt` 之前（按字母顺序），导致
`DefaultSlimSpec.match` 始终 True 提前命中，所有包都走默认规则。

最终方案：在 `__init__.py` 显式调用 `register_spec(QtSlimSpec)` 再
`register_spec(DefaultSlimSpec)`，注释说明顺序约束。Python 模块只初始化
一次，无需去重。

### 3. @override 装饰器兼容

pyrefly strict preset 检查 `missing-override-decorator` 与
`bad-override-param-name`：子类覆盖父类方法必须加 `@override` 且参数名一致。

Python 3.8-3.12 没有 `typing.override`，需引入 `typing-extensions`。
按 rule-11「typing-extensions 用于 override/TypeVar 前向兼容
（python_version < '3.13' 时引入）」允许引入。

`base.py` 顶部条件导入：
```python
if sys.version_info >= (3, 12):  # pragma: no cover
    from typing import override
else:
    from typing_extensions import override
```

`# pragma: no cover` 标记版本守卫分支（单 Python 版本测试无法覆盖两分支）。

### 4. 向后兼容：私有符号重新导出

原 `slim.py` 中 `_qt_module_closure`/`_qt_dll_submodule`/`_normalize_qt_sub`
被测试直接 `from fspack.slim import`。重构后这些函数仍在 `qt.py`，
`__init__.py` 重新导出保持导入路径不变。

测试 `monkeypatch.setattr("fspack.slim.zipfile.ZipFile", ...)` 依赖
`fspack.slim.zipfile` 命名空间属性，`__init__.py` 显式 `import zipfile`
暴露在包命名空间。

### 5. spec 注册表兜底防御

`get_spec` 末尾 `return _SPECS[-1]` 是防御代码——`DefaultSlimSpec.match`
始终 True 保证不会到达。加 `# pragma: no cover` 标记，避免拉低覆盖率。

### 6. _classify_top_or_meta 通用辅助

`SlimSpec._classify_top_or_meta()` 处理 metadata（`*.dist-info/**`）与
跨包 shared（`parts[0] != top_pkg`）两类通用分类。子类 `classify_entry`
开头调用此辅助，命中则直接返回，未命中走具体规则。避免重复代码。

## 代码实现情况

### SlimSpec 抽象基类（base.py）

```python
class SlimSpec(abc.ABC):
    SUBMODULE_EXTS: frozenset[str] = frozenset({".pyd", ".pyi", ".so"})

    @classmethod
    @abc.abstractmethod
    def match(cls, whl_pkg: str) -> bool: ...

    @classmethod
    @abc.abstractmethod
    def normalize_submodule(cls, sub: str) -> str: ...

    @classmethod
    @abc.abstractmethod
    def expand_closure(cls, subs: set[str]) -> set[str]: ...

    @classmethod
    @abc.abstractmethod
    def classify_entry(cls, entry: str, top_pkg: str, keep_subs: set[str]) -> tuple[str, str | None]: ...

    @classmethod
    def _classify_top_or_meta(cls, entry: str, top_pkg: str) -> tuple[str, str | None] | None:
        """通用 metadata 与跨包 shared 分类。."""
        ...
```

### 注册表（base.py）

```python
_SPECS: list[type[SlimSpec]] = []

def register_spec(spec: type[SlimSpec]) -> type[SlimSpec]:
    _SPECS.append(spec)
    return spec

def get_spec(whl_pkg: str) -> type[SlimSpec]:
    for spec in _SPECS:
        if spec.match(whl_pkg):
            return spec
    return _SPECS[-1]  # pragma: no cover
```

### slim_unpack 按 spec 分发（base.py）

```python
def slim_unpack(...):
    # 合并 submodule_usage 与 keep_modules，按 spec 归一化子模块名
    for pkg, subs in submodule_usage.items():
        spec = get_spec(normalize_name(pkg))
        merged[pkg_norm] = {spec.normalize_submodule(s) for s in subs}
    # 应用各 spec 的依赖闭包扩展
    for pkg, subs in merged.items():
        spec = get_spec(pkg)
        subs.update(spec.expand_closure(subs))
    # 解压时按 spec 分类条目
    for whl in wheels:
        ...
        spec = get_spec(normalize_name(top_pkg))
        for info in zf.infolist():
            category, sub = spec.classify_entry(info.filename, top_pkg, keep_subs)
            ...
```

### __init__.py 显式按顺序注册

```python
from fspack.slim.base import (SlimSpec, classify_entry, get_spec, register_spec, slim_unpack)
from fspack.slim.default import DefaultSlimSpec
from fspack.slim.qt import (QT_PACKAGES, QtSlimSpec, _normalize_qt_sub, _qt_dll_submodule, _qt_module_closure)

# 显式按顺序注册：QtSlimSpec 优先于 DefaultSlimSpec（兜底）
register_spec(QtSlimSpec)
register_spec(DefaultSlimSpec)
```

## 整合优化情况

- **OCP 扩展**：新增包精简规则只需新增 `slim/<pkg>.py` 继承 `SlimSpec`，
  在 `__init__.py` 调用 `register_spec` 注册，无需修改 `classify_entry`/
  `slim_unpack` 分发逻辑
- **职责分离**：base.py 只管分发与解压编排；qt.py 集中 Qt 库规则；
  default.py 兜底。每个文件单一职责
- **测试结构对齐**：`TestDefaultSlimSpec`/`TestSlimSpecRegistry`/
  `TestQtSubdirSharedFallback` 等测试类与代码结构对应
- **覆盖率提升**：slim 包全部 100%，总覆盖率从 iter-20 的 98.10% 提升到
  98.30%

## 测试验证结果

### 门禁

- `ruff check`：All checks passed
- `ruff format --check`：50 files already formatted
- `pyrefly check`：0 errors
- `pytest -m "not slow" --cov=fspack`：480 passed，覆盖率 98.30%
  （slim/__init__.py 100%、slim/base.py 100%、slim/default.py 100%、
  slim/qt.py 100%）

### 新增测试

- `tests/test_slim.py`：
  - `TestDefaultSlimSpec`（8 个）：match/normalize_submodule/expand_closure/
    classify_entry 全分支（dist-info/cross-pkg/init/private/other-top/
    subdir/pyd-submodule）
  - `TestSlimSpecRegistry`（5 个）：get_spec Qt/默认分发、classify_entry
    分发、自定义 spec 注册
  - `TestQtSubdirSharedFallback`（1 个）：Qt 非 plugins/resources/qml 子目录
  - `TestSlimUnpackStageCallback`（2 个）：stage 回调与 None 边界

## 遗留事项

- 暂未新增其他包（如 numpy/pygame/matplotlib）的专属 spec——当前非 Qt 库
  走 `DefaultSlimSpec` 兜底（顶层 .pyd/.pyi/.so 按子模块选择性保留，子目录
  全保留）。如未来某包需要更精细的精简（如剥离 tests/docs 子目录），
  新增 `<pkg>.py` + `register_spec` 即可
- fspacker 的 `PATTERNS`/`EXCLUDES` 显式白名单模型未采用——fspack 的
  子模块 import + 闭包推导机制更动态，但若某包需要显式白名单（如
  matplotlib 只保留 `matplotlib/*`/`matplotlib.libs/*`/`mpl_toolkits/*`），
  可在对应 spec 子类中实现 `classify_entry` 返回 `submodule`/`exclude`
- `typing-extensions` 作为新直接依赖加入 `pyproject.toml`——已是众多
  第三方库的传递依赖（pytest、pyrefly 等），无额外安装成本

## 下一轮计划

无。本迭代需求清单全部完成，门禁通过。
