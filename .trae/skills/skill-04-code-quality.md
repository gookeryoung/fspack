# skill-04 代码质量改进

## 核心决策

### BuildConfig.arch 移除
- `arch: str = "win_amd64"` 字段未被业务代码使用（`wheel_platform_tag(target)` 派生），且默认值对 Linux 不正确。移除字段。

### coverage 误报修复（detect_entry）
- `if mod in seen or not path.is_file(): continue` 的 continue 行不被 coverage 记录——Python sys.settrace 对 `if: continue` 的 JUMP_ABSOLUTE 指令不触发 line event。
- 修复：改写为正向条件 `if mod not in seen and path.is_file():`，De Morgan 等价，消除 continue 语句。

### 测试策略
- `detect_platform` Windows 分支：monkeypatch `platform.system` 返回 "Windows"。
- `_parse_target` windows 分支：补 `--target windows` 分发测试。
- 测试用 monkeypatch，禁用 `@patch` 装饰器。

## 覆盖率演进
- P3：118 passed, cov 99.08%
- P4：122 passed, cov 99.87%（仅剩 argparse 结构性不可达分支）
