# 版本变更说明

## v1.1

本版本围绕“转写质量、总结结构、运行可追踪性和文档可用性”做了增强。

- README 明确默认 Docker 用法下 B 站 cookies 文件必须命名为 `cookies.txt`，路径为 `cookies/cookies.txt`；其他文件名需要显式配置 `--cookies` 或 `VIDEO_SUMMARY_COOKIES`。
- `transcript.txt` 增加时间节点支持：SRT、VTT、JSON3 字幕和 Whisper 转写会尽量输出 `[HH:MM:SS] 文本`，便于总结按时间组织。
- Whisper 音频转写会自动从视频标题、UP 主、简介、标签等元数据中提取少量上下文关键词，降低专有名词、产品名和版本号误识别概率；实际使用的关键词会记录到 `meta.json` 的 `whisper_context_terms`。
- 默认总结模板新增“文本质量提醒”，提示大模型转写稿可能存在识别错误，并要求只修正上下文强烈支持的明显误识别，避免编造。
- 默认总结结构调整：`视频主题` 用一段话概括，`分段要点` 优先按时间节点组织并写得更详细。
- 长文本总结采用先分段提取结构化要点、再合并总结的流程，减少长视频直接压缩造成的细节丢失。
- `meta.json` 增加运行参数和阶段耗时记录，便于判断慢在元数据读取、字幕下载、音频下载、转写还是总结。
- 同一输出目录再次运行时，会清理旧的 `transcript.txt`、`summary.md`、`chatgpt_prompt.md`，避免不同运行的旧产物混在一起。
- README 补充英文视频建议使用 `--language en` 或 `--language auto`，避免默认中文识别影响英文视频转写质量。
- `.dockerignore` 补充 `.DS_Store`、测试缓存和 Python 编译产物，减少无关文件进入 Docker 构建上下文。

## v1.0

初始版本提供 Docker 化的视频总结 CLI。

- 支持 B 站、YouTube 等 `yt-dlp` 可解析的视频链接，以及本地音视频文件。
- 优先下载平台字幕；字幕不可用时自动下载音频并使用 Whisper 转写。
- 支持调用 OpenAI 兼容接口生成中文总结。
- 支持 `--no-llm` 只生成转写稿。
- 支持 `--export-prompt` 或 LLM 失败兜底生成 `chatgpt_prompt.md`，方便复制到 ChatGPT 手动总结。
- 支持 B 站 cookies 文件和浏览器 cookies 读取配置。
