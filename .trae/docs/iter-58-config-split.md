# iter-58：config.py 拆分（887 行 → 4 模块）

## 需求清单

- [x] iter-58：config.py 拆分（887 行 → `models.py` dataclass /
  `parsing.py` toml 解析 / `versions.py` 版本解析，`config/__init__.py` 作 facade）

## 迭代目标

将 887 行的 `config.py` 按职责拆分为三个模块 + facade，提升可维护性。
保持公开 API 不变（`parse_project`/`resolve_py_version`/`ProjectInfo`/
`BuildConfig` 等与所有 import 路径兼容），所有现有测试不破坏。

## 改动文件清单

- `src/fspack/config/models.py`（新增，~382 行）：数据结构与镜像源
  - `AppType`/`MirrorConfig`/`EntryPoint`/`ProjectInfo`/`DependencyReport`/
    `BuildConfig`/`BuildOptions`/`SlimRules`/`BuildDefaults` 等 dataclass/enum
  - `MIRRORS`/`DEFAULT_MIRROR`/`get_mirror` 镜像源定义
  - `_parse_string_list_cfg`/`_match_any_glob` 工具函数
  - `build_options_from_defaults` 构造函数
- `src/fspack/config/parsing.py`（新增，~296 行）：pyproject.toml 解析
  - `parse_project` 项目解析入口
  - `detect_entry`/`infer_app_type` 入口识别与应用类型推断
  - `_parse_build_defaults`/`_parse_entries`/`_parse_exclude_dirs`/`_resolve_icon` 等
    `[tool.fspack]` 配置项解析
  - `_BUILD_DEFAULT_KEYS`/`_GUI_HINTS`/`_has_entry`/`_is_main_check` 常量与辅助
- `src/fspack/config/versions.py`（新增，~265 行）：版本管理
  - `KNOWN_EMBED_VERSIONS`/`KNOWN_STANDALONE_VERSIONS`/`NUITKA_VERSIONS` 版本映射
  - `resolve_py_version`/`nuitka_version_for`/`known_versions` 版本解析
  - `_satisfies`/`_satisfies_wildcard`/`_normalize_py_version`/`_ver_key` PEP 440 匹配
  - `DEFAULT_PY_VERSION`/`DEFAULT_LINUX_PY_VERSION`/`DEFAULT_NUITKA_VERSION` 默认值
- `src/fspack/config/__init__.py`（重写为 facade，~103 行）：
  - re-export 所有公开 API 与测试所需私有符号
  - 文档说明三个子模块的职责划分

## 关键决策与依据

### config 转为 package

**选型**：将 `config.py` 转为 `config/` package（含 `__init__.py` facade）。

**理由**：
1. 三个职责（数据结构/解析/版本）逻辑独立，按模块拆分自然
2. package `__init__.py` 作 facade 保持 `from fspack.config import X` 路径兼容
3. 无需修改任何调用方的 import 语句

### re-export 私有符号

facade 显式 re-export `_parse_build_defaults`/`_resolve_icon`/`_satisfies` 等私有符号，
保持测试 `from fspack.config import _xxx` 路径兼容。标注 `# noqa: F401` 抑制未使用导入告警。

## 代码实现情况

- 三个新模块完整实现，所有符号签名与 docstring 从原 config.py 原样迁移
- facade `config/__init__.py` 仅含 re-export，无业务逻辑
- 测试无改动

## 测试验证结果

- ruff check：通过
- ruff format --check：通过
- pyrefly check：0 errors
- pytest：全部通过
- coverage：≥95% 门禁

## 下一轮计划

iter-59：wheels.py 拆分（609 行 → `wheel_pip.py`/`wheel_cache.py`/`wheel_markers.py`，`wheels.py` 作 facade）
