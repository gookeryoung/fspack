# 配置参考

`pyproject.toml` 的 `[tool.fspack]` 段支持以下配置项（均可选）：

```toml
[tool.fspack]
icon = "assets/app.ico"                    # exe 图标
exclude = ["examples", "docs"]             # 源码复制时额外排除的 glob 模式
slim-include = ["PySide6/Qt6Charts.dll"]   # wheel 精简：强制保留
slim-exclude = [                           # wheel 精简：强制剥离
    "PySide6/opengl32sw.dll",
    "PySide6/translations/*",
]
extra-index-urls = ["https://pypi.company.com/simple/"]  # 私有 PyPI 源
find-links = ["./wheels"]                  # 本地 wheel 目录

[project.scripts]                          # 多入口声明（PEP 621 标准）
cli = "myapp.cli:main"
gui = "myapp.gui:main"
```

## wheel 精简用户规则

`slim-include`/`slim-exclude` 支持 fnmatch glob 模式，匹配 wheel 内 POSIX 相对路径。

**优先级**：`slim-include` > `slim-exclude` > 自动分类

典型场景：

```toml
# 强制保留被自动闭包排除的 Qt 模块
slim-include = ["PySide6/Qt6Charts.dll"]

# 剥离不需要的大体积文件
slim-exclude = [
    "PySide6/opengl32sw.dll",      # 软件 OpenGL 后备（20MB）
    "PySide6/translations/*",      # 翻译资源（29MB）
    "PySide6/include/*",           # C 头文件（14MB）
]
```

## 构建默认值

以下配置项作为 CLI 标志未显式指定时的回退默认值：

```toml
[tool.fspack]
nuitka = false           # 启用 Nuitka 编译模式
pyc_strip = false        # 剥离 .py 仅留 .pyc
pyc_optimize = 2         # 字节码优化级别 0/1/2
no_site = false          # 禁用 site.py
no_pyc = false           # 关闭字节码预编译
no_stdlib_trim = false   # 关闭标准库精简
ccache = false           # Nuitka 编译启用 ccache
nuitka_packages = []     # Nuitka 编译包含的额外包
```

## 可选依赖分组（extras）

fspack 支持 [PEP 621](https://peps.python.org/pep-0621/) 的 `[project.optional-dependencies]`，
按需启用分组依赖。等价 `pip install pkg[extra]` 语义：分组内依赖合并到下载集合，
自引用 `my-pkg[extra]` 递归展开，第三方 `pkg[extra]` 原样透传 pip。

```toml
[project.optional-dependencies]
gui = ["PySide2"]
web = ["flask", "uvicorn"]
full = ["myapp[gui]", "myapp[web]", "numpy"]  # 自引用递归展开

[tool.fspack]
extras = ["gui"]   # 配置默认启用分组（可省略，用 CLI --extra 覆盖）
```

```bash
fsp b --extra gui --extra web    # CLI 启用多个分组（覆盖配置默认）
fsp p --extra full               # package 子命令同样支持
```

**优先级**：CLI `--extra` 完全覆盖 `[tool.fspack] extras` 配置默认（集合语义，非合并）。
未指定 CLI `--extra` 时用配置默认；两者均未指定时仅打包 `[project] dependencies`。
未知分组名报错并列出可选分组。extras 变化触发依赖分析缓存失效（重新分析）。

## 多入口声明

入口声明使用 PEP 621 标准的 `[project.scripts]`，fspack 自动识别 flat/src layout，
将 dotted module 解析为脚本路径：

```toml
[project.scripts]
cli = "myapp.cli:main"   # 生成 cli.exe
gui = "myapp.gui:main"   # 生成 gui.exe（GUI 类型，无控制台窗口）
web = "myapp.web:main"   # 生成 web.exe
```

顶层脚本（单段模块名）同样支持，如 `cli = "cli:main"` 解析为项目根目录的
`cli.py`。

> 历史版本的 `[tool.fspack.entries]` 已移除：声明该表时会报错并提示迁移到
> `[project.scripts]`。
