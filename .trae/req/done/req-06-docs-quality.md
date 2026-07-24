# P6 代码质量/文档改进需求

## 背景

P2 遗留（NSIS 端到端验证）已解决，项目进入 P6 代码质量/文档改进阶段。
当前 README.md 仍是 copier 模板骨架，仅讲开发工具链（hatchling/uv/ruff），未体现 fspack 作为 Python 打包 CLI 的实际功能。

## 需求清单

- [x] 重写 README.md 突出 fspack 核心功能（cargo 风格短命令、embed python、C loader、NSIS 安装包、Windows/Linux 双平台）
- [x] 新增"命令用法"章节，展示 fsp b/c/r/p 子命令示例
- [x] 新增"工作原理"章节，简述构建流水线（解析 → 下载 → loader → 打包）
- [x] 新增"示例"章节，链接 tests/examples/ 下 5 类典型项目
- [x] 新增"平台支持"说明（Windows embed + mingw，Linux python-build-standalone + gcc）
- [x] 保留开发/文档/多版本测试章节
- [x] 审查 CLI 帮助文本，必要时补充子命令描述
- [x] 跑全套门禁确认无回归

## 验收标准

- README.md 准确反映 fspack 当前能力（P1-P5 已交付功能）
- 命令用法示例可直接复制运行
- 非 slow 门禁全过（ruff/pyrefly/pytest cov≥95%）
