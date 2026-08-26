# astrbot_plugin_parser — Hermes X/TikTok 媒体解析插件

这是 [Zhalslar/astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser)
的 Hermes 适配分支。当前基线为上游提交
[`8b5e71c`](https://github.com/Zhalslar/astrbot_plugin_parser/commit/8b5e71c84d0aeb0c4970320b981d6d32001074f2)
（v1.5.6），Hermes 版本位于
[`codex/hermes-x-tiktok-adapter`](https://github.com/Jethuit/astrbot_plugin_parser/tree/codex/hermes-x-tiktok-adapter)
分支。

本项目没有重写 X/Twitter、TikTok extractor，也没有实现 Telegram Bot API
或 QQ Bot API。它保留上游已经解耦的 parser、下载器、cookie 和数据模型，
只增加很薄的 Hermes 工具与 gateway hook 适配层。

## 工作方式

```text
Telegram / QQBot
        ↓
Hermes Gateway
        ↓
Hermes plugin hooks + parse_social_media
        ↓
上游 TwitterParser / TikTokParser
        ↓ 失败
gallery-dl
        ↓ 视频仍失败
yt-dlp relaxed fallback
        ↓
Hermes plugin data/cache 临时文件
        ↓
Hermes 原生 MEDIA:<绝对路径> 投递
        ↓
发送后清理；不支持回调时最多保留 1 小时
```

第一版只处理：

- `x.com`、`twitter.com` 的单条 `status` 链接；
- TikTok `www`、`vt`、`vm` 单帖或短链；
- Telegram 与 QQBot 入站消息；
- 一条消息中最先出现的一个受支持链接。

不处理账号主页、播放列表、直播或其他平台。

## 安装

当前 fork 的 `main` 仍保持上游内容，Hermes 改造尚未合并到 `main`。请在
运行 Hermes 的服务器上直接克隆开发分支，不要省略 `--branch`：

```bash
mkdir -p ~/.hermes/plugins
git clone \
  --branch codex/hermes-x-tiktok-adapter \
  --single-branch \
  https://github.com/Jethuit/astrbot_plugin_parser.git \
  ~/.hermes/plugins/astrbot-plugin-parser
```

在 Hermes 实际使用的 Python 环境中安装依赖：

```bash
cd ~/.hermes/plugins/astrbot-plugin-parser
python -m pip install -r requirements.txt
```

启用并检查插件：

```bash
hermes plugins enable astrbot-plugin-parser
hermes plugins doctor ~/.hermes/plugins/astrbot-plugin-parser --ci
```

修改插件或配置后重启 Hermes gateway。

> Hermes 的 `python_dependencies` 目前只负责声明和诊断，不会自动安装依赖。
> 如果 `hermes` 来自 venv、pipx 或其他隔离环境，请确保依赖安装到了同一个
> Python 环境。

## 配置

普通设置位于 Hermes profile 的
`plugins.entries.astrbot-plugin-parser.settings`：

```yaml
plugins:
  enabled:
    - astrbot-plugin-parser
  entries:
    astrbot-plugin-parser:
      settings:
        max_media_mb: 90
        max_duration_minutes: 15
        download_timeout_seconds: 280
        download_retries: 2
        enabled_platforms: [telegram, qqbot]
```

| 设置 | 默认值 | 作用 |
|---|---:|---|
| `max_media_mb` | `90` | 单个媒体文件的硬上限，单位 MiB |
| `max_duration_minutes` | `15` | 视频时长上限 |
| `download_timeout_seconds` | `280` | 单次下载超时 |
| `download_retries` | `2` | 流式下载重试次数，最大按 5 处理 |
| `enabled_platforms` | `[telegram, qqbot]` | 自动识别链接的 gateway 平台 |

Cookie 和代理可能包含凭据，不写入普通 YAML 配置。按需使用环境变量：

```bash
export SOCIAL_PARSER_TWITTER_COOKIES='name=value; other=value'
export SOCIAL_PARSER_TIKTOK_COOKIES='name=value; other=value'
export SOCIAL_PARSER_PROXY='http://user:password@proxy.example:8080'
```

Cookie 同时接受普通请求头格式和 Netscape cookie 文件格式。生成的 cookie
文件存放在当前 Hermes profile 的 plugin data 目录，并尝试设置为仅当前
用户可读写。

## Hermes 接口

插件注册一个工具：

```json
{
  "name": "parse_social_media",
  "arguments": {
    "url": "https://x.com/example/status/123456789"
  }
}
```

工具返回简短 JSON 摘要及一个或多个：

```text
MEDIA:/absolute/path/to/media.mp4
```

`pre_gateway_dispatch` 只负责识别链接和改写工具调用提示，不在 gateway
入站事件循环内下载。`transform_llm_output` 会在模型遗漏标签时补齐本轮
`MEDIA:` 行，之后由 Hermes 自己的 Telegram/QQBot adapter 上传媒体。

插件不会直接调用 `adapter.send()`，也不会接触平台 token 或上传协议。

## 低资源约束

面向 2 GB RAM 服务器，运行路径固定遵守：

- 全局解析和下载并发数为 1；
- aiohttp 以固定 chunk 写入 `.part`，不把媒体完整读入内存；
- 即使响应没有 `Content-Length`，累计字节超限也会中止并删除残留；
- yt-dlp 同时使用 `max_filesize` 和 progress hook 限制大小；
- TikTok 优先选择带音视频的单文件 MP4；
- 不合并音视频，不调用 FFmpeg，不转码；
- 不使用 `BytesIO`、base64、图片合成或卡片渲染；
- 不加载 `core/render.py`、PIL、字体和其他 parser。

上游的其他 parser、渲染代码和资源仍保留在仓库中，方便以后同步上游，
但不会进入 Hermes 运行时导入闭包。

## 临时文件清理

媒体统一写入当前 Hermes profile 的 plugin data/cache 目录，每次工具调用使用
独立子目录。插件在 `on_session_end` 中登记 Hermes adapter 的发送后回调，
主回复和媒体投递完成后才删除文件。

如果目标 Hermes 版本没有 `register_post_delivery_callback`，插件不会提前
删除媒体。遗留文件最多保留 1 小时，由下一轮消息或插件启动时清理。删除前
会解析真实路径并检查其仍位于本插件 cache 目录；符号链接不会被跟随删除。

## 测试

```bash
python -m pytest -q
hermes plugins doctor . --ci
```

测试覆盖 URL 识别、xdown HTML fixture、TikTok yt-dlp 映射、fallback 顺序、
有无 `Content-Length` 的大小限制、零字节、超时、重试、`.part` 清理、Hermes
消息改写、`MEDIA:` 补强和发送后生命周期清理。

仓库内与第一版无关的 QZone API 测试在 `pytest.ini` 中排除，因为对应 parser
不进入运行闭包，且其 `json5` 依赖有意未加入 Hermes 第一版依赖。Telegram
与 QQBot 的真实媒体投递仍需在目标服务器上进行实机确认。

## 同步上游

远端约定：

```text
origin   https://github.com/Jethuit/astrbot_plugin_parser.git
upstream https://github.com/Zhalslar/astrbot_plugin_parser.git
```

同步前先获取上游：

```bash
git fetch upstream
git switch codex/hermes-x-tiktok-adapter
git rebase upstream/main
```

也可以只 cherry-pick 上游针对 `twitter.py`、`tiktok.py` 或下载器的修复。
适配层集中在根目录 `__init__.py`、`hermes_adapter.py`、`schemas.py` 以及替换后
的 `core/config.py`、`core/sender.py`，便于将 parser 变更与框架适配区分开。

## 许可证与署名

沿用上游 MIT License。原项目作者和许可证信息保留在仓库历史与 `LICENSE`
中；Hermes 适配不改变上游代码的许可声明。
