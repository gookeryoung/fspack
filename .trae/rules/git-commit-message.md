---
alwaysApply: true
scene: git_message
---

# Git 提交规范

## 提交信息格式

- 格式: <type>(<scope>): <subject>
- 英文 scope，中文 subject。
- 描述说明"做了什么"，必要时补充"为什么"。

示例:

- `feat: 新增登录功能`
- `fix: 修复登录功能错误`
- `refactor: 优化登录功能`

## 提交前门禁

- `make check` 门禁定义见 `workflow.md`（commit 前必须本地通过，收尾总结注明"已本地通过 `make check`"）。

## 推送规范

- 所有推送一律使用 `make push`，**禁止**直接 `git push`。