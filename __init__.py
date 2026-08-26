"""Hermes X/TikTok media parser plugin."""

from .hermes_adapter import HermesSocialParser
from .schemas import PARSE_SOCIAL_MEDIA


def register(ctx):
    runtime = HermesSocialParser(ctx)
    ctx.register_tool(
        name="parse_social_media",
        toolset="social_media_parser",
        schema=PARSE_SOCIAL_MEDIA,
        handler=runtime.parse_tool,
        is_async=True,
    )
    ctx.register_hook("pre_gateway_dispatch", runtime.pre_gateway_dispatch)
    ctx.register_hook("transform_llm_output", runtime.transform_llm_output)
    ctx.register_hook("on_session_end", runtime.on_session_end)
    ctx.register_hook("on_session_finalize", runtime.on_session_finalize)
