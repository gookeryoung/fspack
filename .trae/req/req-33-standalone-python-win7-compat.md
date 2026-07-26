# standalone python Win7 兼容性 DLL 注入

## 背景

打包 fspack 自身安装包后，在 Win7 上运行 `fsp b <project>`（项目启用 nuitka）时调用 standalone python 运行 nuitka 编译失败。

根因：[builder.py](../../src/fspack/builder.py) 在准备 embed runtime 时已通过 `_inject_win7_compat_dll` 注入 `api-ms-win-core-path-l1-1-0.dll`，但 [nuitka.py](../../src/fspack/packaging/nuitka.py) 的 `_ensure_build_python` 解压 standalone python 后未注入此 DLL。

Python 3.9+ 在 Win7 上启动需 `api-ms-win-core-path-l1-1-0.dll`（提供 `PathCchSkipRoot` 等 API，Win8+ 自带，Win7 缺失）。standalone python 同样需要此 DLL，fspack 的 loader.exe 仅把 `runtime\` 加入 DLL 搜索路径，standalone python 由 subprocess 启动不经过 loader，找不到该 DLL。

KNOWN_STANDALONE_VERSIONS 最低 3.10，故所有 standalone python 版本均需要此 DLL。

## 需求清单

- [x] 在 `_ensure_build_python` 返回 standalone python 路径前注入 Win7 兼容 DLL 到 `python.exe` 同目录
- [x] 复用 `builder._inject_win7_compat_dll`（幂等），惰性导入避免循环依赖
- [x] 缓存命中与新建分支均注入（覆盖用户清理过 DLL 但保留 python.exe 的场景）
- [x] Linux 分支早返回，不触发注入
- [x] 添加测试覆盖三个场景：解压后注入、缓存命中补充注入、Linux 不注入
- [x] 全套门禁通过（ruff/pyrefly/pytest/coverage ≥ 95%）

## 验收标准

- fspack 自身打包后在 Win7 上能调用 standalone python 运行 nuitka 编译目标项目源码
- 不破坏现有 `_ensure_build_python` 测试（缓存命中跳过下载、下载解压成功、损坏 tarball、缺失 python.exe 等场景）
- 覆盖率不下降
