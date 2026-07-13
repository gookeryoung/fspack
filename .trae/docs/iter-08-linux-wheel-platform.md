# P8 Linux wheel 平台标签兼容性修复

## 迭代目标

修复 Linux target 下 PySide6 6.3+ 等 manylinux_2_28 wheel 下载失败的问题。

## 改动文件清单

- `src/fspack/platform.py`：`wheel_platform_tag` → `wheel_platform_tags`，返回 `tuple[str, ...]`。Linux 返回 `("manylinux2014_x86_64", "manylinux_2_28_x86_64")`
- `src/fspack/builder.py`：`download_wheels` 参数 `platform_tag: str` → `platform_tags: Sequence[str]`，cmd 构造时展开多个 `--platform` 参数；`build` 调用处同步更新
- `tests/test_platform.py`：`test_wheel_platform_tag` → `test_wheel_platform_tags`，断言 tuple 返回值
- `tests/test_builder.py`：lambda 的 `platform_tag` → `platform_tags`；新增 `test_download_wheels_multi_platform` 验证多标签展开为多个 `--platform`

## 关键决策与依据

1. **多标签而非单标签**：pip `--platform` 支持重复指定（append 语义，OR 匹配）。Linux 同时传 manylinux2014（覆盖老 wheel + manylinux_2_17）与 manylinux_2_28（覆盖 PySide6 6.3+/numpy 2.x 等现代库），兼容性最广。

2. **manylinux_2_28 而非 manylinux_2_31/2_34**：PySide6 当前最新（6.9.x）用 manylinux_2_28。manylinux_2_31/2_34 标签的库极少，且 glibc 2.28 已覆盖主流 Linux 发行版（Ubuntu 18.04+/CentOS 8+）。

3. **不向上兼容 manylinux1**：manylinux1（glibc 2.5）已弃用，现代 pip 默认不匹配。PySide6 6.0-6.2 的 manylinux1 wheel 太老，不考虑。

4. **返回 tuple 而非 list**：平台标签是固定常量，用不可变 tuple 表达意图。

## 验证结果

- 手动验证：`pip download PySide6 --platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64 --python-version 3.11 --abi cp311 --implementation cp --only-binary=:all:` 成功下载 PySide6 6.9.3（manylinux_2_28 wheel）
- 真实构建：`build(guicalc, target=LINUX, py_version=3.11.10)` 成功下载 PySide6 并生成 dist/guicalc 可执行文件
- 非 slow 门禁全过：137 passed, 9 deselected, coverage 99.89%

## 遗留事项

- 无
