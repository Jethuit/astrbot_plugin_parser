from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_parser.core.data import ImageContent, ParseResult, Platform, VideoContent
from astrbot_plugin_parser.core.exception import DownloadException
from astrbot_plugin_parser.hermes_adapter import HermesSocialParser


def test_registration_import_closure_has_no_astrbot_or_render(fake_ctx):
    package = importlib.import_module("astrbot_plugin_parser")
    package.register(fake_ctx)
    assert [item["name"] for item in fake_ctx.tools] == ["parse_social_media"]
    assert {name for name, _ in fake_ctx.hooks} == {
        "pre_gateway_dispatch",
        "transform_llm_output",
        "on_session_end",
        "on_session_finalize",
    }
    loaded = set(sys.modules)
    assert not any(name == "astrbot" or name.startswith("astrbot.") for name in loaded)
    assert not any(name.endswith("core.render") for name in loaded)
    assert not any(name == "PIL" or name.startswith("PIL.") for name in loaded)
    forbidden = ("bilibili", "douyin", "instagram", "youtube", "xhs", "weibo")
    assert not any(
        name.startswith("astrbot_plugin_parser.core.parsers.")
        and any(part in name for part in forbidden)
        for name in loaded
    )


def test_gateway_rewrite_scope(fake_ctx):
    runtime = HermesSocialParser(fake_ctx)
    source = lambda platform: SimpleNamespace(platform=platform)
    gateway = SimpleNamespace(adapters={})
    store = SimpleNamespace()

    normal = SimpleNamespace(text="hello", source=source("telegram"))
    assert runtime.pre_gateway_dispatch(normal, gateway, store) is None
    other = SimpleNamespace(
        text="https://x.com/u/status/1", source=source("discord")
    )
    assert runtime.pre_gateway_dispatch(other, gateway, store) is None

    original = "帮我发这个 https://x.com/u/status/1 谢谢"
    event = SimpleNamespace(text=original, source=source("telegram"))
    rewritten = runtime.pre_gateway_dispatch(event, gateway, store)
    assert rewritten["action"] == "rewrite"
    assert rewritten["text"].startswith(original)
    assert "parse_social_media" in rewritten["text"]


@pytest.mark.asyncio
async def test_fallback_order_stops_after_success(fake_ctx, tmp_path, monkeypatch):
    runtime = HermesSocialParser(fake_ctx)
    calls = []
    media_path = tmp_path / "ok.jpg"
    media_path.write_bytes(b"ok")

    class Parser:
        headers = {}
        proxy = None

        @staticmethod
        def search_url(url):
            return "upstream", object()

        @staticmethod
        async def parse(*_args):
            calls.append("upstream")
            raise ParseExceptionForTest()

    class Downloader:
        @staticmethod
        async def gallerydl_extract_urls(_url):
            calls.append("gallery")
            return ["https://cdn.example/a.jpg"]

        @staticmethod
        def download_img(_url):
            async def done():
                return media_path

            return asyncio.create_task(done())

        @staticmethod
        def download_video(_url):
            raise AssertionError("not video")

        @staticmethod
        def ytdlp_download_video_relaxed(*_args, **_kwargs):
            calls.append("ytdlp")
            raise AssertionError("must stop after gallery success")

    result, media = await runtime._run_fallback_chain(
        "twitter", "https://x.com/u/status/1", Parser(), Downloader(), tmp_path
    )
    assert result.platform.name == "twitter"
    assert media == [("image", media_path.resolve())]
    assert calls == ["upstream", "gallery"]


class ParseExceptionForTest(Exception):
    pass


@pytest.mark.asyncio
async def test_fallback_reaches_ytdlp_last(fake_ctx, tmp_path):
    runtime = HermesSocialParser(fake_ctx)
    calls = []
    video_path = tmp_path / "final.mp4"
    video_path.write_bytes(b"video")

    class Parser:
        headers = {}
        proxy = None
        cookiejar = SimpleNamespace(cookie_file=None)

        @staticmethod
        def search_url(url):
            return "upstream", object()

        @staticmethod
        async def parse(*_args):
            calls.append("upstream")
            raise DownloadException()

    class Downloader:
        @staticmethod
        async def gallerydl_extract_urls(_url):
            calls.append("gallery")
            raise DownloadException()

        @staticmethod
        def ytdlp_download_video_relaxed(*_args, **_kwargs):
            calls.append("ytdlp")

            async def done():
                return video_path

            return asyncio.create_task(done())

    _, media = await runtime._run_fallback_chain(
        "tiktok",
        "https://www.tiktok.com/@u/video/1",
        Parser(),
        Downloader(),
        tmp_path,
    )
    assert media == [("video", video_path.resolve())]
    assert calls == ["upstream", "gallery", "ytdlp"]


def test_media_transform_and_post_delivery_cleanup(fake_ctx):
    runtime = HermesSocialParser(fake_ctx)
    run_dir = runtime.cache_root / "run"
    run_dir.mkdir()
    first = (run_dir / "a.jpg").resolve()
    second = (run_dir / "b.mp4").resolve()
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    runtime._pending["session"] = {first, second}

    transformed = runtime.transform_llm_output(
        f"done\nMEDIA:{first}", "session", platform="telegram"
    )
    assert transformed.count("MEDIA:") == 2
    assert f"MEDIA:{second}" in transformed
    assert "session" not in runtime._pending
    assert runtime._awaiting_delivery["session"] == {first, second}

    callbacks = []

    class Adapter:
        @staticmethod
        def register_post_delivery_callback(
            session_key, callback, *, generation=None
        ):
            assert session_key == "telegram:dm:1"
            assert generation == 7
            callbacks.append(callback)

    source = SimpleNamespace(platform="telegram")
    entry = SimpleNamespace(
        session_key="telegram:dm:1", origin=source, platform="telegram"
    )
    adapter = Adapter()
    active = SimpleNamespace(_hermes_run_generation=7)
    adapter._active_sessions = {"telegram:dm:1": active}
    runtime._gateway = SimpleNamespace(adapters={"telegram": adapter})
    runtime._session_store = SimpleNamespace(
        lookup_by_session_id=lambda session_id: entry if session_id == "session" else None
    )

    runtime.on_session_end("session")
    assert first.exists() and second.exists()
    assert "session" not in runtime._awaiting_delivery
    assert len(callbacks) == 1
    callbacks[0]()
    assert not first.exists() and not second.exists()


def test_repeated_turns_do_not_append_previous_media(fake_ctx):
    runtime = HermesSocialParser(fake_ctx)
    first_dir = runtime.cache_root / "first"
    second_dir = runtime.cache_root / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = (first_dir / "first.jpg").resolve()
    second = (second_dir / "second.jpg").resolve()
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    runtime._pending["session"] = {first}
    first_response = runtime.transform_llm_output(
        "done", "session", platform="qqbot"
    )
    assert f"MEDIA:{first}" in first_response

    entry = SimpleNamespace(
        session_key="qqbot:dm:1",
        origin=SimpleNamespace(platform="qqbot"),
        platform="qqbot",
    )
    runtime._gateway = SimpleNamespace(adapters={"qqbot": object()})
    runtime._session_store = SimpleNamespace(lookup_by_session_id=lambda _sid: entry)
    runtime.on_session_end("session")
    assert first.exists()

    runtime._pending["session"] = {second}
    second_response = runtime.transform_llm_output(
        "done", "session", platform="qqbot"
    )
    assert f"MEDIA:{second}" in second_response
    assert f"MEDIA:{first}" not in second_response


def test_untransformed_media_is_cleaned_at_turn_end(fake_ctx):
    runtime = HermesSocialParser(fake_ctx)
    run_dir = runtime.cache_root / "interrupted"
    run_dir.mkdir()
    path = (run_dir / "orphan.jpg").resolve()
    path.write_bytes(b"orphan")
    runtime._pending["session"] = {path}

    runtime.on_session_end("session", completed=False, interrupted=True)

    assert "session" not in runtime._pending
    assert not path.exists()


def test_missing_delivery_callback_keeps_file(fake_ctx):
    runtime = HermesSocialParser(fake_ctx)
    run_dir = runtime.cache_root / "run"
    run_dir.mkdir()
    path = (run_dir / "a.jpg").resolve()
    path.write_bytes(b"a")
    runtime._awaiting_delivery["session"] = {path}
    entry = SimpleNamespace(
        session_key="qqbot:dm:1",
        origin=SimpleNamespace(platform="qqbot"),
        platform="qqbot",
    )
    runtime._gateway = SimpleNamespace(adapters={"qqbot": object()})
    runtime._session_store = SimpleNamespace(lookup_by_session_id=lambda _sid: entry)
    runtime.on_session_end("session")
    assert path.exists()


def test_low_resource_runtime_sources():
    root = Path(__file__).parents[1]
    runtime_files = [
        root / "__init__.py",
        root / "hermes_adapter.py",
        root / "core" / "download.py",
        root / "core" / "sender.py",
        root / "core" / "parsers" / "tiktok.py",
        root / "core" / "parsers" / "twitter.py",
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in runtime_files)
    forbidden = ("BytesIO", "base64", "FFmpeg", "postprocessors", "core.render", "PIL")
    assert not any(token in source for token in forbidden)
