# req-28 打包体积与执行速度优化

## 需求

- [x] 短期：剥离 QtWebEngine 调试资源 `.debug.pak`，按需保留 `icudtl.dat`/`QtWebEngineProcess.exe`
- [x] 中期：新增 `--pyc-optimize` 控制字节码优化级别，新增 `--no-site` 跳过 `site.py` 启动开销
- [x] 长期：新增 `--nuitka` 将用户源码编译为 `.pyd` 本机执行（默认关闭）
- [x] 交叉构建跳过 Nuitka 编译
- [x] 入口 wrapper 保持 `runpy` 调用方式（不采纳直接 import）

## 背景

参考 RimSort 项目（`https://github.com/RimSort/RimSort.git`）及其打包产物 `ref/RimSort`，分析 fspack 在打包体积与执行速度上的优化空间。RimSort 用 Nuitka 全量编译（构建几十分钟），fspack 取其精简策略：仅编译用户源码，第三方依赖保持 wheel + `.pyc`。

## 实现

详见 `iter-35-优化打包体积与速度.md`。

- `slim/qt.py` 新增 `_QT_WEBENGINE_TOP_FILES` 与 `.debug.pak` 剥离规则
- `builder.py` 新增 `pyc_optimize`/`no_site`/`nuitka` 参数与「Nuitka 编译」阶段
- `packaging/nuitka.py` 新增 `NuitkaCompiler` 类
- `packaging/runtime.py` `write_pth` 支持 `enable_site` 控制
- `cli.py` 新增三个 CLI 选项
