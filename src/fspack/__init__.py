"""fspack - 极速 Python 项目打包器."""

from __future__ import annotations

# 运行时兼容：embed python 3.8 携带的 typing_extensions 可能版本过新，import 时
# 访问 typing._SpecialGenericAlias（3.9+）导致 AttributeError。rich 等运行时依赖
# 不在运行时导入 typing_extensions，故此处仅 stub override 即可保证后续模块可用。
try:
    import typing_extensions  # noqa: F401
except (ImportError, AttributeError):  # pragma: no cover
    import sys
    import types

    sys.modules.pop("typing_extensions", None)
    _stub = types.ModuleType("typing_extensions")

    def override(obj):  # type: ignore[no-redef]
        """运行时 no-op 回退."""
        return obj

    _stub.override = override  # type: ignore[attr-defined]
    sys.modules["typing_extensions"] = _stub

__all__ = ["__version__"]

__version__ = "0.1.9"
