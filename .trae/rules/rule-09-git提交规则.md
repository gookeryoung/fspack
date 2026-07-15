---
name: "rule-09-git提交规则"
alwaysApply: true
---

## 提交信息格式

- 使用中文编写，简洁明了，不超过一段落。
- 必须包含变更类型前缀，采用 `类型: 描述` 格式。
- 变更类型包括：
  - `feat`：新增功能
  - `fix`：修复缺陷
  - `refactor`：重构（不改变外部行为）
  - `docs`：文档更新
  - `style`：格式调整（不影响代码逻辑）
  - `test`：测试相关
  - `chore`：构建、依赖、工具链等杂项
  - `perf`：性能优化
  - `build`：构建系统或外部依赖变更
  - `ci`：CI 配置变更
- 描述部分说明"做了什么"，必要时补充"为什么"。
- 单条提交仅包含一个逻辑变更，避免混合多个无关改动。
- 禁止仅写"update"、"fix bug"等模糊描述；禁止中英文混用描述。
- 涉及 issue/任务编号时，置于段末括号内，如 `(refs #123)`。

## 正面示例

- `feat: 新增侧边栏折叠功能，支持快捷键 Ctrl+B 切换`
- `fix: 修复 QThread 退出时未等待导致 worker 泄漏的问题 (refs #42)`
- `refactor: 将样式令牌加载逻辑抽取到 theme.py，消除 main.py 中的重复代码`

## 反面示例

- `update`：缺少类型前缀，描述模糊，无法判断变更性质
- `修复了一些问题`：缺少类型前缀，未说明修复了什么问题
- `feat: add sidebar collapse feature and fix thread leak and update docs`：单条提交混合多个无关变更，且中英文混用
