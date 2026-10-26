"""对象存储（S3 兼容）上传与公开读自检（2026-10-20）。

只做三件事：按 SigV4 签名 PUT 一个对象、拼公开 URL、HEAD 一次确认"公开可读"。
**不引入 boto3**：分发包已经 213 MB，这里只需要一个签名 PUT，没必要为它多带一套 SDK。

口径（与「设置 → 图片与附件」一致）：

- 只支持 S3 兼容协议（AWS S3 / 阿里 OSS / 腾讯 COS / MinIO / R2 都提供）；
- 对象**公开读**：模型与页面都直接拿 ``public_base_url + key``，不签名、不过期；
- 密钥只来自凭据存储，**不进 DTO、不进日志、不进异常消息**。
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import httpx

from local_agent.exceptions import LocalAgentError


class OssStorageError(LocalAgentError):
    """对象存储请求失败（配置、网络或权限）。消息面向用户，不含密钥。"""


@dataclass(frozen=True)
class OssConfig:
    """一条对象存储配置（不含密钥）。"""

    endpoint: str
    bucket: str
    region: str = "auto"
    public_base_url: str = ""
    key_prefix: str = "chaiai/"
    #: ``True`` = path-style（``endpoint/bucket/key``，MinIO/自建常用）；
    #: ``False`` = virtual-host（``bucket.endpoint/key``，云厂商默认）。
    path_style: bool = True

    def prefix(self) -> str:
        """规范化后的键前缀：空、无尾斜杠都归一成 ``x/`` 形式。"""
        value = (self.key_prefix or "").strip().lstrip("/")
        if not value:
            return ""
        return value if value.endswith("/") else f"{value}/"

    def object_url(self, key: str) -> str:
        """**写入**用的地址（含 bucket）。"""
        clean = key.lstrip("/")
        if self.path_style:
            return f"{self.endpoint.rstrip('/')}/{self.bucket}/{clean}"
        parts = urlsplit(self.endpoint)
        return f"{parts.scheme}://{self.bucket}.{parts.netloc}/{clean}"

    def public_url(self, key: str) -> str:
        """**读取**用的公开地址：``public_base_url`` 是桶的公开根（不含 key）。"""
        clean = key.lstrip("/")
        base = (self.public_base_url or self.endpoint).rstrip("/")
        return f"{base}/{clean}"


class S3ObjectStore:
    """最小 S3 客户端：签名 PUT + HEAD 探活。"""

    def __init__(
        self,
        config: OssConfig,
        access_key: str,
        secret_key: str,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._config = config
        self._access_key = access_key
        self._secret_key = secret_key
        self._client = client or httpx.Client(timeout=30.0, follow_redirects=True)

    @property
    def config(self) -> OssConfig:
        return self._config

    def put(self, key: str, data: bytes, *, content_type: str) -> str:
        """上传一个对象，返回它的**公开 URL**。"""
        url = self._config.object_url(key)
        headers = self._signed_headers("PUT", url, data)
        headers["Content-Type"] = content_type
        try:
            response = self._client.put(url, content=data, headers=headers)
        except httpx.HTTPError as exc:
            raise OssStorageError(f"连不上对象存储（{type(exc).__name__}）：检查端点与网络") from exc
        if response.status_code >= 300:
            raise OssStorageError(
                f"对象存储拒绝写入（HTTP {response.status_code}）：{_snippet(response)}"
            )
        return self._config.public_url(key)

    def probe(self, url: str) -> int:
        """HEAD 一个公开地址，返回状态码（用于"公开读"自检）。"""
        try:
            return self._client.head(url).status_code
        except httpx.HTTPError as exc:
            raise OssStorageError(f"连不上公开地址（{type(exc).__name__}）：检查 public_base_url") from exc

    # ---- 签名 -------------------------------------------------------------

    def _signed_headers(
        self, method: str, url: str, data: bytes, *, moment: datetime | None = None
    ) -> dict[str, str]:
        """签名头。``moment`` 只为测试可注入——生产路径一律用当前 UTC 时间。"""
        parts = urlsplit(url)
        host = parts.netloc
        canonical_uri = quote(parts.path or "/", safe="/~")
        payload_hash = hashlib.sha256(data).hexdigest()
        now = moment if moment is not None else datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date = now.strftime("%Y%m%d")
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_headers = (
            f"host:{host}\n"
            f"x-amz-content-sha256:{payload_hash}\n"
            f"x-amz-date:{amz_date}\n"
        )
        canonical_request = _canonical_request(
            method, canonical_uri, canonical_headers, signed_headers, payload_hash
        )
        scope = f"{date}/{self._config.region or 'auto'}/s3/aws4_request"
        string_to_sign = _string_to_sign(amz_date, scope, canonical_request)
        signature = hmac.new(self._signing_key(date), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        return {
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
            "Authorization": (
                f"AWS4-HMAC-SHA256 Credential={self._access_key}/{scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
        }

    def _signing_key(self, date: str) -> bytes:
        key = _hmac(f"AWS4{self._secret_key}".encode(), date)
        key = _hmac(key, self._config.region or "auto")
        key = _hmac(key, "s3")
        return _hmac(key, "aws4_request")


def _canonical_request(
    method: str, canonical_uri: str, canonical_headers: str,
    signed_headers: str, payload_hash: str,
) -> str:
    """SigV4 规范请求：``METHOD\\nURI\\nQUERY\\nHEADERS\\nSIGNED_HEADERS\\nPAYLOAD_HASH``。

    单独成函数只为一件事：**能被逐字节测**。这段拼错的表现是对方回 403，而 403 不会说
    "你第 4 段少了一个换行"——只能靠测试钉死。

    ``QUERY`` 段恒为空（本模块只发 PUT / HEAD，不带查询参数）；``canonical_headers``
    自身以 ``\\n`` 结尾，所以它后面那个分隔换行会让这里出现**连续两个** ``\\n``。
    那是对的，别"顺手"删掉一个。
    """
    return f"{method}\n{canonical_uri}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"


def _string_to_sign(amz_date: str, scope: str, canonical_request: str) -> str:
    """待签名串：算法、时间、作用域，以及**规范请求的 sha256**（不是规范请求本身）。"""
    digest = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    return f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{digest}"


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _snippet(response: httpx.Response) -> str:
    """取响应正文的一小段用于诊断；不整段回显（可能很长，也不该进日志）。"""
    text = response.text.strip().replace("\n", " ")
    return text[:200] or "（空响应）"
