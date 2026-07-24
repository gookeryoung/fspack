# P8 Linux wheel 平台标签兼容性修复

## 背景

用户在 guicalc 示例（PySide6 依赖）上跑 `fspack b`（Linux target）报错：

```
ERROR: Could not find a version that satisfies the requirement PySide6 (from versions: none)
ERROR: No matching distribution found for PySide6
```

## 根因

`wheel_platform_tag(Platform.LINUX)` 返回单个 `manylinux2014_x86_64` 标签。PySide6 6.3+ 的 Linux wheel 只用 `manylinux_2_28_x86_64`（PEP 600，要求 glibc 2.28），**不兼容** manylinux2014（glibc 2.17）。pip `--platform manylinux2014_x86_64` 匹配不到 manylinux_2_28 wheel，导致 "from versions: none"。

同样影响 numpy 2.x、scipy 1.13+ 等现代库。

## 需求清单

- [x] `platform.wheel_platform_tag` → `wheel_platform_tags`，返回 `tuple[str, ...]`
- [x] Linux 返回 `("manylinux2014_x86_64", "manylinux_2_28_x86_64")`，覆盖老 wheel 与现代库
- [x] `builder.download_wheels` 参数 `platform_tag: str` → `platform_tags: Sequence[str]`，cmd 展开多个 `--platform`
- [x] 更新 test_platform.py / test_builder.py
- [x] 新增 `test_download_wheels_multi_platform` 测试验证多标签展开
- [x] 真实 guicalc Linux 构建验证 PySide6 下载成功
- [x] 跑全套门禁确认无回归

## 验收标准

- `fsp b` 在 Linux target 下能下载 PySide6 6.3+（manylinux_2_28 wheel）
- 非 slow 门禁全过（ruff/pyrefly/pytest cov≥95%）
- 多平台标签展开为多个 `--platform` 参数
