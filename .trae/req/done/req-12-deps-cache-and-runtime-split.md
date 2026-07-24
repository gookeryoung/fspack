# 需求 12：依赖解析缓存与运行时 stage 拆分

## 背景

iter-11 实现了 `--no-index` 快速路径，将下载依赖阶段从 1.66s 降到 1.18s。但 1.18s 中 pip 固有开销约 0.99s（启动 0.2s + 依赖解析 0.79s），fspack 侧约 0.19s。即使缓存命中仍需调用 pip 解析依赖树。

同时"准备运行时"阶段把下载与解压合在一起，无法分别观察耗时。

## 需求

- [x] `download_wheels` 缓存依赖解析结果到 `cache_dir/.deps-<hash>.json`，key 为 `(packages, py_version, platform_tags)` 的 hash；命中且 wheel 文件都存在时直接返回，跳过 pip 调用
- [x] 缓存失效条件：缓存文件不存在、缓存中的 wheel 文件缺失、packages/py_version/platform_tags 变化
- [x] 缓存写入 best-effort，失败仅 warning 不影响构建
- [x] ~~新增 `extract_embed_if_needed` / `extract_standalone_if_needed`~~ → 改为在 build 函数中直接检查 runtime_ready（设计变更，见 iter-12 文档）
- [x] `build` 拆分"准备运行时"为"下载运行时"与"解压运行时"两个独立 stage
- [x] ~~重构 `ensure_embed`/`ensure_standalone` 复用新函数~~ → 保留原样，build 直接调 download_*/extract_*（设计变更）
- [x] 适配测试，新增测试覆盖依赖解析缓存命中与失效
- [x] 验证：ruff/pyrefly/pytest --cov ≥ 95% 全部通过

## 验收标准

1. 清理后重建，依赖解析缓存命中时 download_wheels 阶段 ~50ms（跳过 pip 调用）
2. 汇总表显示"下载运行时"与"解压运行时"两行，耗时各自独立
3. 全套门禁通过，覆盖率不降
