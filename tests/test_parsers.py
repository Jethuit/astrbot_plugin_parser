from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_parser.core.config import PluginConfig
from astrbot_plugin_parser.core.data import DynamicContent, ImageContent, VideoContent
from astrbot_plugin_parser.core.parsers.tiktok import TikTokParser
from astrbot_plugin_parser.core.parsers.twitter import TwitterParser
from astrbot_plugin_parser.hermes_adapter import find_supported_url


class StubDownloader:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.video_calls = []

    @staticmethod
    def _task(path: Path):
        async def done():
            return path

        return asyncio.create_task(done())

    def download_img(self, url, **kwargs):
        return self._task(self.tmp_path / Path(url).name)

    def download_video(self, url, **kwargs):
        return self._task(self.tmp_path / Path(url).name)

    async def ytdlp_extract_info(self, url, **kwargs):
        return SimpleNamespace(
            title="tiktok fixture",
            channel="alice",
            duration=12,
            timestamp=1700000000,
            thumbnail="https://cdn.example/unused.jpg",
        )

    def ytdlp_download_video(self, url, **kwargs):
        self.video_calls.append((url, kwargs))
        return self._task(self.tmp_path / "tiktok.mp4")


@pytest.mark.parametrize(
    ("url", "platform"),
    [
        ("https://x.com/user/status/123", "twitter"),
        ("https://www.x.com/user/status/123?s=20", "twitter"),
        ("https://twitter.com/user/status/123", "twitter"),
        ("https://mobile.twitter.com/user/status/123", "twitter"),
        ("https://www.tiktok.com/@user/video/123", "tiktok"),
        ("https://vt.tiktok.com/ABC123/", "tiktok"),
        ("https://vm.tiktok.com/ABC123/", "tiktok"),
    ],
)
def test_supported_urls(url, platform):
    assert find_supported_url(f"before {url} after") == (platform, url)


def test_first_supported_url_wins():
    text = (
        "https://vm.tiktok.com/ABC123/ then "
        "https://x.com/user/status/123"
    )
    assert find_supported_url(text) == ("tiktok", "https://vm.tiktok.com/ABC123/")


@pytest.mark.asyncio
async def test_twitter_fixture_keeps_upstream_mapping(fake_ctx, tmp_path):
    config = PluginConfig(fake_ctx, cache_dir=tmp_path / "cache")
    downloader = StubDownloader(tmp_path)
    parser = TwitterParser(config, downloader)
    html = (Path(__file__).parent / "fixtures" / "twitter_xdown.html").read_text(
        encoding="utf-8"
    )

    media_result = parser.parse_twitter_html(html)
    assert media_result.title == "fixture tweet"
    assert [type(item) for item in media_result.contents] == [
        ImageContent,
        DynamicContent,
    ]

    video_html = (
        Path(__file__).parent / "fixtures" / "twitter_xdown_video.html"
    ).read_text(encoding="utf-8")
    video_result = parser.parse_twitter_html(video_html)
    assert [type(item) for item in video_result.contents] == [VideoContent]
    await asyncio.gather(
        *(item.path_task for item in media_result.contents + video_result.contents)
    )


@pytest.mark.asyncio
async def test_tiktok_does_not_download_cover(fake_ctx, tmp_path):
    config = PluginConfig(fake_ctx, cache_dir=tmp_path / "cache")
    downloader = StubDownloader(tmp_path)
    parser = TikTokParser(config, downloader)
    keyword, match = parser.search_url("https://www.tiktok.com/@user/video/123")

    result = await parser.parse(keyword, match)
    video = result.contents[0]
    assert isinstance(video, VideoContent)
    assert video.cover is None
    assert video.duration == 12
    assert "best[ext=mp4]" in downloader.video_calls[0][1]["format"]
    await video.path_task
