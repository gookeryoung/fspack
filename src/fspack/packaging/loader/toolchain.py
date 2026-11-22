"""交叉编译工具链发现与 Windows 资源段编译.

从 :mod:`fspack.packaging.loader.compile` 拆分而来，聚集「工具链」职责：

- 编译器可执行名常量（mingw gcc / windres / gcc / clang）
- :func:`_find_mingw_gcc` / :func:`_find_windres`：编译器发现（交叉前缀优先）
- :func:`_compile_resource_obj`：windres 编译 Windows 资源段（icon + 版本信息 + manifest）
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Tuple, cast

from fspack.packaging.loader.resource import (
    LoaderVersionInfo,
    generate_app_manifest,
    generate_resource_rc,
)

__all__ = ["LINUX_GCC", "MACOS_CLANG", "MINGW_GCC", "MINGW_WINDRES", "toolchain_fingerprint"]

# 共享 logger 名：与 loader 包各子模块一致，测试 caplog 按 logger 名过滤
_logger = logging.getLogger("fspack.packaging.loader")
MINGW_GCC = "x86_64-w64-mingw32-gcc"
MINGW_WINDRES = "x86_64-w64-mingw32-windres"
LINUX_GCC = "gcc"
MACOS_CLANG = "clang"

# windres 单次资源编译超时（秒）：单 rc 文件实测 <1s，120s 裕量覆盖杀软
# 扫描延迟；超时与编译失败同路径降级（warning + 跳过资源段，不阻断构建）
_WINDRES_TIMEOUT = 120.0

_WHICH_LIMIT = 64
_which_lock = threading.Lock()
_which_results: OrderedDict[tuple[object, ...], str | None] = OrderedDict()


def _which(name: str) -> str | None:
    """进程内复用 PATH 查找；PATH、PATHEXT、工作目录或解析器变化时重新查找."""
    # 解析器本身参与键：被替换（嵌入方/测试 patch）时不复用旧结果
    key = (name, os.environ.get("PATH"), os.environ.get("PATHEXT"), Path.cwd(), shutil.which)
    with _which_lock:
        if key in _which_results:
            found = _which_results[key]
            if found is None or Path(found).is_file():
                _which_results.move_to_end(key)
                return found
        found = shutil.which(name)
        _which_results[key] = found
        _which_results.move_to_end(key)
        while len(_which_results) > _WHICH_LIMIT:
            _which_results.popitem(last=False)
        return found


def _find_windres() -> str:
    """查找可用的 windres，优先交叉前缀，回退无前缀.

    Windows mingw64 发行版通常命名 ``windres``（无前缀），Linux 交叉编译
    环境命名 ``x86_64-w64-mingw32-windres``（带前缀）。两者都查找不到时
    返回默认名，让后续 subprocess 报 FileNotFoundError。
    """
    for name in (MINGW_WINDRES, "windres"):
        if _which(name):
            return name
    return MINGW_WINDRES


def _find_mingw_gcc() -> str | None:
    """查找可用的 mingw gcc，优先交叉前缀.

    与 :func:`_find_windres` 类似但回退受限：Windows 原生 mingw64 发行版
    （MSYS2、WinLibs、chocolatey mingw 包）通常命名 ``gcc``（无前缀），
    仅在 ``sys.platform == "win32"`` 时回退 ``"gcc"``。Linux/macOS 的
    ``gcc`` 是 host 编译器（产出 ELF/Mach-O 而非 PE），交叉构建 Windows
    exe 时不能回退，无 mingw 前缀编译器时返回 ``None``（调用方据此判
    ``available()`` 为 False）。
    """
    if _which(MINGW_GCC):
        return MINGW_GCC
    if sys.platform == "win32" and _which("gcc"):
        return "gcc"
    return None


def _compile_resource_obj(
    icon: Path | None,
    work_dir: Path,
    *,
    version_info: LoaderVersionInfo | None = None,
) -> Path | None:
    """用 windres 编译 Windows 资源（icon + 版本信息 + manifest）为 COFF ``.o``，返回路径.

    生成 ``resource.rc``（icon 引用 + VS_VERSIONINFO + manifest 引用）与
    ``app.manifest``，windres 编译为 ``resource.o`` 供 gcc 链接到 exe。

    - ``icon`` 非 None 且文件存在时复制到 work_dir 并在 rc 中引用；不存在则 warning
      并跳过 icon（其余资源仍编译）
    - ``version_info`` 非 None 时 rc 含 VERSIONINFO 块；为 None 时省略版本信息
    - manifest 总是生成并引用（asInvoker + DPI + supportedOS），单独嵌入即降低可疑度

    windres 不可用时 warning 并返回 None（exe 仍可编译，仅无资源段）。windres
    编译失败（如 rc 语法错误）时同样返回 None，不阻断构建。
    """
    windres = _find_windres()
    if not _which(windres):
        _logger.warning("未找到 windres，跳过资源嵌入（版本信息/manifest/icon，请安装 mingw-w64）")
        return None
    work_dir.mkdir(parents=True, exist_ok=True)
    # icon 复制到 work_dir（windres 解析相对路径，统一文件名 icon.ico）
    has_icon = False
    if icon is not None and icon.is_file():
        shutil.copy2(icon, work_dir / "icon.ico")
        has_icon = True
    elif icon is not None:
        _logger.warning("icon 文件不存在，跳过图标嵌入（其余资源仍编译）: %s", icon)
    # 写 resource.rc 与 app.manifest（UTF-8；rc 顶部 #pragma code_page(65001) 声明编码，
    # 使中文 CompanyName/FileDescription 被 windres 正确转为 UTF-16LE 存入 PE 资源段）
    rc_content = generate_resource_rc(version_info, has_icon=has_icon)
    rc_file = work_dir / "resource.rc"
    rc_file.write_text(rc_content, encoding="utf-8")
    manifest_name = version_info.name if version_info is not None else "app"
    manifest_version = version_info.version if version_info is not None else "1.0.0"
    manifest_content = generate_app_manifest(manifest_name, manifest_version)
    manifest_file = work_dir / "app.manifest"
    manifest_file.write_text(manifest_content, encoding="utf-8")
    obj_file = work_dir / "resource.o"
    cmd = [windres, "--input", str(rc_file), "--output", str(obj_file), "--output-format=coff"]
    _logger.debug("编译 Windows 资源: %s", " ".join(cmd))
    try:
        completed = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=work_dir,
            timeout=_WINDRES_TIMEOUT,
        )
        _forward_tool_output(completed.stdout, completed.stderr)
    except FileNotFoundError as e:
        _logger.warning("windres 不可用，跳过资源嵌入: %s", e)
        return None
    except subprocess.TimeoutExpired:
        _logger.warning("资源编译超时（%ds），跳过资源嵌入", int(_WINDRES_TIMEOUT))
        return None
    except subprocess.CalledProcessError as e:
        _forward_tool_output(e.stdout, e.stderr)
        _logger.warning("资源编译失败，跳过资源嵌入:\n%s", e.stderr)
        return None
    return obj_file


def _forward_tool_output(stdout: str | None, stderr: str | None) -> None:
    """把 windres 原始输出转发到 DEBUG（``[loader]`` 前缀，默认不显示）."""
    if not stdout and not stderr:
        return
    from fspack.console import console

    for text in (stdout, stderr):
        if text:
            console.tool_output("loader", text.encode("utf-8"))


_FINGERPRINT_LIMIT = 32
_PROBE_TIMEOUT = 10.0
_fingerprint_lock = threading.RLock()
_FingerprintEntry = Tuple[Tuple[Tuple[str, str], ...], Tuple[object, ...], str]
_fingerprints: OrderedDict[str, _FingerprintEntry] = OrderedDict()
_FINGERPRINT_ENV = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "HOME",
        "USERPROFILE",
        "TMPDIR",
        "TMP",
        "TEMP",
        "CPATH",
        "C_INCLUDE_PATH",
        "CPLUS_INCLUDE_PATH",
        "OBJC_INCLUDE_PATH",
        "COMPILER_PATH",
        "GCC_EXEC_PREFIX",
        "LIBRARY_PATH",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "LD_RUN_PATH",
        "LIB",
        "LIBPATH",
        "INCLUDE",
        "SDKROOT",
        "DEVELOPER_DIR",
        "MACOSX_DEPLOYMENT_TARGET",
        "SDK_DIR",
        "SYSROOT",
        "CC",
        "CXX",
        "CPP",
        "RC",
        "CFLAGS",
        "CXXFLAGS",
        "CPPFLAGS",
        "LDFLAGS",
        "RCFLAGS",
        "SOURCE_DATE_EPOCH",
        "ZERO_AR_DATE",
        "LANG",
        "DEPENDENCIES_OUTPUT",
        "SUNPRO_DEPENDENCIES",
        "CCC_OVERRIDE_OPTIONS",
        "QA_OVERRIDE_GCC3_OPTIONS",
        "CL",
        "_CL_",
        "LINK",
        "_LINK_",
        "MSYSTEM",
        "MSYSTEM_PREFIX",
        "MINGW_PREFIX",
        "WINDOWSSDKDIR",
        "WINDOWSSDKVERSION",
        "UCRTVERSION",
        "UNIVERSALCRTSDKDIR",
        "VCTOOLSINSTALLDIR",
        "VCTOOLSVERSION",
        "VSCMD_ARG_TGT_ARCH",
        "VSINSTALLDIR",
        "VCINSTALLDIR",
        "LDEMULATION",
        "GNUTARGET",
    }
)


def _fingerprint_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolve_tool(compiler: str) -> str:
    path = Path(compiler)
    if path.is_absolute():
        return str(path)
    return str(Path(_which(compiler) or compiler).absolute())


def _path_identity(path: str) -> tuple[object, ...]:
    absolute = str(Path(path).absolute())
    real = os.path.realpath(absolute)
    try:
        info = Path(real).stat()
    except OSError as exc:
        return (absolute, real, "missing", exc.errno)
    return (absolute, real, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode, info.st_ino)


def _watch_identity(watch: tuple[str, str]) -> tuple[object, ...]:
    kind, name = watch
    path = _resolve_tool(name) if kind == "tool" else str(Path(name).absolute())
    try:
        info = Path(path).stat()
    except OSError as exc:
        return (path, "missing", exc.errno)
    return (path, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode, info.st_ino, info.st_dev)


def _probe_tool(executable: str, flag: str, environment: dict[str, str]) -> tuple[object, str, str]:
    try:
        completed = subprocess.run(
            [executable, flag],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PROBE_TIMEOUT,
            env=environment,
        )
        return completed.returncode, completed.stdout or "", completed.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        _logger.debug("工具链身份探测失败: %s %s: %s", executable, flag, exc)
        return type(exc).__name__, str(exc), ""


def _search_paths(output: str) -> list[str]:
    paths = []
    for line in output.splitlines():
        label, separator, value = line.partition(":")
        if separator and label.strip() in ("install", "programs", "libraries"):
            paths.extend(p for p in value.strip().lstrip("=").split(os.pathsep) if p)
    return paths


def _resource_compiler(executable: str) -> str | None:
    path = Path(executable)
    stem = path.stem
    prefix = stem[: -len("windres")] if stem.endswith("windres") else ""
    name = prefix + "gcc"
    sibling = path.with_name(name + path.suffix)
    if sibling.is_file():
        return str(sibling)
    return _which(name) or _which("gcc")


def _sdk_watches(
    environment: dict[str, str], probes: dict[str, tuple[object, str, str]], *, resource: bool
) -> list[tuple[str, str]]:
    watches = []
    parts = ("", "usr/include", "usr/lib", "SDKSettings.json", "SDKSettings.plist")
    for name, value in environment.items():
        if value and name.upper() in ("SDKROOT", "SYSROOT", "SDK_DIR", "DEVELOPER_DIR", "GCC_EXEC_PREFIX"):
            watches.extend(("path", str(Path(value) / part)) for part in parts)
    if sys.platform == "darwin" and not resource:
        watches.extend((("tool", "xcrun"), ("path", "/var/db/xcode_select_link")))
        xcrun = shutil.which("xcrun")
        if xcrun:
            sdk = _probe_tool(xcrun, "--show-sdk-path", environment)
            probes["xcrun-sdk"] = sdk
            if sdk[0] == 0 and sdk[1].strip():
                watches.extend(("path", str(Path(sdk[1].strip()) / part)) for part in parts)
    return watches


def _fingerprint_probes(
    executable: str, environment: dict[str, str], *, resource: bool
) -> tuple[list[tuple[str, str]], dict[str, tuple[object, str, str]], bool]:
    watches = [("path", executable), ("path", str(Path(executable).parent))]
    probes: dict[str, tuple[object, str, str]] = {}
    flags = (
        ("--version",)
        if resource
        else (
            "-v",
            "-dumpmachine",
            "-print-search-dirs",
            "-print-sysroot",
            "-print-resource-dir",
            "-print-prog-name=ld",
            "-print-prog-name=cc1",
            "-print-prog-name=as",
            "-print-prog-name=collect2",
            "-print-file-name=specs",
            "-print-file-name=libgcc.a",
            "-print-file-name=crtbegin.o",
            "-print-file-name=crt2.o",
        )
    )
    for flag in flags:
        result = _probe_tool(executable, flag, environment)
        probes[flag] = result
        output = result[1].strip()
        if result[0] != 0 or not output or "\n" in output:
            continue
        if flag.startswith("-print-prog-name="):
            watches.append(("tool", output))
        elif flag.startswith("-print-file-name="):
            if output != flag.partition("=")[2]:
                watches.append(("path", output))
        elif flag in ("-print-sysroot", "-print-resource-dir"):
            watches.extend(("path", str(Path(output) / part)) for part in ("", "include", "usr/include", "usr/lib"))
    watches.extend(("path", directory) for directory in _search_paths(probes.get("-print-search-dirs", (0, "", ""))[1]))
    watches.extend(_sdk_watches(environment, probes, resource=resource))
    cacheable = probes[flags[0]][0] == 0 and all(isinstance(result[0], int) for result in probes.values())
    return watches, probes, cacheable


def _load_fingerprint(store: Path | None, key: str) -> _FingerprintEntry | None:
    if store is None:
        return None
    try:
        data = json.loads((store / f"{key}.json").read_text(encoding="utf-8"))
        watches = tuple((str(kind), str(name)) for kind, name in data["watches"])
        state = tuple(tuple(item) for item in data["state"])
        digest = data["digest"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return (watches, state, digest) if isinstance(digest, str) else None


def _save_fingerprint(store: Path, key: str, entry: _FingerprintEntry) -> None:
    watches, state, digest = entry
    payload = json.dumps({"watches": watches, "state": state, "digest": digest}, ensure_ascii=True)
    temporary: Path | None = None
    try:
        store.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=store, prefix=f".{key}.", suffix=".tmp")
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
        temporary.replace(store / f"{key}.json")
    except OSError as exc:
        _logger.debug("工具链身份持久化失败: %s", exc)
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()


def toolchain_fingerprint(compiler: str, *, resource: bool = False, store: Path | None = None) -> str:
    """返回工具链身份；命中仅重查路径、组件和环境，不重复启动工具.

    ``store`` 指定时探测结果跨进程复用，失效条件与进程内记忆相同。
    """
    with _fingerprint_lock:
        environment = dict(os.environ)
        relevant_env = {
            key: value
            for key, value in environment.items()
            if key.upper() in _FINGERPRINT_ENV
            or key.upper().startswith(("LC_", "GCC_", "CLANG_", "CCACHE_", "DISTCC_", "DYLD_", "SDK"))
        }
        executable = _resolve_tool(compiler)
        identity = _path_identity(executable)
        present = len(identity) > 4 and stat.S_ISREG(cast(int, identity[5]))
        preprocessor = _resource_compiler(executable) if resource and present else None
        preprocessor_key = toolchain_fingerprint(preprocessor, store=store) if preprocessor else ""
        inputs = ["loader-toolchain-v1", compiler, resource, identity, relevant_env, str(Path.cwd()), preprocessor_key]
        key = _fingerprint_hash(inputs)
        previous = _fingerprints.get(key) or _load_fingerprint(store, key)
        if previous is not None:
            watches, old_state, digest = previous
            if tuple(_watch_identity(watch) for watch in watches) == old_state:
                _remember_fingerprint(key, previous)
                return digest
        if present:
            watches_list, probes, cacheable = _fingerprint_probes(executable, environment, resource=resource)
        else:
            watches_list = [("path", executable)]
            probes: dict[str, tuple[object, str, str]] = {}
            cacheable = True
        watches = tuple(dict.fromkeys(watches_list))
        state = tuple(_watch_identity(watch) for watch in watches)
        digest = _fingerprint_hash([inputs, probes, state])
        if cacheable:
            _remember_fingerprint(key, (watches, state, digest))
            if store is not None:
                _save_fingerprint(store, key, (watches, state, digest))
        return digest


def _remember_fingerprint(key: str, entry: _FingerprintEntry) -> None:
    _fingerprints[key] = entry
    _fingerprints.move_to_end(key)
    while len(_fingerprints) > _FINGERPRINT_LIMIT:
        _fingerprints.popitem(last=False)
