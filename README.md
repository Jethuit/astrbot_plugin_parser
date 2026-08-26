# astrbot_plugin_parser — Hermes X/TikTok adapter

这是 [Zhalslar/astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser)
的薄 Hermes 适配分支，基线为上游提交
`8b5e71c84d0aeb0c4970320b981d6d32001074f2`（v1.5.6）。

第一版只加载上游的 X/Twitter、TikTok parser，并复用其数据模型、cookie
与下载结构。发送完全交给 Hermes 的 `MEDIA:<绝对路径>` 管线，不实现
Telegram Bot API 或 QQ Bot API。上游 parser 失败时依次回退到 gallery-dl
和 yt-dlp；不会加载卡片渲染、PIL、字体或其他 parser，也不会调用 FFmpeg
转码或在内存中缓存媒体。

## 安装

```bash
hermes plugins install Jethuit/astrbot_plugin_parser
hermes plugins enable astrbot-plugin-parser
```

Hermes 只声明依赖而不会自动安装，请在 Hermes 使用的 Python 环境中安装：

```bash
python -m pip install -r requirements.txt
```

## 配置

非秘密设置位于 `plugins.entries.astrbot-plugin-parser.settings`：

```yaml
plugins:
  entries:
    astrbot-plugin-parser:
      settings:
        max_media_mb: 90
        max_duration_minutes: 15
        download_timeout_seconds: 280
        download_retries: 2
        enabled_platforms: [telegram, qqbot]
```

可选秘密环境变量：

- `SOCIAL_PARSER_TWITTER_COOKIES`
- `SOCIAL_PARSER_TIKTOK_COOKIES`
- `SOCIAL_PARSER_PROXY`

运行文件写入当前 Hermes profile 的 plugin data 目录。媒体在 gateway 完成
主回复和 `MEDIA:` 投递后删除；旧版 adapter 没有发送后回调时，文件最多
保留一小时并由下一次消息或插件启动清理。

## 开发

```bash
python -m pytest -q
hermes plugins doctor . --ci
```

仓库保留上游其他 parser、`core/render.py` 和资源文件，便于同步上游修复，
但它们不进入 Hermes 运行时导入闭包。许可证沿用上游 MIT。
