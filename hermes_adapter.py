"""Hermes 工具与 gateway hook 适配层。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .core.config import PluginConfig
from .core.data import ImageContent, ParseResult, Platform, VideoContent
from .core.download import Downloader
from .core.exception import DownloadLimitException, ParseException
from .core.parsers import TikTokParser, TwitterParser
from .core.sender import MessageSender

logger = logging.getLogger(__name__)

_TWITTER_RE = re.compile(
    r"https?://(?:(?:www|mobile)\.)?(?:x\.com|twitter\.com)/"
    r"(?:[A-Za-z0-9_]+/)*status/\d+[^\s<>]*",
    re.IGNORECASE,
)
_TIKTOK_RE = re.compile(
    r"https?://(?:www|vt|vm)\.tiktok\.com/[^\s<>]+",
    re.IGNORECASE,
)
_TRAILING_PUNCTUATION = ".,;:!?)]}\"'，。；：！？）】》"
_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".m3u8"}


def find_supported_url(text: str) -> tuple[str, str] | None:
    """返回文本中最先出现的受支持单帖链接。"""
    candidates: list[tuple[int, str, str]] = []
    for platform, pattern in (("twitter", _TWITTER_RE), ("tiktok", _TIKTOK_RE)):
        match = pattern.search(text or "")
        if match:
            candidates.append(
                (match.start(), platform, match.group(0).rstrip(_TRAILING_PUNCTUATION))
            )
    if not candidates:
        return None
    _, platform, url = min(candidates, key=lambda item: item[0])
    return platform, url


class HermesSocialParser:
    CACHE_TTL_SECONDS = 3600

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        base_config = PluginConfig(ctx)
        self.cache_root = base_config.cache_root.resolve()
        self.enabled_platforms = base_config.enabled_platforms
        self.sender = MessageSender()
        self._parse_semaphore = asyncio.Semaphore(1)
        self._pending: dict[str, set[Path]] = {}
        self._lock = threading.RLock()
        self._gateway = None
        self._session_store = None
        self.cleanup_expired()

    @staticmethod
    def _platform_value(value) -> str:
        return str(getattr(value, "value", value) or "").lower()

    def pre_gateway_dispatch(self, event, gateway=None, session_store=None, **kwargs):
        del kwargs
        source = getattr(event, "source", None)
        platform = self._platform_value(getattr(source, "platform", ""))
        if platform not in self.enabled_platforms:
            return None
        found = find_supported_url(getattr(event, "text", ""))
        if found is None:
            return None

        self._gateway = gateway
        self._session_store = session_store
        self.cleanup_expired()
        _, url = found
        original = getattr(event, "text", "")
        instruction = (
            "\n\n[Hermes 社交媒体解析指令]\n"
            f"请调用 parse_social_media，参数 url 必须是：{url}\n"
            "最终回复要简短，并原样保留工具返回的全部 MEDIA:<绝对路径> 行。"
        )
        return {"action": "rewrite", "text": original + instruction}

    async def parse_tool(self, args, **kwargs) -> str:
        url = str((args or {}).get("url", "")).strip()
        found = find_supported_url(url)
        if found is None:
            return self._error("仅支持 X/Twitter 或 TikTok 的单帖链接")
        platform, matched_url = found
        if matched_url != url.rstrip(_TRAILING_PUNCTUATION):
            return self._error("参数必须只包含一个受支持的帖子链接")

        session_id = str(kwargs.get("session_id") or "")
        run_dir = self.cache_root / uuid.uuid4().hex
        config = PluginConfig(self.ctx, cache_dir=run_dir)

        async with self._parse_semaphore:
            downloader = Downloader(config)
            parser = (
                TwitterParser(config, downloader)
                if platform == "twitter"
                else TikTokParser(config, downloader)
            )
            try:
                result, media = await self._run_fallback_chain(
                    platform, matched_url, parser, downloader, run_dir
                )
                paths = [path for _, path in media]
                if session_id:
                    with self._lock:
                        self._pending.setdefault(session_id, set()).update(paths)
                return self.sender.build_response(result, media)
            except DownloadLimitException as exc:
                self._remove_run_dir(run_dir)
                return self._error(str(exc))
            except Exception as exc:
                logger.warning("社交媒体解析失败: %s", exc)
                self._remove_run_dir(run_dir)
                return self._error("媒体解析或下载失败")
            finally:
                await parser.close_session()
                await downloader.close()

    async def _run_fallback_chain(
        self,
        platform: str,
        url: str,
        parser,
        downloader: Downloader,
        run_dir: Path,
    ) -> tuple[ParseResult, list[tuple[str, Path]]]:
        try:
            keyword, searched = parser.search_url(url)
            upstream = await parser.parse(keyword, searched)
            return upstream, await self.sender.materialize(upstream)
        except DownloadLimitException:
            raise
        except Exception as exc:
            logger.info("上游 %s parser 失败，进入 gallery-dl: %s", platform, exc)
            self._clear_run_files(run_dir)

        try:
            extracted = await downloader.gallerydl_extract_urls(url)
            gallery_result = self._gallery_result(platform, url, extracted, downloader)
            return gallery_result, await self.sender.materialize(gallery_result)
        except DownloadLimitException:
            raise
        except Exception as exc:
            logger.info("gallery-dl 失败，进入 yt-dlp 最终回退: %s", exc)
            self._clear_run_files(run_dir)

        video = downloader.ytdlp_download_video_relaxed(
            url,
            cookiefile=self._cookie_file(platform, parser),
            headers=parser.headers,
            proxy=parser.proxy,
        )
        final_result = ParseResult(
            platform=self._result_platform(platform),
            url=url,
            contents=[VideoContent(video)],
        )
        return final_result, await self.sender.materialize(final_result)

    def _gallery_result(
        self,
        platform: str,
        source_url: str,
        urls: list[str],
        downloader: Downloader,
    ) -> ParseResult:
        contents = []
        for media_url in urls:
            if media_url.startswith("ytdl:"):
                contents.append(
                    VideoContent(
                        downloader.ytdlp_download_video_relaxed(media_url[5:])
                    )
                )
                continue
            suffix = Path(urlsplit(media_url).path).suffix.lower()
            is_video = suffix in _VIDEO_SUFFIXES or (
                platform == "tiktok" and suffix not in {".jpg", ".jpeg", ".png", ".webp"}
            )
            if is_video:
                contents.append(VideoContent(downloader.download_video(media_url)))
            else:
                contents.append(ImageContent(downloader.download_img(media_url)))
        return ParseResult(
            platform=self._result_platform(platform),
            url=source_url,
            contents=contents,
        )

    @staticmethod
    def _cookie_file(platform: str, parser) -> Path | None:
        if platform == "tiktok":
            return getattr(getattr(parser, "cookiejar", None), "cookie_file", None)
        return None

    @staticmethod
    def _result_platform(platform: str) -> Platform:
        return Platform(
            name=platform,
            display_name="TikTok" if platform == "tiktok" else "推特",
        )

    @staticmethod
    def _error(message: str) -> str:
        return json.dumps({"ok": False, "error": message}, ensure_ascii=False)

    def transform_llm_output(
        self,
        response_text: str,
        session_id: str,
        platform: str = "",
        **kwargs,
    ) -> str | None:
        del kwargs
        if self._platform_value(platform) not in self.enabled_platforms:
            return None
        with self._lock:
            paths = sorted(self._pending.get(session_id, set()), key=str)
        missing = [f"MEDIA:{path}" for path in paths if f"MEDIA:{path}" not in response_text]
        if not missing:
            return None
        return response_text.rstrip() + "\n" + "\n".join(missing)

    def on_session_end(self, session_id: str = "", **kwargs) -> None:
        del kwargs
        with self._lock:
            paths = set(self._pending.get(session_id, set()))
        if not paths:
            return
        store = self._session_store
        gateway = self._gateway
        entry = store.lookup_by_session_id(session_id) if store else None
        if entry is None or gateway is None:
            return
        source = getattr(entry, "origin", None)
        platform = self._platform_value(
            getattr(source, "platform", None) or getattr(entry, "platform", None)
        )
        adapter = self._find_adapter(gateway, platform)
        callback = getattr(adapter, "register_post_delivery_callback", None)
        if not callable(callback):
            return

        def cleanup() -> None:
            self._cleanup_paths(paths)
            with self._lock:
                current = self._pending.get(session_id)
                if current is not None:
                    current.difference_update(paths)
                    if not current:
                        self._pending.pop(session_id, None)

        try:
            callback(entry.session_key, cleanup)
        except Exception:
            logger.warning("无法登记发送后清理回调", exc_info=True)

    def on_session_finalize(self, **kwargs) -> None:
        del kwargs
        self.cleanup_expired()

    @staticmethod
    def _find_adapter(gateway, platform: str):
        for key, adapter in getattr(gateway, "adapters", {}).items():
            if HermesSocialParser._platform_value(key) == platform:
                return adapter
        return None

    def _safe_cache_path(self, path: Path) -> Path | None:
        if path.is_symlink():
            return None
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if not resolved.is_relative_to(self.cache_root):
            return None
        return resolved

    def _cleanup_paths(self, paths: set[Path]) -> None:
        parents: set[Path] = set()
        for path in paths:
            safe = self._safe_cache_path(path)
            if safe is None:
                logger.warning("拒绝删除 cache 目录外的路径: %s", path)
                continue
            parents.add(safe.parent)
            try:
                safe.unlink(missing_ok=True)
            except OSError:
                logger.warning("删除临时媒体失败: %s", safe)
        for parent in sorted(parents, key=lambda item: len(item.parts), reverse=True):
            try:
                parent.rmdir()
            except OSError:
                pass

    def cleanup_expired(self) -> None:
        cutoff = time.time() - self.CACHE_TTL_SECONDS
        if not self.cache_root.exists():
            return
        for path in self.cache_root.iterdir():
            try:
                if path.is_symlink():
                    continue
                if path.stat().st_mtime >= cutoff:
                    continue
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(self.cache_root):
                    continue
                if path.is_dir():
                    shutil.rmtree(resolved)
                else:
                    resolved.unlink(missing_ok=True)
            except OSError:
                logger.warning("清理过期缓存失败: %s", path)

    def _clear_run_files(self, run_dir: Path) -> None:
        if not run_dir.exists():
            return
        for path in run_dir.iterdir():
            safe = self._safe_cache_path(path)
            if safe is not None and safe.is_file():
                safe.unlink(missing_ok=True)

    def _remove_run_dir(self, run_dir: Path) -> None:
        self._clear_run_files(run_dir)
        try:
            run_dir.rmdir()
        except OSError:
            pass
