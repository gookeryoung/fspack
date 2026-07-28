# 需求：README 与文档完善

## 背景

原 README（459 行）与 docs/ 存在问题：

1. **README 技术导向**：第二段就开始讲技术实现（embed python/C loader/NSIS/dpkg-deb），
   用户还没决定用不用就看到技术细节
2. **特性列表强调技术实现**：每项解释"怎么实现"而非"用户得到什么"
3. **工作原理 12 步流水线过早暴露**：占据 README 大量篇幅，劝退新用户
4. **模块索引等开发者内容混在用户文档**：用户文档与开发者文档混杂
5. **缺少价值主张与吸引力**：没有"为什么选 fspack"的价值对比
6. **docs/index.rst 重复 README 技术内容**：架构概览直接放在首页

## 需求

- [x] 1. README 重构为使用导向：价值主张开头 + 30 秒上手 + 用户价值特性
- [x] 2. 新增"为什么选 fspack"价值对比表格（用户想要的 → fspack 给你的）
- [x] 3. 特性列表从技术实现改为用户价值（"一行命令零配置"、"自动依赖推断"等）
- [x] 4. 技术细节（工作原理/模块索引）移到 docs/architecture.rst
- [x] 5. docs/index.rst 重构为用户导向首页，移除技术架构概览
- [x] 6. 新增 docs/architecture.rst：12 步流水线 + 模块索引 + 性能优化
- [x] 7. 全套门禁通过（ruff/format/pyrefly/pytest/coverage ≥ 95%）
- [x] 8. Sphinx 文档构建无新增警告

## 验收标准

- README 从 459 行精简到 ~360 行，开头 30 秒内展示完整流程
- README 没有"工作原理"章节（移到 docs/architecture.rst）
- docs/index.rst 用户导向，无技术架构概览
- docs/architecture.rst 包含完整的 12 步流水线与模块索引
- 现有 1083 个测试全部通过，覆盖率 98.56% 无回归
- Sphinx 构建成功，新增文件无警告

## 关键决策

- **README 开头用价值主张代替技术描述**：第一句"把 Python 项目变成可执行文件与安装包"
  说清能做什么，不提 embed python/C loader 等实现细节
- **"为什么选 fspack"表格**：左列用户想要的，右列 fspack 给你的，快速建立价值认知
- **技术细节移到 docs/architecture.rst**：README 仅保留链接，用户文档与开发者文档分离
- **docs/index.rst 用户导向**：移除"架构概览"章节，改为 30 秒上手 + 价值对比 + 快速上手
- **不修复预先存在的 docstring 警告**：68 条警告来自源码 docstring 的 RST 格式问题
  （analyzer.py/installer.py 等），不在本次范围内
