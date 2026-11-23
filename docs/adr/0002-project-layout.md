# ADR 0002：项目结构布局与包内分层

- 状态：提议（2026-06，P0 落地）
- 关联：`docs/adr/0001-layering.md`（依赖方向）、`tests/test_layout.py`（守卫）
- 目标：把平铺的入口/工具/资源收拢为可读、可扩展、可验证的目录结构，
  同时保持 ADR-0001 的依赖方向与 CLI 懒加载性能约束不变。

## 背景

`src/fspack` 当前有 22 个顶层模块，CLI 三个文件合计 2000+ 行；资源路径
（`assets/templates`、`assets/init_templates`、`assets/loader`、`assets/runtime`、
`assets/icons`，P2 已迁移到 `resources/`）以字符串硬编码散落在 10+ 模块；测试 104 个文件全部平铺。
结构问题不阻断功能，但会持续抬高定位成本，并让"新增代码放哪里"依赖口口相传。

## 决策

### 1. 目标目录

```
src/fspack/
├── __init__.py            # 仅版本号
├── __main__.py            # python -m fspack
├── api.py                 # 公开 Python API（build/package/run）
├── cli/                   # 命令入口：main / parser / help / dispatch / commands/*
├── core/                  # 叶子原语：exceptions/compat/platform/fsutil/jsoncache/...
│   └── resources.py       # 唯一资源定位入口
├── ui/                    # 终端展示：console/progress/wizard
├── analyzer/ config/ doctor/ packaging/ slim/ templates/ runner/
└── resources/             # 包内数据：icons/loader/runtime/templates/{init,doctor}
```

测试按源码域镜像：`tests/unit/<domain>/`、`tests/integration/`、`tests/perf/`。

### 2. 分层规则（在 ADR-0001 基础上按新路径表述）

```
cli/commands → builder/runner/doctor/templates/packaging（服务与编排）
             → packaging（引擎）
             → analyzer/config/slim
             → core（叶子） + ui（仅展示入口）
```

- `core` 不得 import `core` 以外的任何 `fspack.*`（`exceptions` 除外，见守卫）。
- `ui` 只能依赖 `core`；展示层不得被纯逻辑模块反向依赖。
- `packaging` 的 display allowlist 继续由 `tests/test_layout.py` 双向校验。
- 原有 facade（`builder.py`、各包 `__init__.py`）保留，门面允许转出口。

## 迁移规则

1. 每个阶段单独 PR：先加/改守卫，再 `git mv`，最后全量测试。
2. 旧模块路径在至少一个 minor 版本内保留薄 shim（`__all__` + re-export），
   shim 不得新增内部依赖；下个 minor 在 CHANGELOG 标注后删除。
3. 资源只允许通过 `fspack.core.resources` 定位；新增资源目录必须同步
   `pyproject.toml` 的 `[tool.fspack] data-dirs` 与 `test_resources.py`。
4. CLI 顶层 import 白名单与 `fsp --help` 冷启动基线是硬门禁：任一阶段回退
   超过 5% 即回滚该阶段。

## 后果

- 目录即可表达分层，新增模块有明确归属；资源移动只需改一处。
- 迁移期存在旧路径 shim 与两份测试路径映射，成本约 7~10 人日。
- 不改变用户可见 CLI、配置格式与 wheel 布局（资源在包内相对路径变化，
  由 `resources.py` 与打包清单统一吸收）。
