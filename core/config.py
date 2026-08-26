"""Hermes 配置兼容层。

保留上游 parser 依赖的 ``PluginConfig`` / ``ParserItem`` 名称，但不再
依赖 AstrBot。所有普通设置来自 Hermes 的插件配置命名空间，cookie 和
代理仅从环境变量读取。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ParserItem:
    name: str
    enable: bool = True
    use_proxy: bool = False
    cookies: str | None = None
    show_body_text: bool | None = None
    video_send_mode: str | None = None
    video_codec_list: list | None = None
    video_quality: str | None = None


class ParserConfig:
    def __init__(self, twitter: ParserItem, tiktok: ParserItem) -> None:
        self.twitter = twitter
        self.tiktok = tiktok

    def platforms(self) -> list[str]:
        return ["twitter", "tiktok"]

    def enabled_platforms(self) -> list[str]:
        return [item.name for item in (self.twitter, self.tiktok) if item.enable]


class PluginConfig:
    """上游 parser/downloader 所需的最小配置表面。"""

    def __init__(self, ctx: Any, *, cache_dir: Path | None = None) -> None:
        self.context = ctx
        self.data_dir = Path(ctx.state.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_root = self.data_dir / "cache"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.cache_dir = cache_dir or self.cache_root
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_dir = self.data_dir / "cookies"
        self.cookie_dir.mkdir(parents=True, exist_ok=True)

        self.source_max_size = self._positive_int(
            ctx.get_config("max_media_mb", default=90), 90
        )
        self.source_max_minute = self._positive_int(
            ctx.get_config("max_duration_minutes", default=15), 15
        )
        self.download_timeout = self._positive_int(
            ctx.get_config("download_timeout_seconds", default=280), 280
        )
        self.download_retry_times = max(
            0,
            min(5, self._int(ctx.get_config("download_retries", default=2), 2)),
        )
        self.common_timeout = min(self.download_timeout, 60)
        self.max_duration = self.source_max_minute * 60
        self.max_size = self.source_max_size * 1024 * 1024

        self.proxy = os.environ.get("SOCIAL_PARSER_PROXY") or None
        use_proxy = self.proxy is not None
        self.parser = ParserConfig(
            twitter=ParserItem(
                name="twitter",
                use_proxy=use_proxy,
                cookies=os.environ.get("SOCIAL_PARSER_TWITTER_COOKIES") or None,
            ),
            tiktok=ParserItem(
                name="tiktok",
                use_proxy=use_proxy,
                cookies=os.environ.get("SOCIAL_PARSER_TIKTOK_COOKIES") or None,
            ),
        )

        raw_platforms = ctx.get_config(
            "enabled_platforms", default=["telegram", "qqbot"]
        )
        if not isinstance(raw_platforms, list):
            raw_platforms = ["telegram", "qqbot"]
        self.enabled_platforms = {
            str(value).strip().lower()
            for value in raw_platforms
            if str(value).strip()
        }

    @staticmethod
    def _int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _positive_int(cls, value: Any, default: int) -> int:
        return max(1, cls._int(value, default))
