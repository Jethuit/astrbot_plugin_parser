from __future__ import annotations

import asyncio
from collections import deque

import pytest
from aiohttp import ClientError

from astrbot_plugin_parser.core.config import PluginConfig
from astrbot_plugin_parser.core.download import Downloader
from astrbot_plugin_parser.core.exception import (
    DownloadException,
    SizeLimitException,
    ZeroSizeException,
)


class FakeContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self.chunks:
            yield chunk


class FakeResponse:
    status = 200
    reason = "OK"

    def __init__(self, chunks, content_length=None):
        self.content = FakeContent(chunks)
        self.content_length = content_length

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class FailingResponse:
    async def __aenter__(self):
        raise ClientError("boom")

    async def __aexit__(self, *_args):
        return None


class TimeoutResponse:
    async def __aenter__(self):
        raise asyncio.TimeoutError

    async def __aexit__(self, *_args):
        return None


class FakeClient:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        return self.responses.popleft()

    async def close(self):
        return None


async def make_downloader(fake_ctx, tmp_path, responses, **settings):
    fake_ctx.settings.update(settings)
    config = PluginConfig(fake_ctx, cache_dir=tmp_path / "cache")
    downloader = Downloader(config)
    await downloader.client.close()
    downloader.client = FakeClient(responses)
    return downloader


@pytest.mark.asyncio
async def test_stream_without_content_length(fake_ctx, tmp_path):
    downloader = await make_downloader(
        fake_ctx, tmp_path, [FakeResponse([b"abc", b"def"])]
    )
    path = await downloader.streamd("https://cdn.example/a", file_name="a.bin")
    assert path.read_bytes() == b"abcdef"
    assert not path.with_name("a.bin.part").exists()


@pytest.mark.asyncio
async def test_content_length_limit_prevents_write(fake_ctx, tmp_path):
    downloader = await make_downloader(
        fake_ctx,
        tmp_path,
        [FakeResponse([], content_length=2 * 1024 * 1024)],
        max_media_mb=1,
    )
    with pytest.raises(SizeLimitException):
        await downloader.streamd("https://cdn.example/a", file_name="a.bin")
    assert not (tmp_path / "cache" / "a.bin.part").exists()


@pytest.mark.asyncio
async def test_unknown_length_limit_cleans_part(fake_ctx, tmp_path):
    downloader = await make_downloader(
        fake_ctx,
        tmp_path,
        [FakeResponse([b"a" * (1024 * 1024), b"b"])],
        max_media_mb=1,
    )
    with pytest.raises(SizeLimitException):
        await downloader.streamd("https://cdn.example/a", file_name="a.bin")
    assert not (tmp_path / "cache" / "a.bin.part").exists()
    assert not (tmp_path / "cache" / "a.bin").exists()


@pytest.mark.asyncio
async def test_zero_size_is_rejected(fake_ctx, tmp_path):
    downloader = await make_downloader(
        fake_ctx, tmp_path, [FakeResponse([], content_length=0)]
    )
    with pytest.raises(ZeroSizeException):
        await downloader.streamd("https://cdn.example/a", file_name="a.bin")


@pytest.mark.asyncio
async def test_retry_count_and_cleanup(fake_ctx, tmp_path):
    downloader = await make_downloader(
        fake_ctx,
        tmp_path,
        [FailingResponse(), FailingResponse()],
        download_retries=1,
    )
    with pytest.raises(DownloadException):
        await downloader.streamd("https://cdn.example/a", file_name="a.bin")
    assert downloader.client.calls == 2
    assert not (tmp_path / "cache" / "a.bin.part").exists()


@pytest.mark.asyncio
async def test_timeout_retries_and_cleans_part(fake_ctx, tmp_path):
    downloader = await make_downloader(
        fake_ctx,
        tmp_path,
        [TimeoutResponse(), TimeoutResponse()],
        download_retries=1,
    )
    with pytest.raises(DownloadException):
        await downloader.streamd("https://cdn.example/a", file_name="a.bin")
    assert downloader.client.calls == 2
    assert not (tmp_path / "cache" / "a.bin.part").exists()
