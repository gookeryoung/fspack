# 需求 23：科学库精简规则与示例

- [x] 扩展 `SlimSpec._default_classify` 支持 `nested_excludes`（任意层级剥离，含跨包）
- [x] 新增 `MatplotlibSlimSpec`：剥离 `sphinxext` 与跨包/嵌套 `tests` 目录
- [x] 新增 `ScipySlimSpec`：剥离各子模块下嵌套 `tests` 目录
- [x] 在 `slim/__init__.py` 注册新 spec 并导出
- [x] 新增 `test_slim.py` 测试覆盖新 spec 与 `nested_excludes` 行为
- [x] 新增 `examples/sci_numpy`、`sci_matplotlib`、`sci_scipy` 示例项目
- [x] 新增 slow 端到端测试验证精简打包与运行
- [x] 门禁通过（ruff/pyrefly/pytest，覆盖率 98.37%）
