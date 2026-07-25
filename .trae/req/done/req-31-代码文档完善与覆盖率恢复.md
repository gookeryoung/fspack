# req-31 代码与文档完善（覆盖率恢复）

## 需求

- [x] 补齐 nuitka.py 缺测区域，恢复覆盖率不低于上一轮值（97.23%）
- [x] 修正 nuitka.py 模块 docstring 过时描述（安装目标、检查方式、stamp 键）
- [x] 修正 README Nuitka 描述（stamp 键四要素、standalone python 编译环境、入口文件跳过）
- [x] 补 docs/changelog.rst 自 v0.2.6 以来的未发布变更条目
- [x] 修复 tarfile extractall 在 Python 3.12+ 的 DeprecationWarning（PEP 706 data 过滤器）

## 背景

最近三个 Nuitka 修复提交（bc73260/221bfac/2f4f6b8）引入 `_ensure_build_python` 等代码但未补测试，总覆盖率从 97.23% 降到 95.63%，违反 rule-11「覆盖率不得低于上一次的值」。同时文档与代码出现多处不一致：nuitka.py docstring 仍描述装到 runtime site-packages（实际已改本地缓存）、README stamp 键仍为三要素（实际已加 entry_rels）、changelog 自 v0.2.6 后未更新。

## 实现

详见 `iter-38-代码文档完善与覆盖率恢复.md`。

- tests/test_nuitka.py 新增 12 个测试覆盖 `_build_python_cache_dir`/`_build_python_exe`/`_ensure_build_python` 全分支/stamp 读写 OSError 容错
- nuitka.py `tarfile.extractall` 加 `filter="data"`（3.12+，低版本回退）
- nuitka.py 模块 docstring、README.md、docs/changelog.rst 同步修正
