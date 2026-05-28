from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv


SUBTITLE_EXTS = (".srt", ".vtt", ".ass", ".json3", ".srv1", ".srv2", ".srv3", ".ttml")
AUDIO_EXTS = (".mp3", ".m4a", ".opus", ".wav", ".webm")
DEFAULT_COOKIES_PATH = "/app/cookies/cookies.txt"
GENERATED_OUTPUT_FILES = ("transcript.txt", "summary.md", "chatgpt_prompt.md")
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
文本质量提醒：{transcript_quality_note}

请按以下格式输出：

## 视频主题
用一段话概括视频整体内容。

## 核心观点

## 分段要点
按转写文本中的时间节点组织，每个节点写出稍详细的内容要点。只能使用原文已有的时间节点，不要自行编造时间。

## 重要结论

## 可执行建议

这是第 {chunk_index}/{chunk_count} 段转写文本：

{transcript}
"""
CHUNK_SUMMARY_TEMPLATE = """你是严谨的视频内容总结助手。请只基于给定转写文本，提取这一段的结构化要点，不要编造原文没有的信息。

视频标题：{title}
文本来源：{source}
文本质量提醒：{transcript_quality_note}

请输出：

## 本段主题
用一两句话概括这一段内容。

## 本段时间节点与要点
按转写文本中的时间节点组织，保留原文已有时间节点；每个节点写出稍详细的事实、观点、步骤或例子。不要自行编造时间。

## 本段重要细节
列出后续合并总结时不能丢失的关键细节、术语、工具名、参数或限制。

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
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
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


def string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


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
        description=data.get("description") if isinstance(data.get("description"), str) else None,
        tags=string_list(data.get("tags")),
        categories=string_list(data.get("categories")),
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
    processing: dict | None = None,
    options: dict | None = None,
    whisper_context_terms: list[str] | None = None,
) -> None:
    data = asdict(meta)
    data.pop("description", None)
    data.pop("tags", None)
    data.pop("categories", None)
    data["transcript_source"] = transcript_source
    data["warnings"] = warnings or []
    if whisper_context_terms is not None:
        data["whisper_context_terms"] = whisper_context_terms
    if processing is not None:
        data["processing"] = processing
    if options is not None:
        data["options"] = options
    (output_dir / "meta.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def clean_generated_outputs(output_dir: Path) -> None:
    for name in GENERATED_OUTPUT_FILES:
        (output_dir / name).unlink(missing_ok=True)


def mark_stage(processing: dict, stage: str, started_at: float) -> None:
    processing.setdefault("stages", {})[stage] = round(time.monotonic() - started_at, 3)


def mark_total(processing: dict, started_at: float) -> None:
    processing["total_seconds"] = round(time.monotonic() - started_at, 3)


def build_processing_info(args: argparse.Namespace, whisper_language: str | None) -> tuple[dict, dict]:
    processing = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "stages": {},
    }
    options = {
        "model_size": args.model_size,
        "language": args.language,
        "normalized_language": whisper_language,
        "sub_langs": args.sub_langs,
        "max_chars": args.max_chars,
        "force_transcribe": args.force_transcribe,
        "keep_audio": args.keep_audio,
        "no_llm": args.no_llm,
        "export_prompt": args.export_prompt or bool(args.summary_from_file),
        "summary_from_file": bool(args.summary_from_file),
    }
    return processing, options


def maybe_warn_language(meta: VideoMeta, requested_language: str | None, warnings: list[str]) -> None:
    if requested_language != "zh":
        return
    latin_terms = re.findall(r"[A-Za-z][A-Za-z0-9.+_-]*", meta.title)
    if len(latin_terms) >= 3:
        warnings.append("标题包含较多英文术语；如果视频主要是英文，建议使用 --language en 或 --language auto 提升 Whisper 识别质量。")


def normalize_context_term(value: str) -> str:
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n,，。.!！?？:：;；()（）[]【】<>《》\"'")
    return value.strip()


def add_context_term(terms: list[str], seen: set[str], value: str, max_total_chars: int) -> None:
    term = normalize_context_term(value)
    if len(term) < 2 or len(term) > 60:
        return
    key = term.casefold()
    if key in seen:
        return
    if any(key != item.casefold() and key in item.casefold() for item in terms):
        return
    current_len = sum(len(item) + 2 for item in terms)
    if current_len + len(term) > max_total_chars:
        return
    terms.append(term)
    seen.add(key)


def extract_context_terms_from_text(text: str, terms: list[str], seen: set[str], max_total_chars: int) -> None:
    if not text:
        return

    # 优先抓取标题和简介中显式出现的英文、数字、连字符、版本号等专有词。
    patterns = [
        r"\b[A-Za-z][A-Za-z0-9]*(?:[.+_-][A-Za-z0-9]+)+\b",
        r"\b[A-Za-z]*[A-Z][A-Za-z0-9]*(?:\s+[A-Za-z]*[A-Z][A-Za-z0-9]*){0,2}\b",
        r"\b[A-Za-z]+(?:\s+[A-Z][A-Za-z0-9]+){1,2}\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            add_context_term(terms, seen, match.group(0), max_total_chars)


def build_whisper_context_terms(meta: VideoMeta, max_total_chars: int = 800) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()

    for text in (meta.title, meta.uploader or "", meta.description or ""):
        extract_context_terms_from_text(text, terms, seen, max_total_chars)

    # tags/categories 本身通常已经是平台给出的关键词，保守收取短项即可。
    for item in [*meta.tags[:12], *meta.categories[:8]]:
        if len(item) <= 30:
            add_context_term(terms, seen, item, max_total_chars)

    return terms


def build_whisper_initial_prompt(terms: list[str]) -> str | None:
    if not terms:
        return None
    return "视频元数据中出现的关键词包括：" + "、".join(terms)


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
    if suffix == ".srt":
        return clean_srt(path)
    if suffix == ".vtt":
        return clean_vtt(path)
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


def format_timestamp(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def parse_subtitle_timestamp(value: str) -> str | None:
    match = re.match(r"(?:(\d+):)?(\d{1,2}):(\d{2})(?:[,.](\d{1,3}))?", value.strip())
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def clean_subtitle_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"\{\\.*?\}", "", value)
    return re.sub(r"\s+", " ", value).strip()


def format_timed_line(timestamp: str, text: str) -> str:
    return f"[{timestamp}] {text}"


def clean_srt(path: Path) -> str:
    content = path.read_text(encoding="utf-8-sig", errors="ignore").replace("\r\n", "\n")
    entries: list[str] = []
    for block in re.split(r"\n{2,}", content):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        time_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if time_index is None:
            continue
        timestamp = parse_subtitle_timestamp(lines[time_index].split("-->", 1)[0])
        text = clean_subtitle_text(" ".join(lines[time_index + 1 :]))
        if timestamp and text:
            entries.append(format_timed_line(timestamp, text))
    return normalize_lines(entries)


def clean_vtt(path: Path) -> str:
    content = path.read_text(encoding="utf-8-sig", errors="ignore").replace("\r\n", "\n")
    entries: list[str] = []
    for block in re.split(r"\n{2,}", content):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines or lines[0].startswith("WEBVTT"):
            continue
        time_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if time_index is None:
            continue
        timestamp = parse_subtitle_timestamp(lines[time_index].split("-->", 1)[0])
        # VTT cue settings 跟在时间行后，清洗文本时只保留字幕正文。
        text = clean_subtitle_text(" ".join(lines[time_index + 1 :]))
        if timestamp and text:
            entries.append(format_timed_line(timestamp, text))
    return normalize_lines(entries)


def clean_json3(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    lines: list[str] = []
    for event in data.get("events", []):
        parts = event.get("segs") or []
        text = clean_subtitle_text("".join(part.get("utf8", "") for part in parts))
        start_ms = event.get("tStartMs")
        if text and isinstance(start_ms, int):
            lines.append(format_timed_line(format_timestamp(start_ms / 1000), text))
        elif text:
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


def transcribe_audio(
    audio_path: Path,
    model_size: str,
    language: str | None = None,
    initial_prompt: str | None = None,
) -> str:
    from faster_whisper import WhisperModel

    # 使用 auto 让同一镜像可以兼容 CPU/GPU 环境；模型大小由 CLI 参数控制。
    model = WhisperModel(model_size, device="auto", compute_type="auto")
    segments, _info = model.transcribe(
        str(audio_path),
        language=language,
        vad_filter=True,
        initial_prompt=initial_prompt,
    )
    lines = [
        format_timed_line(format_timestamp(segment.start), segment.text.strip())
        for segment in segments
        if segment.text.strip()
    ]
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


def base_transcript_source(source: str) -> str:
    suffix = "_partials"
    return source[: -len(suffix)] if source.endswith(suffix) else source


def transcript_quality_note(source: str) -> str:
    source = base_transcript_source(source)
    if source == "whisper":
        return (
            "本文本由 Whisper 音频转写生成，可能存在专有名词、英文术语、产品名、人名、数字、标点和断句错误。"
            "请结合上下文只修正明显误识别；无法确定时保守表述，不要编造原文没有的信息。"
        )
    if source == "auto_subtitle":
        return (
            "本文本来自平台自动字幕，可能存在自动识别错误。"
            "请结合上下文只修正明显误识别；无法确定时保守表述，不要编造原文没有的信息。"
        )
    if source == "manual_subtitle":
        return "本文本来自人工字幕，通常较可靠，但仍可能存在错字、漏字或排版问题；请只基于原文总结。"
    if source == "file":
        return "本文本来自已有转写稿，来源质量未知；如遇疑似识别错误，请结合上下文保守理解，不要编造。"
    return "转写文本可能存在识别、清洗或断句错误；请结合上下文保守总结，不要编造原文没有的信息。"


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
        "transcript_quality_note": transcript_quality_note(source),
    }
    try:
        return template.format(**values)
    except KeyError as exc:
        raise AppError(
            "invalid_prompt_template",
            f"prompt 模板包含未知变量：{exc.args[0]}",
            [
                "支持变量：{title}、{source}、{transcript}、{chunk_index}、{chunk_count}、"
                "{webpage_url}、{uploader}、{duration}、{transcript_quality_note}"
            ],
        ) from exc


def render_chunk_summary_prompt(
    transcript: str,
    meta: VideoMeta,
    source: str,
    chunk_index: int,
    chunk_count: int,
) -> str:
    return render_prompt_template(
        CHUNK_SUMMARY_TEMPLATE,
        transcript,
        meta,
        source,
        chunk_index,
        chunk_count,
    )


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
    if len(chunks) == 1:
        prompt = render_prompt_template(prompt_template, chunks[0], meta, source, 1, 1)
        try:
            return call_llm(client, model, prompt)
        except Exception as exc:
            raise classify_llm_error(exc) from exc

    partials: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        prompt = render_chunk_summary_prompt(
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

    # 多段长文本先提取细节，再由最终模板合并，减少直接二次压缩造成的要点丢失。
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
    parser.add_argument("--language", default="zh", help="Whisper 识别语言；中文视频用 zh，英文视频建议 en，不确定可用 auto；默认 zh")
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
    run_started = time.monotonic()
    load_dotenv()
    args = parse_args()
    prompt_template = load_prompt_template(args.prompt, args.prompt_file)
    whisper_language = normalize_language(args.language)
    processing, options = build_processing_info(args, whisper_language)
    output_dir: Path | None = None
    transcript_path: Path | None = None
    prompt_path: Path | None = None
    summary_path: Path | None = None
    meta: VideoMeta | None = None
    transcript_source: str | None = None
    whisper_context_terms: list[str] | None = None
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
            clean_generated_outputs(output_dir)
            transcript_path = output_dir / "transcript.txt"
            transcript_path.write_text(transcript, encoding="utf-8")
            transcript_source = "file"
            write_meta(meta, output_dir, transcript_source, warnings, processing, options)
            stage_started = time.monotonic()
            prompt_path = write_chatgpt_prompt(transcript, meta, "file", output_dir, args.max_chars, prompt_template)
            mark_stage(processing, "export_prompt", stage_started)
            mark_total(processing, run_started)
            write_meta(meta, output_dir, transcript_source, warnings, processing, options)
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

        stage_started = time.monotonic()
        meta = load_local_meta(local_input) if is_local_file else load_meta(args.url, output_root, cookies)
        mark_stage(processing, "load_meta", stage_started)
        maybe_warn_language(meta, args.language, warnings)
        output_dir = output_root / (safe_name(meta.title) if is_local_file else safe_name(f"{meta.id}-{meta.title}"))
        output_dir.mkdir(parents=True, exist_ok=True)
        clean_generated_outputs(output_dir)
        write_meta(meta, output_dir, None, warnings, processing, options)

        subtitle_path = None
        if is_local_file:
            stage_started = time.monotonic()
            whisper_context_terms = build_whisper_context_terms(meta)
            transcript = transcribe_audio(
                local_input,
                args.model_size,
                whisper_language,
                build_whisper_initial_prompt(whisper_context_terms),
            )
            mark_stage(processing, "transcribe", stage_started)
            transcript_source = "whisper"
        elif not args.force_transcribe:
            stage_started = time.monotonic()
            subtitle_info = download_subtitle(args.url, output_dir, cookies, args.sub_langs, meta)
            mark_stage(processing, "download_subtitle", stage_started)
            if subtitle_info:
                subtitle_path, transcript_source, subtitle_warnings = subtitle_info
                warnings.extend(subtitle_warnings)

        if not is_local_file and subtitle_path:
            stage_started = time.monotonic()
            transcript = clean_subtitle(subtitle_path)
            mark_stage(processing, "clean_subtitle", stage_started)
            if not transcript.strip():
                print("字幕文件为空，将尝试音频转写。", file=sys.stderr)
                warnings.append("字幕文件为空，已回退到 Whisper 转写。")
                subtitle_path = None

        if not is_local_file and not subtitle_path:
            stage_started = time.monotonic()
            audio_path = download_audio(args.url, output_dir, cookies)
            mark_stage(processing, "download_audio", stage_started)
            stage_started = time.monotonic()
            whisper_context_terms = build_whisper_context_terms(meta)
            transcript = transcribe_audio(
                audio_path,
                args.model_size,
                whisper_language,
                build_whisper_initial_prompt(whisper_context_terms),
            )
            mark_stage(processing, "transcribe", stage_started)
            transcript_source = "whisper"
            if not args.keep_audio:
                audio_path.unlink(missing_ok=True)

        transcript_path = output_dir / "transcript.txt"
        transcript_path.write_text(transcript, encoding="utf-8")
        write_meta(meta, output_dir, transcript_source, warnings, processing, options, whisper_context_terms)

        if args.export_prompt:
            stage_started = time.monotonic()
            prompt_path = write_chatgpt_prompt(transcript, meta, transcript_source, output_dir, args.max_chars, prompt_template)
            mark_stage(processing, "export_prompt", stage_started)
            mark_total(processing, run_started)
            write_meta(meta, output_dir, transcript_source, warnings, processing, options, whisper_context_terms)
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
            mark_total(processing, run_started)
            write_meta(meta, output_dir, transcript_source, warnings, processing, options, whisper_context_terms)
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

        stage_started = time.monotonic()
        summary = summarize_with_llm(transcript, meta, transcript_source, args.max_chars, prompt_template)
        mark_stage(processing, "summarize", stage_started)
        summary_path = output_dir / "summary.md"
        summary_path.write_text(summary.strip() + "\n", encoding="utf-8")
        mark_total(processing, run_started)
        write_meta(meta, output_dir, transcript_source, warnings, processing, options, whisper_context_terms)
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
                stage_started = time.monotonic()
                prompt_path = write_chatgpt_prompt(
                    transcript_path.read_text(encoding="utf-8"),
                    meta or VideoMeta(title="video", id="video", webpage_url=""),
                    transcript_source or "unknown",
                    output_dir,
                    args.max_chars,
                    prompt_template,
                )
                mark_stage(processing, "export_prompt", stage_started)
                exc.hints.append(f"已自动生成 ChatGPT 提示词，可复制到 ChatGPT：{prompt_path}")
            except Exception:
                pass
        if output_dir and meta:
            try:
                mark_total(processing, run_started)
                write_meta(meta, output_dir, transcript_source, warnings, processing, options, whisper_context_terms)
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
                stage_started = time.monotonic()
                prompt_path = write_chatgpt_prompt(
                    transcript_path.read_text(encoding="utf-8"),
                    meta or VideoMeta(title="video", id="video", webpage_url=""),
                    transcript_source or "unknown",
                    output_dir,
                    args.max_chars,
                    prompt_template,
                )
                mark_stage(processing, "export_prompt", stage_started)
            except Exception:
                pass
        if output_dir and meta:
            try:
                mark_total(processing, run_started)
                write_meta(meta, output_dir, transcript_source, warnings, processing, options, whisper_context_terms)
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
