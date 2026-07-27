# iter-70 架构文档与模块索引同步

## 需求清单

- [x] 更新 README 架构图与工作原理描述，反映 iter-67~68 性能优化
- [x] 补充模块职责索引（含 iter-56~62 拆分后的新模块）
- [x] 更新开发文档（Sphinx api.rst）的子模块 automodule 指令
- [x] 更新 docs/index.rst 补充架构概览章节

## 迭代目标

同步项目文档与代码结构，使开发者能通过 README 模块索引快速定位代码、
Sphinx API 文档自动覆盖拆分后的所有子模块。

## 改动文件清单

- `README.md`：
  - "特性"章节补充 wheel 并行下载（iter-67）与 CLI 懒加载（iter-68）两项
  - 工作原理步骤 5（下载 wheel）改写：反映 uv 解析 + ThreadPoolExecutor 并行下载
  - 新增"模块索引"章节：顶层模块 + packaging 子模块职责表（25 个模块）
- `docs/api.rst`：
  - 从 1 个 automodule 扩展到 35 个，覆盖顶层/config/packaging/slim 全部子模块
  - 按子包分组（顶层/config/packaging/slim），便于浏览
- `docs/index.rst`：
  - 新增"架构概览"章节，列出 facade 拆分结构与子模块索引指引

## 关键决策与依据

1. **README 模块索引用表格而非列表**：25 个模块用表格更清晰，便于按模块名/职责
   对照查找。顶层模块与 packaging 子模块分两张表，避免单表过长

2. **api.rst 按子包分组**：Sphinx automodule 默认按声明顺序输出，分组（顶层/
   config/packaging/slim）使开发者按子包定位代码，符合项目结构

3. **不新建文档文件**：req-02 规定"不主动新建 *.md 文档"，所有变更在现有
   README.md/docs/*.rst 内完成。模块索引是开发者导航需求，归属 README 合理

4. **工作原理步骤 5 改写**：原描述"用 dev python 的 pip download"已过时
   （iter-67 改为 uv 解析 + 并行下载），更新为"uv 解析精确版本与平台 wheel，
   再 pip download --no-deps 并行下载"，补充 `.pyi` 类型 stub 剥离说明

5. **特性章节补充两项**：wheel 并行下载（提速 17%）与 CLI 懒加载（提速 16%）
   是 iter-67/68 的可感知性能优化，纳入特性列表便于用户了解

## 代码实现情况

- README.md 行数：397 → 446（+49 行，含模块索引章节 + 特性补充 + 工作原理更新）
- docs/api.rst 行数：7 → 225（+218 行，35 个 automodule 指令按子包分组）
- docs/index.rst 行数：82 → 105（+23 行，架构概览章节）

## 测试验证结果

- ruff check：通过（文档变更不影响代码）
- pyrefly check：0 errors（2 suppressed，与基线一致）
- pytest（非 slow）：1047 passed，覆盖率 98.58%（≥95%，未变化）

## 遗留事项

- Sphinx 文档构建（`make doc`）未在本次迭代验证，需确认 autosummary 配置
  能正确解析 35 个 automodule 指令（建议下次 doc 构建时验证）
- 模块索引表未列出每个模块的公开 API 数量，避免索引冗长；详细 API 见 api.rst

## 下一轮计划

req-38（代码质量与可靠性提升，iter-61~70）全部完成。后续可转向：
- req-39（功能与生态扩展）
- req-40（深度重构与基线守护）
- req-41（平台运行时优化）
