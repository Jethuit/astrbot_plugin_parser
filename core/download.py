import asyncio
import logging
import sys
from asyncio import Task, TimeoutError, create_task, gather, sleep, to_thread
from collections.abc import Callable, Coroutine
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

import aiofiles
import yt_dlp
from aiohttp import ClientError, ClientSession, ClientTimeout
from msgspec import Struct, convert
from .config import PluginConfig
from .constants import COMMON_HEADER
from .exception import (
    DownloadException,
    DurationLimitException,
    ParseException,
    SizeLimitException,
    ZeroSizeException,
)
from .utils import LimitedSizeDict, generate_file_name, safe_unlink

logger = logging.getLogger(__name__)

P = ParamSpec("P")
T = TypeVar("T")


def auto_task(func: Callable[P, Coroutine[Any, Any, T]]) -> Callable[P, Task[T]]:
    """装饰器：自动将异步函数调用转换为 Task, 完整保留类型提示"""

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> Task[T]:
        coro = func(*args, **kwargs)
        name = " | ".join(str(arg) for arg in args if isinstance(arg, str))
        return create_task(coro, name=func.__name__ + " | " + name)

    return wrapper


class VideoInfo(Struct):
    title: str
    """标题"""
    channel: str
    """频道名称"""
    uploader: str
    """上传者 id"""
    duration: int
    """时长"""
    timestamp: int
    """发布时间戳"""
    thumbnail: str
    """封面图片"""
    description: str
    """简介"""
    channel_id: str
    """频道 id"""

    @property
    def author_name(self) -> str:
        return f"{self.channel}@{self.uploader}"


class Downloader:
    """下载器，支持youtube-dlp 和 流式下载"""

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.max_size = self.cfg.source_max_size
        self.max_bytes = self.max_size * 1024 * 1024
        self._download_semaphore = asyncio.Semaphore(1)
        self.default_headers: dict[str, str] = COMMON_HEADER.copy()
        # 视频信息缓存
        self.info_cache: LimitedSizeDict[str, VideoInfo] = LimitedSizeDict()
        # 用于流式下载的客户端
        self.client = ClientSession(
            timeout=ClientTimeout(total=self.cfg.download_timeout)
        )

    async def close(self):
        """关闭网络客户端"""
        await self.client.close()

    @auto_task
    async def streamd(
        self,
        url: str,
        *,
        file_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
    ) -> Path:
        """流式下载"""
        if not file_name:
            file_name = generate_file_name(url)
        file_path = self.cfg.cache_dir / file_name
        part_path = file_path.with_name(f"{file_path.name}.part")
        # 如果文件存在，则直接返回
        if file_path.exists():
            return file_path
        headers = headers or self.default_headers
        retries = self.cfg.download_retry_times
        async with self._download_semaphore:
            for attempt in range(retries + 1):
                try:
                    await safe_unlink(part_path)
                    actual_proxy = None if proxy is ... else proxy
                    async with self.client.get(
                        url,
                        headers=headers,
                        allow_redirects=True,
                        proxy=actual_proxy,
                    ) as response:
                        if response.status >= 400:
                            raise ClientError(
                                f"HTTP {response.status} {response.reason}"
                            )
                        content_length = response.content_length
                        if content_length == 0:
                            logger.warning(f"媒体 url: {url}, 大小为 0, 取消下载")
                            raise ZeroSizeException
                        if content_length and content_length > self.max_bytes:
                            logger.warning(
                                "媒体 url: %s 大小 %.2f MB 超过 %s MB, 取消下载",
                                url,
                                content_length / 1024 / 1024,
                                self.max_size,
                            )
                            raise SizeLimitException

                        downloaded = 0
                        async with aiofiles.open(part_path, "wb") as file:
                            async for chunk in response.content.iter_chunked(256 * 1024):
                                downloaded += len(chunk)
                                if downloaded > self.max_bytes:
                                    raise SizeLimitException
                                await file.write(chunk)

                        if downloaded == 0:
                            logger.warning(f"媒体 url: {url}, 实际大小为 0, 取消下载")
                            raise ZeroSizeException
                        if content_length and downloaded < content_length:
                            raise ClientError(
                                f"HTTP payload incomplete {downloaded}/{content_length}"
                            )

                    await to_thread(part_path.replace, file_path)
                    return file_path
                except (ZeroSizeException, SizeLimitException):
                    await safe_unlink(part_path)
                    await safe_unlink(file_path)
                    raise
                except (ClientError, TimeoutError, OSError) as exc:
                    await safe_unlink(part_path)
                    await safe_unlink(file_path)
                    if attempt < retries:
                        await sleep(1 + attempt)
                        continue
                    logger.exception("下载失败 | url=%s file=%s", url, file_path)
                    raise DownloadException("媒体下载失败") from exc
        raise DownloadException("媒体下载失败")

    @auto_task
    async def download_video(
        self,
        url: str,
        *,
        video_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
    ) -> Path:
        if video_name is None:
            video_name = generate_file_name(url, ".mp4")
        return await self.streamd(
            url, file_name=video_name, headers=headers, proxy=proxy
        )

    @auto_task
    async def download_audio(
        self,
        url: str,
        *,
        audio_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
    ) -> Path:
        if audio_name is None:
            audio_name = generate_file_name(url, ".mp3")
        return await self.streamd(
            url, file_name=audio_name, headers=headers, proxy=proxy
        )

    @auto_task
    async def download_file(
        self,
        url: str,
        *,
        file_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
    ) -> Path:
        if file_name is None:
            file_name = generate_file_name(url, ".zip")
        return await self.streamd(
            url, file_name=file_name, headers=headers, proxy=proxy
        )

    @auto_task
    async def download_img(
        self,
        url: str,
        *,
        img_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
    ) -> Path:
        if img_name is None:
            img_name = generate_file_name(url, ".jpg")
        return await self.streamd(url, file_name=img_name, headers=headers, proxy=proxy)

    async def download_imgs_without_raise(
        self,
        urls: list[str],
        *,
        headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
    ) -> list[Path]:
        paths_or_errs = await gather(
            *[self.download_img(url, headers=headers, proxy=proxy) for url in urls],
            return_exceptions=True,
        )
        return [p for p in paths_or_errs if isinstance(p, Path)]

    async def gallerydl_extract_urls(self, url: str) -> list[str]:
        """让 gallery-dl 只解析单帖 URL，不让它负责媒体落盘。"""
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "gallery_dl",
            "--config-ignore",
            "--get-urls",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if process.stdout is None:
            raise ParseException("gallery-dl 未返回输出")

        urls: list[str] = []
        total_output = 0
        try:
            while True:
                line = await asyncio.wait_for(
                    process.stdout.readline(),
                    timeout=self.cfg.download_timeout,
                )
                if not line:
                    break
                total_output += len(line)
                if total_output > 64 * 1024:
                    process.kill()
                    raise ParseException("gallery-dl 输出超过安全限制")
                value = line.decode("utf-8", errors="replace").strip()
                if value.startswith(("http://", "https://", "ytdl:")):
                    urls.append(value)
                if len(urls) >= 16:
                    process.kill()
                    break
            return_code = await asyncio.wait_for(
                process.wait(), timeout=self.cfg.download_timeout
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            process.kill()
            await process.wait()
            raise ParseException("gallery-dl 解析超时") from exc

        if return_code and not urls:
            raise ParseException("gallery-dl 解析失败")
        if not urls:
            raise ParseException("gallery-dl 未返回媒体")
        return urls

    async def ytdlp_extract_info(
        self,
        url: str,
        *,
        cookiefile: Path | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
    ) -> VideoInfo:
        if (info := self.info_cache.get(url)) is not None:
            return info
        opts = {
            "quiet": True,
            "skip_download": True,
            "http_headers": headers or self.default_headers,
        }
        if proxy:
            opts["proxy"] = proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)
        if format:
            opts["format"] = format
        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
            raw = await to_thread(ydl.extract_info, url, download=False)
            if not raw:
                raise ParseException("获取视频信息失败")
        info = convert(raw, VideoInfo)
        self.info_cache[url] = info
        return info

    async def ytdlp_extract_raw(
        self,
        url: str,
        *,
        cookiefile: Path | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
    ) -> dict[str, Any]:
        opts = {
            "quiet": True,
            "skip_download": True,
            "http_headers": headers or self.default_headers,
        }
        if proxy:
            opts["proxy"] = proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)
        if format:
            opts["format"] = format

        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
            raw = await to_thread(ydl.extract_info, url, download=False)
            if not isinstance(raw, dict):
                raise ParseException("yt-dlp 返回数据异常")
            return raw  # type: ignore

    def _ytdlp_options(
        self,
        *,
        outtmpl: str,
        cookiefile: Path | None,
        headers: dict[str, str] | None,
        proxy: str | None,
        format: str | None,
        limit_hit: list[bool],
    ) -> dict[str, Any]:
        def enforce_size(status: dict[str, Any]) -> None:
            current = status.get("downloaded_bytes") or 0
            expected = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
            if current > self.max_bytes or expected > self.max_bytes:
                limit_hit[0] = True
                raise SizeLimitException

        opts: dict[str, Any] = {
            "outtmpl": outtmpl,
            "http_headers": headers or self.default_headers,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "max_filesize": self.max_bytes,
            "concurrent_fragment_downloads": 1,
            "retries": self.cfg.download_retry_times,
            "fragment_retries": self.cfg.download_retry_times,
            "progress_hooks": [enforce_size],
        }
        if format:
            opts["format"] = format
        if proxy:
            opts["proxy"] = proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)
        return opts

    async def _ytdlp_download(
        self,
        url: str,
        *,
        file_stem: str,
        cookiefile: Path | None,
        headers: dict[str, str] | None,
        proxy: str | None,
        format: str | None,
        node: bool,
    ) -> Path:
        candidates = [
            path
            for path in self.cfg.cache_dir.glob(f"{file_stem}.*")
            if not path.name.endswith((".part", ".ytdl")) and path.is_file()
        ]
        if candidates:
            return candidates[0]

        limit_hit = [False]
        opts = self._ytdlp_options(
            outtmpl=str(self.cfg.cache_dir / file_stem) + ".%(ext)s",
            cookiefile=cookiefile,
            headers=headers,
            proxy=proxy,
            format=format,
            limit_hit=limit_hit,
        )
        if node:
            opts["js_runtimes"] = {"node": {}}

        async with self._download_semaphore:
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
                    code = await to_thread(ydl.download, [url])
                if code:
                    raise DownloadException("yt-dlp 视频下载失败")
            except Exception as exc:
                for path in self.cfg.cache_dir.glob(f"{file_stem}.*"):
                    await safe_unlink(path)
                if limit_hit[0]:
                    raise SizeLimitException from exc
                if isinstance(exc, (SizeLimitException, DurationLimitException)):
                    raise
                raise DownloadException("yt-dlp 视频下载失败") from exc

        candidates = [
            path
            for path in self.cfg.cache_dir.glob(f"{file_stem}.*")
            if not path.name.endswith((".part", ".ytdl")) and path.is_file()
        ]
        if not candidates:
            raise DownloadException("yt-dlp 视频下载失败")
        result = candidates[0]
        if result.stat().st_size == 0:
            await safe_unlink(result)
            raise ZeroSizeException
        if result.stat().st_size > self.max_bytes:
            await safe_unlink(result)
            raise SizeLimitException
        return result

    @auto_task
    async def ytdlp_download_video(
        self,
        url: str,
        *,
        cookiefile: Path | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
        node: bool = False,
    ) -> Path:
        info = await self.ytdlp_extract_info(
            url, cookiefile=cookiefile, headers=headers, proxy=proxy
        )
        if info.duration > self.cfg.max_duration:
            raise DurationLimitException
        return await self._ytdlp_download(
            url,
            file_stem=generate_file_name(url),
            cookiefile=cookiefile,
            headers=headers,
            proxy=proxy,
            format=format or "best[ext=mp4][vcodec!=none][acodec!=none]",
            node=node,
        )

    @auto_task
    async def ytdlp_download_video_relaxed(
        self,
        url: str,
        *,
        cookiefile: Path | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
        node: bool = False,
    ) -> Path:
        raw = await self.ytdlp_extract_raw(
            url, cookiefile=cookiefile, headers=headers, proxy=proxy
        )
        duration = raw.get("duration") or 0
        if duration and float(duration) > self.cfg.max_duration:
            raise DurationLimitException
        return await self._ytdlp_download(
            url,
            file_stem=generate_file_name(url),
            cookiefile=cookiefile,
            headers=headers,
            proxy=proxy,
            format=format,
            node=node,
        )

    @auto_task
    async def ytdlp_download_audio(
        self,
        url: str,
        *,
        cookiefile: Path | None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
    ) -> Path:
        return await self._ytdlp_download(
            url,
            file_stem=generate_file_name(url),
            cookiefile=cookiefile,
            headers=headers,
            proxy=proxy,
            format=format or "bestaudio[protocol^=http]/bestaudio",
            node=False,
        )
