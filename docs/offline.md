# 离线打包

`FSPACK_OFFLINE=1` 启用离线模式，所有下载阶段（运行时、wheel、Nuitka、ccache、
tkinter 补充包）只从本地缓存读取，缓存未命中时立即报清晰错误，不卡死、不重试网络。
适用于内网 CI、离线打包机或需精确控制缓存来源的场景。

也可用 `fsp b -O`（或 `--offline`）做单次约定：仅本次构建启用离线模式，
无需预先设置环境变量。

## 环境变量与 CLI 标志

| 变量/标志 | 作用 | 默认值 |
|------|------|--------|
| `FSPACK_OFFLINE=1` | 启用离线模式（值为 `1`/`true`/`yes`/`on`，不区分大小写） | 关闭 |
| `fsp b -O` / `--offline` | 单次构建启用离线模式（等价 `FSPACK_OFFLINE=1`，仅当前命令生效） | 关闭 |
| `FSPACK_CACHE_DIR` | 自定义缓存根目录 | `~/.fspack/cache` |

缓存目录结构：

```text
<cache_root>/
├── embed/          # Windows embed python zip
├── standalone/     # Linux/macOS python-build-standalone tar.gz
├── wheels/         # 第三方 wheel + 依赖解析缓存
├── nuitka/         # Nuitka 包 + 编译用 standalone python
├── nuitka-winlibs-mingw/  # Nuitka winlibs gcc 工具链（Windows 编译 .pyd；检测到 MSVC 时跳过预填充）
├── nuitka-work/    # Nuitka 编译中间缓存（clcache/scons-config 等，隔离系统位置防污染）
├── loaders/        # C loader 编译缓存
├── ccache/         # ccache 二进制与编译缓存
└── tkinter/        # tkinter 补充包缓存
```

## 典型用法

### 1. 预下载缓存（联网机器）

在能联网的机器上跑一次正常构建，缓存会自动填充到 `~/.fspack/cache/`：

```bash
fsp b                    # 正常构建，自动下载并缓存
```

将整个 `~/.fspack/cache/` 目录拷贝到离线机器（或用 `FSPACK_CACHE_DIR` 指定路径）。

### 2. 离线机器构建

```bash
# 方式一：环境变量 + 指定缓存路径
export FSPACK_OFFLINE=1
export FSPACK_CACHE_DIR=/path/to/cache
fsp b                    # 仅从本地缓存读取，不联网

# 方式二：-O 单次约定（仅本次构建离线）
fsp b -O
```

### 3. 用 --find-links 指定额外的本地 wheel 目录

若 wheel 不在默认缓存目录，可通过 `--find-links`（或 `pyproject.toml` 的
`find-links`）指定额外的本地 wheel 仓库，离线模式下也会搜索这些路径：

```bash
fsp b -O --find-links /data/wheels --find-links /shared/wheels
```

```toml
# pyproject.toml
[tool.fspack]
find-links = ["./wheels", "/shared/wheels"]
```

## 离线模式错误排查

缓存未命中时，fspack 会抛出包含"离线模式"关键字的明确异常，并列出已搜索路径，
便于快速定位：

```text
fspack.exceptions.DependencyError: 离线模式下依赖缓存未命中: pypdf，
已搜索路径: /home/user/.fspack/cache/wheels; /data/wheels。
请预先下载 wheel 放入上述路径之一，或通过 --find-links 指定本地 wheel 目录，
或取消 FSPACK_OFFLINE 环境变量
```

排查步骤：

1. 检查错误信息中"已搜索路径"是否包含你预下载的目录
2. 用 `pip download -d <cache_path> <package>` 预下载缺失的 wheel
3. 运行时缓存（embed python、standalone）放入对应子目录（`embed/`、`standalone/`）
4. 若需联网，删除 `FSPACK_OFFLINE` 环境变量即可恢复在线模式

缓存健康检查（损坏/过期/孤儿文件）见 [CLI 参考](cli.md) 的 `fsp cache` 一节。
