"""运行时归档 sha256 清单与校验开关.

与 Win7 兼容 DLL 的 ``WIN7_EMBED_SHA256`` 同样的机制：下载/复用缓存归档时
校验 sha256，检测镜像投毒、下载截断与 URL 指向错误版本。

清单键为归档**文件名**（含版本、平台与 release tag）：

- Windows embed zip：取自 python.org 官方归档（版本与
  :data:`fspack.config.versions.KNOWN_EMBED_VERSIONS` 对齐）
- python-build-standalone ``install_only`` tarball：取自 astral-sh
  ``20260718`` release 的 ``SHA256SUMS``（Linux/macOS/Windows，含
  ``-freethreaded`` 变体）

升级 :data:`fspack.config.versions.KNOWN_EMBED_VERSIONS` /
``KNOWN_STANDALONE_VERSIONS`` 或修改 release tag 时必须同步更新本清单，
否则该归档退化为「未收录 → warning 跳过校验」（不阻断构建）。设置
``FSPACK_SKIP_RUNTIME_HASH=1`` 可显式跳过校验（离线/自建镜像/内部运行时）。
"""

from __future__ import annotations

import logging
import os

__all__ = [
    "EMBED_SHA256",
    "SKIP_RUNTIME_HASH_ENV",
    "STANDALONE_SHA256",
    "expected_runtime_hash",
    "runtime_hash_enabled",
    "runtime_sha256",
]

_logger = logging.getLogger(__name__)

SKIP_RUNTIME_HASH_ENV = "FSPACK_SKIP_RUNTIME_HASH"

# Windows embed zip：python.org /ftp/python/<version>/python-<version>-embed-amd64.zip
EMBED_SHA256: dict[str, str] = {
    "python-3.8.10-embed-amd64.zip": "abbe314e9b41603dde0a823b76f5bbbe17b3de3e5ac4ef06b759da5466711271",
    "python-3.9.13-embed-amd64.zip": "938a1f3b80d580320836260612084d74ce094a261e36f9ff3ac7b9463df5f5e4",
    "python-3.10.11-embed-amd64.zip": "608619f8619075629c9c69f361352a0da6ed7e62f83a0e19c63e0ea32eb7629d",
    "python-3.11.9-embed-amd64.zip": "009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b",
    "python-3.12.10-embed-amd64.zip": "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3",
    "python-3.13.14-embed-amd64.zip": "90b4e5b9898b72d744650524bff92377c367f44bd5fbd09e3148656c080ad907",
    "python-3.14.6-embed-amd64.zip": "df901e84a896ff1ee720ad03377e0c8d8c2244fda79808aeeaff6316df1cb75c",
}

# python-build-standalone 20260718 install_only tarball（astral-sh SHA256SUMS）
STANDALONE_SHA256: dict[str, str] = {
    "cpython-3.10.20+20260718-aarch64-apple-darwin-install_only.tar.gz": "5ce056c4294bc7155cdd98ea35ee764bffebb08854c4910eb433cc5f4e45e9a5",
    "cpython-3.10.20+20260718-x86_64-apple-darwin-install_only.tar.gz": "2dd748d347829d90cec271cdfe25cc6d87e105c27936fd20d1f50188cd0be096",
    "cpython-3.10.20+20260718-x86_64-pc-windows-msvc-install_only.tar.gz": "97a6d248dcd27d8abdf0fbf225d183c4a40d24a25b9ac2e146c82c64053bedaa",
    "cpython-3.10.20+20260718-x86_64-unknown-linux-gnu-install_only.tar.gz": "9c28d8017eeaf692f24dbaf26fd4679ce496c7f58e48b897d278739661794e37",
    "cpython-3.11.15+20260718-aarch64-apple-darwin-install_only.tar.gz": "125587d03495bebdf30ec9e549a8469c97c0925d863ff401f24f157fd44d91d6",
    "cpython-3.11.15+20260718-x86_64-apple-darwin-install_only.tar.gz": "870df60775609536b7c3b82ad5c10c5ffbf822e6819bb86d2218e43c172a5f32",
    "cpython-3.11.15+20260718-x86_64-pc-windows-msvc-install_only.tar.gz": "c3d782be3733f779d585633da374ff1bd92400d4d74c0c3922aee1526446096b",
    "cpython-3.11.15+20260718-x86_64-unknown-linux-gnu-install_only.tar.gz": "c2082e977138e307e3da4ea2c65421d3cb6b80f4890890b28b96fc6b422d4f0d",
    "cpython-3.12.13+20260718-aarch64-apple-darwin-install_only.tar.gz": "62aeee6161d57303a71a138b75fd5cc6fb8c89c4b1d9c7f0a052d89fa0b6652b",
    "cpython-3.12.13+20260718-x86_64-apple-darwin-install_only.tar.gz": "10b47148de86f9d87ba6e96a3db606ced90a206a3454d7d6d8fa68536a05d81f",
    "cpython-3.12.13+20260718-x86_64-pc-windows-msvc-install_only.tar.gz": "56c9dd9681c4810cb8bfdec277ee2606d8ab17e678e5bc2bd138eb8098e330b6",
    "cpython-3.12.13+20260718-x86_64-unknown-linux-gnu-install_only.tar.gz": "7eea0959fa425c8aff3ea0a1352ee7d01d794b51439ed8f5fcfa017dbc0ec661",
    "cpython-3.13.14+20260718-aarch64-apple-darwin-freethreaded-install_only.tar.gz": "ae7be1a21d8ee1ae9286e205ec0ae9eb8d7cf3689686fd655fa8fd6d52dd999c",
    "cpython-3.13.14+20260718-aarch64-apple-darwin-install_only.tar.gz": "dca7c3bac21f023cf294705b27f4f3e9c70399c40790ebb81e8d0eff15b00770",
    "cpython-3.13.14+20260718-x86_64-apple-darwin-freethreaded-install_only.tar.gz": "7ac1e3e55af8cf3a4e5122710ad30868ec2d147a18c6183f6bc272ee23f5ce0a",
    "cpython-3.13.14+20260718-x86_64-apple-darwin-install_only.tar.gz": "2c7daabe0e94de636064d42f1afa46ea26a303bb23e14f4a64961d36eaecdb73",
    "cpython-3.13.14+20260718-x86_64-pc-windows-msvc-freethreaded-install_only.tar.gz": "bf2ddd6a485e7282092992082f9c87bc14817e5d7cde049b4c29c42b70cf9599",
    "cpython-3.13.14+20260718-x86_64-pc-windows-msvc-install_only.tar.gz": "aeacaec792528b8bda4ee7be9dc5721b0a830ba798de4c7d1cf727bc9c246ded",
    "cpython-3.13.14+20260718-x86_64-unknown-linux-gnu-freethreaded-install_only.tar.gz": "49597b6eb3f72a94769f4676565d90fb35da64d810723412eeea2157f453b2e3",
    "cpython-3.13.14+20260718-x86_64-unknown-linux-gnu-install_only.tar.gz": "5df2686713fcc114290a3b37db3f7253eb837721820ddba4575fdc131e70ec59",
    "cpython-3.14.6+20260718-aarch64-apple-darwin-freethreaded-install_only.tar.gz": "690e7159fb106d8d3733fc8dce4fe7d2d519a3d530c93266564955413df96e6e",
    "cpython-3.14.6+20260718-aarch64-apple-darwin-install_only.tar.gz": "5a234e405386bf486bab196018c01bc4577a4f0cc9fd5bc50f7a979fe4f5c59d",
    "cpython-3.14.6+20260718-x86_64-apple-darwin-freethreaded-install_only.tar.gz": "887c1db59a279f7dec0b76e5ef4f09066f8d41dc508674648fe6a932c8e89b1e",
    "cpython-3.14.6+20260718-x86_64-apple-darwin-install_only.tar.gz": "b3bc968180342cfffde185f2c6d1de22d4e6c38b5f0f02487e32ccac2b8766ee",
    "cpython-3.14.6+20260718-x86_64-pc-windows-msvc-freethreaded-install_only.tar.gz": "0f171d67352005eb0808779cd40068527c03b30e93cbbb90f748af2e390a7558",
    "cpython-3.14.6+20260718-x86_64-pc-windows-msvc-install_only.tar.gz": "97c01acd70108234ff042699996f3f4163c791f1d6e35de898475936b86ec3b2",
    "cpython-3.14.6+20260718-x86_64-unknown-linux-gnu-freethreaded-install_only.tar.gz": "3b4e09857ebeb16617ef8064f433a2255bc3cd357891f92b888a3752126582bb",
    "cpython-3.14.6+20260718-x86_64-unknown-linux-gnu-install_only.tar.gz": "b442fb93903c6f3392b2430961d4725354ae0da7558c785a908d714264309b28",
}

# 归档文件名 → sha256（embed 与 standalone 合并查询）
RUNTIME_SHA256: dict[str, str] = {**EMBED_SHA256, **STANDALONE_SHA256}


def runtime_hash_enabled() -> bool:
    """是否启用运行时归档 sha256 校验（``FSPACK_SKIP_RUNTIME_HASH=1`` 时关闭）."""
    return os.environ.get(SKIP_RUNTIME_HASH_ENV, "").strip() not in ("1", "true", "yes")


def runtime_sha256(archive_name: str) -> str | None:
    """返回归档文件名的期望 sha256；未收录时返回 ``None``."""
    return RUNTIME_SHA256.get(archive_name)


def expected_runtime_hash(archive_name: str, label: str) -> str | None:
    """下载前查询期望 sha256：未收录或显式跳过时告警并返回 ``None``.

    ``None`` 表示本次不做校验（向后兼容未收录的自定义运行时版本），
    调用方把结果原样传给 ``expected_hash`` 参数即可。
    """
    if not runtime_hash_enabled():
        _logger.info("FSPACK_SKIP_RUNTIME_HASH=1，跳过 %s sha256 校验", label)
        return None
    digest = runtime_sha256(archive_name)
    if digest is None:
        _logger.warning(
            "%s 未收录 sha256 清单（%s），跳过校验；升级运行时版本或 release tag 时"
            "请同步更新 fspack/packaging/runtime/hashes.py",
            label,
            archive_name,
        )
    return digest
