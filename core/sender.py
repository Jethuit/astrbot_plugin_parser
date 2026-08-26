"""把上游 ``ParseResult`` 转成 Hermes 原生 ``MEDIA:`` 指令。"""

from __future__ import annotations

import asyncio
import json
from itertools import chain
from pathlib import Path

from .data import (
    AudioContent,
    DynamicContent,
    FileContent,
    ImageContent,
    MediaContent,
    ParseResult,
    TextContent,
    VideoContent,
)
from .exception import DownloadException


class MessageSender:
    """薄输出适配器；不调用 Telegram/QQ API，也不读取媒体内容。"""

    @staticmethod
    def _iter_media(result: ParseResult):
        contents = chain(
            result.contents,
            result.repost.contents if result.repost else (),
        )
        for content in contents:
            if not isinstance(content, TextContent):
                yield content

    @staticmethod
    def _media_type(content: MediaContent) -> str:
        if isinstance(content, (VideoContent, DynamicContent)):
            return "video"
        if isinstance(content, ImageContent):
            return "image"
        if isinstance(content, AudioContent):
            return "audio"
        if isinstance(content, FileContent):
            return "file"
        return "media"

    async def materialize(self, result: ParseResult) -> list[tuple[str, Path]]:
        contents = list(self._iter_media(result))
        resolved: list[tuple[str, Path]] = []
        try:
            for content in contents:
                path = (await content.get_path()).resolve(strict=True)
                if not path.is_file() or path.stat().st_size == 0:
                    raise DownloadException("下载结果为空")
                resolved.append((self._media_type(content), path))
        except Exception:
            tasks = [
                content.path_task
                for content in contents
                if isinstance(content.path_task, asyncio.Task)
            ]
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for _, path in resolved:
                path.unlink(missing_ok=True)
            raise
        if not resolved:
            raise DownloadException("解析结果没有媒体")
        return resolved

    @staticmethod
    def build_response(
        result: ParseResult,
        media: list[tuple[str, Path]],
    ) -> str:
        directives = [f"MEDIA:{path}" for _, path in media]
        summary = {
            "ok": True,
            "platform": result.platform.name,
            "title": result.title or "",
            "media_types": [kind for kind, _ in media],
            "media_count": len(media),
            "directives": directives,
        }
        return json.dumps(summary, ensure_ascii=False) + "\n" + "\n".join(directives)
