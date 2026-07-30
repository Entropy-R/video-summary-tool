# video-summary

`video-summary` 是一个 Docker 化的视频总结 CLI。它解决的是一个很常见的痛点：想总结一个 B 站、YouTube 或本地视频时，既不想手动下载视频、找字幕、转写音频，又不想把一堆 Python/FFmpeg/Whisper 依赖装到本机环境里。

这个项目会按下面的顺序处理视频：

1. 优先用 `yt-dlp` 抓取字幕。
2. 字幕不可用时，下载音频并用 `faster-whisper` 转写。
3. 默认不解析画面；显式使用 `--with-vision` 时，才会抽取关键帧并调用宿主机上的本地视觉模型。
4. 默认调用宿主机 Ollama 中的 Qwen3-VL 生成中文总结；也可显式切换到 OpenAI 兼容 API。
5. 如果没有 API 额度，也可以导出 `chatgpt_prompt.md`，复制到 ChatGPT 手动总结。

新增的图片/画面阅读能力适合 PPT、代码、图表、软件界面和操作步骤较多的视频。它不是逐帧识别，
而是按视频时长自适应筛选关键帧，将画面证据与完整语音/字幕按时间轴合并后再生成总结。

适合这些场景：

- 总结 B 站视频、YouTube 视频或其他 `yt-dlp` 支持的视频链接。
- 总结本地音视频文件。
- 阅读课程、录屏和产品演示中的 PPT、代码、图表、界面与关键操作画面。
- 只生成转写稿，不调用大模型。
- 把长视频转写成 prompt，手动发给 ChatGPT。

推荐使用 Docker 运行，避免污染本地 Python 环境。

## 解析模式

| 模式 | 信息来源 | 适用场景 | 特点 |
| --- | --- | --- | --- |
| 快速解析（默认） | 平台字幕；没有字幕时使用 Whisper 转写语音 | 访谈、播客、口播、知识讲解等主要内容由讲述承载的视频 | 速度较快，但不会识别 PPT、代码、图表和操作画面，可能遗漏仅在画面中出现的信息。 |
| 多模态解析 | 语音/字幕 + 关键帧画面 | 录屏教程、课程、演示、评测以及画面信息较多的视频 | 语音与画面都是重要证据，准确度通常更高，但会增加下载、抽帧和本地模型推理时间。 |

快速解析并不表示视频画面本身不重要，而是明确采用“主要内容由语音或字幕承载”的处理假设。多模态解析中，
语音/字幕主要提供讲述逻辑、观点和因果关系，画面主要提供屏幕文字、PPT、代码、图表、操作步骤和场景变化；
最终总结会综合两类证据，发生冲突时保守标记不确定性。

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

默认总结也使用本机 Ollama。先安装 Ollama 并拉取模型：

```bash
ollama pull qwen3-vl:8b-instruct-q4_K_M
```

`.env.example` 已将最终总结配置为本地 Ollama。默认流程仍不下载或解析视频画面；只有显式使用
`--with-vision` 时才会启用关键帧分析。

默认文字模式：

```powershell
docker compose run --rm video-summary "视频链接或本地视频路径"
```

启用图片/画面阅读：

```powershell
docker compose run --rm video-summary "视频链接或本地视频路径" --with-vision
```

### 运行环境自检

切换开发机器、修改模型配置或排查运行环境时，可以先执行自检。自检不会处理视频，也不会生成
转写稿、总结或其他视频产物：

```powershell
docker compose run --rm video-summary --doctor
```

同时检查视觉模型：

```powershell
docker compose run --rm video-summary --doctor --with-vision
```

自检会检查 Python 依赖、`yt-dlp`、FFmpeg、FFprobe、输出目录写入权限、模型配置、服务连通性和
目标模型。检查结果分为 `PASS`、`WARN`、`FAIL`；存在 `FAIL` 时退出码为 1，否则为 0。云端
OpenAI 兼容 API 如果不支持模型列表接口，会显示 `WARN`，不会误判为服务不可用。

需要交给脚本或 CI 读取时追加 `--json`：

```powershell
docker compose run --rm video-summary --doctor --with-vision --json
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
- 默认调用本机 Qwen3-VL 总结
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

### 6. 启用图片/画面阅读（多模态解析）

当前方案要求 macOS 14 或更高版本。在 macOS 宿主机安装并启动 Ollama，然后拉取模型：

```bash
ollama pull qwen3-vl:8b-instruct-q4_K_M
```

`.env` 中配置：

```text
VISION_API_KEY=ollama
VISION_BASE_URL=http://host.docker.internal:11434/v1
VISION_MODEL=qwen3-vl:8b-instruct-q4_K_M
VISION_TIMEOUT=300
```

运行：

```powershell
docker compose run --rm video-summary "视频链接或本地视频路径" --with-vision
```

图片/画面阅读会执行以下步骤：

1. 下载视频或读取本地文件，并完整保留字幕/Whisper 转写。
2. 按视频时长扫描候选画面，结合镜头变化与相似度去重筛选关键帧。
3. 使用本地 Qwen3-VL 读取画面描述和屏幕文字，并结合相邻语音校正 OCR。
4. 将语音/字幕与画面证据按时间轴合并，最后生成包含“关键画面信息”的中文总结。

多模态模式会根据视频时长自动计算候选帧密度和实际帧预算，并补充镜头变化帧。短视频保留基础覆盖，
长视频约按每 30 秒增长一帧，最终受 `--vision-max-frames` 硬上限约束。画面差异去重会忽略边缘
页眉、水印和播放器装饰；关键帧默认缩放到 768 像素宽，视觉模型每批分析 4 帧，并结合相邻字幕
校正 OCR，只提取字幕之外的
新增画面证据。原始图片不会发送给最终的文本总结接口。

如果当前 Ollama 上下文无法一次处理默认批量，程序会自动拆成更小批次，并在后续请求中记住已经
验证可用的批量。最终多模态材料会完整保留字幕，并根据总结模型的安全上下文限制视觉补充预算；
短中视频会尽量使用单次总结，长视频仍采用均衡分段。

成功后可重点查看：

- `summary.md`：综合语音与画面生成的最终总结。
- `visual_context.md`：按时间列出的关键画面描述，便于人工核对。
- `multimodal_context.txt`：完整语音/字幕与画面证据合并后的时间轴材料。
- `frames/run-*/`：筛选出的关键帧，仅用于调试和复核。

图片仅发送给配置的视觉模型。默认配置使用宿主机 Ollama，本地处理图片；最终文本总结只接收已经
生成的文字材料。首次运行需要下载 Whisper 模型，并可能花费较长时间，16 GB 内存机器建议保持
单任务运行。

为保证旧参数语义不变，`--with-vision` 暂不能与 `--no-llm`、`--export-prompt`、`--summary-from-file` 同时使用。

## B 站 Cookies

B 站经常会对未登录或容器网络请求返回 412，或者只允许登录用户获取字幕：

```text
HTTP Error 412: Precondition Failed
```

这通常不是程序错误，而是缺少有效登录态。没有 cookies 时，即使视频网页上能看到 AI 字幕，
`yt-dlp` 也可能只能获得弹幕轨道，程序将提示登录要求并回退到 Whisper。建议导出 cookies。

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
    visual_context.json
    visual_context.md
    multimodal_context.txt
    frames/run-*/
    *.srt
```

各文件含义：

| 文件 | 说明 |
| --- | --- |
| `meta.json` | 视频元数据和处理信息，例如标题、URL、时长、文本来源、warnings。 |
| `transcript.txt` | 最终用于总结的文本。可能来自字幕，也可能来自 Whisper 转写；可识别时间时会保留为 `[00:01:23] 文本`。 |
| `summary.md` | 大模型生成的中文总结。只有调用 LLM 成功时生成，默认会尽量按转写稿中的时间节点组织分段要点。 |
| `chatgpt_prompt.md` | 可复制到 ChatGPT 的提示词。使用 `--export-prompt` 或 LLM 失败兜底时生成。 |
| `visual_context.json` / `visual_context.md` | 视觉模式生成的带时间戳画面解析结果。 |
| `multimodal_context.txt` | 按时间轴合并语音/字幕与画面描述的最终总结输入。 |
| `frames/run-*/` | 视觉模式筛选出的关键帧；调试阶段默认保留。 |
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
  "automatic_caption_langs": ["ai-zh"],
  "selected_subtitle_lang": "ai-zh",
  "selected_subtitle_type": "automatic",
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
| `--doctor` | `false` | 检查依赖、输出权限、模型配置和服务连通性，不处理视频。 |
| `--output` | `outputs` | 输出目录。 |
| `--model-size` | `small` | Whisper 模型大小，影响转写速度和准确率。 |
| `--language` | `zh` | Whisper 识别语言。中文视频默认不用配置；英文视频建议传 `en`；中英文不确定或多语言内容可传 `auto`。 |
| `--sub-langs` | `zh.*,ai-zh,en.*` | 字幕语言匹配规则，覆盖常见中文、B 站 AI 中文字幕和英文轨道。 |
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
| `--summary-provider` | `ollama` | 最终总结提供方；`api` 使用 `OPENAI_*` 配置。 |
| `--no-resume` | `false` | 忽略阶段缓存，强制重新处理。 |
| `--with-vision` | `false` | 启用多模态解析（语音/字幕 + 关键帧）；默认使用快速解析，仅处理语音/字幕。 |
| `--vision-scan-interval` | `5` | 候选帧扫描间隔秒数。 |
| `--vision-max-gap` | `45` | 自适应采样允许的相似画面最长保留间隔上限秒数。 |
| `--vision-max-frames` | `60` | 单个视频最多解析的关键帧数。 |
| `--vision-batch-size` | `4` | 每次视觉模型请求包含的图片数。 |
| `--vision-frame-width` | `768` | 送入视觉模型前的关键帧宽度。 |

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

## 运行元数据、缓存和断点续跑

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

程序默认在 `pipeline_cache.json` 中记录转写、关键帧、视觉解析和最终总结的签名。同一视频以相同参数重跑时，
会复用已完成阶段；视觉处理中断后也会从缺失帧继续。修改模型、抽帧参数、总结模型或输入文件后，相应阶段会自动失效。
需要全部重新处理时使用 `--no-resume`。调试用的 `frames/run-*/` 不会自动删除。

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

`.env.example` 默认使用本机 Ollama 完成视觉解析和最终总结：

```text
SUMMARY_PROVIDER=ollama
SUMMARY_API_KEY=ollama
SUMMARY_BASE_URL=http://host.docker.internal:11434/v1
SUMMARY_MODEL=qwen3-vl:8b-instruct-q4_K_M
SUMMARY_TIMEOUT=300
SUMMARY_LOCAL_CHUNK_CHARS=auto
SUMMARY_OLLAMA_CONTEXT_LENGTH=8192
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_MODEL=deepseek-v4-flash
VISION_API_KEY=ollama
VISION_BASE_URL=http://host.docker.internal:11434/v1
VISION_MODEL=qwen3-vl:8b-instruct-q4_K_M
VISION_TIMEOUT=300
VIDEO_SUMMARY_COOKIES=
VIDEO_SUMMARY_COOKIES_FROM_BROWSER=
```

需要调用云端 OpenAI 兼容接口时，设置 `SUMMARY_PROVIDER=api`，或者传入
`--summary-provider api`，再配置 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 和 `OPENAI_MODEL`。
程序不会在本地总结失败后自动调用云端接口，避免意外费用和数据外发。
本地总结默认读取 Qwen 模型向 Ollama 声明的最大上下文，并在每次原生 Ollama 请求中显式设置
`SUMMARY_OLLAMA_CONTEXT_LENGTH`（默认 8192）。程序会为提示词和输出预留空间，自动计算不超过
当前上下文安全容量的分段；文本能安全放入一次请求时直接总结，超过时才均衡分段。若 Ollama返回
上下文超限，程序会缩小分段后重试。需要固定分段时可把 `SUMMARY_LOCAL_CHUNK_CHARS` 设置为正整数。
长分段的中间摘要会保留更充足的输出预算，避免为了减少调用次数而丢失分段后半部分。提高 Ollama
上下文会增加内存占用。

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

视觉连通和短视频验证：

```powershell
docker compose run --rm video-summary "/app/outputs/local/sample.mp4" --model-size tiny --with-vision --vision-max-frames 6
```

## 注意事项

- 首次 Whisper 转写会下载模型，模型缓存在 Docker volume `video_summary_cache`。
- Qwen3-VL 运行在宿主机 Ollama 中，不占用 Docker volume；16 GB 内存机器建议保持单任务运行。
- 使用 `--summary-provider api` 时，ChatGPT Plus 不等于 OpenAI API 额度；API 需要单独配置 billing。
- B 站链接可能需要 cookies、代理或换网络。
- `.env`、`cookies/`、`outputs/` 已加入忽略列表，避免误提交敏感数据或产物。

## 版本变更

README 主要说明当前版本的安装和使用方式。各版本之间的功能变化、升级说明和历史记录统一维护在 [CHANGELOG.md](CHANGELOG.md)。

## 开源协议

MIT License。详见 `LICENSE`。
