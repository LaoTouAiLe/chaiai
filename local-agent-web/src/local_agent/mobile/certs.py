"""局域网通道的自签证书（架构 §24）。

**为什么自己签一个 CA，而不是只签一张服务器证书**：只签服务器证书的话，每次局域网地址一变
（DHCP 换个 IP）证书就与地址不匹配，手机要么报错要么得重新点一遍"继续访问"。签一个私有 CA
并把 **CA** 装进手机，之后换 IP 只需要重签服务器证书，手机上不会再弹。

**什么时候重签**：SAN 覆盖不了当前全部局域网地址、或证书已过期时重签。少了这一步，换网络
之后用户看到的是"证书与此地址不匹配"——那看起来像服务器坏了，而不像"该刷新证书了"。

**这不是 PKI 安全**：私钥就在本机数据目录里，能做的是"这条 TLS 只给这台机器自己用"，不是
"证明这台机器是谁"。远程通道（Tailscale / cloudflared）用的是它们的公开证书，不经过这里。
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

#: CA 有效期（天）。十年：这是本地自建 CA，过期只会让所有手机同时失效。
CA_VALID_DAYS: Final[int] = 3650
#: 服务器证书有效期（天）。一年：短一点，重签成本很低（只影响本机文件）。
SERVER_VALID_DAYS: Final[int] = 365
#: 提前一天生效：机器与手机时钟有偏差时不至于"证书还没生效"。
CLOCK_SKEW: Final[dt.timedelta] = dt.timedelta(days=1)

CA_CERT_NAME: Final[str] = "ca.pem"
CA_KEY_NAME: Final[str] = "ca.key"
SERVER_CERT_NAME: Final[str] = "server.pem"
SERVER_KEY_NAME: Final[str] = "server.key"


@dataclass(frozen=True, slots=True)
class CertificatePaths:
    """局域网 TLS 需要的三个文件。CA 单独给出来，方便用户装进手机。"""

    ca_cert: Path
    server_cert: Path
    server_key: Path
    regenerated: bool


def _write_private(path: Path, data: bytes) -> None:
    """写私钥并尽量收紧权限。

    Windows 上 ``chmod`` 对 ACL 的效果有限（不是等价物），但仍然写 0600：在 POSIX 上这是
    实际生效的，在 Windows 上至少不留下"人人可读"的默认。
    """
    path.write_bytes(data)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - 少数文件系统不支持
        pass


def _new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _key_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _ca_subject() -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "chaiai mobile access CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "chaiai"),
        ]
    )


def _ensure_ca(cert_dir: Path) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    """读取或创建 CA。CA 一旦存在就**永不自动重签**：重签会让所有已装 CA 的手机同时失效。"""
    cert_path = cert_dir / CA_CERT_NAME
    key_path = cert_dir / CA_KEY_NAME
    if cert_path.is_file() and key_path.is_file():
        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        except (ValueError, OSError) as exc:
            raise RuntimeError(f"CA 文件已存在但无法解析（{cert_path}）：{exc}") from exc
        if not isinstance(key, rsa.RSAPrivateKey):  # pragma: no cover - 只由本模块生成
            raise RuntimeError(f"CA 私钥不是 RSA：{key_path}")
        return cert, key

    key = _new_key()
    now = dt.datetime.now(dt.timezone.utc) - CLOCK_SKEW
    subject = _ca_subject()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=CA_VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    _write_private(key_path, _key_pem(key))
    return cert, key


def _san_entries(addresses: list[str], hostname: str) -> list[x509.GeneralName]:
    """SAN：本机名 + localhost + 全部局域网 IP。

    必须把**每个**局域网地址都写进去：用户从 192.168 换到 10.0 网段时，缺了那一条的表现是
    "证书不匹配"，而手机不会告诉用户"证书里少了个 IP"。
    """
    entries: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    if hostname:
        entries.append(x509.DNSName(hostname))
    for item in addresses:
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(item)))
        except ValueError:  # pragma: no cover - 调用方已过滤
            continue
    return entries


def _existing_san(cert_path: Path) -> tuple[set[str], dt.datetime]:
    """已存在证书的 IP 集合与到期时间；读不出来按"需要重签"处理。"""
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (ValueError, OSError):
        return set(), dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        found = {str(ip) for ip in ext.value.get_values_for_type(x509.IPAddress)}
    except x509.ExtensionNotFound:
        found = set()
    return found, cert.not_valid_after_utc


def ensure_certificates(cert_dir: Path, addresses: list[str]) -> CertificatePaths:
    """保证证书存在且覆盖 ``addresses``；必要时重签服务器证书。

    Args:
        cert_dir: 证书目录（``data_root/mobile/certs``）。
        addresses: 当前局域网地址（不含回环，调用方已过滤）。

    Raises:
        RuntimeError: CA 文件损坏（不静默重建——重建会让已装 CA 的手机全部失效）。
    """
    cert_dir.mkdir(parents=True, exist_ok=True)
    ca_cert, ca_key = _ensure_ca(cert_dir)

    server_cert_path = cert_dir / SERVER_CERT_NAME
    server_key_path = cert_dir / SERVER_KEY_NAME
    wanted = set(addresses)
    regenerated = True
    if server_cert_path.is_file() and server_key_path.is_file():
        existing, expires_at = _existing_san(server_cert_path)
        covers = wanted.issubset(existing)
        fresh = expires_at - CLOCK_SKEW > dt.datetime.now(dt.timezone.utc)
        regenerated = not (covers and fresh)

    if regenerated:
        key = _new_key()
        now = dt.datetime.now(dt.timezone.utc) - CLOCK_SKEW
        hostname = os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or ""
        builder = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "chaiai mobile access")])
            )
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + dt.timedelta(days=SERVER_VALID_DAYS))
            .add_extension(
                x509.SubjectAlternativeName(_san_entries(addresses, hostname)), critical=False
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
            )
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        )
        cert = builder.sign(ca_key, hashes.SHA256())
        server_cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        _write_private(server_key_path, _key_pem(key))

    return CertificatePaths(
        ca_cert=cert_dir / CA_CERT_NAME,
        server_cert=server_cert_path,
        server_key=server_key_path,
        regenerated=regenerated,
    )


__all__ = ["CertificatePaths", "ensure_certificates"]
