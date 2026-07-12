# fspack P1 需求清单

参考本人 fspacker（PyPI）设计的新一代 Python 打包 CLI 工具 `fspack`。

## 总体目标

构建一个类似 cargo 的简洁 Python 打包工具，使用 embed python 调用用户脚本，通过 C loader 配置运行环境，最终用 NSIS 打包为 Windows 安装包分发。本文件记录第一阶段（P1）需求。

## 平台与范围（用户确认）

- 目标平台：Windows 优先，Linux 后续阶段。
- Loader 实现：C + Python 引导（C 设置环境加载 embed python，重逻辑放 Python）。
- 安装包工具：NSIS（后续阶段，P1 不含）。
- 开发环境：Linux 交叉编译（mingw-w64 + wine）。

## P1 范围（垂直切片：能运行 hello world）

[x] 1. CLI 骨架：`fsp b`(build) / `fsp c`(clean) / `fsp r`(run) / `fsp -V`(version)，cargo 风格短命令，argparse 子命令分发。
[x] 2. 项目元数据解析：解析 pyproject.toml（name/version/dependencies），识别入口（`def main()` 或 `if __name__=='__main__'`），判定 CLI/GUI 类型。
[x] 3. AST 依赖分析：扫描源码 import，提取顶层模块，与标准库/本地包/第三方分类，补全未声明依赖。
[x] 4. embed python 下载：国内镜像源（华为云优先，阿里云/清华备选）下载 `python-X.Y.Z-embed-amd64.zip`，缓存到 `~/.fspack/cache/embed/`，解压到 `dist/runtime/`。
[x] 5. _pth 配置：生成 `python3X._pth` 启用 site-packages，创建 `Lib/site-packages` 目录。
[x] 6. 依赖下载：用 dev python 的 `pip download --platform win_amd64 --only-binary=:all:` 拉取 Windows wheel 到 wheelhouse，解包到 `dist/runtime/Lib/site-packages/`。
[x] 7. C loader 生成与编译：生成 C 源码（动态加载 python3X.dll，调用 `Py_Main` 运行入口脚本），支持 CLI(console)/GUI(windows) 子系统，用 `x86_64-w64-mingw32-gcc` 交叉编译为 .exe（编译命令已实现并单测 mock，真实编译待 mingw）。
[x] 8. 构建流水线编排：parse → download embed → download deps → copy src → gen loader → compile → assemble dist/。
[x] 9. run/clean 命令：`fsp r` 用 wine 运行 dist 下的 .exe；`fsp c` 清理 dist/。
[] 10. hello world 端到端验证：一个无依赖的示例项目能 `fsp b` 后 `fsp r` 输出 "hello, world"（待 mingw-w64 + wine 安装后跑 slow 测试验证）。

## 不在 P1 范围

- NSIS 安装包生成（P2）。
- Linux 平台支持（P3+）。
- 源码加密（pyarmor）、nuitka 编译优化。
- 多项目批量打包。

## 验收标准

- `fsp b` 对无依赖项目产出 `dist/<name>.exe` + `dist/runtime/` + `dist/src/` + `dist/runtime/Lib/site-packages/`。
- `fsp r` 通过 wine 运行 .exe 输出预期内容。
- `fsp c` 清空 dist/。
- 全套门禁通过：`ruff check`、`ruff format --check`、`pyrefly check`、`pytest --cov≥95%`（mingw/wine/网络相关标 slow）。
- Python 3.8 兼容。

## 约束

- 国内镜像优先；网络/编译/运行类测试标 `slow`。
- 不引入 requests 等重依赖，下载用 urllib + tomli（仅 py<3.11）。
- 遵循 rule-11 Python 规范与既有工具链配置。
