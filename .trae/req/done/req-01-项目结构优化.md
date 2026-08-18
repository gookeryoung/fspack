# req-01 项目结构优化

## 需求清单

- [x] 拆分超大模块（>500 行），每个模块单一职责，提高可读性
- [x] packaging 顶层前缀家族模块（runtime/pyc/win7）归组为子包
- [x] 消除 PLR0913 noqa 泛滥（参数过多改为 dataclass 封装）
- [x] 健壮性专项：异常链、类型收紧、死代码交叉验证
- [x] 全程保持 `make check` 全绿，覆盖率不低于 95%
- [x] 同步迁移测试 patch 路径，不放宽任何断言
- [x] 同步更新 docs/architecture.rst 模块导览

## 背景

- 8 个模块超过 500 行，最大 `doctor/envs.py` 891 行，三类职责混杂
- `packaging/` 顶层散落 20+ 模块，runtime/pyc/win7 三大家族未归组
- `resolver.py` 7 处、`installer/base.py` 5 处 `# noqa: PLR0913`

## 约束

- 重命名/移动公共模块路径属高风险操作，归组阶段（P6）方向需用户确认后执行
- 不引入新依赖（vulture/deptry 暂不安装，用 ruff F + pyrefly + coverage 交叉验证替代）
- 每个拆分独立提交，便于回滚
