from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv


SUBTITLE_EXTS = (".srt", ".vtt", ".ass", ".json3", ".srv1", ".srv2", ".srv3", ".ttml")
AUDIO_EXTS = (".mp3", ".m4a", ".opus", ".wav", ".webm")
DEFAULT_COOKIES_PATH = "/app/cookies/cookies.txt"
AVAILABLE_MODEL_SIZES = (
    "tiny",
    "tiny.en",
    "base",
    "base.en",
    "small",
    "small.en",
    "medium",
    "medium.en",
    "large-v1",
    "large-v2",
    "large-v3",
    "large",
    "turbo",
    "distil-small.en",
    "distil-medium.en",
    "distil-large-v2",
    "distil-large-v3",
)
DEFAULT_PROMPT_TEMPLATE = """你是严谨的视频内容总结助手。请只基于给定转写文本，用中文输出一份视频总结，不要编造原文没有的信息。

视频标题：{title}
文本来源：{source}

请按以下格式输出：

## 视频主题
## 核心观点
## 分段要点
## 重要结论
## 可执行建议

这是第 {chunk_index}/{chunk_count} 段转写文本：

{transcript}
"""


@dataclass
class VideoMeta:
    title: str
    id: str
    webpage_url: str
    uploader: str | None = None
    duration: int | None = None
    subtitle_langs: list[str] = field(default_factory=list)
    automatic_caption_langs: list[str] = field(default_factory=list)


@dataclass
class CookieConfig:
    file: str | None = None
    browser: str | None = None


@dataclass
class TranscriptInfo:
    text: str
    source: str
    warnings: list[str] = field(default_factory=list)


class AppError(RuntimeError):
    def __init__(self, code: str, message: str, hints: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hints = hints or []


def run_command(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise AppError("missing_tool", f"缺少命令行工具：{name}。请确认它已安装并在 PATH 中。")


def safe_name(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]+', "_", value).strip()
    value = re.sub(r"\s+", " ", value)
    return value[:80] or "video"


def is_bilibili_url(url: str) -> bool:
    return "bilibili.com" in url or "b23.tv" in url


def classify_command_error(stderr: str, url: str | None = None, action: str = "命令执行") -> AppError:
    hints: list[str] = []
    code = "upstream_error"
    if "HTTP Error 412" in stderr and url and is_bilibili_url(url):
        code = "bilibili_precondition_failed"
        hints.extend(
            [
                "B 站返回 412，通常是风控或未携带有效登录态。",
                "请优先尝试 --cookies /app/cookies/cookies.txt，或 --cookies-from-browser edge/chrome/firefox。",
                "如果在 Docker 中读取浏览器 cookies，请先把浏览器 profile 挂载进容器，或导出 cookies.txt。",
            ]
        )
    elif "429" in stderr or "rate limit" in stderr.lower():
        code = "rate_limited"
        hints.append("上游服务限流，请稍后重试。")
    elif "insufficient_quota" in stderr:
        code = "insufficient_quota"
        hints.append("OpenAI API 额度不足，请检查 API billing 和项目 quota。")
    return AppError(code, f"{action}失败：\n{stderr.strip()}", hints)


def resolve_cookies(value: str | None, *, required: bool) -> str | None:
    cookies = value or None
    if not cookies:
        return None
    path = Path(cookies).expanduser()
    if not path.is_absolute():
        path = path.resolve()
    if not path.exists():
        if not required:
            return None
        raise AppError(
            "cookies_not_found",
            f"cookies 文件不存在：{cookies}",
            ["确认 cookies.txt 已放到 cookies/ 目录，容器内路径通常是 /app/cookies/cookies.txt。"],
        )
    return str(path)


def resolve_cookie_config(cli_file: str | None, cli_browser: str | None) -> CookieConfig:
    browser = cli_browser or os.getenv("VIDEO_SUMMARY_COOKIES_FROM_BROWSER") or None
    if cli_file:
        return CookieConfig(file=resolve_cookies(cli_file, required=True))
    if browser:
        return CookieConfig(browser=browser)
    env_file = os.getenv("VIDEO_SUMMARY_COOKIES") or DEFAULT_COOKIES_PATH
    return CookieConfig(file=resolve_cookies(env_file, required=False))


def with_cookies(args: list[str], cookies: CookieConfig | None) -> list[str]:
    if not cookies:
        return args
    injected: list[str] = []
    if cookies.file:
        injected.extend(["--cookies", cookies.file])
    elif cookies.browser:
        injected.extend(["--cookies-from-browser", cookies.browser])
    return args[:1] + injected + args[1:] if injected else args


def load_meta(url: str, workdir: Path, cookies: CookieConfig | None) -> VideoMeta:
    args = with_cookies(["yt-dlp", "--dump-single-json", "--no-playlist", url], cookies)
    result = run_command(args, workdir)
    if result.returncode != 0:
        raise classify_command_error(result.stderr, url, "获取视频元数据")

    data = json.loads(result.stdout)
    title = data.get("title") or data.get("id") or "video"
    return VideoMeta(
        title=title,
        id=data.get("id") or safe_name(title),
        webpage_url=data.get("webpage_url") or url,
        uploader=data.get("uploader") or data.get("channel"),
        duration=data.get("duration"),
        subtitle_langs=sorted((data.get("subtitles") or {}).keys()),
        automatic_caption_langs=sorted((data.get("automatic_captions") or {}).keys()),
    )


def load_local_meta(path: Path) -> VideoMeta:
    name = safe_name(path.stem)
    return VideoMeta(
        title=path.stem or "local-video",
        id=name,
        webpage_url=str(path),
    )


def write_meta(
    meta: VideoMeta,
    output_dir: Path,
    transcript_source: str | None = None,
    warnings: list[str] | None = None,
) -> None:
    data = asdict(meta)
    data["transcript_source"] = transcript_source
    data["warnings"] = warnings or []
    (output_dir / "meta.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def detect_subtitle_source(path: Path, meta: VideoMeta) -> tuple[str, list[str]]:
    name = path.name
    warnings: list[str] = []
    for lang in meta.subtitle_langs:
        if f".{lang}." in name or name.endswith(f".{lang}{path.suffix}"):
            return "manual_subtitle", warnings
    for lang in meta.automatic_caption_langs:
        if f".{lang}." in name or name.endswith(f".{lang}{path.suffix}"):
            warnings.append("使用的是自动字幕，内容可能存在识别错误。")
            return "auto_subtitle", warnings
    warnings.append("已使用字幕文件，但无法确认是人工字幕还是自动字幕。")
    return "subtitle", warnings


def download_subtitle(
    url: str,
    output_dir: Path,
    cookies: CookieConfig | None,
    langs: str,
    meta: VideoMeta,
) -> tuple[Path, str, list[str]] | None:
    require_tool("yt-dlp")
    before = set(output_dir.glob("*"))
    args = with_cookies(
        [
            "yt-dlp",
            "--skip-download",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            langs,
            "--convert-subs",
            "srt",
            "--no-playlist",
            "-o",
            "%(title).80s.%(ext)s",
            url,
        ],
        cookies,
    )

    result = run_command(args, output_dir)
    after = set(output_dir.glob("*"))
    candidates = sorted(path for path in after - before if path.suffix.lower() in SUBTITLE_EXTS)
    if candidates:
        source, warnings = detect_subtitle_source(candidates[0], meta)
        return candidates[0], source, warnings

    existing = sorted(path for path in output_dir.glob("*") if path.suffix.lower() in SUBTITLE_EXTS)
    if existing:
        source, warnings = detect_subtitle_source(existing[0], meta)
        return existing[0], source, warnings

    if result.returncode != 0:
        app_error = classify_command_error(result.stderr, url, "字幕下载")
        print(f"字幕下载失败，将尝试音频转写：\n{app_error.message}", file=sys.stderr)
        for hint in app_error.hints:
            print(f"提示：{hint}", file=sys.stderr)
    else:
        print("未找到可用字幕，将尝试音频转写。", file=sys.stderr)
    return None


def download_audio(url: str, output_dir: Path, cookies: CookieConfig | None) -> Path:
    require_tool("yt-dlp")
    require_tool("ffmpeg")

    before = set(output_dir.glob("*"))
    args = with_cookies(
        [
            "yt-dlp",
            "-x",
            "--audio-format",
            "mp3",
            "--no-playlist",
            "-o",
            "%(title).80s.%(ext)s",
            url,
        ],
        cookies,
    )

    result = run_command(args, output_dir)
    if result.returncode != 0:
        raise classify_command_error(result.stderr, url, "音频下载")

    after = set(output_dir.glob("*"))
    candidates = sorted(path for path in after - before if path.suffix.lower() in AUDIO_EXTS)
    if candidates:
        return candidates[0]

    existing = sorted(path for path in output_dir.glob("*") if path.suffix.lower() in AUDIO_EXTS)
    if existing:
        return existing[0]
    raise AppError("audio_not_found", "音频下载完成，但没有找到生成的音频文件。")


def clean_subtitle(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".json3":
        return clean_json3(path)
    if suffix in (".srv1", ".srv2", ".srv3", ".ttml"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        text = re.sub(r"<[^>]+>", "\n", text)
        return normalize_lines(text.splitlines())

    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    cleaned: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.isdigit() or "-->" in stripped:
            continue
        stripped = re.sub(r"<[^>]+>", "", stripped)
        stripped = re.sub(r"\{\\.*?\}", "", stripped)
        if stripped:
            cleaned.append(stripped)
    return normalize_lines(cleaned)


def clean_json3(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    lines: list[str] = []
    for event in data.get("events", []):
        parts = event.get("segs") or []
        text = "".join(part.get("utf8", "") for part in parts).strip()
        if text:
            lines.append(text)
    return normalize_lines(lines)


def normalize_lines(lines: Iterable[str]) -> str:
    result: list[str] = []
    previous = ""
    for raw in lines:
        line = re.sub(r"\s+", " ", raw).strip()
        if not line or line == previous:
            continue
        result.append(line)
        previous = line
    text = "\n".join(result).strip()
    return text + "\n" if text else ""


def transcribe_audio(audio_path: Path, model_size: str, language: str | None = None) -> str:
    from faster_whisper import WhisperModel

    # 使用 auto 让同一镜像可以兼容 CPU/GPU 环境；模型大小由 CLI 参数控制。
    model = WhisperModel(model_size, device="auto", compute_type="auto")
    segments, _info = model.transcribe(str(audio_path), language=language, vad_filter=True)
    lines = [segment.text.strip() for segment in segments if segment.text.strip()]
    return normalize_lines(lines)


def split_text(text: str, max_chars: int) -> list[str]:
    if max_chars <= 0:
        raise RuntimeError("--max-chars 必须大于 0")
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in text.splitlines():
        parts = [paragraph[i : i + max_chars] for i in range(0, len(paragraph), max_chars)] or [""]
        for part in parts:
            part_len = len(part) + 1
            if current and current_len + part_len > max_chars:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            current.append(part)
            current_len += part_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def load_prompt_template(prompt_text: str | None, prompt_file: str | None) -> str:
    if prompt_text and prompt_file:
        raise AppError("invalid_arguments", "--prompt 和 --prompt-file 不能同时使用。")
    if prompt_text:
        return prompt_text
    if prompt_file:
        path = Path(prompt_file).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        if not path.is_file():
            raise AppError("prompt_file_not_found", f"prompt 文件不存在：{prompt_file}")
        return path.read_text(encoding="utf-8")
    return DEFAULT_PROMPT_TEMPLATE


def normalize_language(value: str | None) -> str | None:
    if value is None:
        return "zh"
    value = value.strip()
    if not value or value.lower() == "auto":
        return None
    return value


def validate_model_size(value: str) -> str:
    if value not in AVAILABLE_MODEL_SIZES:
        raise AppError(
            "invalid_model_size",
            f"不支持的 Whisper 模型：{value}",
            ["可用值：" + ", ".join(AVAILABLE_MODEL_SIZES)],
        )
    return value


def render_prompt_template(
    template: str,
    transcript: str,
    meta: VideoMeta,
    source: str,
    chunk_index: int,
    chunk_count: int,
) -> str:
    values = {
        "title": meta.title,
        "source": source,
        "transcript": transcript,
        "chunk_index": str(chunk_index),
        "chunk_count": str(chunk_count),
        "webpage_url": meta.webpage_url,
        "uploader": meta.uploader or "",
        "duration": "" if meta.duration is None else str(meta.duration),
    }
    try:
        return template.format(**values)
    except KeyError as exc:
        raise AppError(
            "invalid_prompt_template",
            f"prompt 模板包含未知变量：{exc.args[0]}",
            ["支持变量：{title}、{source}、{transcript}、{chunk_index}、{chunk_count}、{webpage_url}、{uploader}、{duration}"],
        ) from exc


def summarize_with_llm(
    transcript: str,
    meta: VideoMeta,
    source: str,
    max_chars: int,
    prompt_template: str,
) -> str:
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise AppError(
            "missing_openai_api_key",
            "缺少 OPENAI_API_KEY。可设置 .env，或使用 --no-llm / --export-prompt。",
            ["如果没有 API 额度，可以使用 --export-prompt 生成提示词后复制到 ChatGPT。"],
        )

    client = OpenAI(
        api_key=api_key,
        base_url=os.getenv("OPENAI_BASE_URL") or None,
    )
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    chunks = split_text(transcript, max_chars)

    partials: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        prompt = render_prompt_template(
            prompt_template,
            chunk,
            meta,
            source,
            index,
            len(chunks),
        )
        try:
            partials.append(call_llm(client, model, prompt))
        except Exception as exc:
            raise classify_llm_error(exc) from exc

    final_prompt = render_prompt_template(
        prompt_template,
        "\n\n".join(partials),
        meta,
        f"{source}_partials",
        1,
        1,
    )
    try:
        return call_llm(client, model, final_prompt)
    except Exception as exc:
        raise classify_llm_error(exc) from exc


def classify_llm_error(exc: Exception) -> AppError:
    message = str(exc)
    if "insufficient_quota" in message or "exceeded your current quota" in message:
        return AppError(
            "insufficient_quota",
            message,
            [
                "OpenAI API 额度不足，ChatGPT Plus 不等于 API 额度。",
                "程序会尽量生成 chatgpt_prompt.md，你可以复制到 ChatGPT 手动总结。",
            ],
        )
    if "rate limit" in message.lower() or "429" in message:
        return AppError("rate_limited", message, ["请求被限流，请稍后重试或换用 --export-prompt。"])
    return AppError("llm_error", message, ["可以使用 --export-prompt 导出提示词后手动总结。"])


def build_chatgpt_prompt(
    transcript: str,
    meta: VideoMeta,
    source: str,
    max_chars: int,
    prompt_template: str,
) -> str:
    chunks = split_text(transcript, max_chars)
    safe_chunks = [chunk.replace("```", "'''") for chunk in chunks]
    chunk_text = "\n\n".join(
        render_prompt_template(prompt_template, chunk, meta, source, index, len(safe_chunks))
        for index, chunk in enumerate(safe_chunks, start=1)
    )
    return chunk_text.rstrip() + "\n"


def write_chatgpt_prompt(
    transcript: str,
    meta: VideoMeta,
    source: str,
    output_dir: Path,
    max_chars: int,
    prompt_template: str,
) -> Path:
    prompt_path = output_dir / "chatgpt_prompt.md"
    prompt_path.write_text(
        build_chatgpt_prompt(transcript, meta, source, max_chars, prompt_template),
        encoding="utf-8",
    )
    return prompt_path


def print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def call_llm(client, model: str, prompt: str) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "你是严谨的视频内容总结助手，只基于给定文本总结，并使用中文输出。",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    return response.choices[0].message.content or ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="字幕优先、Whisper 兜底的视频总结 CLI")
    parser.add_argument("url", nargs="?", help="视频链接，或容器/本机可访问的本地音视频文件路径")
    parser.add_argument("--output", default="outputs", help="输出目录，默认 outputs")
    parser.add_argument("--model-size", default="small", choices=AVAILABLE_MODEL_SIZES, help="faster-whisper 模型大小，默认 small")
    parser.add_argument("--language", default="zh", help="Whisper 识别语言，例如 zh、en、auto；默认 zh")
    parser.add_argument("--sub-langs", default="zh-Hans,zh-CN,zh,en", help="字幕语言优先级")
    parser.add_argument("--cookies", default=None, help="cookies.txt 路径；默认自动尝试 /app/cookies/cookies.txt，缺失时忽略")
    parser.add_argument("--force-transcribe", action="store_true", help="跳过字幕，强制下载音频并转写")
    parser.add_argument("--keep-audio", action="store_true", help="保留下载的音频文件")
    parser.add_argument("--no-llm", action="store_true", help="只生成转写稿，不调用 LLM 总结")
    parser.add_argument("--export-prompt", action="store_true", help="生成可复制到 ChatGPT 的提示词文件，不调用 LLM")
    parser.add_argument("--summary-from-file", default=None, help="读取已有 transcript.txt 并生成 ChatGPT 提示词")
    parser.add_argument("--cookies-from-browser", default=None, help="从浏览器读取 cookies，例如 edge、chrome、firefox")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出成功结果或结构化错误")
    parser.add_argument("--prompt", default=None, help="自定义总结 prompt 模板文本")
    parser.add_argument("--prompt-file", default=None, help="从文件读取自定义总结 prompt 模板")
    parser.add_argument("--max-chars", type=int, default=12000, help="LLM 单段最大字符数")
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()
    prompt_template = load_prompt_template(args.prompt, args.prompt_file)
    whisper_language = normalize_language(args.language)
    output_dir: Path | None = None
    transcript_path: Path | None = None
    prompt_path: Path | None = None
    summary_path: Path | None = None
    meta: VideoMeta | None = None
    transcript_source: str | None = None
    warnings: list[str] = []

    try:
        output_root = Path(args.output).resolve()
        output_root.mkdir(parents=True, exist_ok=True)

        if args.summary_from_file:
            transcript_path = Path(args.summary_from_file).expanduser()
            if not transcript_path.is_absolute():
                transcript_path = transcript_path.resolve()
            if not transcript_path.is_file():
                raise RuntimeError(f"转写稿文件不存在：{args.summary_from_file}")

            transcript = transcript_path.read_text(encoding="utf-8")
            title = transcript_path.parent.name if transcript_path.stem == "transcript" else transcript_path.stem
            meta = VideoMeta(
                title=title,
                id=safe_name(title),
                webpage_url=str(transcript_path),
            )
            output_dir = output_root / safe_name(meta.title)
            output_dir.mkdir(parents=True, exist_ok=True)
            transcript_path = output_dir / "transcript.txt"
            transcript_path.write_text(transcript, encoding="utf-8")
            transcript_source = "file"
            write_meta(meta, output_dir, transcript_source)
            prompt_path = write_chatgpt_prompt(transcript, meta, "file", output_dir, args.max_chars, prompt_template)
            if args.json:
                print_json(
                    {
                        "ok": True,
                        "output_dir": str(output_dir),
                        "transcript": str(transcript_path),
                        "prompt": str(prompt_path),
                        "transcript_source": transcript_source,
                        "warnings": warnings,
                    }
                )
            else:
                print(f"已生成 ChatGPT 提示词：{prompt_path}")
            return 0

        if not args.url:
            raise RuntimeError("请提供视频链接、本地音视频文件路径，或使用 --summary-from-file 指定转写稿。")

        local_input = Path(args.url).expanduser()
        if not local_input.is_absolute():
            local_input = local_input.resolve()
        is_local_file = local_input.is_file()
        cookies = None if is_local_file else resolve_cookie_config(args.cookies, args.cookies_from_browser)
        if not is_local_file:
            require_tool("yt-dlp")

        meta = load_local_meta(local_input) if is_local_file else load_meta(args.url, output_root, cookies)
        output_dir = output_root / (safe_name(meta.title) if is_local_file else safe_name(f"{meta.id}-{meta.title}"))
        output_dir.mkdir(parents=True, exist_ok=True)
        write_meta(meta, output_dir)

        subtitle_path = None
        if is_local_file:
            transcript = transcribe_audio(local_input, args.model_size, whisper_language)
            transcript_source = "whisper"
        elif not args.force_transcribe:
            subtitle_info = download_subtitle(args.url, output_dir, cookies, args.sub_langs, meta)
            if subtitle_info:
                subtitle_path, transcript_source, subtitle_warnings = subtitle_info
                warnings.extend(subtitle_warnings)

        if not is_local_file and subtitle_path:
            transcript = clean_subtitle(subtitle_path)
            if not transcript.strip():
                print("字幕文件为空，将尝试音频转写。", file=sys.stderr)
                warnings.append("字幕文件为空，已回退到 Whisper 转写。")
                subtitle_path = None

        if not is_local_file and not subtitle_path:
            audio_path = download_audio(args.url, output_dir, cookies)
            transcript = transcribe_audio(audio_path, args.model_size, whisper_language)
            transcript_source = "whisper"
            if not args.keep_audio:
                audio_path.unlink(missing_ok=True)

        transcript_path = output_dir / "transcript.txt"
        transcript_path.write_text(transcript, encoding="utf-8")
        write_meta(meta, output_dir, transcript_source, warnings)

        if args.export_prompt:
            prompt_path = write_chatgpt_prompt(transcript, meta, transcript_source, output_dir, args.max_chars, prompt_template)
            if args.json:
                print_json(
                    {
                        "ok": True,
                        "output_dir": str(output_dir),
                        "transcript": str(transcript_path),
                        "prompt": str(prompt_path),
                        "transcript_source": transcript_source,
                        "warnings": warnings,
                    }
                )
            else:
                print(f"已生成 ChatGPT 提示词：{prompt_path}")
            return 0

        if args.no_llm:
            if args.json:
                print_json(
                    {
                        "ok": True,
                        "output_dir": str(output_dir),
                        "transcript": str(transcript_path),
                        "transcript_source": transcript_source,
                        "warnings": warnings,
                    }
                )
            else:
                print(f"已生成转写稿：{transcript_path}")
            return 0

        summary = summarize_with_llm(transcript, meta, transcript_source, args.max_chars, prompt_template)
        summary_path = output_dir / "summary.md"
        summary_path.write_text(summary.strip() + "\n", encoding="utf-8")
        if args.json:
            print_json(
                {
                    "ok": True,
                    "output_dir": str(output_dir),
                    "transcript": str(transcript_path),
                    "summary": str(summary_path),
                    "transcript_source": transcript_source,
                    "warnings": warnings,
                }
            )
        else:
            print(f"已生成总结：{summary_path}")
        return 0
    except AppError as exc:
        if output_dir and transcript_path and transcript_path.exists() and not prompt_path:
            try:
                prompt_path = write_chatgpt_prompt(
                    transcript_path.read_text(encoding="utf-8"),
                    meta or VideoMeta(title="video", id="video", webpage_url=""),
                    transcript_source or "unknown",
                    output_dir,
                    args.max_chars,
                    prompt_template,
                )
                exc.hints.append(f"已自动生成 ChatGPT 提示词，可复制到 ChatGPT：{prompt_path}")
            except Exception:
                pass
        if args.json:
            print_json(
                {
                    "ok": False,
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "hints": exc.hints,
                    },
                    "output_dir": str(output_dir) if output_dir else None,
                    "transcript": str(transcript_path) if transcript_path else None,
                    "prompt": str(prompt_path) if prompt_path else None,
                    "warnings": warnings,
                }
            )
        else:
            print(f"错误：{exc.message}", file=sys.stderr)
            for hint in exc.hints:
                print(f"提示：{hint}", file=sys.stderr)
        return 1
    except Exception as exc:
        if output_dir and transcript_path and transcript_path.exists() and not prompt_path:
            try:
                prompt_path = write_chatgpt_prompt(
                    transcript_path.read_text(encoding="utf-8"),
                    meta or VideoMeta(title="video", id="video", webpage_url=""),
                    transcript_source or "unknown",
                    output_dir,
                    args.max_chars,
                    prompt_template,
                )
            except Exception:
                pass
        if args.json:
            print_json(
                {
                    "ok": False,
                    "error": {
                        "code": "unexpected_error",
                        "message": str(exc),
                        "hints": [],
                    },
                    "output_dir": str(output_dir) if output_dir else None,
                    "transcript": str(transcript_path) if transcript_path else None,
                    "prompt": str(prompt_path) if prompt_path else None,
                    "warnings": warnings,
                }
            )
        else:
            print(f"错误：{exc}", file=sys.stderr)
            if prompt_path:
                print(f"提示：已自动生成 ChatGPT 提示词，可复制到 ChatGPT：{prompt_path}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
