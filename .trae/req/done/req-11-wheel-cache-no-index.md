# 需求 11：wheel 缓存优化（--no-index 快速路径）

## 背景

用户反馈：项目清理（`fspack c`）后重新构建（`fspack b`）时，wheel 依然需要重新下载。

实测确认：wheel 已正确缓存到 `~/.fspack/cache/wheels/`（缓存命中、0 字节下载），但 `pip download` 命令仍执行 ~1.6s 并查询网络 index，让用户感觉"在重新下载"。

## 需求

- [x] `download_wheels` 先用 `--no-index --find-links cache_dir` 从本地缓存解析依赖，命中则完全跳过网络查询
- [x] 缓存不完整或条件依赖未满足时，自动回退到带 `-i index` 的完整下载
- [x] 回退路径的错误信息完整保留（含 stderr）
- [x] `download_wheels` 与 `unpack_wheels` 分开使用独立 stage，汇总表显示"下载依赖"与"解压 wheel"两行
- [x] 阶段备注明确标注缓存命中情况（如"缓存命中，跳过网络"）
- [x] 现有测试适配新逻辑，新增测试覆盖 --no-index 成功与回退两条路径
- [x] 验证：ruff check / ruff format --check / pyrefly check / pytest --cov ≥ 95% 全部通过

## 验收标准

1. 清理后重建，缓存命中时 pip 不查询网络（--no-index 路径）
2. 缓存缺失时自动回退到带 index 下载，功能不受影响
3. 汇总表"下载依赖"与"解压 wheel"分两行显示，项数准确
4. 全套门禁通过（ruff/pyrefly/pytest/coverage ≥ 95%）
