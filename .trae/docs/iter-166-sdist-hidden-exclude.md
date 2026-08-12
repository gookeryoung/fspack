# iter-166 sdist 隐藏文件默认排除与白名单保留

## 需求清单
- [x] req-44-sdist-exclude-dev-dirs.md: 所有 `.` 开头隐藏文件/目录默认不打 sdist；白名单保留 `.coveragerc`/`.gitignore`/`.gitattributes`；排除 `Makefile`/`templates`；保留源码分发必需的非隐藏文件（源码/测试/文档/配置/README/LICENSE/锁文件）

## 迭代目标
针对上一版 sdist 排除规则是「10 个隐藏项逐个枚举」的风险——任何新增隐藏文件（例如 `.idea/`、`.env`、`.devcontainer/`）都会漏进 sdist——切换为 hatch 默认的 `.*` 全局排除 + `force-include` 白名单模式，同时对 `exclude` 列表语义与 `force-include` 优先级做构建产物核验。

## 改动文件清单
- `pyproject.toml`：重写 `[tool.hatch.build.targets.sdist]` exclude + 新增 `sdist.force-include` 三节
- `.trae/req/req-44-sdist-exclude-dev-dirs.md` → `.trae/req/done/req-44-sdist-exclude-dev-dirs.md`（完成迁移）
- `.trae/docs/iter-145~iter-151`：迭代记录超量清理（保留最新 5 条 iter-152~iter-156，本轮 iter-166 将接续，合计 6 条，下一轮再清理）

## 关键决策与依据
1. **排除模式选择：全局 `.*` vs 枚举**
   - 枚举方案只能覆盖已知项，遇到 `.env` / `.idea` / `.devcontainer` / `.tox` / `.mypy_cache` / `.pytest_cache` 等新增项时，sdist 会意外把这些开发/缓存目录打进去；
   - hatchling 中 `exclude` 支持 glob，`.*` 会匹配所有 `.` 开头的文件与目录（含其子路径），而 `force-include` 的优先级高于 `exclude`，正好支持白名单机制；
   - 方案：`exclude = [".*", "templates", "Makefile"]` + `force-include` 中三条白名单。

2. **白名单三项的理由**
   - `.coveragerc`：sdist 解压后用户可能想跑 `pytest --cov`，该文件定义覆盖率阈值和路径规则，缺失会导致 coverage 无法达到项目默认的 95% 阈值或测量范围出错；
   - `.gitignore` / `.gitattributes`：sdist 解压后仍可作为一个干净的 git 工作目录子集使用（例如用户从 sdist 初始化自己的 fork、或作为 vendor 源码），保留忽略/属性规则能避免 `git status` 全红。

3. **wheel 不需要额外改**
   - wheel 本来只包 `src/fspack` 下的源（wheel.force-include 仅 `py.typed`），不含顶层隐藏文件，因此不存在 `.开头` 泄漏风险；
   - 验证中 wheel 确实没有隐藏条目。

## 代码实现情况（pyproject.toml hatch config 片段）
```toml
[tool.hatch.build.targets.sdist]
# 隐藏文件/目录默认全局排除：.开头的 10+ 类开发内部项统一拦截，避免新增隐藏项时漏排；
# 需保留的 sdist 隐藏配置（覆盖率/VCS 规则）见下方 force-include 白名单。
exclude = [
    ".*",       # 所有.开头的隐藏文件/目录（.benchmarks/.github/.trae/.vscode 与
                # .copier-answers.yml/.python-version/.bumpversion.toml/
                # .pre-commit-config.yaml/.readthedocs.yaml 等开发配置）
    "templates",# GitHub Actions 打包模板（非 fsp init 模板；fsp 模板在 src/fspack/assets/）
    "Makefile", # 开发辅助脚本（sdist 用户不需要）
]

[tool.hatch.build.targets.sdist.force-include]
# sdist 隐藏文件白名单：对用户有价值的 .开头配置
# - .coveragerc：用户跑 sdist 内 pytest 时需要覆盖率阈值配置
# - .gitignore / .gitattributes：sdist 解压后仍可作为 git 工作目录子集使用
".coveragerc"    = ".coveragerc"
".gitignore"     = ".gitignore"
".gitattributes" = ".gitattributes"
```

## 整合优化情况
- 本轮只改 `pyproject.toml`，无 Python 源码改动；
- 通过 `force-include` + `exclude` 组合而非单独的排除清单，将「维护成本」从 O(隐藏项数量) 降低到 O(白名单数量)；
- 上一轮遗留的 12 条迭代记录（iter-145~iter-156）按规则清理了 7 条（iter-145~iter-151），保留最新 5 条。

## 测试验证结果
1. **静态检查**：`ruff check src tests` + `pyrefly check` 全部 0 errors。
2. **pytest (not slow)**：2232 passed, 12 skipped, 33 deselected（33 个 slow 被排除），耗时 ~18.8s，无失败/错误；覆盖率保持 ≥95%（上一轮记录 95.31%，本轮未降低）。
3. **sdist/wheel 构建验证**：
   - `uv build` 成功产出 `fspack-0.4.3.tar.gz`（262 文件）与 `fspack-0.4.3-py3-none-any.whl`（196 文件）；
   - sdist 隐藏条目（包前缀剥离后）**仅有白名单三项**：`.coveragerc`、`.gitattributes`、`.gitignore` ✅；
   - sdist 中无 `.benchmarks/`、`.github/`、`.trae/`、`.vscode/`、`.copier-answers.yml`、`.python-version`、`.bumpversion.toml`、`.pre-commit-config.yaml`、`.readthedocs.yaml` 等开发内部隐藏项 ✅；
   - `Makefile` 与 `templates/` 均被排除 ✅；
   - 必备内容齐全：`src/`、`tests/`、`docs/`、`scripts/`、`ruff.toml`、`pyrefly.toml`、`pytest.ini`、`tox.ini`、`README.md`、`LICENSE`、`pyproject.toml`、`uv.lock` ✅；
   - wheel 中无任何隐藏条目 ✅。

## 遗留事项
- 暂无。本轮的 hatch 配置对未来新增隐藏项具备「默认拦截」能力，只要隐藏项不被白名单化，就不会进入 sdist。

## 下一轮计划
- 本阶段（iter-157~iter-166）20 轮重构闭环已达成；
- 若有后续需求（例如打包产物尺寸进一步压缩、deb/dmg/pkg 安装器子包化、doctor 新增项等），先沉淀需求到 `.trae/req/`，再按新 20 轮迭代启动；
- 本轮完成后直接收尾：commit + 推送（若分支已跟踪远程）。
