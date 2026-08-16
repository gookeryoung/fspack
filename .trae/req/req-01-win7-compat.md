# Win7 兼容后续开发计划

现状基线（已交付）：python3XX.dll 清单驱动替换（3.12+）+ shim 注入（3.9+）+ 导入表静态检查工具（win7_check）已集成打包流程。

## P1 产物门禁补全（优先级最高）

- [x] loader.exe 构建后自动跑 check_win7_imports，Win8+ 导入违规即构建失败
- [x] dist 全量 Win7 扫描：runtime/ 内全部 .dll/.pyd（含第三方与 Nuitka 产物）导入表检查
- [x] 第三方违规不阻断，生成 win7 兼容报告（文件/API/影响面），随 dist 输出

## P2 运行前提闭环（UCRT）

- [x] 扫描 dist 内 api-ms-win-crt-* 依赖，NSIS 安装包检测目标机 UCRT 缺失并提示
- [x] zip 发行版文档注明 KB2999226 前提
- [x] README 增补 Win7 支持章节：支持矩阵（3.9-3.14 × Win7 SP1）/原理/前提/已知限制

## P3 诊断与守卫

- [x] doctor 新增 Win7 诊断项：缓存 dll 抽检、清单对齐性、shim 资产存在性
- [x] 清单对齐守卫测试：KNOWN_EMBED_VERSIONS 的 3.12+ 版本必须收录 WIN7_EMBED_SHA256
- [x] 决策项：是否需要 --no-win7-compat 开关 → 已确认不加（重编译版 Win7-11 通用，dll 替换无条件执行）

## P4 实机验证

- [ ] Win7 SP1 虚拟机冒烟清单：3.9/3.12/3.14 各打包一个 demo 验证启动/路径/os.getppid/shutil.copy2
- [ ] 冒烟结果回填 README Win7 章节（需 Win7 环境，无环境则交付 checklist）

## 风险与依赖

- Nuitka 4.1.3 编译用户代码产物的 Win7 兼容性未知：P1 全量扫描会暴露；若普遍违规，评估 Win7 目标降级 Nuitka 2.5.1
- 第三方 pyd 违规无自动修复手段：走报告模式而非阻断
- UCRT 内置分发涉及许可与体积：优先检测+提示，内置分发作为增强
