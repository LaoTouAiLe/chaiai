"""对象存储（S3 兼容，2026-10-20）：签名 PUT、公开 URL、错误如实报。

不连真实对象存储：用一个 HTTP 传输桩接住请求，断言"发出去的到底是什么"。
这能钉住三件最容易写错的事——URL 形状（path-style / virtual-host）、签名头、payload 摘要。
"""

from __future__ import annotations

import hashlib

import httpx
import pytest

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
