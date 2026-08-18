# iter-01 项目结构优化

## 需求清单

来源 `.trae/req/req-01-项目结构优化.md`（已完成移入 done/）：拆分超大模块、归组前缀家族、消除 PLR0913、健壮性专项。

## 迭代目标

制定并执行项目结构优化：8 个超 500 行模块拆至单一职责，packaging 顶层家族归组，全程 `make check` 全绿（覆盖率 >= 95%）。

## 现状证据（2026-08-18 收集）

### 行数基线（Top，拆分前）

| 行数 | 模块 | 拆分后 |
|-----|------|--------|
| 891 | doctor/envs.py | envs.py + integrity.py + cache_health.py |
| 701 | packaging/wheels/resolver.py | resolver.py + uv_bridge.py + parallel.py |
| 646 | config/parsing.py | parsing.py + entries.py + app_type.py |
| 604 | packaging/installer/base.py | base.py + facade.py + dist_prep.py + request.py |
| 573 | packaging/loader/compile.py | compile.py + toolchain.py + cache_keys.py |
| 539 | doctor/templates.py | templates.py + template_report.py |
| 591/575 | cli.py / cli_parser.py | 边缘达标，未动（遗留） |

### 结构事实（拆分前）

- packaging 顶层 20+ 散模块：runtime 家族 5 文件、pyc 家族 3 文件、win7 家族 3 文件
- resolver.py 7 处、installer/base.py 5 处 `# noqa: PLR0913`
- `__init__.py` facade 全部合规

## 阶段执行记录（P1-P8 全部完成）

### P2 doctor 域拆分

- `envs.py`（891）拆三：`integrity.py`（归档完整性）+ `cache_health.py`（缓存健康引擎）+ envs.py 保留纯环境检查；`cache.py` 命令层改从 `cache_health.py` 导入
- `templates.py`（539）拆二：报告渲染 `_print_*`/`_format_*` → `template_report.py`

### P3 wheels 域拆分 + 参数收敛

- `resolver.py`（701）拆三：`uv_bridge.py`（uv 发现/能力检测/输出转换）+ `parallel.py`（并行下载编排）+ resolver.py 保留编排入口
- `DownloadContext` dataclass 消除 7 处 PLR0913

### P4 config 域拆分

- `parsing.py`（646）拆三：`entries.py`（入口解析家族）+ `app_type.py`（类型推断）+ parsing.py 保留 pyproject 主解析

### P5 installer / loader 瘦身

- `installer/base.py`（604）拆四：`dist_prep.py` + `facade.py` + `request.py`；`ReleaseRequest`/`SignOptions` dataclass（`_NO_SIGN` 单例消 B008）消除 5 处 PLR0913（提交 0f9c36f）
- `loader/compile.py`（573）拆三：`toolchain.py`（工具链发现 + windres 资源编译）+ `cache_keys.py`（缓存键计算）+ compile.py 保留编译器基类/平台子类（提交 14bf5c8）

### P6 packaging 家族归组（方向经用户确认）

- `runtime*.py`×5 → `packaging/runtime/` 子包（urls/extract/download/trim/pth），原 facade 并入 `__init__.py`，`write_pth` 函数迁出至 `pth.py`（遵守 `__init__.py` 零业务定义规则）
- `pyc*.py`×3 + `source_strip.py` → `packaging/pyc/` 子包（compile/stamp/source_strip），trim 归 runtime 家族由 pyc facade re-export
- `win7_*.py`×3 → `packaging/win7/` 子包（check/dll/scan），新建 facade `__init__.py`
- 导入路径 `fspack.packaging.runtime` / `fspack.packaging.pyc` 不变（facade re-export）；monkeypatch 语义不变（`_R`/`_P` 延迟 dispatch 指向包 `__init__` 模块对象）
- 关键修复：被移动文件中 2 处 `Path(__file__).parent.parent` 资产定位层级加一层（win7/dll.py 的 shim、runtime/trim.py 的兼容 DLL）；win7/check.py CLI 路径改为 `python -m fspack.packaging.win7.check`（提交 4d84e80）

### P7 健壮性专项（交叉验证，无代码变更）

- 异常链：B904 已启用且 lint 全绿；7 处 `raise SystemExit(2) from None` 均为 CLI 已打印错误后的显式吞链，合理
- 类型抑制：src 内 11 处 `# type: ignore` 逐个核实均有注释依据（tomli 条件导入/Windows 平台 API/tenacity stub/monkeypatch dispatch），pyrefly 0 errors
- 死代码：uvx vulture（95% 置信度，未入项目依赖）仅 2 处命中且均为带 noqa 的刻意设计（lru_cache 缓存键参数/override 签名兼容参数），无真实死代码

### P8 收尾

- `docs/architecture.rst` 模块导览同步（runtime/pyc/win7/loader/installer/wheels/pipeline 新结构）
- `packaging/__init__.py` docstring 概览同步
- req-01 全项勾选移入 `.trae/req/done/`
- 全量 `make check` 复核 + 提交推送

## 关键决策

1. **只拆不改语义**：所有拆分保持函数签名与行为不变，测试断言不放宽，仅迁移 patch 路径
2. **渐进归组**：家族归组通过 facade re-export 保持导入路径兼容，外部引用零破坏
3. **facade 动态 dispatch 保留**：`_R`/`_P` 延迟解析机制使 `monkeypatch.setattr("fspack.packaging.runtime.<name>", ...)` 在包化后继续生效，测试 patch 路径零迁移
4. **每维度独立提交**：0f9c36f（installer）/ 14bf5c8（loader）/ 4d84e80（家族归组）等，可独立回滚
5. **不引入清理工具依赖**：vulture 经 uvx 临时运行完成交叉验证，未写入项目依赖

## 改动文件（P5 后半程 + P6 + P7 + P8）

- 拆分新增：`packaging/loader/toolchain.py`、`packaging/loader/cache_keys.py`、`packaging/runtime/`（6 文件）、`packaging/pyc/`（4 文件）、`packaging/win7/`（4 文件）
- 引用同步：`pipeline/executor.py`、`pipeline/runtime_stage.py`、`installer/nsis.py`、`doctor/win7.py`、`analyzer/fingerprint.py`、`packaging/__init__.py`
- 测试同步：`test_win7_check.py`、`test_win7_dll.py`、`test_win7_scan.py`、`test_builder.py`（caplog logger 名）、`test_installer.py`
- 文档：`docs/architecture.rst`

## 测试结果

- 全量 `make check`：lint（ruff check + format）0 违规、pyrefly 0 errors、2495 passed / 12 skipped、覆盖率 95.32%（>= 95% 门禁）
- 冒烟验证：三子包导入 + `WIN7_SHIM_DLL_PATH` 资产定位实测正确（shim exists: True）

## 遗留事项

- cli.py（591）/ cli_parser.py（575）边缘超标：已拆出 cli_parser/cli_init，进一步拆分收益低，暂不动
- `_util` 包命名不理想但职责清晰（format/fsutil/jsoncache），暂不动
- pyrefly 9 warnings（not shown）为第三方库 stub 缺失类，待上游完善

## 下一轮计划

无（本迭代全阶段交付完毕）。后续结构优化需求另立 req。
