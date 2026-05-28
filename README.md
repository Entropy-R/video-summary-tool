# video-summary

`video-summary` 是一个 Docker 化的视频总结 CLI。它解决的是一个很常见的痛点：想总结一个 B 站、YouTube 或本地视频时，既不想手动下载视频、找字幕、转写音频，又不想把一堆 Python/FFmpeg/Whisper 依赖装到本机环境里。

这个项目会按下面的顺序处理视频：

1. 优先用 `yt-dlp` 抓取字幕。
2. 字幕不可用时，下载音频并用 `faster-whisper` 转写。
3. 最后调用 OpenAI 兼容接口（例如 DeepSeek、OpenAI、OpenRouter 等）生成中文总结。
4. 如果没有 API 额度，也可以导出 `chatgpt_prompt.md`，复制到 ChatGPT 手动总结。

适合这些场景：

- 总结 B 站视频、YouTube 视频或其他 `yt-dlp` 支持的视频链接。
- 总结本地音视频文件。
- 只生成转写稿，不调用大模型。
- 把长视频转写成 prompt，手动发给 ChatGPT。

推荐使用 Docker 运行，避免污染本地 Python 环境。

## 路径说明

README 中会同时出现两类路径：

- **宿主机路径**：你在 Windows 上看到的路径，例如 `G:\code\video-summary\outputs\local\demo.mp4`。
- **容器内路径**：Docker 容器里看到的路径，例如 `/app/outputs/local/demo.mp4`。

`docker-compose.yml` 默认做了两组目录映射：

| 宿主机项目目录 | 容器内目录 | 用途 |
| --- | --- | --- |
| `./outputs` | `/app/outputs` | 输入本地视频、保存转写和总结结果。 |
| `./cookies` | `/app/cookies` | 保存 B 站 cookies。 |

所以：

- README 里的 `outputs/`、`cookies/` 是相对于项目根目录的路径。
- 在 Docker 命令里传给程序的本地文件路径，应该使用容器内路径 `/app/outputs/...`。
- `--output outputs/test` 是容器内相对路径，实际会写到宿主机项目目录下的 `outputs/test`。

## 快速开始

构建镜像：

```powershell
docker compose build
```

准备配置：

```powershell
copy .env.example .env
```

编辑 `.env`，填入 OpenAI 兼容接口配置：

```text
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_MODEL=deepseek-v4-flash
```

## 常用场景

### 1. 总结本地视频

把视频放到 `outputs/` 下，例如：

```text
outputs/local/demo.mp4
```

这表示宿主机项目目录下的：

```text
G:\code\video-summary\outputs\local\demo.mp4
```

运行：

```powershell
docker compose run --rm video-summary "/app/outputs/local/demo.mp4"
```

默认行为：

- 使用 Whisper 转写，默认语言 `zh`
- 调用 `.env` 里的大模型接口
- 输出 `transcript.txt` 和 `summary.md`

### 2. 总结 B 站视频

先把 B 站 cookies 放到：

```text
cookies/cookies.txt
```

这表示宿主机项目目录下的：

```text
G:\code\video-summary\cookies\cookies.txt
```

然后运行：

```powershell
docker compose run --rm video-summary "https://www.bilibili.com/video/BVxxxx/"
```

默认行为：

- 自动尝试读取 `/app/cookies/cookies.txt`
- 优先抓字幕
- 没有字幕时自动下载音频并转写
- 调用大模型生成总结

### 3. 只转写，不调用大模型

```powershell
docker compose run --rm video-summary "视频链接或本地文件路径" --no-llm
```

适合先验证字幕抓取、下载、Whisper 转写是否正常。

### 4. 没有 API 额度，导出 ChatGPT prompt

```powershell
docker compose run --rm video-summary "视频链接或本地文件路径" --export-prompt
```

会生成 `chatgpt_prompt.md`，可以复制到 ChatGPT 手动总结。

### 5. 从已有转写稿生成 prompt

```powershell
docker compose run --rm video-summary --summary-from-file /app/outputs/example/transcript.txt --output outputs/prompts
```

适合已经有 `transcript.txt`，不想重新下载或转写的情况。

## B 站 Cookies

B 站经常会对未登录或容器网络请求返回：

```text
HTTP Error 412: Precondition Failed
```

这通常不是程序错误，而是缺少有效登录态。建议导出 cookies。

默认 Docker 用法会自动尝试读取容器内的 `/app/cookies/cookies.txt`，所以宿主机项目目录下的文件名必须是：

```text
cookies/cookies.txt
```

如果你想使用其他文件名，需要显式传入容器内路径，例如 `--cookies /app/cookies/bilibili.txt`，或在 `.env` 中设置 `VIDEO_SUMMARY_COOKIES=/app/cookies/bilibili.txt`。

### 方法一：浏览器插件导出（推荐）

1. 在 Chrome 或 Edge 安装插件 `Get cookies.txt LOCALLY`。
2. 登录 B 站并打开 `https://www.bilibili.com`。
3. 点击插件，导出当前站点 cookies。
4. 格式选择 Netscape cookies.txt。
5. 保存为：

```text
cookies/cookies.txt
```

Windows 项目路径示例：

```text
G:\code\video-summary\cookies\cookies.txt
```

容器内路径会自动映射为：

```text
/app/cookies/cookies.txt
```

### 方法二：本机 yt-dlp 导出

如果本机安装了 `yt-dlp`，可以从浏览器读取 cookies 并写出文件。

Edge：

```powershell
yt-dlp --cookies-from-browser edge --cookies G:\code\video-summary\cookies\cookies.txt --skip-download https://www.bilibili.com
```

Chrome：

```powershell
yt-dlp --cookies-from-browser chrome --cookies G:\code\video-summary\cookies\cookies.txt --skip-download https://www.bilibili.com
```

浏览器最好先完全退出，否则 cookies 数据库可能被锁。

### 验证 cookies 是否生效

```powershell
docker compose run --rm video-summary "https://www.bilibili.com/video/BVxxxx/" --no-llm --json
```

如果不再出现 `HTTP Error 412`，说明 cookies 生效。

注意：`cookies.txt` 等同登录凭证，不要分享，不要提交到 GitHub。

## 输出结构

每个视频会生成一个独立目录：

```text
outputs/
  video-title/
    meta.json
    transcript.txt
    summary.md
    chatgpt_prompt.md
    *.srt
```

各文件含义：

| 文件 | 说明 |
| --- | --- |
| `meta.json` | 视频元数据和处理信息，例如标题、URL、时长、文本来源、warnings。 |
| `transcript.txt` | 最终用于总结的文本。可能来自字幕，也可能来自 Whisper 转写；可识别时间时会保留为 `[00:01:23] 文本`。 |
| `summary.md` | 大模型生成的中文总结。只有调用 LLM 成功时生成，默认会尽量按转写稿中的时间节点组织分段要点。 |
| `chatgpt_prompt.md` | 可复制到 ChatGPT 的提示词。使用 `--export-prompt` 或 LLM 失败兜底时生成。 |
| `*.srt` | 下载到的字幕文件，如果视频有字幕才会出现。 |
| `*.mp3` / `*.m4a` | 下载的音频文件。默认转写后删除，使用 `--keep-audio` 时保留。 |

`meta.json` 示例：

```json
{
  "title": "example",
  "id": "example",
  "webpage_url": "https://www.bilibili.com/video/BVxxxx/",
  "uploader": "up-name",
  "duration": 120,
  "subtitle_langs": [],
  "automatic_caption_langs": [],
  "whisper_context_terms": ["LLaMA-Factory", "AI"],
  "transcript_source": "whisper",
  "warnings": []
}
```

`transcript_source` 可能的值：

| 值 | 说明 |
| --- | --- |
| `manual_subtitle` | 人工字幕。 |
| `auto_subtitle` | 自动字幕，可能有识别错误。 |
| `subtitle` | 使用了字幕，但无法判断人工还是自动。 |
| `whisper` | 使用 Whisper 音频转写。 |
| `file` | 从已有 `transcript.txt` 读取。 |

## 常用参数

大多数用户只需要默认命令。下面是高级参数说明。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--output` | `outputs` | 输出目录。 |
| `--model-size` | `small` | Whisper 模型大小，影响转写速度和准确率。 |
| `--language` | `zh` | Whisper 识别语言。中文视频默认不用配置；英文视频建议传 `en`；中英文不确定或多语言内容可传 `auto`。 |
| `--sub-langs` | `zh-Hans,zh-CN,zh,en` | 字幕语言优先级。 |
| `--cookies` | 自动尝试 `/app/cookies/cookies.txt` | cookies 文件路径。默认文件不存在时会忽略。 |
| `--cookies-from-browser` | 空 | 从浏览器读取 cookies，例如 `edge`、`chrome`、`firefox`。Docker 中通常需要额外挂载浏览器 profile。 |
| `--no-llm` | `false` | 只生成 `transcript.txt`，不调用大模型。 |
| `--export-prompt` | `false` | 生成 `chatgpt_prompt.md`，不调用大模型。 |
| `--summary-from-file` | 空 | 从已有 `transcript.txt` 生成 prompt。 |
| `--force-transcribe` | `false` | 跳过字幕，强制用 Whisper 转写。 |
| `--keep-audio` | `false` | 保留下载的音频文件。 |
| `--json` | `false` | 输出结构化 JSON 结果或错误。 |
| `--prompt` | 空 | 直接传入自定义总结 prompt 模板文本。 |
| `--prompt-file` | 空 | 从文件读取自定义总结 prompt 模板。 |
| `--max-chars` | `12000` | LLM 或 prompt 分段最大字符数。 |

## Whisper 模型大小

`--model-size` 控制语音转文字模型，不影响 DeepSeek/OpenAI 总结模型。

常用选择：

| 模型 | 特点 |
| --- | --- |
| `tiny` | 最快，质量最低，适合测试。 |
| `base` | 较快，质量略好。 |
| `small` | 默认推荐，速度和准确率平衡。 |
| `medium` | 更准，但更慢。 |
| `large-v3` | 准确率更高，资源占用更大。 |
| `turbo` | 速度更快的优化模型。 |

当前 CLI 校验的可用值：

```text
tiny, tiny.en, base, base.en, small, small.en, medium, medium.en,
large-v1, large-v2, large-v3, large, turbo,
distil-small.en, distil-medium.en, distil-large-v2, distil-large-v3
```

中文视频建议保持默认 `small`。快速测试可以用 `tiny`。英文视频建议同时指定 `--language en`，并可使用 `.en` 后缀模型，例如 `--model-size small.en --language en`。如果不确定视频语言，可以使用 `--language auto`，让 Whisper 自动检测。

注意：默认 `--language zh` 会让 Whisper 按中文识别音频。处理英文视频时如果忘记传 `--language en` 或 `--language auto`，专有名词和句子可能被识别得更差。

## Whisper 自动上下文

如果没有可用字幕，程序会用 Whisper 转写音频。转写前会自动从视频标题、UP 主、简介、标签等元数据中提取少量关键词，并作为上下文提示传给 Whisper，帮助减少专有名词、产品名、版本号等误识别。

这个过程默认启用，不需要额外参数。关键词只来自视频自身元数据，不使用固定内置词表；如果没有提取到有效关键词，就不会传上下文提示。实际使用的关键词会记录到 `meta.json` 的 `whisper_context_terms` 字段。视频有字幕时会优先使用字幕，这个 Whisper 上下文不会生效。

## 运行元数据和旧产物

每次运行会在 `meta.json` 中记录本次参数和阶段耗时：

```json
{
  "processing": {
    "started_at": "2026-05-28T10:30:00+0800",
    "stages": {
      "load_meta": 1.234,
      "download_subtitle": 2.345,
      "download_audio": 12.345,
      "transcribe": 180.123,
      "summarize": 20.456
    }
  },
  "options": {
    "model_size": "small",
    "language": "zh",
    "max_chars": 12000
  }
}
```

如果同一个视频输出目录已经存在，程序会在本次运行开始前清理旧的 `transcript.txt`、`summary.md`、`chatgpt_prompt.md`，避免不同运行产生的旧结果混在一起。其他文件（例如字幕文件、手动放入的资料、保留的音频）不会被自动删除。

## Prompt 模板

默认模板内置在程序中，也提供了可编辑文件：

```text
prompts/default_summary.md
```

支持变量：

```text
{title}
{source}
{transcript}
{chunk_index}
{chunk_count}
{webpage_url}
{uploader}
{duration}
{transcript_quality_note}
```

使用自定义模板：

```powershell
docker compose run --rm video-summary "/app/outputs/local/demo.mp4" --prompt-file /app/prompts/default_summary.md
```

`--prompt` 和 `--prompt-file` 不能同时使用。`--export-prompt` 和真实 LLM 总结会使用同一套模板。

默认模板会要求：

- `视频主题` 用一段话概括视频整体内容。
- `分段要点` 尽量按转写稿中的时间节点组织，并写出更详细的内容要点。
- 只使用转写稿已有的时间节点，不要求模型自行猜测时间。
- `文本质量提醒` 会根据文本来源提示大模型：Whisper、自动字幕或已有转写稿可能存在识别错误；只能修正上下文强烈支持的明显误识别，不能编造。

## 配置说明

`.env.example` 默认使用 DeepSeek 兼容接口示例：

```text
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_MODEL=deepseek-v4-flash
VIDEO_SUMMARY_COOKIES=
VIDEO_SUMMARY_COOKIES_FROM_BROWSER=
```

如果使用 OpenAI，把 `OPENAI_BASE_URL` 和 `OPENAI_MODEL` 改成对应值即可。

注意：`docker compose config` 会展开 `.env` 中的真实 API key，不要把它的输出截图或发到公开场合。

## 验证流程

本地文件转写：

```powershell
docker compose run --rm video-summary "/app/outputs/local/sample.mp4" --model-size tiny --no-llm --output outputs/smoke-no-llm
```

Prompt 导出：

```powershell
docker compose run --rm video-summary "/app/outputs/local/sample.mp4" --model-size tiny --export-prompt --output outputs/smoke-prompt
```

结构化输出：

```powershell
docker compose run --rm video-summary "/app/outputs/local/sample.mp4" --model-size tiny --no-llm --json
```

## 注意事项

- 首次 Whisper 转写会下载模型，模型缓存在 Docker volume `video_summary_cache`。
- ChatGPT Plus 不等于 OpenAI API 额度；API 需要单独配置 billing。
- B 站链接可能需要 cookies、代理或换网络。
- `.env`、`cookies/`、`outputs/` 已加入忽略列表，避免误提交敏感数据或产物。

## 开源协议

MIT License。详见 `LICENSE`。
