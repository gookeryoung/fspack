# iter-48 抽离 SlimRules 共用类与合并解析函数

## 需求清单

- [x] 抽离 `SlimRules` frozen dataclass 聚合 `slim_include`/`slim_exclude` 字段
- [x] 合并三个相似解析函数为通用 `_parse_string_list_cfg`
- [x] 消除 `_match_any` 辅助函数，逻辑收敛到 `SlimRules` 方法
- [x] 解决 B008（mutable default）用模块级单例 `DEFAULT_SLIM_RULES`
- [x] 全套门禁通过

## 迭代目标

参考 `python-class-design` SKILL 的"组合"原则，重构 wheel 精简用户规则代码：

1. 抽离 `SlimRules` frozen dataclass 聚合 `include`/`exclude` 两个字段，
   把 `_match_any` 辅助函数收敛为 `matches_include`/`matches_exclude` 方法
2. 合并 `_parse_exclude_dirs`/`_parse_string_list`/`_parse_slim_patterns` 三个
   逻辑相似的解析函数为通用 `_parse_string_list_cfg(value, cfg_key, *, reject_empty)`
3. `ProjectInfo.slim_include`+`slim_exclude` → `slim_rules: SlimRules`，
   减少字段数量，符合"dataclass 组合"原则

## 改动文件清单

- [src/fspack/config.py](../../src/fspack/config.py)
  - 新增 `SlimRules` frozen dataclass（`include`/`exclude`/`from_config`/`has_rules`/`matches_include`/`matches_exclude`）
  - 新增 `DEFAULT_SLIM_RULES` 模块级单例（解决 B008 mutable default）
  - 新增通用 `_parse_string_list_cfg(value, cfg_key, *, reject_empty)` 解析函数
  - 新增 `_match_any_glob` 辅助函数（fnmatch 匹配）
  - `_parse_exclude_dirs` 简化为薄包装（委托 `_parse_string_list_cfg`）
  - 删除 `_parse_string_list`/`_parse_slim_patterns`（合并到通用函数）
  - `ProjectInfo.slim_include`/`slim_exclude` → `slim_rules: SlimRules`
- [src/fspack/slim/base.py](../../src/fspack/slim/base.py)
  - 导入 `SlimRules`/`DEFAULT_SLIM_RULES`，移除 `fnmatch` 导入
  - `_slim_extract`/`_unpack_one_wheel`/`slim_unpack` 用 `slim_rules: SlimRules` 替代 `user_include`/`user_exclude` 双参数
  - 删除 `_match_any` 辅助函数（逻辑移入 `SlimRules.matches_*`）
  - 默认值用 `DEFAULT_SLIM_RULES` 单例（解决 B008）
- [src/fspack/builder.py](../../src/fspack/builder.py)
  - 导入 `SlimRules`/`DEFAULT_SLIM_RULES`
  - `unpack_wheels` 用 `slim_rules` 参数替代 `slim_include`/`slim_exclude`
  - `_install_wheels` 透传 `ctx.info.slim_rules`
- [tests/test_slim.py](../../tests/test_slim.py)：6 个用户规则测试改用 `SlimRules(include=..., exclude=...)`
- [tests/test_config.py](../../tests/test_config.py)：
  - 4 个 slim 测试改断言 `info.slim_rules.include`/`exclude`/`has_rules`
  - `test_parse_project_find_links_non_string_element_raises` 更新错误消息匹配
    （"必须是字符串列表" → "元素必须是字符串"，新解析器更精确）

## 关键决策与依据

### 1. SlimRules 用 frozen dataclass

依据 `python-class-design` SKILL："配置/描述类用 `@dataclass(frozen=True)`"。
`SlimRules` 是不可变值对象，`include`/`exclude` 用 `tuple[str, ...]`（非 list）。
frozen 使其可哈希、线程安全，且 `DEFAULT_SLIM_RULES` 单例可安全共享。

### 2. 行为内聚：matches_* 方法

`_match_any(path, patterns)` 辅助函数原本是模块级纯函数，但它的语义与 `SlimRules`
紧密耦合（匹配 include/exclude 规则）。按 SKILL 的"行为内聚"原则，将其收敛为
`SlimRules.matches_include(path)`/`matches_exclude(path)` 方法，使规则数据与匹配
行为内聚到同一对象。

### 3. DEFAULT_SLIM_RULES 模块级单例

ruff B008 禁止函数调用作为默认参数值（`slim_rules: SlimRules = SlimRules()`）。
虽然 frozen dataclass 不可变，但 ruff 无法识别。按 SKILL 建议"读默认值从模块级
单例变量"，定义 `DEFAULT_SLIM_RULES = SlimRules()` 作为公开单例，跨模块共享。

### 4. _parse_string_list_cfg 的 reject_empty 参数

三个原函数的差异在于空元素处理：
- `_parse_exclude_dirs`/`_parse_slim_patterns`：空元素报错
- `_parse_string_list`（extra-index-urls/find-links）：空元素静默过滤

用 `reject_empty: bool` 关键字参数区分，保持单一函数覆盖所有场景。
错误消息也更精确：原 `_parse_string_list` 对非字符串元素报"必须是字符串列表"，
新实现报"元素必须是字符串"（更准确，因为 value 已经是 list，只是元素类型错）。

### 5. 不重构 extra_index_urls/find_links

`extra_index_urls`/`find_links` 在 `packaging/wheels.py` 中 10+ 个函数签名独立
透传，重构为 `PackageSources` dataclass 会增加 wheels.py 的复杂度（每个函数都要
解构 dataclass）。按"避免过度工程化"原则，保持现状。

## 代码实现情况

### SlimRules dataclass

```python
@dataclass(frozen=True)
class SlimRules:
    """wheel 精简用户自定义规则（glob 模式）."""
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, fspack_cfg: dict[str, Any]) -> SlimRules:
        """从 [tool.fspack] 解析 slim-include/slim-exclude."""
        ...

    @property
    def has_rules(self) -> bool:
        """是否配置了任何用户规则."""
        return bool(self.include or self.exclude)

    def matches_include(self, path: str) -> bool:
        """检查路径是否匹配任一 include 规则."""
        return _match_any_glob(path, self.include)

    def matches_exclude(self, path: str) -> bool:
        """检查路径是否匹配任一 exclude 规则."""
        return _match_any_glob(path, self.exclude)
```

### _slim_extract 用户规则集成

```python
def _slim_extract(zf, dest, top_pkg, keep_subs, slim_rules=DEFAULT_SLIM_RULES):
    spec = get_spec(normalize_name(top_pkg))
    for info in zf.infolist():
        # 用户规则优先级最高：include > exclude > spec 自动分类
        if slim_rules.matches_include(info.filename):
            zf.extract(info, dest)
            continue
        if slim_rules.matches_exclude(info.filename):
            continue
        category, sub = spec.classify_entry(info.filename, top_pkg, keep_subs)
        # ... spec 自动分类逻辑
```

## 测试验证结果

- ruff check：All checks passed
- ruff format：47 files already formatted
- pyrefly check：0 errors
- pytest（非 slow）：966 passed, 21 deselected
- 覆盖率：97.12%（slim/base.py 100%，slim/default.py 100%，slim/qt.py 100%）

## 遗留事项

无。

## 下一轮计划

无。本次重构闭环完成。
