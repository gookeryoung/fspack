# 需求 22：slim 重构为包，类继承形式按包分发

## 需求描述

将单文件 `src/fspack/slim.py` 重构为 `src/fspack/slim/` 包，采用类继承形式
让用户能够按不同包配置精简规则。参考 fspacker `packers.libspec` 设计：

- 抽象基类 `SlimSpec` 定义 4 个核心接口（match/normalize_submodule/
  expand_closure/classify_entry）
- 注册表 `register_spec`/`get_spec` 按 wheel 归一化包名分发到对应 spec 子类
- `QtSlimSpec`（PySide2/PySide6/PyQt5/PyQt6 共享）+ `DefaultSlimSpec`（兜底）
- 新增包精简规则只需继承 `SlimSpec` 并注册，无需修改分发逻辑（OCP）

## 验收标准

- [x] slim.py 拆分为 slim/ 包（base.py/default.py/qt.py/__init__.py）
- [x] SlimSpec 抽象基类 + 注册表机制
- [x] QtSlimSpec + DefaultSlimSpec 实现，行为与原 slim.py 一致
- [x] slim_unpack 与 classify_entry 按 spec 注册表分发
- [x] 向后兼容：`from fspack.slim import _qt_module_closure` 等仍可用
- [x] 向后兼容：`fspack.slim.zipfile.ZipFile` 仍可被 monkeypatch
- [x] 测试覆盖 spec 类、注册表分发、stage 回调
- [x] 门禁通过（ruff/pyrefly/pytest，覆盖率 ≥ 95%）
