"""对象存储（S3 兼容，2026-10-20）：签名 PUT、公开 URL、错误如实报。

不连真实对象存储：用一个 HTTP 传输桩接住请求，断言"发出去的到底是什么"。
这能钉住三件最容易写错的事——URL 形状（path-style / virtual-host）、签名头、payload 摘要。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import httpx
import pytest

from local_agent import oss_storage as mod
from local_agent.oss_storage import OssConfig, OssStorageError, S3ObjectStore


def _store(
    *, path_style: bool = True, status: int = 200
) -> tuple[list[httpx.Request], S3ObjectStore]:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    config = OssConfig(
        endpoint="https://s3.example.com",
        bucket="images",
        region="us-east-1",
        public_base_url="https://cdn.example.com",
        key_prefix="chaiai/",
        path_style=path_style,
    )
    return captured, S3ObjectStore(config, "AKID", "SECRET", client=client)


def test_put_signs_the_request_and_returns_the_public_url() -> None:
    captured, store = _store()

    url = store.put("chaiai/a.jpg", b"abc", content_type="image/jpeg")

    assert url == "https://cdn.example.com/chaiai/a.jpg"
    request = captured[0]
    assert request.method == "PUT"
    assert str(request.url) == "https://s3.example.com/images/chaiai/a.jpg"
    assert request.headers["content-type"] == "image/jpeg"
    # payload 摘要必须是**请求体的** sha256（S3 校验这一条）
    assert request.headers["x-amz-content-sha256"] == hashlib.sha256(b"abc").hexdigest()
    authorization = request.headers["authorization"]
    assert authorization.startswith("AWS4-HMAC-SHA256 Credential=AKID/")
    assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in authorization
    assert "Signature=" in authorization


def test_virtual_host_style_puts_the_bucket_in_the_host() -> None:
    captured, store = _store(path_style=False)

    store.put("chaiai/a.jpg", b"abc", content_type="image/jpeg")

    assert str(captured[0].url) == "https://images.s3.example.com/chaiai/a.jpg"


def test_rejected_write_reports_the_status_without_leaking_the_key() -> None:
    _captured, store = _store(status=403)

    with pytest.raises(OssStorageError, match="HTTP 403") as caught:
        store.put("chaiai/a.jpg", b"abc", content_type="image/jpeg")

    assert "SECRET" not in str(caught.value)


def test_probe_returns_the_status_code() -> None:
    captured, store = _store(status=200)

    assert store.probe("https://cdn.example.com/chaiai/a.jpg") == 200
    assert captured[0].method == "HEAD"


# ---- 签名串本身（2026-10-26）------------------------------------------------
# 此前只有"Authorization 里有 Signature=" 这种弱断言：规范请求拼错时对方回 403，
# 而 403 不会告诉你第几段错了。下面三条把它钉到字节级。


def test_canonical_request_keeps_the_sigv4_segment_layout() -> None:
    request = mod._canonical_request(
        "PUT",
        "/images/chaiai/a.jpg",
        "host:example.com\nx-amz-content-sha256:abc\nx-amz-date:20261026T000000Z\n",
        "host;x-amz-content-sha256;x-amz-date",
        "abc",
    )

    assert request == (
        "PUT\n"
        "/images/chaiai/a.jpg\n"
        "\n"                                       # 空 QUERY 段
        "host:example.com\n"
        "x-amz-content-sha256:abc\n"
        "x-amz-date:20261026T000000Z\n"
        "\n"                                       # 头块自带尾部换行 + 分隔符 = 连续两个
        "host;x-amz-content-sha256;x-amz-date\n"
        "abc"
    )


def test_string_to_sign_hashes_the_canonical_request() -> None:
    canonical = "PUT\n/images/a.jpg\n\nhost:example.com\n\nhost\nabc"
    signed = mod._string_to_sign("20261026T000000Z", "20261026/us-east-1/s3/aws4_request", canonical)

    algorithm, amz_date, scope, digest = signed.split("\n")
    assert algorithm == "AWS4-HMAC-SHA256"
    assert amz_date == "20261026T000000Z"
    assert scope == "20261026/us-east-1/s3/aws4_request"
    # 最后一段是**规范请求的哈希**，不是规范请求本身——写错这一处会导致签名恒不匹配。
    assert digest == hashlib.sha256(canonical.encode()).hexdigest()


def test_signature_changes_when_any_signed_input_changes() -> None:
    """规范请求的每一段都参与 HMAC：改 method / uri / body / 时间，签名必须都变。

    防的是"某一段没进签名"——那种错误在真机上表现为"有时能传、有时 403"，
    而且换个对象存储厂商就可能复现不了。
    """
    _captured, store = _store()
    moment = datetime(2026, 10, 26, tzinfo=timezone.utc)

    def signature(
        method: str = "PUT", url: str = "https://s3.example.com/images/a.jpg", data: bytes = b"abc",
        at: datetime = moment,
    ) -> str:
        headers = store._signed_headers(method, url, data, moment=at)
        return headers["Authorization"].rsplit("Signature=", 1)[1]

    # 同样的输入得到同样的签名：链路上没有随机量或"当前时间"漏进来
    assert signature() == signature()
    assert signature(method="HEAD") != signature()
    assert signature(url="https://s3.example.com/images/b.jpg") != signature()
    assert signature(data=b"abd") != signature()
    assert signature(at=datetime(2026, 10, 26, 1, tzinfo=timezone.utc)) != signature()
