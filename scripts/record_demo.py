"""真实录制 fspack 典型工作流 → asciinema cast + 终端 GIF。

用法：

    uv run python scripts/record_demo.py                     # 录制并生成 GIF
    uv run python scripts/record_demo.py --only-gif          # 仅用已有 cast 渲染 GIF
    uv run python scripts/record_demo.py --no-cleanup        # 保留临时 demo 项目

产出：

    docs/assets/demo.cast    # asciinema v2 格式，GitHub 可预览
    docs/assets/demo.gif     # 终端风格 GIF，README 可直接嵌入
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Pillow 渲染（仅 GIF 输出需要）
from PIL import Image, ImageDraw, ImageFont  # type: ignore[import-not-found]

# ============================================================
# 路径与常量
# ============================================================

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = REPO_ROOT / "docs" / "assets"
DEMO_DIR = REPO_ROOT / "demo"

# GIF 渲染参数（终端仿真）—— 紧凑布局 + 64色调色板压缩体积
TERM_WIDTH_CHARS = 72
TERM_HEIGHT_CHARS = 18
FONT_SIZE = 12  # 字号
LINE_SPACING = 1  # 行间距（最小）
PADDING = 12  # 边距
BG_COLOR = (30, 30, 46)  # 深色终端背景（Tokyo Night Storm）
FG_COLOR = (204, 211, 222)  # 默认前景色（淡灰）
INFO_COLOR = (125, 231, 255)  # [INFO] 青色
OK_COLOR = (158, 206, 107)  # [OK]/[DONE] 绿色
ERR_COLOR = (243, 139, 168)  # [ERROR] 粉红
WARN_COLOR = (249, 226, 175)  # [WARN] 黄色
CMD_COLOR = (180, 190, 255)  # 命令提示符蓝紫

# 字体优先：微软雅黑（覆盖中英文）→ 等宽字体 fallback
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",  # 微软雅黑（含中英文）
    r"C:\Windows\Fonts\msyhbd.ttc",  # 微软雅黑 Bold
    r"C:\Windows\Fonts\consola.ttf",  # Consolas
    r"C:\Windows\Fonts\lucon.ttf",  # Lucida Console
    r"C:\Windows\Fonts\cour.ttf",  # Courier New
]

# CJK → 英文短语翻译表（按短语匹配，避免单字误替换）
# 用于把 fspack CLI 中文输出翻译成英文演示
_ZH_TO_EN: list[tuple[str, str]] = [
    # 状态标志
    ("[OK]", "[OK]"),  # 已经是英文，占位
    ("[INFO]", "[INFO]"),
    ("[DONE]", "[DONE]"),
    ("[ERROR]", "[ERROR]"),
    ("[WARN]", "[WARN]"),
    # 核心动词/短语
    ("已创建项目", "Project created"),
    ("已生成", "Generated"),
    ("已缓存", "Cached"),
    ("已在", ""),
    ("已完成", "Done"),
    ("构建完成", "Build complete"),
    ("发行包已生成", "Release generated"),
    ("安装包已生成", "Installer generated"),
    ("产物清单已生成", "Artifact manifest saved"),
    ("兼容报告已写入", "Compatibility report written"),
    ("构建阶段汇总", "Build pipeline summary"),
    ("打包阶段汇总", "Package pipeline summary"),
    ("类别分布", "Category breakdown"),
    ("压缩后", "Compressed"),
    ("目录", "Dir"),
    ("文件", "File"),
    ("文件数", "Files"),
    ("体积", "Size"),
    ("占比", "Share"),
    ("耗时", "Duration"),
    ("阶段", "Stage"),
    ("项数", "Items"),
    ("跳过", "Skip"),
    ("条目", "Entry"),
    ("项目", "Project"),
    ("模板", "Template"),
    ("类型", "Type"),
    ("类别", "Category"),
    ("脚本", "Script"),
    ("执行", "Executing"),
    ("生成", "Generate"),
    ("安装包", "Installer"),
    ("安装包将检测目标机", "Installer will check target machine"),
    ("并在缺失时提示", "and prompt if missing"),
    ("下载", "Download"),
    ("下载运行时", "Downloading runtime"),
    ("解析项目", "Parsing project"),
    ("解析图标", "Parsing icon"),
    ("复制源码", "Copying source"),
    ("分析依赖", "Analyzing deps"),
    ("精简标准库", "Slimming stdlib"),
    ("精简", "Slimming"),
    ("注入", "Injecting"),
    ("资源", "Resources"),
    ("编译", "Compile"),
    ("预编译字节码", "Pre-compiling bytecode"),
    ("解压运行时", "Extracting runtime"),
    ("准备项目", "Preparing project"),
    ("校验", "Verify"),
    ("缓存", "Cache"),
    ("缺失且无法就地编译", "Missing, cannot compile in-place"),
    ("产物依赖", "Artifact depends on"),
    ("会在注入时再报一次", "will be reported again during injection"),
    ("仅", "only"),
    ("保守档剥", ""),
    ("保守档剥离", ""),
    ("兼容扫描", "Compat scan"),
    ("目标", "Target"),
    ("目标机", "Target machine"),
    ("缺失", "Missing"),
    ("不可用", "Unavailable"),
    ("净省", "Saved"),
    ("节省", "Saved"),
    ("共", "Total"),
    ("总计", "Total"),
    ("其他", "Other"),
    ("通过", "Pass"),
    ("重写", "Rewrite"),
    ("命中", "Hit"),
    ("备注", "Note"),
    ("无第三方依赖", "No third-party deps"),
    ("无调试符", "No debug symbols"),
    ("标准库", "stdlib"),
    ("运行时", "Runtime"),
    ("第三方", "third-party"),
    ("字节", "B"),
    ("个", ""),
    ("个包", " packages"),
    ("个文件", " files"),
    ("个第三方", " third-party"),
    ("文件通", ""),
    ("中存在", " exists in"),
    ("打包", "Package"),
    ("打包前", "Before packaging"),
    ("打包后", "After packaging"),
    ("下一步", "Next step"),
    ("下载中", "Downloading"),
    ("字节码", "bytecode"),
    ("精简后", "After slimming"),
    ("精简前", "Before slimming"),
    ("构建", "Build"),
    ("打包阶段", "Package stage"),
    ("标准库剥离", "stdlib strip"),
    # 标点/格式残留
    ("（模板:", "(Template:"),
    ("（模板", "(Template"),
    ("（会在", "(will"),
    ("（", "("),
    ("）", ")"),
    ("：", ": "),
    ("目标:", "Target:"),
    ("项目:", "Project:"),
]


def _translate_to_en(text: str) -> str:
    """把 fspack CLI 中文输出替换成英文。

    逐短语替换，保持顺序（先长后短）避免冲突。
    """
    # 按长度降序排序，确保长短语优先匹配
    sorted_pairs = sorted(_ZH_TO_EN, key=lambda x: len(x[0]), reverse=True)
    result = text
    for zh, en in sorted_pairs:
        if zh and zh in result:
            result = result.replace(zh, en)
    # 清理常见残留：多余空格、空括号
    import re as _re

    result = _re.sub(r"\s{2,}", " ", result)  # 多空格 → 单空格
    result = _re.sub(r"\(\s*\)", "", result)  # 空括号移除
    result = _re.sub(r"\s+\n", "\n", result)  # 行尾空格
    return result


# ============================================================
# 数据类
# ============================================================


@dataclass
class TimedOutput:
    """带时间戳的单行输出。"""

    delay: float  # 相对上一行的延迟秒数
    text: str  # 输出文本（含换行符）
    is_command: bool = False  # 是否为输入的命令


@dataclass
class CaptureResult:
    """一次录制的完整输出序列。"""

    events: list[TimedOutput] = field(default_factory=list)

    def add(self, text: str, delay: float = 0.0, is_cmd: bool = False) -> None:
        self.events.append(TimedOutput(delay=delay, text=text, is_command=is_cmd))


# ============================================================
# 命令捕获
# ============================================================


def _find_font() -> str:
    """查找可用的等宽字体文件。"""
    from pathlib import Path as _P

    for path in FONT_CANDIDATES:
        if _P(path).exists():
            return path
    raise FileNotFoundError(f"找不到等宽字体，候选：{FONT_CANDIDATES}")


def _capture_command(
    cmd: list[str],
    cwd: Path | None = None,
    timeout: int = 300,
) -> list[TimedOutput]:
    """执行一条命令，逐行捕获带时间戳的输出。"""
    print(f"  → {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,  # 行缓冲
    )

    events: list[TimedOutput] = []
    start = time.monotonic()
    last_ts = start

    # 先记录命令本身（模拟用户输入）
    elapsed = time.monotonic() - last_ts
    events.append(
        TimedOutput(delay=elapsed, text=f"$ {' '.join(cmd[1:]) if len(cmd) > 1 else cmd[0]}\n", is_command=True)
    )
    last_ts = time.monotonic()

    assert proc.stdout is not None
    for line in proc.stdout:
        elapsed = time.monotonic() - last_ts
        # 去掉 rich 控制台 ANSI 转义码（GIF 渲染不支持）
        clean = _strip_ansi(line.rstrip("\n"))
        events.append(TimedOutput(delay=elapsed, text=clean + "\n"))
        last_ts = time.monotonic()

        # 实时打印（便于用户看进度）
        sys.stdout.write(line)
        sys.stdout.flush()

    proc.wait(timeout=timeout)
    return events


_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07")


def _strip_ansi(text: str) -> str:
    """移除 ANSI 转义序列，保留纯文本。"""
    return _ANSI_RE.sub("", text)


# ============================================================
# 录制主流程
# ============================================================


def run_demo_capture() -> CaptureResult:
    """执行完整录制流程：init → b → p。"""
    result = CaptureResult()

    # 确保 fsp 命令可用
    fsp_cmd = _resolve_fsp()

    # ---- 清理旧的临时目录 ----
    if DEMO_DIR.exists():
        shutil.rmtree(DEMO_DIR)

    # ---- Step 1: fsp init ----
    print("\n[1/3] Scaffolding demo project...")
    result.add(f"$ {' '.join(fsp_cmd)} init demo --template helloworld\n", delay=0.5, is_cmd=True)
    init_events = _capture_command([*fsp_cmd, "init", "demo", "--template", "helloworld"], timeout=60)
    result.events.extend(init_events)

    if not DEMO_DIR.exists():
        print("ERROR: fsp init failed, demo dir not created. Aborting.")
        return result

    # ---- Step 2: fsp b ----
    print("\n[2/3] Building...")
    os.chdir(DEMO_DIR)
    result.add(f"$ {' '.join(fsp_cmd)} b\n", delay=0.8, is_cmd=True)
    build_events = _capture_command([*fsp_cmd, "b"], timeout=240)
    result.events.extend(build_events)

    # ---- Step 3: fsp p ----
    print("\n[3/3] Packaging installer...")
    result.add(f"$ {' '.join(fsp_cmd)} p\n", delay=0.8, is_cmd=True)
    pkg_events = _capture_command([*fsp_cmd, "p"], timeout=120)
    result.events.extend(pkg_events)

    return result


def _resolve_fsp() -> list[str]:
    """定位 fsp 命令前缀。返回 list 以便与子命令拼接。"""
    for candidate in ["fsp", "fspack"]:
        if shutil.which(candidate):
            return [candidate]
    # 回退：python -m fspack.cli（开发环境）
    return [sys.executable, "-m", "fspack.cli"]


# ============================================================
# asciinema cast 生成
# ============================================================


def build_cast(result: CaptureResult) -> dict[str, Any]:
    """把 CaptureResult 转为 asciinema v2 格式。"""
    # 把事件展平为 asciinema 的 [delay, "o", "text"] 三元组
    # 累积时间戳
    cumulative = 0.0
    events_cast: list[list[float | str]] = []

    for ev in result.events:
        delay = max(ev.delay, 0.01)  # 最小间隔防止所有行叠在一起
        # cast 格式：[timestamp, "o", "text"]
        cumulative += delay
        events_cast.append([round(cumulative, 3), "o", ev.text])

    # 还要添加一行初始 prompt 让 cast 看起来自然
    cast = {
        "version": 2,
        "width": TERM_WIDTH_CHARS,
        "height": TERM_HEIGHT_CHARS,
        "timestamp": int(time.time()),
        "env": {"SHELL": "powershell", "TERM": "xterm-256color"},
        "stdout": events_cast,
    }
    return cast


def save_cast(cast: dict[str, Any], path: Path) -> None:
    """保存 asciinema v2 格式 cast（NDJSON：每行一个 JSON）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {k: v for k, v in cast.items() if k not in {"stdout", "stdin"}}
    lines = [json.dumps(header, ensure_ascii=False)]
    for event in cast.get("stdout", []):
        lines.append(json.dumps(event, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nCast saved: {path}")


def load_cast(path: Path) -> dict[str, Any]:
    """从 asciinema v2 NDJSON cast 文件还原内存 dict。"""
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    header = json.loads(lines[0])
    events = [json.loads(line) for line in lines[1:]]
    header["stdout"] = events
    return header


# ============================================================
# GIF 渲染（Pillow 逐帧绘制）
# ============================================================


def render_gif_from_cast(cast: dict[str, Any], output_path: Path, lang: str = "zh") -> None:
    """用 Pillow 把 asciinema cast 渲染成终端风格 GIF。

    Args:
        cast: asciinema v2 格式字典
        output_path: GIF 输出路径
        lang: 输出语言 — "zh" 保留中文（默认），"en" 翻译为英文
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"\nRendering GIF → {output_path} [lang={lang}] ...")

    font_path = _find_font()
    font = ImageFont.truetype(font_path, FONT_SIZE)

    # 计算像素尺寸
    char_width = _measure_char_width(font)
    term_width_px = TERM_WIDTH_CHARS * char_width + PADDING * 2
    line_height = FONT_SIZE + LINE_SPACING
    term_height_px = TERM_HEIGHT_CHARS * line_height + PADDING * 2

    # 从 cast stdout 重建完整输出历史
    # 策略：累积所有 stdout 文本，然后为每个事件生成"截至当前"的帧
    stdout_events = cast["stdout"]
    frames: list[Image.Image] = []
    current_lines: list[tuple[str, tuple[int, int, int]]] = []

    # 初始空 frame（终端刚打开）
    frames.append(_make_frame(current_lines, font, term_width_px, term_height_px, line_height, PADDING))

    # 帧采样计数器：每 SKIP_N 行才生成一帧，减少总帧数
    _frame_counter = 0
    SKIP_N = 2  # 每 2 行取 1 帧（1=全量，2=半量）

    for ev in stdout_events:
        _timestamp, kind, text = ev
        if kind != "o":
            continue

        # 翻译：英文版做 ZH→EN 替换
        if lang == "en":
            text = _translate_to_en(text)

        # 解析文本为行（保留颜色提示）
        lines = text.split("\n")
        for i, raw_line in enumerate(lines):
            is_last = i == len(lines) - 1
            if not raw_line and is_last:
                continue  # 末尾空行跳过

            # 颜色判定（基于文本内容）
            color = _detect_line_color(raw_line)
            current_lines.append((raw_line, color))

            # 滚屏：超过终端高度时移除最早的行
            if len(current_lines) > TERM_HEIGHT_CHARS:
                current_lines = current_lines[-TERM_HEIGHT_CHARS:]

            # 帧采样：每隔 SKIP_N 行生成一帧
            _frame_counter += 1
            if _frame_counter % SKIP_N != 0:
                continue
            frame = _make_frame(current_lines, font, term_width_px, term_height_px, line_height, PADDING)
            frames.append(frame)

    # 尾部停顿帧（让用户看清最终结果）
    tail_frame = _make_frame(current_lines, font, term_width_px, term_height_px, line_height, PADDING)
    for _ in range(5):
        frames.append(tail_frame)

    # 保存 GIF：显式量化到 64 色大幅压缩
    # 每帧先 quantize 成 P mode（调色板模式），确保全局 64 色
    quantized_frames = [f.quantize(colors=64, method=Image.Quantize.FASTOCTREE) for f in frames]

    durations = [120] * len(quantized_frames)
    durations[-5:] = [160] * 5  # 尾部稍慢

    quantized_frames[0].save(
        str(output_path),
        save_all=True,
        append_images=quantized_frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )
    kb = output_path.stat().st_size / 1024
    print(f"  GIF rendered: {len(quantized_frames)} frames, {kb:.0f} KB ({term_width_px}x{term_height_px}, 64 colors)")


def _measure_char_width(font: ImageFont.FreeTypeFont) -> int:
    """测量等宽字符的宽度。"""
    bbox = font.getbbox("W")  # 用大写 W 算宽度
    return int(bbox[2] - bbox[0])


def _detect_line_color(text: str) -> tuple[int, int, int]:
    """根据行内容判断文本颜色（模拟终端高亮）。

    同时匹配中文和英文关键词，因为翻译在颜色判定之前或之后都可能调用。
    """
    if text.startswith("$ ") or text.startswith("$"):
        return CMD_COLOR
    if any(tok in text for tok in ("[OK]", "[DONE]", "Build complete", "Project created", "Installer generated", "✓")):
        return OK_COLOR
    if any(tok in text for tok in ("[ERROR]", "[FAIL]", "Traceback", "Error", "失败")):
        return ERR_COLOR
    if any(tok in text for tok in ("[WARN]", "WARNING", "注意", "Warning")):
        return WARN_COLOR
    if "[INFO]" in text or text.startswith("["):
        return INFO_COLOR
    return FG_COLOR


def _make_frame(  # noqa: PLR0913
    lines: list[tuple[str, tuple[int, int, int]]],
    font: ImageFont.FreeTypeFont,
    width: int,
    height: int,
    line_h: int,
    pad: int,
) -> Image.Image:
    """渲染单帧终端画面。"""
    img = Image.new("RGB", (width, height), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # 顶部画一条窗口标题栏（macOS 风格圆点）
    dot_r = 6
    for i, color in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        draw.ellipse(
            [pad + i * (dot_r * 2 + 8), pad + 4, pad + i * (dot_r * 2 + 8) + dot_r * 2, pad + 4 + dot_r * 2],
            fill=color,
        )

    y = pad + dot_r * 2 + 8
    max_lines = min(len(lines), TERM_HEIGHT_CHARS)

    for i in range(max_lines):
        text, color = lines[i]
        # 截断超长行
        max_chars = TERM_WIDTH_CHARS
        if len(text) > max_chars:
            text = text[: max_chars - 1] + "…"

        # 多行自动换行
        while len(text) > max_chars:
            chunk = text[:max_chars]
            draw.text((pad, y), chunk, font=font, fill=color)
            y += line_h
            text = text[max_chars:]
            if not text:
                break

        if text:
            draw.text((pad, y), text, font=font, fill=color)
            y += line_h

    return img


# ============================================================
# Main
# ============================================================


def main() -> None:
    parser = argparse.ArgumentParser(description="录制 fspack 真实工作流 → cast + GIF")
    parser.add_argument("--only-gif", action="store_true", help="仅用已有 cast 渲染 GIF")
    parser.add_argument("--no-cleanup", action="store_true", help="保留临时 demo 项目")
    parser.add_argument(
        "--lang",
        choices=["zh", "en"],
        default="zh",
        help="GIF 输出语言：zh 保留中文（默认），en 翻译为英文",
    )
    args = parser.parse_args()

    cast_path = ASSETS_DIR / "demo.cast"
    gif_path = ASSETS_DIR / ("demo-en.gif" if args.lang == "en" else "demo.gif")

    if not args.only_gif:
        print("=" * 60)
        print("  fspack workflow recorder")
        print("  → fsp init demo --template helloworld")
        print("  → fsp b")
        print("  → fsp p")
        print("=" * 60)

        result = run_demo_capture()
        cast = build_cast(result)
        save_cast(cast, cast_path)
    else:
        print(f"Loading existing cast: {cast_path}")
        if not cast_path.exists():
            print(f"ERROR: {cast_path} not found. Run without --only-gif first.")
            sys.exit(1)
        cast = load_cast(cast_path)

    render_gif_from_cast(cast, gif_path, lang=args.lang)

    # 清理临时目录
    if DEMO_DIR.exists() and not args.no_cleanup:
        print(f"\nCleaning up {DEMO_DIR} ...")
        os.chdir(REPO_ROOT)
        shutil.rmtree(DEMO_DIR)

    print("\n" + "=" * 60)
    print("Done!")
    print(f"  Cast:  {cast_path}")
    print(f"  GIF:   {gif_path}")
    print(f"  Lang:  {args.lang}")
    print("=" * 60)


if __name__ == "__main__":
    main()
