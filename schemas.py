PARSE_SOCIAL_MEDIA = {
    "name": "parse_social_media",
    "description": (
        "解析并下载单个 X/Twitter 或 TikTok 帖子的媒体。"
        "工具返回中的每一行 MEDIA:<绝对路径> 都必须原样保留在最终回复中。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "单个 X/Twitter 或 TikTok 帖子链接",
            }
        },
        "required": ["url"],
        "additionalProperties": False,
    },
}
