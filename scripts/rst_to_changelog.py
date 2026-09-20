"""从 docs/changelog.rst 自动生成 CHANGELOG.md。

用法：

    python scripts/rst_to_changelog.py                # 输出到 stdout
    python scripts/rst_to_changelog.py -o CHANGELOG.md  # 写入文件

规则：

- changelog.rst 中的版本号标题 ``v0.5.6（未发布）`` 映射为 Markdown 的
  ``## [Unreleased]``，已发布版本 ``v0.5.5`` 映射为 ``## [0.5.5] - TBD``
- rst 的 ``- feat:`` / ``- fix:`` / ``- refactor:`` / ``- docs:`` / ``- test:`` /
  ``- chore:`` / ``- build:`` / ``- perf:`` / ``- ci:`` 前缀按类型分组
- rst 双反引号 `` ``xxx`` `` 转换为 Markdown 单反引号 `` `xxx` ``
- 输出遵循 Keep a Changelog 格式
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# RST 源码路径
RST_PATH = Path(__file__).resolve().parent.parent / "docs" / "changelog.rst"

# 类型到 Keep a Changelog 分类的映射表（按匹配优先级排序）
PREFIX_TO_CATEGORY: dict[str, str] = {
    "feat": "Added",
    "fix": "Fixed",
    "perf": "Performance",
    "refactor": "Changed",
    "docs": "Documentation",
    "test": "Tests",
    "chore": "Build",
    "build": "Build",
    "ci": "Build",
}

# 版本标题正则：v0.5.6（未发布）→ Unreleased，v0.5.5 → [0.5.5]
VERSION_RE = re.compile(r"^v(\d+\.\d+\.\d+)(.*)$")


def _rst_code_to_md(text: str) -> str:
    """RST 双反引号代码标记 → Markdown 单反引号。"""
    return re.sub(r"``([^`]+)``", r"`\1`", text)


def parse_rst(path: Path) -> list[dict[str, Any]]:
    """解析 changelog.rst，返回版本记录列表。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    releases: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    # 跳过第一行标题（更新日志）和紧随的 ==== 分隔行
    i = 0
    if lines and lines[0].strip() == "更新日志":
        i = 2

    while i < len(lines):
        line = lines[i]

        # 检测版本标题（不含 = 分隔行）
        if line and not line.startswith((" ", "\t")):
            match = VERSION_RE.match(line.strip())
            if match:
                version = match.group(1)
                suffix = match.group(2).strip()
                is_unreleased = "未发布" in suffix
                current = {
                    "version": version,
                    "is_unreleased": is_unreleased,
                    "entries": [],
                }
                releases.append(current)
                i += 2  # 跳过版本标题和紧随的 ---- 分隔行
                continue

        # 检测条目行（以 "- " 开头）
        if current and line.startswith("- "):
            entry = line[2:]
            current["entries"].append(entry)

        i += 1

    return releases


def classify_entry(entry: str) -> str:
    """根据前缀和内容判断 Keep a Changelog 分类。"""
    prefix_match = re.match(r"^(\w+)", entry)
    prefix = prefix_match.group(1) if prefix_match else "chore"
    category = PREFIX_TO_CATEGORY.get(prefix, "Changed")

    # refactor 条目中含"移除"/"removed"关键词的归类为 Removed
    if prefix == "refactor" and any(kw in entry.lower() for kw in ("移除", "removed", "delete", "drop", "cleanup")):
        return "Removed"

    return category


def group_entries_by_type(entries: list[str]) -> dict[str, list[str]]:
    """按 Keep a Changelog 分类分组条目，保持固定的分类顺序。"""
    category_order = [
        "Added",
        "Changed",
        "Deprecated",
        "Removed",
        "Fixed",
        "Security",
        "Performance",
        "Documentation",
        "Tests",
        "Build",
    ]
    groups: dict[str, list[str]] = {c: [] for c in category_order}

    for entry in entries:
        category = classify_entry(entry)
        groups.setdefault(category, []).append(_rst_code_to_md(entry))

    # 过滤空分类并按预定义顺序返回
    return {k: groups[k] for k in category_order if groups.get(k)}


def render_markdown(releases: list[dict[str, Any]]) -> str:
    """渲染为 Keep a Changelog 格式的 Markdown。"""
    lines = [
        "# Changelog",
        "",
        "All notable changes to this project will be documented in this file.",
        "",
        "The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),",
        "and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).",
        "",
    ]

    for release in releases:
        if release["is_unreleased"]:
            lines.append("## [Unreleased]")
        else:
            v = release["version"]
            lines.append(f"## [{v}] - TBD")
        lines.append("")

        if not release["entries"]:
            lines.append("No changes yet.")
            lines.append("")
            continue

        grouped = group_entries_by_type(release["entries"])  # type: ignore[arg-type]
        for category, entries in grouped.items():
            lines.append(f"### {category}")
            for entry in entries:
                # 去掉 "feat: " 等前缀，保留内容
                text = re.sub(r"^\w+[():]\s*", "", entry)
                lines.append(f"- {text}")
            lines.append("")

    return "\n".join(lines)


def main(output: Path | None = None) -> str:
    """主函数：解析 rst → 渲染 md → 输出。"""
    if not RST_PATH.exists():
        raise FileNotFoundError(f"找不到 changelog.rst: {RST_PATH}")

    releases = parse_rst(RST_PATH)
    md_content = render_markdown(releases)

    if output:
        output.write_text(md_content, encoding="utf-8")
        print(f"已写入 {output}")
    else:
        print(md_content)

    return md_content


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="从 changelog.rst 生成 CHANGELOG.md")
    parser.add_argument("-o", "--output", type=Path, help="输出文件路径（默认 stdout）")
    args = parser.parse_args()

    main(args.output)
