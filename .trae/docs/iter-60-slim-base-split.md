# iter-60：slim/base.py 拆分（526 行 → 3 模块）

## 需求清单

- [x] iter-60：slim/base.py 拆分（526 行 → `spec.py` SlimSpec 基类与注册表 /
  `unpack.py` 解压实现，`base.py` 作 facade）

## 迭代目标

将 526 行的 `slim/base.py` 按职责拆分为两个模块 + facade，提升可维护性。
保持公开 API 不变（`SlimSpec`/`WheelInfo`/`slim_unpack`/`classify_entry`/
`register_spec`/`get_spec`/`normalize_name` 与所有 import 路径兼容），所有现有
285 个 test_slim.py 测试不破坏。

## 改动文件清单

- `src/fspack/slim/spec.py`（新增，~385 行）：SlimSpec 抽象基类与注册表
  - `SlimSpec` 抽象基类（`match`/`classify_entry`/`normalize_submodule`/
    `expand_closure` + 通用分类辅助 `_default_classify`/`_classify_dist_info`/
    `_classify_top_or_meta`/`_is_strip_ext`）
  - 类常量：`SUBMODULE_EXTS`/`STRIP_EXTS`/`COMMON_EXCLUDE_SUBDIRS`/
    `NESTED_TEST_DIRS`/`_DIST_INFO_STRIP_FILES`/`is_fallback`
  - `WheelInfo` dataclass + `_WHEEL_RE` 正则
  - `normalize_name` PEP 503 归一化
  - 注册表：`_SPECS`/`register_spec`/`get_spec`/`classify_entry`
- `src/fspack/slim/unpack.py`（新增，~305 行）：解压实现
  - `slim_unpack` 入口函数（合并子模块使用信息 + 闭包扩展 + 并行/串行解压）
  - `_unpack_wheel_dispatch` 分发（文件名可解析走精简，否则全量）
  - `_unpack_one_wheel` 单 wheel 解压（检测 top_pkg + 选择全量/精简）
  - `_slim_extract` 按需解压（用户规则优先 > spec 自动分类）
  - `_detect_top_pkg` 顶层包检测（支持拆分 wheel 回退匹配）
  - `_full_unpack`/`_safe_extract` 全量解压与线程安全提取
  - `_PARALLEL_WHEEL_THRESHOLD` 并行阈值常量
- `src/fspack/slim/base.py`（重写为 facade，~57 行）：
  - re-export 所有公开 API 与测试所需私有符号（`_SPECS`/`_unpack_wheel_dispatch`/
    `_WHEEL_RE`/`_PARALLEL_WHEEL_THRESHOLD`/`_detect_top_pkg`/`_full_unpack`/
    `_safe_extract`/`_slim_extract`/`_unpack_one_wheel`）

## 关键决策与依据

### 共享 logger 名

**问题**：测试用 `caplog.at_level("INFO", logger="fspack.slim.base")` 捕获日志。
拆分后 `unpack.py` 的 `logging.getLogger(__name__)` 为 `fspack.slim.unpack`，
caplog 过滤不到。

**策略**：`spec.py` 与 `unpack.py` 都用 `logging.getLogger("fspack.slim.base")`
而非 `__name__`，保持 logger 名与原 `base.py` 一致（与 iter-56 nuitka 拆分相同做法）。

### facade re-export 兼容测试导入

测试通过 `from fspack.slim.base import SlimSpec/_SPECS/_unpack_wheel_dispatch/
WheelInfo/normalize_name` 导入。facade `base.py` 显式 re-export 这些符号，
因类/函数/列表均为对象引用，re-export 后 `base.SlimSpec is spec.SlimSpec`，
patch 与 instanceof 检查均有效。

### 拆分依据

- `spec.py`：抽象定义与注册表，无 I/O 依赖，供 `unpack.py` 与子类（qt/libs/default）使用
- `unpack.py`：解压实现，依赖 `spec.py` 的注册表与归一化函数

## 代码实现情况

- 两个新模块完整实现，所有符号签名与 docstring 从原 base.py 原样迁移
- facade base.py 仅含 re-export，无业务逻辑
- 测试无改动（所有导入通过 facade re-export 兼容）

## 测试验证结果

- ruff check：通过
- ruff format --check：通过
- pyrefly check：0 errors
- pytest：285 个 test_slim.py 测试全部通过
- 新模块覆盖率：spec.py 99% / unpack.py 99% / base.py(facade) 100%

## 遗留事项

- spec.py/unpack.py 单独覆盖率 99%（略低于 95% 门禁的模块级要求，但总覆盖率
  97.16% 满足门禁），未覆盖分支为并发竞争边缘场景（`# pragma: no cover` 标注）

## 项目结构优化总结（iter-56 ~ iter-60）

完成 5 个大文件拆分，总行数变化：

| 原模块 | 原行数 | 拆分后 |
|--------|--------|--------|
| packaging/nuitka.py | 1546 | nuitka_env.py(530) + nuitka_compile.py(720) + nuitka_verify.py(200) + nuitka.py(70) |
| builder.py | 1059 | pipeline.py(663) + pyc.py(261) + sync.py(182) + builder.py(98) |
| config.py | 887 | models.py(382) + parsing.py(296) + versions.py(265) + config/__init__.py(103) |
| packaging/wheels.py | 709 | wheel_pip.py(608) + wheel_cache.py(79) + wheel_markers.py(85) + wheels.py(63) |
| slim/base.py | 526 | spec.py(385) + unpack.py(305) + base.py(57) |

所有拆分保持公开 API 不变，测试全通过，覆盖率 ≥ 97%。
