from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable
from urllib import error as urllib_error
from urllib import request as urllib_request

from dotenv import load_dotenv


SUBTITLE_EXTS = (".srt", ".vtt", ".ass", ".json3", ".srv1", ".srv2", ".srv3", ".ttml")
AUDIO_EXTS = (".mp3", ".m4a", ".opus", ".wav", ".webm")
VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".webm", ".flv", ".avi", ".m4v")
DEFAULT_COOKIES_PATH = "/app/cookies/cookies.txt"
GENERATED_OUTPUT_FILES = (
    "transcript.txt",
    "summary.md",
    "chatgpt_prompt.md",
    "visual_context.json",
    "visual_context.md",
    "multimodal_context.txt",
)
CACHE_FILE_NAME = "pipeline_cache.json"
CACHE_SCHEMA_VERSION = 1
MIN_VISUAL_CONTEXT_CHARS = 800
MAX_VISUAL_CONTEXT_CHARS = 3200
VISUAL_CONTEXT_RATIO = 0.25
DEFAULT_TRUNCATION_RETRY_TOKENS = 2600
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
先扫描完整分段，再用 6–10 条覆盖开头、中间和结尾。合并相邻时间节点，保留原文已有时间；
每条简洁写出事实、观点、步骤或例子，不要把输出预算都用在前半段，也不要自行编造时间。

## 本段重要细节
简洁列出后续合并总结时不能丢失的关键细节、术语、工具名、参数或限制。

这是第 {chunk_index}/{chunk_count} 段转写文本：

{transcript}
"""
MULTIMODAL_PROMPT_TEMPLATE = """你是严谨的视频内容总结助手。请只基于给定的视频材料，用中文输出一份视频总结，不要编造材料没有的信息。

视频标题：{title}
材料来源：{source}
材料质量提醒：{transcript_quality_note}

材料同时包含“语音/字幕”和本地视觉模型生成的“画面”记录，两类信息都是理解视频的重要证据。语音/字幕主要承载讲述逻辑、观点和因果关系；画面主要承载屏幕文字、PPT、代码、图表、操作步骤和场景变化。核心观点和分段要点必须综合两类证据，不能把画面只当作可忽略的附录。两者冲突时不要擅自猜测，应保守表述并指出不确定性。命令、URL、包名和版本号必须逐字来自材料；无法确认时不要补全或改写。

请按以下格式输出：

## 视频主题
用一段话概括视频整体内容。

## 核心观点

## 分段要点
按材料中的时间节点组织，每个节点写出稍详细的内容要点。只能使用材料已有的时间节点，不要自行编造时间。

## 关键画面信息
列出画面中对理解视频有关键作用的信息，并避免重复前文。没有则写“无”。

## 重要结论

## 可执行建议

这是第 {chunk_index}/{chunk_count} 段视频材料：

{transcript}
"""
MULTIMODAL_CHUNK_SUMMARY_TEMPLATE = """你是严谨的视频内容总结助手。请只基于给定的视频材料提取结构化要点，不要编造材料没有的信息。

视频标题：{title}
材料来源：{source}
材料质量提醒：{transcript_quality_note}

材料包含按时间排列的语音/字幕和画面描述，两类信息都是重要证据。语音/字幕主要承载讲述逻辑、观点和因果关系；画面主要承载屏幕文字、PPT、代码、图表、操作步骤和场景变化。时间节点与要点必须综合两类证据。
先扫描完整分段，再用 6–10 条合并相邻时间点，必须覆盖分段的开头、中间和结尾，不要把输出预算都用在前半段。

请输出：

## 本段主题

## 本段时间节点与要点

## 本段关键画面信息

## 本段重要细节

这是第 {chunk_index}/{chunk_count} 段视频材料：

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
    selected_subtitle_lang: str | None = None
    selected_subtitle_type: str | None = None


@dataclass
class CookieConfig:
    file: str | None = None
    browser: str | None = None


@dataclass
class TranscriptInfo:
    text: str
    source: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class FrameCandidate:
    path: Path
    timestamp: float
    source: str
    hash_value: int | None = None


@dataclass
class VisionConfig:
    api_key: str
    base_url: str
    model: str
    timeout: float


@dataclass
class VisionSamplingPlan:
    scan_interval: float
    max_gap: float
    max_frames: int


@dataclass
class SummaryConfig:
    provider: str
    api_key: str
    base_url: str
    model: str
    timeout: float
    chunk_chars: int | None = None
    context_length: int | None = None
    model_context_length: int | None = None


@dataclass
class SummaryResult:
    text: str
    chunk_chars: int
    chunks: int
    chunk_lengths: list[int]
    context_length: int | None
    fallback_used: bool = False


class AppError(RuntimeError):
    def __init__(self, code: str, message: str, hints: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hints = hints or []


class LLMOutputTruncatedError(RuntimeError):
    pass


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


def cookie_cache_identity(cookies: CookieConfig | None) -> dict | None:
    if not cookies:
        return None
    if cookies.file:
        path = Path(cookies.file).expanduser().resolve()
        stat = path.stat()
        # Cookie 内容属于凭据，只使用文件元数据判断同路径文件是否已更新。
        return {
            "type": "file",
            "path": str(path),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    if cookies.browser:
        return {"type": "browser", "browser": cookies.browser}
    return None


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
        duration=probe_media_duration(path),
    )


def probe_media_duration(path: Path) -> float | None:
    if not shutil.which("ffprobe"):
        return None
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    try:
        duration = float(result.stdout.strip())
    except ValueError:
        return None
    return duration if duration > 0 else None


def build_vision_sampling_plan(
    duration: float | None,
    minimum_scan_interval: float,
    maximum_gap: float,
    maximum_frames: int,
) -> VisionSamplingPlan:
    """根据时长控制候选密度和推理预算，CLI 参数仍作为用户边界。"""
    if not duration or duration <= 0:
        return VisionSamplingPlan(
            scan_interval=minimum_scan_interval,
            max_gap=maximum_gap,
            max_frames=min(maximum_frames, 16),
        )

    desired_frames = max(8, math.floor(duration / 30) + 1)
    frame_budget = min(maximum_frames, desired_frames)
    scan_interval = max(minimum_scan_interval, duration / max(frame_budget * 4, 1))
    coverage_gap = min(maximum_gap, max(15, duration / max(frame_budget - 1, 1)))
    return VisionSamplingPlan(
        scan_interval=round(scan_interval, 3),
        max_gap=round(coverage_gap, 3),
        max_frames=frame_budget,
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
        data["analysis_mode"] = options.get("analysis_mode")
        data["options"] = options
    (output_dir / "meta.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def clean_generated_outputs(output_dir: Path, *, clear_cache: bool = False) -> None:
    for name in GENERATED_OUTPUT_FILES:
        (output_dir / name).unlink(missing_ok=True)
    if clear_cache:
        (output_dir / CACHE_FILE_NAME).unlink(missing_ok=True)


def load_pipeline_cache(output_dir: Path) -> dict:
    path = output_dir / CACHE_FILE_NAME
    if not path.is_file():
        return {"schema_version": CACHE_SCHEMA_VERSION, "stages": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": CACHE_SCHEMA_VERSION, "stages": {}}
    if payload.get("schema_version") != CACHE_SCHEMA_VERSION or not isinstance(payload.get("stages"), dict):
        return {"schema_version": CACHE_SCHEMA_VERSION, "stages": {}}
    return payload


def save_pipeline_cache(output_dir: Path, cache: dict) -> None:
    cache["schema_version"] = CACHE_SCHEMA_VERSION
    (output_dir / CACHE_FILE_NAME).write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def stable_signature(payload: dict) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def source_signature(meta: VideoMeta, local_path: Path | None = None) -> str:
    payload: dict = {
        "id": meta.id,
        "url": meta.webpage_url,
        "duration": meta.duration,
    }
    if local_path and local_path.is_file():
        stat = local_path.stat()
        payload["local_size"] = stat.st_size
        payload["local_mtime_ns"] = stat.st_mtime_ns
    return stable_signature(payload)


def cache_stage_matches(cache: dict, stage: str, signature: str) -> bool:
    item = cache.get("stages", {}).get(stage)
    return isinstance(item, dict) and item.get("signature") == signature


def update_cache_stage(cache: dict, stage: str, signature: str, **values) -> None:
    cache.setdefault("stages", {})[stage] = {
        "signature": signature,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        **values,
    }


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
        "analysis_mode": "multimodal" if args.with_vision else "quick",
        "model_size": args.model_size,
        "language": args.language,
        "normalized_language": whisper_language,
        "sub_langs": args.sub_langs,
        "max_chars": args.max_chars,
        "summary_provider": args.summary_provider,
        "force_transcribe": args.force_transcribe,
        "keep_audio": args.keep_audio,
        "resume": args.resume,
        "no_llm": args.no_llm,
        "export_prompt": args.export_prompt or bool(args.summary_from_file),
        "summary_from_file": bool(args.summary_from_file),
        "with_vision": args.with_vision,
        "vision_scan_interval": args.vision_scan_interval,
        "vision_max_gap": args.vision_max_gap,
        "vision_max_frames": args.vision_max_frames,
        "vision_batch_size": args.vision_batch_size,
        "vision_frame_width": args.vision_frame_width,
    }
    return processing, options


def validate_args(args: argparse.Namespace) -> None:
    if args.with_vision and (args.no_llm or args.export_prompt or args.summary_from_file):
        raise AppError(
            "invalid_arguments",
            "--with-vision 暂不能与 --no-llm、--export-prompt 或 --summary-from-file 同时使用。",
        )
    positive_values = {
        "--max-chars": args.max_chars,
        "--vision-scan-interval": args.vision_scan_interval,
        "--vision-max-gap": args.vision_max_gap,
        "--vision-max-frames": args.vision_max_frames,
        "--vision-batch-size": args.vision_batch_size,
        "--vision-frame-width": args.vision_frame_width,
    }
    invalid = [name for name, value in positive_values.items() if value <= 0]
    if invalid:
        raise AppError("invalid_arguments", f"{', '.join(invalid)} 必须大于 0。")


def resolve_vision_config() -> VisionConfig:
    base_url = (os.getenv("VISION_BASE_URL") or "http://host.docker.internal:11434/v1").rstrip("/")
    model = os.getenv("VISION_MODEL") or "qwen3-vl:8b-instruct-q4_K_M"
    api_key = os.getenv("VISION_API_KEY") or "ollama"
    timeout_text = os.getenv("VISION_TIMEOUT") or "300"
    try:
        timeout = float(timeout_text)
    except ValueError as exc:
        raise AppError("invalid_vision_config", "VISION_TIMEOUT 必须是数字。") from exc
    if timeout <= 0:
        raise AppError("invalid_vision_config", "VISION_TIMEOUT 必须大于 0。")
    return VisionConfig(api_key=api_key, base_url=base_url, model=model, timeout=timeout)


def resolve_summary_config(provider: str) -> SummaryConfig:
    if provider not in {"ollama", "api"}:
        raise AppError("invalid_summary_config", "SUMMARY_PROVIDER 只能是 ollama 或 api。")
    timeout_text = os.getenv("SUMMARY_TIMEOUT") or "300"
    try:
        timeout = float(timeout_text)
    except ValueError as exc:
        raise AppError("invalid_summary_config", "SUMMARY_TIMEOUT 必须是数字。") from exc
    if timeout <= 0:
        raise AppError("invalid_summary_config", "SUMMARY_TIMEOUT 必须大于 0。")

    if provider == "ollama":
        chunk_chars_text = (os.getenv("SUMMARY_LOCAL_CHUNK_CHARS") or "").strip()
        chunk_chars = None
        if chunk_chars_text and chunk_chars_text.lower() != "auto":
            try:
                chunk_chars = int(chunk_chars_text)
            except ValueError as exc:
                raise AppError(
                    "invalid_summary_config",
                    "SUMMARY_LOCAL_CHUNK_CHARS 必须是整数或 auto。",
                ) from exc
            if chunk_chars <= 0:
                raise AppError("invalid_summary_config", "SUMMARY_LOCAL_CHUNK_CHARS 必须大于 0。")

        context_length_text = os.getenv("SUMMARY_OLLAMA_CONTEXT_LENGTH") or "8192"
        try:
            context_length = int(context_length_text)
        except ValueError as exc:
            raise AppError("invalid_summary_config", "SUMMARY_OLLAMA_CONTEXT_LENGTH 必须是整数。") from exc
        if context_length <= 0:
            raise AppError("invalid_summary_config", "SUMMARY_OLLAMA_CONTEXT_LENGTH 必须大于 0。")
        return SummaryConfig(
            provider=provider,
            api_key=os.getenv("SUMMARY_API_KEY") or "ollama",
            base_url=(
                os.getenv("SUMMARY_BASE_URL")
                or os.getenv("VISION_BASE_URL")
                or "http://host.docker.internal:11434/v1"
            ).rstrip("/"),
            model=(
                os.getenv("SUMMARY_MODEL")
                or os.getenv("VISION_MODEL")
                or "qwen3-vl:8b-instruct-q4_K_M"
            ),
            timeout=timeout,
            chunk_chars=chunk_chars,
            context_length=context_length,
        )

    api_key = os.getenv("OPENAI_API_KEY") or ""
    if not api_key:
        raise AppError(
            "missing_openai_api_key",
            "选择 API 总结时必须设置 OPENAI_API_KEY。",
            ["也可以使用默认的 --summary-provider ollama 在本机完成总结。"],
        )
    return SummaryConfig(
        provider=provider,
        api_key=api_key,
        base_url=(os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/"),
        model=os.getenv("OPENAI_MODEL") or "gpt-4.1-mini",
        timeout=timeout,
    )


def create_summary_client(config: SummaryConfig):
    from openai import OpenAI

    client = OpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=config.timeout,
    )
    if config.provider != "ollama":
        return client

    try:
        models = client.models.list()
    except Exception as exc:
        raise AppError(
            "summary_backend_unavailable",
            f"无法连接本地总结服务：{exc}",
            [f"确认 Ollama 已启动，并能从当前运行环境访问 {config.base_url}。"],
        ) from exc
    available = {item.id for item in models.data}
    if config.model not in available:
        raise AppError(
            "summary_model_not_found",
            f"Ollama 中未找到总结模型：{config.model}",
            [f"请先在宿主机执行：ollama pull {config.model}"],
        )
    model_info = ollama_request(config, "/api/show", {"model": config.model})
    context_values = [
        value
        for key, value in (model_info.get("model_info") or {}).items()
        if "context_length" in key.lower() and isinstance(value, int)
    ]
    if context_values:
        config.model_context_length = max(context_values)
        if config.context_length:
            config.context_length = min(config.context_length, config.model_context_length)
    return client


def ollama_native_base_url(config: SummaryConfig) -> str:
    return config.base_url[:-3] if config.base_url.endswith("/v1") else config.base_url


def ollama_request(config: SummaryConfig, path: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib_request.Request(
        ollama_native_base_url(config) + path,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=config.timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama HTTP {exc.code}: {detail}") from exc
    except (urllib_error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Ollama 请求失败：{exc}") from exc


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


def subtitle_language_from_path(path: Path) -> str | None:
    return path.stem.rsplit(".", 1)[-1] if "." in path.stem else None


def subtitle_language_priority(language: str | None, langs: str) -> int:
    patterns = [
        item.strip()
        for item in langs.split(",")
        if item.strip() and not item.strip().startswith("-")
    ]
    if not language:
        return len(patterns)
    for index, pattern in enumerate(patterns):
        if pattern == "all":
            return index
        try:
            if re.fullmatch(pattern, language):
                return index
        except re.error:
            if pattern == language:
                return index
    return len(patterns)


def subtitle_type_priority(language: str | None, meta: VideoMeta) -> int:
    if not language:
        return 2
    if language.lower().startswith("ai-"):
        return 1
    if language in meta.subtitle_langs:
        return 0
    if language in meta.automatic_caption_langs:
        return 1
    return 2


def select_subtitle_candidate(
    candidates: Iterable[Path],
    langs: str,
    meta: VideoMeta,
) -> Path | None:
    ranked = sorted(
        candidates,
        key=lambda path: (
            subtitle_language_priority(subtitle_language_from_path(path), langs),
            subtitle_type_priority(subtitle_language_from_path(path), meta),
            path.name,
        ),
    )
    return ranked[0] if ranked else None


def detect_subtitle_source(path: Path, meta: VideoMeta) -> tuple[str, list[str]]:
    name = path.name
    warnings: list[str] = []
    language = subtitle_language_from_path(path)
    if language:
        meta.selected_subtitle_lang = language

    # B 站 AI 字幕使用 ai-* 语言代码；实际下载文件比元数据接口更可信。
    if language and language.lower().startswith("ai-"):
        if language not in meta.automatic_caption_langs:
            meta.automatic_caption_langs.append(language)
            meta.automatic_caption_langs.sort()
        meta.selected_subtitle_type = "automatic"
        warnings.append(f"使用的是平台自动字幕（{language}），内容可能存在识别错误。")
        return "auto_subtitle", warnings

    for lang in meta.subtitle_langs:
        if f".{lang}." in name or name.endswith(f".{lang}{path.suffix}"):
            meta.selected_subtitle_lang = lang
            meta.selected_subtitle_type = "manual"
            return "manual_subtitle", warnings
    for lang in meta.automatic_caption_langs:
        if f".{lang}." in name or name.endswith(f".{lang}{path.suffix}"):
            meta.selected_subtitle_lang = lang
            meta.selected_subtitle_type = "automatic"
            warnings.append(f"使用的是平台自动字幕（{lang}），内容可能存在识别错误。")
            return "auto_subtitle", warnings
    if language:
        if language not in meta.subtitle_langs:
            meta.subtitle_langs.append(language)
            meta.subtitle_langs.sort()
        meta.selected_subtitle_type = "manual"
        return "manual_subtitle", warnings
    warnings.append("已使用字幕文件，但无法确认是人工字幕还是自动字幕。")
    return "subtitle", warnings


def restore_cached_subtitle_metadata(
    transcript_stage: dict, meta: VideoMeta, warnings: list[str]
) -> None:
    meta.selected_subtitle_lang = transcript_stage.get("selected_subtitle_lang")
    meta.selected_subtitle_type = transcript_stage.get("selected_subtitle_type")
    if not meta.selected_subtitle_lang or meta.selected_subtitle_type != "automatic":
        return

    language = meta.selected_subtitle_lang
    if language not in meta.automatic_caption_langs:
        meta.automatic_caption_langs.append(language)
        meta.automatic_caption_langs.sort()
    warning = f"使用的是平台自动字幕（{language}），内容可能存在识别错误。"
    if warning not in warnings:
        warnings.append(warning)


def download_subtitle(
    url: str,
    output_dir: Path,
    cookies: CookieConfig | None,
    langs: str,
    meta: VideoMeta,
    warnings: list[str] | None = None,
) -> tuple[Path, str, list[str]] | None:
    require_tool("yt-dlp")
    output_prefix = f"subtitle-source-{time.time_ns()}"
    args = with_cookies(
        [
            "yt-dlp",
            "--skip-download",
            "--write-subs",
            "--write-auto-subs",
            "--force-overwrites",
            "--sub-langs",
            langs,
            "--convert-subs",
            "srt",
            "--no-playlist",
            "-o",
            f"{output_prefix}.%(ext)s",
            url,
        ],
        cookies,
    )

    result = run_command(args, output_dir)
    candidates = [
        path
        for path in output_dir.glob(f"{output_prefix}.*")
        if path.suffix.lower() in SUBTITLE_EXTS
    ]
    selected = select_subtitle_candidate(candidates, langs, meta)
    if selected:
        source, warnings = detect_subtitle_source(selected, meta)
        return selected, source, warnings

    login_required = "Subtitles are only available when logged in" in result.stderr
    if login_required:
        message = "B 站字幕需要登录态，当前未获得可用字幕；已回退到 Whisper 转写。"
        if warnings is not None and message not in warnings:
            warnings.append(message)
        print(message, file=sys.stderr)
        print("提示：请将有效 cookies.txt 放到 cookies/ 目录后重试。", file=sys.stderr)
    elif result.returncode != 0:
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


def download_video(url: str, output_dir: Path, cookies: CookieConfig | None) -> Path:
    require_tool("yt-dlp")
    require_tool("ffmpeg")

    output_prefix = f"vision-source-{time.time_ns()}"
    args = with_cookies(
        [
            "yt-dlp",
            "--no-playlist",
            "--force-overwrites",
            "-f",
            "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
            "--merge-output-format",
            "mp4",
            "-o",
            f"{output_prefix}.%(ext)s",
            url,
        ],
        cookies,
    )
    result = run_command(args, output_dir)
    if result.returncode != 0:
        error = classify_command_error(result.stderr, url, "视频下载")
        cleanup_video_download_artifacts(output_dir, output_prefix)
        raise error

    candidates = sorted(
        path for path in output_dir.glob(f"{output_prefix}.*") if path.suffix.lower() in VIDEO_EXTS
    )
    if candidates:
        return candidates[0]
    cleanup_video_download_artifacts(output_dir, output_prefix)
    raise AppError("video_not_found", "视频下载完成，但没有找到生成的视频文件。")


def cleanup_video_download_artifacts(output_dir: Path, output_prefix: str) -> None:
    for path in output_dir.glob(f"{output_prefix}.*"):
        if not (path.is_file() or path.is_symlink()):
            continue
        try:
            path.unlink()
        except OSError as exc:
            # 清理失败不能覆盖 yt-dlp 的原始下载错误。
            print(f"警告：临时视频清理失败：{exc}", file=sys.stderr)


def cleanup_temporary_vision_video(path: Path, warnings: list[str]) -> bool:
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError as exc:
        warning = f"临时视频清理失败：{exc}"
        if warning not in warnings:
            warnings.append(warning)
        return False


def extract_audio_from_video(video_path: Path, output_dir: Path) -> Path:
    require_tool("ffmpeg")
    audio_path = output_dir / "vision-audio.mp3"
    result = run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(audio_path),
        ],
        output_dir,
    )
    if result.returncode != 0 or not audio_path.is_file():
        raise classify_command_error(result.stderr, action="从视频提取音频")
    return audio_path


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


def image_difference_hash(path: Path) -> int:
    from PIL import Image

    with Image.open(path) as image:
        grayscale = image.convert("L")
        width, height = grayscale.size
        # 边缘常见固定页眉、水印和播放器装饰，裁掉后更关注主体内容变化。
        margin_x = max(1, int(width * 0.06))
        margin_y = max(1, int(height * 0.06))
        grayscale = grayscale.crop((margin_x, margin_y, width - margin_x, height - margin_y)).resize((9, 8))
        # Pillow 14 将移除 getdata()，同时保留对旧版 Pillow 的兼容。
        get_pixels = getattr(grayscale, "get_flattened_data", None)
        pixels = list(get_pixels() if get_pixels else grayscale.getdata())
    value = 0
    for row in range(8):
        offset = row * 9
        for column in range(8):
            value = (value << 1) | int(pixels[offset + column] > pixels[offset + column + 1])
    return value


def hash_distance(left: int, right: int) -> int:
    # 兼容 macOS 可能自带的 Python 3.8；该版本没有 int.bit_count()。
    return bin(left ^ right).count("1")


def evenly_sample_frames(frames: list[FrameCandidate], count: int) -> list[FrameCandidate]:
    if count <= 0:
        return []
    if len(frames) <= count:
        return list(frames)
    if count == 1:
        return [frames[len(frames) // 2]]
    indexes = {
        round(index * (len(frames) - 1) / (count - 1))
        for index in range(count)
    }
    return [frames[index] for index in sorted(indexes)]


def filter_frame_candidates(
    candidates: list[FrameCandidate],
    max_gap: float,
    max_frames: int,
    hash_threshold: int = 10,
) -> list[FrameCandidate]:
    if not candidates:
        return []

    # 相近时间的固定帧和转场帧代表同一画面，优先保留转场帧。
    merged: list[FrameCandidate] = []
    for candidate in sorted(candidates, key=lambda item: (item.timestamp, item.source != "scene")):
        if merged and abs(candidate.timestamp - merged[-1].timestamp) <= 1:
            if candidate.source == "scene":
                merged[-1] = candidate
            continue
        merged.append(candidate)

    filtered: list[FrameCandidate] = []
    for candidate in merged:
        candidate.hash_value = image_difference_hash(candidate.path)
        if not filtered:
            filtered.append(candidate)
            continue
        previous = filtered[-1]
        changed = hash_distance(previous.hash_value or 0, candidate.hash_value) > hash_threshold
        if candidate.source == "scene" or changed or candidate.timestamp - previous.timestamp >= max_gap:
            filtered.append(candidate)

    if len(filtered) <= max_frames:
        return filtered

    anchors: list[FrameCandidate] = [filtered[0], filtered[-1]]
    anchors.extend(frame for frame in filtered if frame.source == "scene")
    unique_anchors = list({frame.path: frame for frame in anchors}.values())
    unique_anchors.sort(key=lambda item: item.timestamp)
    if len(unique_anchors) >= max_frames:
        return evenly_sample_frames(unique_anchors, max_frames)

    anchor_paths = {frame.path for frame in unique_anchors}
    remaining = [frame for frame in filtered if frame.path not in anchor_paths]
    selected = unique_anchors + evenly_sample_frames(remaining, max_frames - len(unique_anchors))
    return sorted(selected, key=lambda item: item.timestamp)


def extract_keyframes(
    video_path: Path,
    output_dir: Path,
    scan_interval: float,
    max_gap: float,
    max_frames: int,
    frame_width: int,
) -> list[FrameCandidate]:
    require_tool("ffmpeg")
    run_name = f"{time.strftime('run-%Y%m%d-%H%M%S')}-{os.getpid()}"
    frames_dir = output_dir / "frames" / run_name
    frames_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="video-summary-frames-") as temp_name:
        temp_dir = Path(temp_name)
        periodic_pattern = temp_dir / "periodic-%06d.jpg"
        periodic_result = run_command(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video_path),
                "-vf",
                f"fps=1/{scan_interval},scale={frame_width}:-2:force_original_aspect_ratio=decrease,showinfo",
                "-q:v",
                "3",
                "-start_number",
                "0",
                str(periodic_pattern),
            ],
            temp_dir,
        )
        if periodic_result.returncode != 0:
            raise classify_command_error(periodic_result.stderr, action="固定间隔抽帧")

        periodic_paths = sorted(temp_dir.glob("periodic-*.jpg"))
        periodic_timestamps = [
            float(value)
            for value in re.findall(r"\bn:\s*\d+.*?\bpts_time:\s*([0-9.]+)", periodic_result.stderr)
        ]
        if len(periodic_timestamps) != len(periodic_paths):
            periodic_timestamps = [index * scan_interval for index in range(len(periodic_paths))]
        candidates = [
            FrameCandidate(path=path, timestamp=timestamp, source="periodic")
            for path, timestamp in zip(periodic_paths, periodic_timestamps)
        ]

        scene_pattern = temp_dir / "scene-%06d.jpg"
        scene_result = run_command(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video_path),
                "-vf",
                f"select=gt(scene\\,0.35),scale={frame_width}:-2:force_original_aspect_ratio=decrease,showinfo",
                "-vsync",
                "vfr",
                "-q:v",
                "3",
                "-start_number",
                "0",
                str(scene_pattern),
            ],
            temp_dir,
        )
        if scene_result.returncode != 0:
            raise classify_command_error(scene_result.stderr, action="镜头变化抽帧")

        scene_paths = sorted(temp_dir.glob("scene-*.jpg"))
        scene_timestamps = [
            float(value)
            for value in re.findall(r"\bn:\s*\d+.*?\bpts_time:\s*([0-9.]+)", scene_result.stderr)
        ]
        candidates.extend(
            FrameCandidate(path=path, timestamp=timestamp, source="scene")
            for path, timestamp in zip(scene_paths, scene_timestamps)
        )

        selected = filter_frame_candidates(candidates, max_gap, max_frames)
        persisted: list[FrameCandidate] = []
        for index, candidate in enumerate(selected, start=1):
            timestamp = format_timestamp(candidate.timestamp).replace(":", "-")
            target = frames_dir / f"{index:04d}-{timestamp}.jpg"
            shutil.copy2(candidate.path, target)
            persisted.append(
                FrameCandidate(
                    path=target,
                    timestamp=candidate.timestamp,
                    source=candidate.source,
                    hash_value=candidate.hash_value,
                )
            )

    if not persisted:
        raise AppError("frames_not_found", "视频抽帧完成，但没有得到可用关键帧。")
    return persisted


def serialize_keyframes(frames: list[FrameCandidate], output_dir: Path) -> list[dict]:
    return [
        {
            "path": frame.path.relative_to(output_dir).as_posix(),
            "timestamp": frame.timestamp,
            "source": frame.source,
            "hash_value": frame.hash_value,
        }
        for frame in frames
    ]


def load_cached_keyframes(cache: dict, signature: str, output_dir: Path) -> list[FrameCandidate] | None:
    if not cache_stage_matches(cache, "keyframes", signature):
        return None
    items = cache["stages"]["keyframes"].get("frames")
    if not isinstance(items, list) or not items:
        return None
    frames: list[FrameCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        path = output_dir / str(item.get("path", ""))
        if not path.is_file():
            return None
        frames.append(
            FrameCandidate(
                path=path,
                timestamp=float(item["timestamp"]),
                source=str(item["source"]),
                hash_value=item.get("hash_value"),
            )
        )
    return frames


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


def create_vision_client(config: VisionConfig):
    from openai import OpenAI

    client = OpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=config.timeout,
    )
    try:
        models = client.models.list()
    except Exception as exc:
        raise AppError(
            "vision_backend_unavailable",
            f"无法连接本地视觉服务：{exc}",
            [f"确认 Ollama 已启动，并能从当前运行环境访问 {config.base_url}。"],
        ) from exc

    available = {item.id for item in models.data}
    if config.model not in available:
        raise AppError(
            "vision_model_not_found",
            f"Ollama 中未找到视觉模型：{config.model}",
            [f"请先在宿主机执行：ollama pull {config.model}"],
        )
    return client


def image_data_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def parse_json_object(text: str) -> dict:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(value[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("视觉模型返回值不是 JSON 对象")
    return data


def normalize_text_field(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "；".join(str(item).strip() for item in value if str(item).strip())
    return ""


def normalize_visual_batch(payload: dict, frames: list[FrameCandidate], output_dir: Path) -> list[dict]:
    items = payload.get("frames")
    if not isinstance(items, list):
        raise ValueError("视觉模型返回值缺少 frames 数组")

    indexed: dict[int, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        indexed[index] = item

    results: list[dict] = []
    for index, frame in enumerate(frames, start=1):
        item = indexed.get(index)
        if not item:
            raise ValueError(f"视觉模型返回值缺少第 {index} 张图片的结果")
        description = normalize_text_field(item.get("description"))
        if not description:
            raise ValueError(f"视觉模型没有描述第 {index} 张图片")
        importance = normalize_text_field(item.get("importance")).lower()
        if importance not in {"high", "medium", "low"}:
            importance = "medium"
        visible_text = normalize_text_field(item.get("visible_text"))
        results.append(
            {
                "timestamp_seconds": round(frame.timestamp, 3),
                "timestamp": format_timestamp(frame.timestamp),
                "image": frame.path.relative_to(output_dir).as_posix(),
                "frame_source": frame.source,
                "description": description[:160].rstrip(),
                "visible_text": visible_text[:320].rstrip(),
                "importance": importance,
                "uncertainty": normalize_text_field(item.get("uncertainty")),
            }
        )
    return results


def write_visual_outputs(
    output_dir: Path,
    config: VisionConfig,
    frames: list[dict],
    status: str,
    error: str | None = None,
    metrics: dict | None = None,
    analysis_signature: str | None = None,
) -> tuple[Path, Path]:
    payload = {
        "schema_version": 1,
        "status": status,
        "model": config.model,
        "analysis_signature": analysis_signature,
        "frame_count": len(frames),
        "frames": frames,
    }
    if error:
        payload["error"] = error
    if metrics:
        payload["metrics"] = metrics

    json_path = output_dir / "visual_context.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# 视频画面解析", "", f"- 状态：{status}", f"- 模型：{config.model}", f"- 已解析帧数：{len(frames)}"]
    if error:
        lines.extend([f"- 错误：{error}"])
    for frame in frames:
        lines.extend(
            [
                "",
                f"## [{frame['timestamp']}]",
                "",
                f"- 关键帧：`{frame['image']}`",
                f"- 画面：{frame['description']}",
                f"- 屏幕文字：{frame['visible_text'] or '无'}",
                f"- 重要程度：{frame['importance']}",
                f"- 不确定性：{frame['uncertainty'] or '无'}",
            ]
        )
    markdown_path = output_dir / "visual_context.md"
    markdown_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return json_path, markdown_path


def nearby_transcript_context(
    transcript: str,
    timestamp: float,
    window_seconds: float = 15,
    max_chars: int = 500,
) -> str:
    timed_lines: list[tuple[float, str]] = []
    for line in transcript.splitlines():
        match = re.match(r"^\[(\d+:\d{2}:\d{2})\]\s*(.+)$", line.strip())
        if match:
            timed_lines.append((timestamp_to_seconds(match.group(1)), match.group(2)))
    nearby = [text for seconds, text in timed_lines if abs(seconds - timestamp) <= window_seconds]
    if not nearby and timed_lines:
        nearby = [min(timed_lines, key=lambda item: abs(item[0] - timestamp))[1]]
    value = " ".join(nearby)
    return value[:max_chars].rstrip()


def analyze_keyframes(
    client,
    config: VisionConfig,
    frames: list[FrameCandidate],
    output_dir: Path,
    batch_size: int,
    existing_results: list[dict] | None = None,
    transcript: str = "",
    context_terms: list[str] | None = None,
    analysis_signature: str | None = None,
) -> list[dict]:
    current_images = {frame.path.relative_to(output_dir).as_posix() for frame in frames}
    results = [
        item
        for item in (existing_results or [])
        if isinstance(item, dict) and item.get("image") in current_images
    ]
    completed_images = {item["image"] for item in results}
    pending_frames = [
        frame
        for frame in frames
        if frame.path.relative_to(output_dir).as_posix() not in completed_images
    ]
    metrics = {
        "batch_size": batch_size,
        "effective_batch_size": batch_size,
        "requested_frames": len(pending_frames),
        "batch_seconds": [],
        "fallback_splits": 0,
    }

    def request_batch(batch: list[FrameCandidate]) -> list[dict]:
        batch_started = time.monotonic()
        content: list[dict] = [
            {
                "type": "text",
                "text": (
                    "请逐张分析后只返回 JSON 对象，不要使用 Markdown。格式为："
                    '{"frames":[{"index":1,"description":"画面事实",'
                    '"visible_text":"可辨认的屏幕文字，没有则为空字符串",'
                    '"importance":"high|medium|low","uncertainty":"不确定信息，没有则为空字符串"}]}。'
                    "必须为每张图片返回一项。相邻字幕只能用于校正 OCR 和判断信息增量，不能当作图片事实。"
                    "description 限 80 个中文字符，只写字幕没有表达的布局、操作、图表或画面变化。"
                    "visible_text 限 200 个中文字符，只保留新增或变化的关键文字、命令、参数和数字；"
                    "忽略重复页眉、页脚、水印、栏目名及前一张已经出现的文字。"
                    "importance 按新增价值判断：high=字幕之外的重要信息，medium=补强字幕，"
                    "low=主要重复字幕或装饰。无法确认的英文、命令或小字必须写入 uncertainty。"
                ),
            }
        ]
        if context_terms:
            content.append(
                {
                    "type": "text",
                    "text": "视频已知术语（仅用于 OCR 拼写校正）：" + "、".join(context_terms),
                }
            )
        for index, frame in enumerate(batch, start=1):
            content.extend(
                [
                    {
                        "type": "text",
                        "text": (
                            f"第 {index} 张图片，对应视频时间 {format_timestamp(frame.timestamp)}。"
                            f"相邻字幕：{nearby_transcript_context(transcript, frame.timestamp) or '无'}"
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": image_data_url(frame.path)},
                    },
                ]
            )

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = client.chat.completions.create(
                    model=config.model,
                    messages=[
                        {
                            "role": "system",
                            "content": "你是严谨的视频关键帧分析器，只报告图片中明确可见的信息。",
                        },
                        {"role": "user", "content": content},
                    ],
                    temperature=0,
                    response_format={"type": "json_object"},
                    max_tokens=max(500, min(900, len(batch) * 180)),
                )
                response_text = response.choices[0].message.content or ""
                normalized = normalize_visual_batch(parse_json_object(response_text), batch, output_dir)
                metrics["batch_seconds"].append(
                    {
                        "frames": len(batch),
                        "seconds": round(time.monotonic() - batch_started, 3),
                    }
                )
                return normalized
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(1)
        if len(batch) > 1:
            metrics["fallback_splits"] += 1
            midpoint = len(batch) // 2
            return request_batch(batch[:midpoint]) + request_batch(batch[midpoint:])
        raise last_error or RuntimeError("视觉批次解析失败")

    try:
        offset = 0
        effective_batch_size = batch_size
        while offset < len(pending_frames):
            batch = pending_frames[offset : offset + effective_batch_size]
            fallback_count = metrics["fallback_splits"]
            results.extend(request_batch(batch))
            results.sort(key=lambda item: float(item["timestamp_seconds"]))
            write_visual_outputs(
                output_dir,
                config,
                results,
                "processing",
                metrics=metrics,
                analysis_signature=analysis_signature,
            )
            if metrics["fallback_splits"] > fallback_count and effective_batch_size > 1:
                effective_batch_size = max(1, effective_batch_size // 2)
                metrics["effective_batch_size"] = effective_batch_size
            offset += len(batch)
    except Exception as exc:
        write_visual_outputs(
            output_dir,
            config,
            results,
            "failed",
            error=str(exc),
            metrics=metrics,
            analysis_signature=analysis_signature,
        )
        raise AppError(
            "vision_analysis_failed",
            f"本地视觉模型解析失败：{exc}",
            ["已保留关键帧和已完成的视觉解析结果，便于调试。"],
        ) from exc

    write_visual_outputs(
        output_dir,
        config,
        results,
        "completed",
        metrics=metrics,
        analysis_signature=analysis_signature,
    )
    return results


def load_visual_results(
    output_dir: Path,
    model: str,
    analysis_signature: str,
) -> list[dict]:
    path = output_dir / "visual_context.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if (
        payload.get("model") != model
        or payload.get("analysis_signature") != analysis_signature
        or not isinstance(payload.get("frames"), list)
    ):
        return []
    return [item for item in payload["frames"] if isinstance(item, dict)]


def load_visual_metrics(output_dir: Path) -> dict:
    path = output_dir / "visual_context.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}


def visual_results_cover_frames(
    results: list[dict],
    frames: list[FrameCandidate],
    output_dir: Path,
) -> bool:
    expected = {frame.path.relative_to(output_dir).as_posix() for frame in frames}
    completed = {item.get("image") for item in results}
    return bool(expected) and expected.issubset(completed)


def timestamp_to_seconds(value: str) -> float:
    parts = [int(part) for part in value.split(":")]
    if len(parts) != 3:
        raise ValueError(value)
    return float(parts[0] * 3600 + parts[1] * 60 + parts[2])


def normalize_visual_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def split_visual_lines(value: str) -> list[str]:
    return [
        line.strip()
        for line in re.split(r"[\r\n；;]+", value)
        if line.strip()
    ]


def compact_visual_material(visual_frames: list[dict], transcript: str = "") -> list[dict]:
    ordered = sorted(visual_frames, key=lambda item: float(item["timestamp_seconds"]))
    normalized_transcript = normalize_visual_line(transcript)
    line_counts: dict[str, int] = {}
    for frame in ordered:
        for line in {normalize_visual_line(item) for item in split_visual_lines(frame.get("visible_text", ""))}:
            line_counts[line] = line_counts.get(line, 0) + 1

    boilerplate_threshold = max(3, (len(ordered) + 3) // 4)
    boilerplate = {
        line
        for line, count in line_counts.items()
        if count >= boilerplate_threshold and len(line) <= 40
    }

    compacted: list[dict] = []
    previous_lines: set[str] = set()
    previous_description = ""
    for frame in ordered:
        description = re.sub(r"\s+", " ", str(frame.get("description", ""))).strip()
        raw_lines = split_visual_lines(str(frame.get("visible_text", "")))
        useful_lines = [
            line
            for line in raw_lines
            if normalize_visual_line(line) not in boilerplate
            and (
                len(normalize_visual_line(line)) < 4
                or normalize_visual_line(line) not in normalized_transcript
            )
        ]
        current_lines = {normalize_visual_line(line) for line in useful_lines}
        delta_lines = [
            line
            for line in useful_lines
            if normalize_visual_line(line) not in previous_lines
        ]
        description_key = normalize_visual_line(description)
        description_similarity = (
            difflib.SequenceMatcher(None, previous_description, description_key).ratio()
            if previous_description and description_key
            else 0
        )
        if not delta_lines and (
            description_key == previous_description
            or description_similarity >= 0.88
            or frame.get("importance") == "low"
        ):
            previous_lines = current_lines
            continue

        visible_text = "\n".join(delta_lines)
        if len(visible_text) > 240:
            visible_text = visible_text[:240].rstrip() + "……"
        description_limit = 80 if visible_text else 120
        if len(description) > description_limit:
            description = description[:description_limit].rstrip() + "……"

        compacted.append(
            {
                **frame,
                "description": description,
                "visible_text": visible_text,
            }
        )
        previous_lines = current_lines
        previous_description = description_key
    return compacted


def limit_visual_events(
    visual_events: list[tuple[float, int, str]],
    char_budget: int,
) -> list[tuple[float, int, str]]:
    if char_budget <= 0 or not visual_events:
        return []
    total_chars = sum(len(text) + 1 for _seconds, _priority, text in visual_events)
    if total_chars <= char_budget:
        return visual_events

    target_count = max(1, min(len(visual_events), math.floor(len(visual_events) * char_budget / total_chars)))
    if target_count == 1:
        indexes = [len(visual_events) // 2]
    else:
        # 在时间均匀覆盖之外，优先保留可执行命令和链接等高价值字面证据，
        # 避免自动字幕中的残缺转写覆盖画面里更可靠的原文。
        evidence_pattern = re.compile(
            r"(?:^|[；：\s])(?:\$\s*)?"
            r"(?:npx|npm|pnpm|yarn|pipx?|uv|docker|git|cargo|go|brew)\s+\S+"
            r"|https?://\S+|github\.com/\S+",
            re.IGNORECASE,
        )
        evidence_indexes = [
            index
            for index, (_seconds, _priority, text) in enumerate(visual_events)
            if evidence_pattern.search(text)
        ]
        reserved_count = min(len(evidence_indexes), max(1, target_count // 5))
        indexes_set = set(evidence_indexes[-reserved_count:])
        coverage_count = target_count - len(indexes_set)
        if coverage_count > 0:
            coverage_indexes = [
                round(index * (len(visual_events) - 1) / max(coverage_count - 1, 1))
                for index in range(coverage_count)
            ]
            for index in coverage_indexes:
                if len(indexes_set) >= target_count:
                    break
                indexes_set.add(index)
        while len(indexes_set) < target_count:
            remaining = set(range(len(visual_events))) - indexes_set
            next_index = max(
                remaining,
                key=lambda candidate: min(abs(candidate - selected) for selected in indexes_set),
            )
            indexes_set.add(next_index)
        indexes = sorted(indexes_set)
    selected = [visual_events[index] for index in indexes]
    per_event_budget = max(1, char_budget // max(len(selected), 1) - 1)
    limited = []
    for seconds, priority, text in selected:
        if len(text) > per_event_budget:
            suffix = "……" if per_event_budget > 2 else ""
            text = text[: max(1, per_event_budget - len(suffix))].rstrip() + suffix
        limited.append((seconds, priority, text))
    while limited and sum(len(text) + 1 for _seconds, _priority, text in limited) > char_budget:
        limited.pop()
    return limited


def determine_visual_char_budget(max_chars: int, transcript_context_chars: int) -> int:
    independent_budget = max(
        MIN_VISUAL_CONTEXT_CHARS,
        math.ceil(max_chars * VISUAL_CONTEXT_RATIO),
    )
    available_budget = max(0, max_chars - transcript_context_chars)
    return min(MAX_VISUAL_CONTEXT_CHARS, max(independent_budget, available_budget))


def build_multimodal_context(
    transcript: str,
    visual_frames: list[dict],
    max_chars: int | None = None,
) -> str:
    transcript_events: list[tuple[float, int, str]] = []
    untimed: list[str] = []
    for line in transcript.splitlines():
        match = re.match(r"^\[(\d+:\d{2}:\d{2})\]\s*(.+)$", line.strip())
        if not match:
            if line.strip():
                untimed.append(line.strip())
            continue
        transcript_events.append((timestamp_to_seconds(match.group(1)), 0, f"[{match.group(1)}] {match.group(2)}"))

    visual_events: list[tuple[float, int, str]] = []
    for frame in compact_visual_material(visual_frames, transcript):
        details = [f"[{frame['timestamp']}] 画面：{frame['description']}"]
        if frame.get("visible_text"):
            details.append(f"屏幕文字：{frame['visible_text']}")
        if frame.get("uncertainty"):
            details.append(f"不确定性：{frame['uncertainty']}")
        visual_events.append((float(frame["timestamp_seconds"]), 1, "；".join(details)))

    header = [
        "# 多模态视频材料",
        "",
        "以下内容按时间轴排列；普通行来自语音/字幕，标有“画面”的行是视觉模型提取的画面证据，两类信息都需要参与内容理解。",
    ]
    if untimed:
        header.extend(["", "## 无时间戳的语音/字幕", "", *untimed])
    header.extend(["", "## 时间轴", ""])

    if max_chars:
        base_chars = len("\n".join([*header, *(text for _seconds, _priority, text in transcript_events)])) + 1
        visual_events = limit_visual_events(
            visual_events,
            determine_visual_char_budget(max_chars, base_chars),
        )

    lines = [
        *header,
        *(
            text
            for _seconds, _priority, text in sorted(
                [*transcript_events, *visual_events],
                key=lambda item: (item[0], item[1]),
            )
        ),
    ]
    return "\n".join(lines).rstrip() + "\n"


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


def split_text_balanced(text: str, max_chars: int) -> list[str]:
    """在不超过上限的前提下均衡分段，避免最后一段明显过短。"""
    if len(text) <= max_chars:
        return [text]
    chunk_count = math.ceil(len(text) / max_chars)
    chunks: list[str] = []
    start = 0
    for index in range(chunk_count - 1):
        remaining_chunks = chunk_count - index
        ideal_end = start + math.ceil((len(text) - start) / remaining_chunks)
        max_end = min(len(text), start + max_chars)
        before = text.rfind("\n", start + 1, ideal_end + 1)
        after = text.find("\n", ideal_end, max_end + 1)
        candidates = [position + 1 for position in (before, after) if position >= start]
        end = min(candidates, key=lambda position: abs(position - ideal_end)) if candidates else ideal_end
        chunks.append(text[start:end].rstrip("\n"))
        start = end
    chunks.append(text[start:].lstrip("\n"))
    return chunks


def determine_summary_chunk_chars(config: SummaryConfig, max_chars: int) -> int:
    if config.chunk_chars:
        return min(max_chars, config.chunk_chars)
    if config.provider != "ollama" or not config.context_length:
        return max_chars

    # 为系统提示、分段模板和最终输出预留约 1800 tokens；中文按约 1 字符/token 保守估算。
    context_safe_chars = max(1200, config.context_length - 1800)
    return min(max_chars, context_safe_chars)


def build_summary_merge_material(partials: list[str]) -> str:
    sections = "\n\n".join(
        f"## 第 {index}/{len(partials)} 段摘要\n\n{partial}"
        for index, partial in enumerate(partials, start=1)
    )
    return (
        "# 合并要求\n\n"
        "以下是全部分段摘要。最终结果必须逐段覆盖，每一段都至少保留其开头、中间和结尾的重要时间节点；"
        "可以合并相邻节点，但不得只详细总结第一段或省略最后一段的后半部分。"
        "若材料本身没有注明截断，不得声称“原文截断”。\n\n"
        "# 全部分段摘要\n\n"
        f"{sections}"
    )


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
    normalized = source[: -len(suffix)] if source.endswith(suffix) else source
    return normalized.split("+", 1)[0]


def transcript_quality_note(source: str) -> str:
    has_vision = "+vision" in source
    normalized_source = base_transcript_source(source)
    if normalized_source == "whisper":
        note = (
            "本文本由 Whisper 音频转写生成，可能存在专有名词、英文术语、产品名、人名、数字、标点和断句错误。"
            "请结合上下文只修正明显误识别；无法确定时保守表述，不要编造原文没有的信息。"
        )
    elif normalized_source == "auto_subtitle":
        note = (
            "本文本来自平台自动字幕，可能存在自动识别错误。"
            "请结合上下文只修正明显误识别；无法确定时保守表述，不要编造原文没有的信息。"
        )
    elif normalized_source == "manual_subtitle":
        note = "本文本来自人工字幕，通常较可靠，但仍可能存在错字、漏字或排版问题；请只基于原文总结。"
    elif normalized_source == "file":
        note = "本文本来自已有转写稿，来源质量未知；如遇疑似识别错误，请结合上下文保守理解，不要编造。"
    else:
        note = "转写文本可能存在识别、清洗或断句错误；请结合上下文保守总结，不要编造原文没有的信息。"
    if has_vision:
        note += " 画面描述由本地视觉模型生成，也可能存在漏读或误判；不确定内容不得当作事实。"
    return note


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
    template = MULTIMODAL_CHUNK_SUMMARY_TEMPLATE if "+vision" in source else CHUNK_SUMMARY_TEMPLATE
    return render_prompt_template(
        template,
        transcript,
        meta,
        source,
        chunk_index,
        chunk_count,
    )


def summarize_with_llm(
    client,
    config: SummaryConfig,
    transcript: str,
    meta: VideoMeta,
    source: str,
    max_chars: int,
    prompt_template: str,
) -> SummaryResult:
    model = config.model
    effective_max_chars = determine_summary_chunk_chars(config, max_chars)
    single_max_tokens = 1300 if config.provider == "ollama" else None
    final_max_tokens = 1300 if config.provider == "ollama" else None
    # 自动放大分段后需要同步提高中间摘要预算，否则长分段后半部分容易被截断。
    partial_max_tokens = 350 if config.provider == "ollama" else None
    fallback_used = False

    while True:
        chunks = split_text_balanced(transcript, effective_max_chars)
        try:
            if len(chunks) == 1:
                prompt = render_prompt_template(prompt_template, chunks[0], meta, source, 1, 1)
                text = call_summary_llm(client, config, model, prompt, single_max_tokens)
            else:
                partials: list[str] = []
                for index, chunk in enumerate(chunks, start=1):
                    prompt = render_chunk_summary_prompt(chunk, meta, source, index, len(chunks))
                    partials.append(call_summary_llm(client, config, model, prompt, partial_max_tokens))

                # 多段长文本先提取细节，再由最终模板合并，减少直接二次压缩造成的要点丢失。
                final_prompt = render_prompt_template(
                    prompt_template,
                    build_summary_merge_material(partials),
                    meta,
                    f"{source}_partials",
                    1,
                    1,
                )
                text = call_summary_llm(client, config, model, final_prompt, final_max_tokens)
            return SummaryResult(
                text=text,
                chunk_chars=effective_max_chars,
                chunks=len(chunks),
                chunk_lengths=[len(chunk) for chunk in chunks],
                context_length=config.context_length,
                fallback_used=fallback_used,
            )
        except Exception as exc:
            if config.provider == "ollama" and is_context_length_error(exc) and effective_max_chars > 1200:
                effective_max_chars = max(1200, effective_max_chars // 2)
                fallback_used = True
                continue
            raise classify_llm_error(exc) from exc


def is_context_length_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in ("context length", "context window", "too long", "exceeds the context")
    )


def validate_llm_output(content, finish_reason, provider: str) -> str:
    reason = str(finish_reason or "").strip().lower()
    if reason in {"length", "max_tokens"}:
        raise LLMOutputTruncatedError(f"{provider} 总结输出因长度限制被截断。")
    if not reason:
        raise RuntimeError(f"{provider} 总结输出缺少结束原因。")
    if reason != "stop":
        raise RuntimeError(f"{provider} 总结输出异常结束：{reason}。")
    text = str(content or "")
    if not text.strip():
        raise RuntimeError(f"{provider} 总结模型返回了空内容。")
    return text


def expanded_output_token_budget(max_tokens: int | None) -> int:
    if max_tokens is None:
        return DEFAULT_TRUNCATION_RETRY_TOKENS
    return max_tokens * 2


def call_summary_llm(
    client,
    config: SummaryConfig,
    model: str,
    prompt: str,
    max_tokens: int | None,
) -> str:
    current_max_tokens = max_tokens
    for attempt in range(2):
        try:
            if config.provider != "ollama":
                return call_llm(client, model, prompt, current_max_tokens)
            options = {"temperature": 0.2}
            if config.context_length:
                options["num_ctx"] = config.context_length
            if current_max_tokens is not None:
                options["num_predict"] = current_max_tokens
            response = ollama_request(
                config,
                "/api/chat",
                {
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "你是严谨的视频内容总结助手，只基于给定文本总结，并使用中文输出。",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "stream": False,
                    "options": options,
                },
            )
            return validate_llm_output(
                (response.get("message") or {}).get("content"),
                response.get("done_reason"),
                "Ollama",
            )
        except LLMOutputTruncatedError:
            if attempt:
                raise
            current_max_tokens = expanded_output_token_budget(current_max_tokens)
    raise RuntimeError("总结模型调用失败。")


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


def call_llm(client, model: str, prompt: str, max_tokens: int | None = None) -> str:
    request = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你是严谨的视频内容总结助手，只基于给定文本总结，并使用中文输出。",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    if max_tokens is not None:
        request["max_tokens"] = max_tokens
    response = client.chat.completions.create(
        **request,
    )
    choice = response.choices[0]
    return validate_llm_output(
        choice.message.content,
        getattr(choice, "finish_reason", None),
        "OpenAI",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="支持快速解析和多模态解析的视频总结 CLI")
    parser.add_argument("url", nargs="?", help="视频链接，或容器/本机可访问的本地音视频文件路径")
    parser.add_argument("--output", default="outputs", help="输出目录，默认 outputs")
    parser.add_argument("--model-size", default="small", choices=AVAILABLE_MODEL_SIZES, help="faster-whisper 模型大小，默认 small")
    parser.add_argument("--language", default="zh", help="Whisper 识别语言；中文视频用 zh，英文视频建议 en，不确定可用 auto；默认 zh")
    parser.add_argument("--sub-langs", default="zh.*,ai-zh,en.*", help="字幕语言匹配规则，默认覆盖中文、B 站 AI 中文字幕和英文")
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
    parser.add_argument(
        "--summary-provider",
        choices=("ollama", "api"),
        default=os.getenv("SUMMARY_PROVIDER") or "ollama",
        help="最终总结提供方，默认 ollama；使用 api 时读取 OPENAI_* 配置",
    )
    parser.add_argument("--no-resume", dest="resume", action="store_false", help="忽略阶段缓存并强制重新处理")
    parser.set_defaults(resume=True)
    parser.add_argument(
        "--with-vision",
        action="store_true",
        help="启用多模态解析（语音/字幕 + 关键帧）；默认使用快速解析，仅处理语音/字幕",
    )
    parser.add_argument("--vision-scan-interval", type=float, default=5, help="视觉候选帧扫描间隔秒数，默认 5")
    parser.add_argument("--vision-max-gap", type=float, default=45, help="相似画面最长保留间隔上限秒数，默认 45")
    parser.add_argument("--vision-max-frames", type=int, default=60, help="送入视觉模型的最大关键帧数，默认 60")
    parser.add_argument("--vision-batch-size", type=int, default=4, help="每次视觉请求包含的关键帧数，默认 4")
    parser.add_argument("--vision-frame-width", type=int, default=768, help="关键帧缩放宽度，默认 768")
    return parser.parse_args(argv)


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
    visual_context_path: Path | None = None
    multimodal_context_path: Path | None = None
    meta: VideoMeta | None = None
    transcript_source: str | None = None
    whisper_context_terms: list[str] | None = None
    vision_config: VisionConfig | None = None
    vision_client = None
    summary_config: SummaryConfig | None = None
    summary_client = None
    vision_video_path: Path | None = None
    downloaded_vision_video = False
    pipeline_cache: dict = {"schema_version": CACHE_SCHEMA_VERSION, "stages": {}}
    pipeline_source_signature: str | None = None
    transcript_cache_signature: str | None = None
    keyframe_cache_signature: str | None = None
    vision_cache_signature: str | None = None
    vision_sampling_plan: VisionSamplingPlan | None = None
    keyframes: list[FrameCandidate] | None = None
    summary_input: str | None = None
    summary_source: str | None = None
    summary_prompt_template = prompt_template
    warnings: list[str] = []

    try:
        validate_args(args)
        if not (args.no_llm or args.export_prompt or args.summary_from_file):
            stage_started = time.monotonic()
            summary_config = resolve_summary_config(args.summary_provider)
            summary_client = create_summary_client(summary_config)
            mark_stage(processing, "summary_preflight", stage_started)
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
            clean_generated_outputs(output_dir, clear_cache=True)
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
        if not args.resume:
            clean_generated_outputs(output_dir, clear_cache=True)
        pipeline_cache = load_pipeline_cache(output_dir)
        pipeline_source_signature = source_signature(meta, local_input if is_local_file else None)
        transcript_cache_signature = stable_signature(
            {
                "source": pipeline_source_signature,
                "model_size": args.model_size,
                "language": whisper_language,
                "sub_langs": args.sub_langs,
                "force_transcribe": args.force_transcribe,
                "cookies": cookie_cache_identity(cookies),
            }
        )
        write_meta(meta, output_dir, None, warnings, processing, options)

        if args.with_vision:
            stage_started = time.monotonic()
            vision_config = resolve_vision_config()
            vision_sampling_plan = build_vision_sampling_plan(
                meta.duration,
                args.vision_scan_interval,
                args.vision_max_gap,
                args.vision_max_frames,
            )
            keyframe_cache_signature = stable_signature(
                {
                    "source": pipeline_source_signature,
                    "scan_interval": vision_sampling_plan.scan_interval,
                    "max_gap": vision_sampling_plan.max_gap,
                    "max_frames": vision_sampling_plan.max_frames,
                    "frame_width": args.vision_frame_width,
                    "selection_schema": 2,
                }
            )
            vision_cache_signature = stable_signature(
                {
                    "keyframes": keyframe_cache_signature,
                    "model": vision_config.model,
                    "batch_size": args.vision_batch_size,
                    "transcript_stage": transcript_cache_signature,
                    "context_terms": build_whisper_context_terms(meta),
                    "prompt_schema": 3,
                }
            )
            processing["vision"] = {
                "model": vision_config.model,
                "status": "preparing",
                "frames": 0,
                "sampling": asdict(vision_sampling_plan),
            }
            write_meta(meta, output_dir, None, warnings, processing, options)
            vision_client = create_vision_client(vision_config)
            mark_stage(processing, "vision_preflight", stage_started)
            keyframes = load_cached_keyframes(pipeline_cache, keyframe_cache_signature, output_dir)
            if keyframes:
                processing.setdefault("cache_hits", []).append("keyframes")
            elif is_local_file:
                vision_video_path = local_input
            else:
                stage_started = time.monotonic()
                vision_video_path = download_video(args.url, output_dir, cookies)
                downloaded_vision_video = True
                mark_stage(processing, "download_video", stage_started)
            write_meta(meta, output_dir, None, warnings, processing, options)

        transcript_path = output_dir / "transcript.txt"
        transcript: str | None = None
        if (
            args.resume
            and transcript_cache_signature
            and cache_stage_matches(pipeline_cache, "transcript", transcript_cache_signature)
            and transcript_path.is_file()
        ):
            transcript = transcript_path.read_text(encoding="utf-8")
            transcript_stage = pipeline_cache["stages"]["transcript"]
            transcript_source = transcript_stage.get("source") or "unknown"
            whisper_context_terms = transcript_stage.get("whisper_context_terms")
            restore_cached_subtitle_metadata(transcript_stage, meta, warnings)
            processing.setdefault("cache_hits", []).append("transcript")

        subtitle_path = None
        if transcript is None and not is_local_file and not args.force_transcribe:
            stage_started = time.monotonic()
            subtitle_info = download_subtitle(
                args.url,
                output_dir,
                cookies,
                args.sub_langs,
                meta,
                warnings,
            )
            mark_stage(processing, "download_subtitle", stage_started)
            if subtitle_info:
                subtitle_path, transcript_source, subtitle_warnings = subtitle_info
                warnings.extend(subtitle_warnings)
                stage_started = time.monotonic()
                transcript = clean_subtitle(subtitle_path)
                mark_stage(processing, "clean_subtitle", stage_started)
                if not transcript.strip():
                    print("字幕文件为空，将尝试音频转写。", file=sys.stderr)
                    warnings.append("字幕文件为空，已回退到 Whisper 转写。")
                    transcript = None
                    subtitle_path = None

        audio_path: Path | None = None
        if transcript is None:
            whisper_context_terms = build_whisper_context_terms(meta)
            transcription_input = local_input
            if not is_local_file:
                stage_started = time.monotonic()
                if vision_video_path:
                    audio_path = extract_audio_from_video(vision_video_path, output_dir)
                    mark_stage(processing, "extract_audio", stage_started)
                else:
                    audio_path = download_audio(args.url, output_dir, cookies)
                    mark_stage(processing, "download_audio", stage_started)
                transcription_input = audio_path

            def transcribe_job() -> tuple[str, float]:
                started = time.monotonic()
                text = transcribe_audio(
                    transcription_input,
                    args.model_size,
                    whisper_language,
                    build_whisper_initial_prompt(whisper_context_terms or []),
                )
                return text, time.monotonic() - started

            def keyframe_job() -> tuple[list[FrameCandidate], float]:
                if not vision_video_path or not vision_sampling_plan:
                    raise AppError("vision_not_ready", "视觉视频尚未准备完成。")
                started = time.monotonic()
                frames = extract_keyframes(
                    vision_video_path,
                    output_dir,
                    vision_sampling_plan.scan_interval,
                    vision_sampling_plan.max_gap,
                    vision_sampling_plan.max_frames,
                    args.vision_frame_width,
                )
                return frames, time.monotonic() - started

            if args.with_vision and keyframes is None and vision_video_path:
                processing["parallel_stages"] = ["transcribe", "extract_keyframes"]
                with ThreadPoolExecutor(max_workers=2, thread_name_prefix="video-summary") as executor:
                    transcript_future = executor.submit(transcribe_job)
                    keyframe_future = executor.submit(keyframe_job)
                    transcript, transcribe_seconds = transcript_future.result()
                    keyframes, keyframe_seconds = keyframe_future.result()
                processing["stages"]["transcribe"] = round(transcribe_seconds, 3)
                processing["stages"]["extract_keyframes"] = round(keyframe_seconds, 3)
            else:
                transcript, transcribe_seconds = transcribe_job()
                processing["stages"]["transcribe"] = round(transcribe_seconds, 3)
            transcript_source = "whisper"
            if audio_path and not args.keep_audio:
                audio_path.unlink(missing_ok=True)

        if transcript is None:
            raise AppError("transcript_empty", "字幕和 Whisper 均未生成可用文本。")
        if not transcript_path.is_file() or "transcript" not in processing.get("cache_hits", []):
            transcript_path.write_text(transcript, encoding="utf-8")
            if transcript_cache_signature:
                update_cache_stage(
                    pipeline_cache,
                    "transcript",
                    transcript_cache_signature,
                    path="transcript.txt",
                    source=transcript_source,
                    whisper_context_terms=whisper_context_terms,
                    selected_subtitle_lang=meta.selected_subtitle_lang,
                    selected_subtitle_type=meta.selected_subtitle_type,
                )

        if args.with_vision and keyframes is None:
            if not vision_video_path or not vision_sampling_plan:
                raise AppError("vision_not_ready", "视觉视频尚未准备完成。")
            stage_started = time.monotonic()
            keyframes = extract_keyframes(
                vision_video_path,
                output_dir,
                vision_sampling_plan.scan_interval,
                vision_sampling_plan.max_gap,
                vision_sampling_plan.max_frames,
                args.vision_frame_width,
            )
            mark_stage(processing, "extract_keyframes", stage_started)

        if args.with_vision and keyframes and keyframe_cache_signature:
            update_cache_stage(
                pipeline_cache,
                "keyframes",
                keyframe_cache_signature,
                frames=serialize_keyframes(keyframes, output_dir),
            )
        save_pipeline_cache(output_dir, pipeline_cache)
        write_meta(meta, output_dir, transcript_source, warnings, processing, options, whisper_context_terms)

        summary_input = transcript
        summary_source = transcript_source
        summary_prompt_template = prompt_template
        if args.with_vision:
            if not vision_config or not vision_client or not keyframes:
                raise AppError("vision_not_ready", "视觉解析环境没有正确初始化。")
            processing["vision"]["frames"] = len(keyframes)
            processing["vision"]["status"] = "analyzing"

            if downloaded_vision_video:
                downloaded_vision_video = not cleanup_temporary_vision_video(
                    vision_video_path,
                    warnings,
                )

            visual_context_path = output_dir / "visual_context.json"
            existing_visual_results = load_visual_results(
                output_dir,
                vision_config.model,
                vision_cache_signature,
            )
            visual_cache_complete = bool(
                args.resume
                and vision_cache_signature
                and cache_stage_matches(pipeline_cache, "vision", vision_cache_signature)
                and visual_results_cover_frames(existing_visual_results, keyframes, output_dir)
            )
            if visual_cache_complete:
                visual_frames = existing_visual_results
                processing.setdefault("cache_hits", []).append("vision")
            else:
                stage_started = time.monotonic()
                visual_frames = analyze_keyframes(
                    vision_client,
                    vision_config,
                    keyframes,
                    output_dir,
                    args.vision_batch_size,
                    existing_results=existing_visual_results if args.resume else None,
                    transcript=transcript,
                    context_terms=build_whisper_context_terms(meta),
                    analysis_signature=vision_cache_signature,
                )
                mark_stage(processing, "analyze_keyframes", stage_started)
                if vision_cache_signature:
                    update_cache_stage(
                        pipeline_cache,
                        "vision",
                        vision_cache_signature,
                        path="visual_context.json",
                        frame_count=len(visual_frames),
                    )
                    save_pipeline_cache(output_dir, pipeline_cache)

            visual_summary_limit = (
                determine_summary_chunk_chars(summary_config, args.max_chars)
                if summary_config
                else args.max_chars
            )
            visual_char_budget = determine_visual_char_budget(
                visual_summary_limit,
                len(build_multimodal_context(transcript, [])),
            )
            summary_input = build_multimodal_context(
                transcript,
                visual_frames,
                max_chars=visual_summary_limit,
            )
            multimodal_context_path = output_dir / "multimodal_context.txt"
            multimodal_context_path.write_text(summary_input, encoding="utf-8")
            summary_source = f"{transcript_source}+vision"
            if not args.prompt and not args.prompt_file:
                summary_prompt_template = MULTIMODAL_PROMPT_TEMPLATE
            processing["vision"].update(
                {
                    "status": "completed",
                    "frames": len(visual_frames),
                    "frame_sources": dict(Counter(frame.source for frame in keyframes)),
                    "context_frames": summary_input.count("] 画面："),
                    "compacted_frames": len(compact_visual_material(visual_frames, transcript)),
                    "visual_char_budget": visual_char_budget,
                    "multimodal_chars": len(summary_input),
                    "analysis_metrics": load_visual_metrics(output_dir),
                    "visual_context": str(visual_context_path),
                    "multimodal_context": str(multimodal_context_path),
                }
            )
            processing["summary_source"] = summary_source
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

        summary_path = output_dir / "summary.md"
        if not summary_config or not summary_client or summary_input is None or summary_source is None:
            raise AppError("summary_not_ready", "最终总结模型没有正确初始化。")
        summary_cache_signature = stable_signature(
            {
                "input": hashlib.sha256(summary_input.encode("utf-8")).hexdigest(),
                "provider": summary_config.provider,
                "base_url": summary_config.base_url,
                "model": summary_config.model,
                "max_chars": args.max_chars,
                "provider_chunk_chars": summary_config.chunk_chars,
                "context_length": summary_config.context_length,
                "summary_schema": 12,
                "prompt": hashlib.sha256(summary_prompt_template.encode("utf-8")).hexdigest(),
            }
        )
        summary_result: SummaryResult | None = None
        if (
            args.resume
            and cache_stage_matches(pipeline_cache, "summary", summary_cache_signature)
            and summary_path.is_file()
        ):
            processing.setdefault("cache_hits", []).append("summary")
        else:
            stage_started = time.monotonic()
            summary_result = summarize_with_llm(
                summary_client,
                summary_config,
                summary_input,
                meta,
                summary_source,
                args.max_chars,
                summary_prompt_template,
            )
            mark_stage(processing, "summarize", stage_started)
            summary_path.write_text(summary_result.text.strip() + "\n", encoding="utf-8")
            update_cache_stage(
                pipeline_cache,
                "summary",
                summary_cache_signature,
                path="summary.md",
                provider=summary_config.provider,
                model=summary_config.model,
                chunk_chars=summary_result.chunk_chars,
                chunks=summary_result.chunks,
                chunk_lengths=summary_result.chunk_lengths,
                context_length=summary_result.context_length,
                fallback_used=summary_result.fallback_used,
            )
            save_pipeline_cache(output_dir, pipeline_cache)
        processing["status"] = "completed"
        summary_stage = pipeline_cache.get("stages", {}).get("summary", {})
        effective_summary_chars = (
            summary_result.chunk_chars
            if summary_result
            else summary_stage.get("chunk_chars") or determine_summary_chunk_chars(summary_config, args.max_chars)
        )
        summary_chunks = (
            summary_result.chunks
            if summary_result
            else summary_stage.get("chunks") or len(split_text_balanced(summary_input, effective_summary_chars))
        )
        processing["summary"] = {
            "provider": summary_config.provider,
            "model": summary_config.model,
            "status": "completed",
            "strategy": "configured" if summary_config.chunk_chars else "ollama_context_auto",
            "chunk_chars": effective_summary_chars,
            "chunks": summary_chunks,
            "chunk_lengths": (
                summary_result.chunk_lengths
                if summary_result
                else summary_stage.get("chunk_lengths")
                or [len(chunk) for chunk in split_text_balanced(summary_input, effective_summary_chars)]
            ),
            "context_length": summary_config.context_length,
            "model_context_length": summary_config.model_context_length,
            "fallback_used": (
                summary_result.fallback_used if summary_result else bool(summary_stage.get("fallback_used"))
            ),
        }
        mark_total(processing, run_started)
        write_meta(meta, output_dir, transcript_source, warnings, processing, options, whisper_context_terms)
        if args.json:
            payload = {
                "ok": True,
                "output_dir": str(output_dir),
                "transcript": str(transcript_path),
                "summary": str(summary_path),
                "transcript_source": transcript_source,
                "warnings": warnings,
            }
            if visual_context_path and multimodal_context_path:
                payload["visual_context"] = str(visual_context_path)
                payload["multimodal_context"] = str(multimodal_context_path)
            print_json(payload)
        else:
            print(f"已生成总结：{summary_path}")
        return 0
    except AppError as exc:
        processing["status"] = "failed"
        processing["error"] = {"code": exc.code, "message": exc.message}
        if (
            args.with_vision
            and processing.get("vision")
            and processing["vision"].get("status") != "completed"
        ):
            processing["vision"]["status"] = "failed"
        if summary_config:
            processing["summary"] = {
                "provider": summary_config.provider,
                "model": summary_config.model,
                "status": "failed",
                "error": exc.message,
            }
        can_export_fallback = not args.with_vision or multimodal_context_path is not None
        if output_dir and transcript_path and transcript_path.exists() and not prompt_path and can_export_fallback:
            try:
                stage_started = time.monotonic()
                prompt_path = write_chatgpt_prompt(
                    summary_input or transcript_path.read_text(encoding="utf-8"),
                    meta or VideoMeta(title="video", id="video", webpage_url=""),
                    summary_source or transcript_source or "unknown",
                    output_dir,
                    args.max_chars,
                    summary_prompt_template,
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
            payload = {
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
            if args.with_vision:
                payload["visual_context"] = str(visual_context_path) if visual_context_path else None
                payload["multimodal_context"] = str(multimodal_context_path) if multimodal_context_path else None
            print_json(payload)
        else:
            print(f"错误：{exc.message}", file=sys.stderr)
            for hint in exc.hints:
                print(f"提示：{hint}", file=sys.stderr)
        return 1
    except Exception as exc:
        processing["status"] = "failed"
        processing["error"] = {"code": "unexpected_error", "message": str(exc)}
        if (
            args.with_vision
            and processing.get("vision")
            and processing["vision"].get("status") != "completed"
        ):
            processing["vision"]["status"] = "failed"
        if summary_config:
            processing["summary"] = {
                "provider": summary_config.provider,
                "model": summary_config.model,
                "status": "failed",
                "error": str(exc),
            }
        can_export_fallback = not args.with_vision or multimodal_context_path is not None
        if output_dir and transcript_path and transcript_path.exists() and not prompt_path and can_export_fallback:
            try:
                stage_started = time.monotonic()
                prompt_path = write_chatgpt_prompt(
                    summary_input or transcript_path.read_text(encoding="utf-8"),
                    meta or VideoMeta(title="video", id="video", webpage_url=""),
                    summary_source or transcript_source or "unknown",
                    output_dir,
                    args.max_chars,
                    summary_prompt_template,
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
            payload = {
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
            if args.with_vision:
                payload["visual_context"] = str(visual_context_path) if visual_context_path else None
                payload["multimodal_context"] = str(multimodal_context_path) if multimodal_context_path else None
            print_json(payload)
        else:
            print(f"错误：{exc}", file=sys.stderr)
            if prompt_path:
                print(f"提示：已自动生成 ChatGPT 提示词，可复制到 ChatGPT：{prompt_path}", file=sys.stderr)
        return 1
    finally:
        if downloaded_vision_video and vision_video_path:
            cleaned = cleanup_temporary_vision_video(vision_video_path, warnings)
            if not cleaned and output_dir and meta:
                try:
                    write_meta(
                        meta,
                        output_dir,
                        transcript_source,
                        warnings,
                        processing,
                        options,
                        whisper_context_terms,
                    )
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
