"""``NuitkaCompilerProtocol`` 类型契约测试：契约方法集与 facade 同步守护."""

from __future__ import annotations


def test_protocol_methods_match_compiler_surface() -> None:
    """Protocol 契约声明的全部方法在 NuitkaCompiler facade 上存在（防签名漂移）.

    :mod:`fspack.packaging.nuitka.protocol` 为纯类型契约（运行时仅类型检查期
    使用），各 mixin 用 ``cls: type[NuitkaCompilerProtocol]`` 注解跨类调用。
    本测试守护契约与 facade 同步：Protocol 声明的方法必须在 NuitkaCompiler
    的 MRO 上有真实实现，防止 mixin 重命名后 Protocol 漂移失真。
    """
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.packaging.nuitka.protocol import NuitkaCompilerProtocol

    # 排除 typing.Protocol/ABCMeta 注入的机制属性（_is_protocol 为 bool、
    # _abc_impl 为 abc 缓存），只收集契约方法
    _TYPING_INTERNAL = {"_is_protocol", "_is_runtime_protocol", "_abc_impl"}
    declared = {
        name for name in vars(NuitkaCompilerProtocol) if not name.startswith("__") and name not in _TYPING_INTERNAL
    }
    assert declared, "Protocol 应声明方法集合"
    for name in declared:
        assert hasattr(NuitkaCompiler, name), f"Protocol 声明的 {name} 未由 NuitkaCompiler 提供"


# ---- NuitkaCompilerProtocol 类型契约模块（纯类型，运行时仅需可导入）----


def test_nuitka_protocol_module_importable() -> None:
    """类型契约模块可导入且定义 NuitkaCompilerProtocol.

    Protocol 仅在类型检查期被 ``if TYPE_CHECKING`` 引用，运行时无人导入；
    本测试保证其 import 依赖（config/platform/progress）始终完整，
    避免 TYPE_CHECKING 引用掩盖模块级 import 断裂。
    """
    from fspack.packaging.nuitka.protocol import NuitkaCompilerProtocol

    assert NuitkaCompilerProtocol is not None
