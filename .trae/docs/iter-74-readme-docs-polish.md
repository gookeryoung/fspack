# iter-74/75：README 与文档完善

## 需求清单

- [x] req-45：README 与文档完善

## 迭代目标

将 README 从技术导向重构为使用导向，强调用户价值而非技术实现；完善 docs/ 文档结构，
将技术细节移到独立的 architecture.rst。

## 改动文件清单

| 文件 | 改动 |
|------|------|
| `README.md` | 从 459 行技术导向重构为 362 行使用导向 |
| `docs/index.rst` | 重构为用户导向首页，移除技术架构概览 |
| `docs/architecture.rst` | 新增：12 步流水线 + 模块索引 + 性能优化（从 README 移入） |

## 关键决策与依据

### iter-74：README 重构

**问题**：原 README 第二段就讲 embed python/C loader/NSIS/dpkg-deb，特性列表每项
解释技术实现而非用户价值，工作原理 12 步流水线过早暴露。

**重构策略**：

| 章节 | 原内容 | 新内容 |
|------|--------|--------|
| 开头 | 技术实现描述（embed/C loader/NSIS） | 价值主张 + 30 秒上手 |
| 特性 | 17 项技术实现细节 | "为什么选 fspack"价值对比表 + 6 项用户价值特性 |
| 工作原理 | 12 步流水线（占 40 行） | 移到 docs/architecture.rst |
| 模块索引 | 顶层模块 + packaging + slim 表格 | 移到 docs/architecture.rst |
| 命令参考 | 长列表式 | 表格化速查 |

**新增章节**：
- **30 秒上手**：3 行命令展示完整流程，附效果说明
- **为什么选 fspack**：8 行价值对比表（你想要的 → fspack 给你的）
- **核心特性**：6 个用户价值导向特性（一行命令零配置 / 自动依赖推断 / 生成可分发安装包等）
- **产物布局**：简化的 dist/ 结构图
- **文档**：4 个文档链接（架构/CI集成/API/更新日志）

### iter-75：docs/ 文档结构完善

**问题**：docs/index.rst 重复 README 技术内容，架构概览直接放在首页。

**重构策略**：

| 文件 | 原内容 | 新内容 |
|------|--------|--------|
| `docs/index.rst` | 技术简介 + 架构概览 + 开发 | 用户导向：30秒上手 + 价值对比 + 快速上手 |
| `docs/architecture.rst` | 不存在 | 新增：12 步流水线 + dist 布局 + 多入口机制 + 递归打包 + 模块结构 + 性能优化 |

**toctree 结构**：
- 指南：integration
- 参考：architecture / api / changelog

## 代码实现情况

### README.md 开头对比

**原**：
```
fspack 将 Python 项目打包为可执行文件与跨平台安装包：用 embed python（Windows）
或 python-build-standalone（Linux）提供运行时，C loader 配置环境并调用用户脚本，
NSIS 生成 Windows 安装包、dpkg-deb 生成 Linux .deb 与 tar.gz 便携包。
```

**新**：
```
fspack 让你的 Python 项目秒变可分发的桌面应用。无需改一行代码，`fsp b` 一行命令
产出 `.exe`，`fsp p` 再一行产出 Windows 安装包或 Linux `.deb`。自动分析依赖、
精简体积、预编译加速，开箱即用。
```

### docs/architecture.rst 内容

- 构建流水线（12 阶段详细说明）
- dist 布局（目录树）
- 多入口机制（.entry 文件查找）
- 递归打包（跳过目录/退出码/排序）
- 模块结构（顶层/config/packaging/slim 子包表格）
- 性能优化（增量缓存/CLI懒加载/并行下载/Win7兼容/镜像/彩色进度）

## 测试验证结果

- `uv run ruff check src tests`：All checks passed
- `uv run ruff format --check src tests`：69 files already formatted
- `uv run pyrefly check`：0 errors
- `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95`：
  1083 passed, 1 skipped, 30 deselected，覆盖率 98.56%
- `uv run sphinx-build -b html docs docs/_build`：构建成功
  - 新增 `architecture.rst` 与重构的 `index.rst` 无警告
  - 68 条预先存在的警告来自源码 docstring RST 格式问题（analyzer/installer 等），
    不在本次范围

## 整合优化情况

- README 与 docs/index.rst 内容去重：README 面向 GitHub 访客，index.rst 面向
  ReadTheDocs 访客，两者都用户导向但不重复技术细节
- 技术细节统一收纳到 docs/architecture.rst，README 仅保留链接

## 遗留事项

- 68 条预先存在的 docstring RST 格式警告（analyzer.py/installer.py 等）未修复，
  属于源码 docstring 问题，不在本次文档完善范围

## 下一轮计划

无（文档完善完成，等待用户下一需求）。
