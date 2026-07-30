import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from urllib import error as urllib_error
from unittest.mock import patch


if importlib.util.find_spec("dotenv") is None:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None
    sys.modules["dotenv"] = dotenv

import video_summary


class JsonResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class ArgumentTests(unittest.TestCase):
    def test_default_path_keeps_vision_disabled(self):
        args = video_summary.parse_args(["https://example.com/video"])

        video_summary.validate_args(args)

        self.assertFalse(args.with_vision)
        self.assertEqual(args.vision_scan_interval, 5)
        self.assertEqual(args.vision_max_gap, 45)
        self.assertEqual(args.vision_max_frames, 60)
        self.assertEqual(args.vision_batch_size, 4)
        self.assertEqual(args.vision_frame_width, 768)
        self.assertEqual(args.summary_provider, "ollama")
        self.assertEqual(args.sub_langs, "zh.*,ai-zh,en.*")
        _processing, options = video_summary.build_processing_info(args, "zh")
        self.assertEqual(options["analysis_mode"], "quick")

    def test_with_vision_uses_multimodal_analysis_mode(self):
        args = video_summary.parse_args(["https://example.com/video", "--with-vision"])

        _processing, options = video_summary.build_processing_info(args, "zh")

        self.assertEqual(options["analysis_mode"], "multimodal")
        self.assertIn("两类信息都是理解视频的重要证据", video_summary.MULTIMODAL_PROMPT_TEMPLATE)
        self.assertIn("## 关键画面信息", video_summary.MULTIMODAL_PROMPT_TEMPLATE)

    def test_meta_exposes_stable_analysis_mode(self):
        meta = video_summary.VideoMeta("video", "id", "https://example.com/video")

        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            video_summary.write_meta(
                meta,
                output_dir,
                options={"analysis_mode": "multimodal", "with_vision": True},
            )
            payload = json.loads((output_dir / "meta.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["analysis_mode"], "multimodal")
        self.assertTrue(payload["options"]["with_vision"])

    def test_vision_rejects_text_only_modes(self):
        for flag in ("--no-llm", "--export-prompt", "--summary-from-file"):
            argv = ["https://example.com/video", "--with-vision", flag]
            if flag == "--summary-from-file":
                argv.append("transcript.txt")
            with self.subTest(flag=flag), self.assertRaises(video_summary.AppError):
                video_summary.validate_args(video_summary.parse_args(argv))

    def test_max_chars_must_be_positive(self):
        for value in ("0", "-1"):
            args = video_summary.parse_args(
                ["https://example.com/video", "--max-chars", value]
            )
            with self.subTest(value=value), self.assertRaises(
                video_summary.AppError
            ) as context:
                video_summary.validate_args(args)

            self.assertIn("--max-chars", context.exception.message)

    def test_summary_provider_defaults_to_local_ollama(self):
        with patch.dict(os.environ, {}, clear=True):
            config = video_summary.resolve_summary_config("ollama")

        self.assertEqual(config.provider, "ollama")
        self.assertEqual(config.model, "qwen3-vl:8b-instruct-q4_K_M")
        self.assertEqual(config.base_url, "http://host.docker.internal:11434/v1")
        self.assertIsNone(config.chunk_chars)
        self.assertEqual(config.context_length, 8192)

    def test_api_summary_requires_key(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(video_summary.AppError) as context:
            video_summary.resolve_summary_config("api")

        self.assertEqual(context.exception.code, "missing_openai_api_key")


class DoctorTests(unittest.TestCase):
    def test_doctor_does_not_require_video_url_or_enter_pipeline(self):
        args = video_summary.parse_args(["--doctor"])

        with patch.object(video_summary, "load_dotenv"), patch.object(
            video_summary, "parse_args", return_value=args
        ), patch.object(
            video_summary, "run_doctor", return_value=0
        ) as run_doctor, patch.object(
            video_summary, "load_prompt_template"
        ) as load_prompt:
            result = video_summary.main()

        self.assertEqual(result, 0)
        self.assertIsNone(args.url)
        run_doctor.assert_called_once_with(args)
        load_prompt.assert_not_called()

    def test_normal_mode_still_requires_video_url(self):
        with tempfile.TemporaryDirectory() as temp_name:
            args = video_summary.parse_args(
                ["--no-llm", "--output", str(Path(temp_name) / "outputs")]
            )
            stderr = io.StringIO()
            with patch.object(video_summary, "load_dotenv"), patch.object(
                video_summary, "parse_args", return_value=args
            ), patch.object(video_summary.sys, "stderr", stderr):
                result = video_summary.main()

        self.assertEqual(result, 1)
        self.assertIn("请提供视频链接", stderr.getvalue())

    def test_python_dependency_check_reports_missing_distribution(self):
        def resolve_version(distribution):
            if distribution == "openai":
                raise video_summary.importlib.metadata.PackageNotFoundError
            return "1.0"

        with patch.object(
            video_summary.importlib.metadata,
            "version",
            side_effect=resolve_version,
        ):
            check = video_summary.check_python_dependencies()

        self.assertEqual(check.status, "fail")
        self.assertIn("openai", check.message)

    def test_command_line_tool_check_reports_missing_tool(self):
        with patch.object(video_summary.shutil, "which", return_value=None):
            check = video_summary.check_command_line_tool("ffmpeg")

        self.assertEqual(check.id, "tool_ffmpeg")
        self.assertEqual(check.status, "fail")

    def test_output_writable_check_passes_without_creating_output_directory(self):
        with tempfile.TemporaryDirectory() as temp_name:
            output = Path(temp_name) / "not-created" / "outputs"

            check = video_summary.check_output_writable(str(output))

            self.assertEqual(check.status, "pass")
            self.assertFalse(output.exists())
            self.assertEqual(list(Path(temp_name).iterdir()), [])

    def test_output_writable_check_reports_write_failure(self):
        with tempfile.TemporaryDirectory() as temp_name, patch.object(
            video_summary.tempfile,
            "mkstemp",
            side_effect=PermissionError,
        ):
            check = video_summary.check_output_writable(temp_name)

        self.assertEqual(check.status, "fail")

    def test_model_probe_passes_when_target_model_exists(self):
        with patch.object(
            video_summary.urllib_request,
            "urlopen",
            return_value=JsonResponse({"data": [{"id": "qwen:latest"}]}),
        ):
            checks = video_summary.probe_model_service(
                prefix="summary",
                api_key="secret",
                base_url="http://service.test/v1",
                model="qwen",
                timeout=300,
                strict_model_listing=True,
            )

        self.assertEqual([check.status for check in checks], ["pass", "pass"])

    def test_model_probe_fails_when_local_target_model_is_missing(self):
        with patch.object(
            video_summary.urllib_request,
            "urlopen",
            return_value=JsonResponse({"data": [{"id": "other-model"}]}),
        ):
            checks = video_summary.probe_model_service(
                prefix="summary",
                api_key="secret",
                base_url="http://service.test/v1",
                model="qwen",
                timeout=300,
                strict_model_listing=True,
            )

        self.assertEqual(checks[-1].status, "fail")
        self.assertIn("qwen", checks[-1].message)

    def test_api_without_model_listing_is_warning(self):
        for status_code in (403, 404):
            with self.subTest(status_code=status_code):
                error = urllib_error.HTTPError(
                    "https://api.example.test/v1/models",
                    status_code,
                    "model listing unavailable",
                    {},
                    None,
                )
                with patch.object(
                    video_summary.urllib_request,
                    "urlopen",
                    side_effect=error,
                ):
                    checks = video_summary.probe_model_service(
                        prefix="summary",
                        api_key="secret",
                        base_url="https://api.example.test/v1",
                        model="cloud-model",
                        timeout=300,
                        strict_model_listing=False,
                    )

                self.assertEqual(
                    [check.status for check in checks],
                    ["pass", "warn"],
                )

    def test_model_probe_reports_connection_failure_without_exception_details(self):
        secret = "secret-value-that-must-not-leak"
        with patch.object(
            video_summary.urllib_request,
            "urlopen",
            side_effect=urllib_error.URLError(secret),
        ):
            checks = video_summary.probe_model_service(
                prefix="summary",
                api_key=secret,
                base_url="http://private-host.test/v1",
                model="qwen",
                timeout=300,
                strict_model_listing=True,
            )

        serialized = json.dumps(
            [video_summary.asdict(check) for check in checks],
            ensure_ascii=False,
        )
        self.assertEqual(checks[0].status, "fail")
        self.assertNotIn(secret, serialized)
        self.assertNotIn("private-host", serialized)

    def test_doctor_only_checks_vision_when_requested(self):
        summary_config = video_summary.SummaryConfig(
            provider="ollama",
            api_key="ollama",
            base_url="http://summary.test/v1",
            model="summary-model",
            timeout=30,
        )
        vision_config = video_summary.VisionConfig(
            api_key="ollama",
            base_url="http://vision.test/v1",
            model="vision-model",
            timeout=30,
        )

        def probe(**kwargs):
            prefix = kwargs["prefix"]
            return [video_summary.DoctorCheck(f"{prefix}_service", "pass", "可用")]

        common_patches = [
            patch.object(
                video_summary,
                "check_python_dependencies",
                return_value=video_summary.DoctorCheck("python_dependencies", "pass", "可用"),
            ),
            patch.object(
                video_summary,
                "check_command_line_tool",
                side_effect=lambda name: video_summary.DoctorCheck(f"tool_{name}", "pass", "可用"),
            ),
            patch.object(
                video_summary,
                "check_output_writable",
                return_value=video_summary.DoctorCheck("output_writable", "pass", "可用"),
            ),
            patch.object(video_summary, "resolve_summary_config", return_value=summary_config),
            patch.object(video_summary, "resolve_vision_config", return_value=vision_config),
            patch.object(video_summary, "probe_model_service", side_effect=probe),
        ]
        for doctor_args, expected_calls in (
            (video_summary.parse_args(["--doctor"]), 1),
            (video_summary.parse_args(["--doctor", "--with-vision"]), 2),
        ):
            with self.subTest(with_vision=doctor_args.with_vision):
                active = [item.start() for item in common_patches]
                try:
                    output = io.StringIO()
                    with redirect_stdout(output):
                        result = video_summary.run_doctor(doctor_args)
                    self.assertEqual(result, 0)
                    self.assertEqual(active[-1].call_count, expected_calls)
                finally:
                    for item in reversed(common_patches):
                        item.stop()

    def test_doctor_outputs_redact_sensitive_values_and_json_has_stable_shape(self):
        secret = "doctor-secret"
        private_host = "private-host.test"
        config = video_summary.SummaryConfig(
            provider="ollama",
            api_key=secret,
            base_url=f"http://{private_host}/v1",
            model="qwen",
            timeout=30,
        )
        passing = video_summary.DoctorCheck("check", "pass", "可用")
        for json_output in (False, True):
            with self.subTest(json=json_output):
                argv = ["--doctor", "--json"] if json_output else ["--doctor"]
                args = video_summary.parse_args(argv)
                with patch.object(
                    video_summary, "check_python_dependencies", return_value=passing
                ), patch.object(
                    video_summary, "check_command_line_tool", return_value=passing
                ), patch.object(
                    video_summary, "check_output_writable", return_value=passing
                ), patch.object(
                    video_summary, "resolve_summary_config", return_value=config
                ), patch.object(
                    video_summary.urllib_request,
                    "urlopen",
                    side_effect=urllib_error.URLError(f"{secret}@{private_host}"),
                ):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        result = video_summary.run_doctor(args)

                serialized = output.getvalue()
                self.assertEqual(result, 1)
                self.assertNotIn(secret, serialized)
                self.assertNotIn(private_host, serialized)
                if json_output:
                    payload = json.loads(serialized)
                    self.assertFalse(payload["ok"])
                    self.assertEqual(payload["mode"], "doctor")
                    self.assertEqual(
                        set(payload),
                        {"ok", "mode", "checks", "summary"},
                    )
                    self.assertEqual(
                        set(payload["summary"]),
                        {"pass", "warn", "fail"},
                    )


class SubtitleTests(unittest.TestCase):
    def test_bilibili_ai_subtitle_language_and_type_are_detected_from_file(self):
        meta = video_summary.VideoMeta("video", "BV1", "https://www.bilibili.com/video/BV1")

        source, warnings = video_summary.detect_subtitle_source(Path("video.ai-zh.srt"), meta)

        self.assertEqual(source, "auto_subtitle")
        self.assertEqual(meta.selected_subtitle_lang, "ai-zh")
        self.assertEqual(meta.selected_subtitle_type, "automatic")
        self.assertIn("ai-zh", meta.automatic_caption_langs)
        self.assertTrue(any("自动字幕（ai-zh）" in warning for warning in warnings))

    def test_bilibili_login_requirement_is_reported_before_whisper_fallback(self):
        result = subprocess.CompletedProcess(
            args=["yt-dlp"],
            returncode=0,
            stdout="",
            stderr="WARNING: [BiliBili] Subtitles are only available when logged in.",
        )
        meta = video_summary.VideoMeta("video", "BV1", "https://www.bilibili.com/video/BV1")
        warnings = []

        with tempfile.TemporaryDirectory() as temp_name, patch.object(
            video_summary, "require_tool"
        ), patch.object(video_summary, "run_command", return_value=result):
            subtitle = video_summary.download_subtitle(
                meta.webpage_url,
                Path(temp_name),
                None,
                "zh.*,ai-zh,en.*",
                meta,
                warnings,
            )

        self.assertIsNone(subtitle)
        self.assertTrue(any("需要登录态" in warning for warning in warnings))

    def test_cached_automatic_subtitle_restores_quality_warning(self):
        meta = video_summary.VideoMeta("video", "BV1", "https://www.bilibili.com/video/BV1")
        warnings = []

        video_summary.restore_cached_subtitle_metadata(
            {
                "selected_subtitle_lang": "ai-zh",
                "selected_subtitle_type": "automatic",
            },
            meta,
            warnings,
        )
        video_summary.restore_cached_subtitle_metadata(
            {
                "selected_subtitle_lang": "ai-zh",
                "selected_subtitle_type": "automatic",
            },
            meta,
            warnings,
        )

        self.assertEqual(meta.selected_subtitle_lang, "ai-zh")
        self.assertEqual(meta.selected_subtitle_type, "automatic")
        self.assertEqual(meta.automatic_caption_langs, ["ai-zh"])
        self.assertEqual(
            warnings,
            ["使用的是平台自动字幕（ai-zh），内容可能存在识别错误。"],
        )

    def test_download_prefers_requested_manual_subtitle_from_current_run(self):
        meta = video_summary.VideoMeta(
            "video",
            "BV1",
            "https://www.bilibili.com/video/BV1",
            subtitle_langs=["zh"],
            automatic_caption_langs=["ai-zh"],
        )
        captured_args = []

        def create_subtitles(args, cwd):
            captured_args.extend(args)
            (cwd / "subtitle-source-123.ai-zh.srt").write_text("自动字幕", encoding="utf-8")
            (cwd / "subtitle-source-123.zh.srt").write_text("人工字幕", encoding="utf-8")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as temp_name, patch.object(
            video_summary, "require_tool"
        ), patch.object(video_summary.time, "time_ns", return_value=123), patch.object(
            video_summary, "run_command", side_effect=create_subtitles
        ):
            selected = video_summary.download_subtitle(
                meta.webpage_url,
                Path(temp_name),
                None,
                "zh,ai-zh",
                meta,
            )

        self.assertEqual(selected[0].name, "subtitle-source-123.zh.srt")
        self.assertEqual(selected[1], "manual_subtitle")
        self.assertIn("--force-overwrites", captured_args)

    def test_download_does_not_reuse_subtitle_from_previous_run(self):
        meta = video_summary.VideoMeta("video", "BV1", "https://www.bilibili.com/video/BV1")
        result = subprocess.CompletedProcess(
            args=["yt-dlp"],
            returncode=0,
            stdout="",
            stderr="",
        )

        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "old-video.zh.srt").write_text("旧字幕", encoding="utf-8")
            with patch.object(video_summary, "require_tool"), patch.object(
                video_summary.time, "time_ns", return_value=456
            ), patch.object(video_summary, "run_command", return_value=result):
                selected = video_summary.download_subtitle(
                    meta.webpage_url,
                    output_dir,
                    None,
                    "zh.*",
                    meta,
                )

        self.assertIsNone(selected)


class CacheSignatureTests(unittest.TestCase):
    def test_cookie_file_update_changes_cache_identity_without_reading_content(self):
        with tempfile.TemporaryDirectory() as temp_name:
            cookie_path = Path(temp_name) / "cookies.txt"
            cookie_path.write_text("first", encoding="utf-8")
            first = video_summary.cookie_cache_identity(
                video_summary.CookieConfig(file=str(cookie_path))
            )
            first_mtime = cookie_path.stat().st_mtime_ns
            cookie_path.write_text("other", encoding="utf-8")
            os.utime(cookie_path, ns=(first_mtime + 1_000_000, first_mtime + 1_000_000))
            second = video_summary.cookie_cache_identity(
                video_summary.CookieConfig(file=str(cookie_path))
            )

        self.assertEqual(first["path"], str(cookie_path.resolve()))
        self.assertEqual(first["size"], second["size"])
        self.assertNotEqual(first["mtime_ns"], second["mtime_ns"])
        self.assertNotEqual(
            video_summary.stable_signature(first),
            video_summary.stable_signature(second),
        )
        self.assertEqual(set(first), {"type", "path", "size", "mtime_ns"})
        self.assertEqual(
            video_summary.cookie_cache_identity(
                video_summary.CookieConfig(browser="chrome")
            ),
            {"type": "browser", "browser": "chrome"},
        )


class SummaryChunkTests(unittest.TestCase):
    def test_ollama_context_determines_default_chunk_size(self):
        config = video_summary.SummaryConfig(
            provider="ollama",
            api_key="ollama",
            base_url="http://localhost:11434/v1",
            model="qwen3-vl:8b",
            timeout=300,
            context_length=8192,
        )

        self.assertEqual(video_summary.determine_summary_chunk_chars(config, 12000), 6392)

    def test_balanced_split_uses_two_similar_chunks(self):
        text = "\n".join(f"[00:00:{index:02d}] " + "内容" * 90 for index in range(50))

        chunks = video_summary.split_text_balanced(text, 5000)

        self.assertEqual(len(chunks), 2)
        self.assertLessEqual(max(map(len, chunks)), 5000)
        self.assertLess(abs(len(chunks[0]) - len(chunks[1])), 500)

    def test_balanced_split_does_not_leave_tiny_subtitle_tail(self):
        text = "\n".join(f"[00:00:{index:02d}] " + "内容" * 40 for index in range(58))

        chunks = video_summary.split_text_balanced(text, 5000)

        self.assertEqual(len(chunks), 2)
        self.assertLess(abs(len(chunks[0]) - len(chunks[1])), 300)

    def test_merge_material_requires_full_coverage_of_every_chunk(self):
        material = video_summary.build_summary_merge_material(["第一段", "第二段"])

        self.assertIn("第 1/2 段摘要", material)
        self.assertIn("第 2/2 段摘要", material)
        self.assertIn("开头、中间和结尾", material)
        self.assertIn("不得声称“原文截断”", material)

    def test_ollama_chunk_summaries_have_sufficient_output_budget(self):
        config = video_summary.SummaryConfig(
            provider="ollama",
            api_key="ollama",
            base_url="http://localhost:11434/v1",
            model="qwen3-vl:8b",
            timeout=300,
            context_length=8192,
        )
        meta = video_summary.VideoMeta("video", "video", "https://example.com/video")

        with patch.object(
            video_summary,
            "call_summary_llm",
            return_value="完整摘要",
        ) as call:
            result = video_summary.summarize_with_llm(
                None,
                config,
                "a" * 7000,
                meta,
                "whisper",
                12000,
                video_summary.DEFAULT_PROMPT_TEMPLATE,
            )

        self.assertEqual(result.chunks, 2)
        self.assertEqual(
            [item.args[4] for item in call.call_args_list],
            [
                video_summary.OLLAMA_PARTIAL_SUMMARY_TOKENS,
                video_summary.OLLAMA_PARTIAL_SUMMARY_TOKENS,
                1300,
            ],
        )
        self.assertEqual(video_summary.OLLAMA_PARTIAL_SUMMARY_TOKENS, 1000)
        self.assertEqual(
            video_summary.expanded_output_token_budget(
                video_summary.OLLAMA_PARTIAL_SUMMARY_TOKENS
            ),
            2000,
        )


class SummaryResponseTests(unittest.TestCase):
    def test_ollama_length_truncation_retries_once_with_larger_budget(self):
        config = video_summary.SummaryConfig(
            provider="ollama",
            api_key="ollama",
            base_url="http://localhost:11434/v1",
            model="qwen",
            timeout=30,
            context_length=8192,
        )
        responses = [
            {"done_reason": "length", "message": {"content": "部分结果"}},
            {"done_reason": "stop", "message": {"content": "完整结果"}},
        ]

        with patch.object(
            video_summary,
            "ollama_request",
            side_effect=responses,
        ) as request:
            text = video_summary.call_summary_llm(
                None,
                config,
                config.model,
                "prompt",
                100,
            )

        self.assertEqual(text, "完整结果")
        self.assertEqual(
            [call.args[2]["options"]["num_predict"] for call in request.call_args_list],
            [100, 200],
        )

    def test_repeated_ollama_truncation_fails(self):
        config = video_summary.SummaryConfig(
            provider="ollama",
            api_key="ollama",
            base_url="http://localhost:11434/v1",
            model="qwen",
            timeout=30,
        )
        response = {"done_reason": "length", "message": {"content": "部分结果"}}

        with patch.object(
            video_summary,
            "ollama_request",
            side_effect=[response, response],
        ), self.assertRaises(video_summary.LLMOutputTruncatedError):
            video_summary.call_summary_llm(None, config, config.model, "prompt", 100)

    def test_openai_length_truncation_retries_and_empty_output_fails(self):
        requests = []
        responses = [
            types.SimpleNamespace(
                choices=[
                    types.SimpleNamespace(
                        finish_reason="length",
                        message=types.SimpleNamespace(content="部分结果"),
                    )
                ]
            ),
            types.SimpleNamespace(
                choices=[
                    types.SimpleNamespace(
                        finish_reason="stop",
                        message=types.SimpleNamespace(content="完整结果"),
                    )
                ]
            ),
        ]

        def create(**kwargs):
            requests.append(kwargs)
            return responses.pop(0)

        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
        )
        config = video_summary.SummaryConfig(
            provider="api",
            api_key="key",
            base_url="https://api.openai.com/v1",
            model="model",
            timeout=30,
        )

        text = video_summary.call_summary_llm(client, config, config.model, "prompt", 100)

        self.assertEqual(text, "完整结果")
        self.assertEqual([request["max_tokens"] for request in requests], [100, 200])

        empty_client = types.SimpleNamespace(
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(
                    create=lambda **_kwargs: types.SimpleNamespace(
                        choices=[
                            types.SimpleNamespace(
                                finish_reason="stop",
                                message=types.SimpleNamespace(content="  "),
                            )
                        ]
                    )
                )
            )
        )
        with self.assertRaisesRegex(RuntimeError, "空内容"):
            video_summary.call_summary_llm(
                empty_client,
                config,
                config.model,
                "prompt",
                100,
            )

    def test_missing_completion_reason_fails_for_both_providers(self):
        ollama_config = video_summary.SummaryConfig(
            provider="ollama",
            api_key="ollama",
            base_url="http://localhost:11434/v1",
            model="qwen",
            timeout=30,
        )
        with patch.object(
            video_summary,
            "ollama_request",
            return_value={"message": {"content": "未确认完成的结果"}},
        ) as request, self.assertRaisesRegex(RuntimeError, "缺少结束原因"):
            video_summary.call_summary_llm(
                None,
                ollama_config,
                ollama_config.model,
                "prompt",
                100,
            )
        self.assertEqual(request.call_count, 1)

        api_config = video_summary.SummaryConfig(
            provider="api",
            api_key="key",
            base_url="https://api.openai.com/v1",
            model="model",
            timeout=30,
        )
        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(
                    create=lambda **_kwargs: types.SimpleNamespace(
                        choices=[
                            types.SimpleNamespace(
                                message=types.SimpleNamespace(
                                    content="未确认完成的结果"
                                )
                            )
                        ]
                    )
                )
            )
        )
        with self.assertRaisesRegex(RuntimeError, "缺少结束原因"):
            video_summary.call_summary_llm(
                client,
                api_config,
                api_config.model,
                "prompt",
                100,
            )


class FrameSelectionTests(unittest.TestCase):
    def test_sampling_plan_adapts_to_video_duration(self):
        short = video_summary.build_vision_sampling_plan(60, 5, 45, 60)
        medium = video_summary.build_vision_sampling_plan(510, 5, 45, 60)
        long = video_summary.build_vision_sampling_plan(3600, 5, 45, 60)

        self.assertEqual(short.max_frames, 8)
        self.assertEqual(medium.max_frames, 18)
        self.assertEqual(long.max_frames, 60)
        self.assertEqual(medium.scan_interval, 7.083)
        self.assertEqual(long.scan_interval, 15)
        self.assertEqual(long.max_gap, 45)

    def test_sampling_plan_uses_safe_budget_when_duration_is_unknown(self):
        plan = video_summary.build_vision_sampling_plan(None, 5, 45, 60)

        self.assertEqual(plan.max_frames, 16)
        self.assertEqual(plan.scan_interval, 5)

    def test_filters_similar_frames_but_keeps_changes_scenes_and_max_gap(self):
        candidates = [
            video_summary.FrameCandidate(Path("0.jpg"), 0, "periodic"),
            video_summary.FrameCandidate(Path("5.jpg"), 5, "periodic"),
            video_summary.FrameCandidate(Path("10.jpg"), 10, "periodic"),
            video_summary.FrameCandidate(Path("12.jpg"), 12, "scene"),
            video_summary.FrameCandidate(Path("35.jpg"), 35, "periodic"),
        ]
        hashes = {
            Path("0.jpg"): 0,
            Path("5.jpg"): 0,
            Path("10.jpg"): (1 << 64) - 1,
            Path("12.jpg"): (1 << 64) - 1,
            Path("35.jpg"): (1 << 64) - 1,
        }

        with patch.object(video_summary, "image_difference_hash", side_effect=lambda path: hashes[path]):
            selected = video_summary.filter_frame_candidates(candidates, max_gap=20, max_frames=60)

        self.assertEqual([frame.timestamp for frame in selected], [0, 10, 12, 35])

    def test_selection_respects_maximum(self):
        candidates = [
            video_summary.FrameCandidate(Path(f"{index}.jpg"), index * 5, "periodic")
            for index in range(20)
        ]

        with patch.object(video_summary, "image_difference_hash", side_effect=range(20)):
            selected = video_summary.filter_frame_candidates(
                candidates,
                max_gap=5,
                max_frames=6,
                hash_threshold=0,
            )

        self.assertEqual(len(selected), 6)
        self.assertEqual(selected[0].timestamp, 0)
        self.assertEqual(selected[-1].timestamp, 95)

    def test_keyframe_cache_requires_all_files(self):
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            frame_path = output_dir / "frames/one.jpg"
            frame_path.parent.mkdir()
            frame_path.write_bytes(b"frame")
            frames = [video_summary.FrameCandidate(frame_path, 5, "scene", 123)]
            cache = {"schema_version": 1, "stages": {}}
            video_summary.update_cache_stage(
                cache,
                "keyframes",
                "signature",
                frames=video_summary.serialize_keyframes(frames, output_dir),
            )

            restored = video_summary.load_cached_keyframes(cache, "signature", output_dir)
            frame_path.unlink()
            missing = video_summary.load_cached_keyframes(cache, "signature", output_dir)

        self.assertEqual(restored[0].timestamp, 5)
        self.assertIsNone(missing)


class VisionOutputTests(unittest.TestCase):
    def test_nearby_transcript_context_uses_timestamp_window(self):
        transcript = "[00:00:05] 开头\n[00:00:20] 当前内容\n[00:01:00] 远处内容\n"

        context = video_summary.nearby_transcript_context(transcript, 18, window_seconds=5)

        self.assertEqual(context, "当前内容")

    def test_normalizes_visual_response_with_timestamps(self):
        output_dir = Path("/tmp/output")
        frames = [
            video_summary.FrameCandidate(output_dir / "frames/one.jpg", 5, "periodic"),
            video_summary.FrameCandidate(output_dir / "frames/two.jpg", 12, "scene"),
        ]
        payload = {
            "frames": [
                {
                    "index": 1,
                    "description": "显示项目首页",
                    "visible_text": ["开始", "设置"],
                    "importance": "high",
                    "uncertainty": "",
                },
                {
                    "index": 2,
                    "description": "打开设置页面",
                    "visible_text": "模型配置",
                    "importance": "medium",
                    "uncertainty": "小字无法辨认",
                },
            ]
        }

        results = video_summary.normalize_visual_batch(payload, frames, output_dir)

        self.assertEqual(results[0]["timestamp"], "00:00:05")
        self.assertEqual(results[0]["visible_text"], "开始；设置")
        self.assertEqual(results[1]["frame_source"], "scene")

    def test_multimodal_context_is_sorted_by_time(self):
        transcript = "[00:00:10] 第二句话\n[00:00:02] 第一句话\n"
        visuals = [
            {
                "timestamp_seconds": 5,
                "timestamp": "00:00:05",
                "description": "显示流程图",
                "visible_text": "输入→输出",
                "uncertainty": "",
            }
        ]

        context = video_summary.build_multimodal_context(transcript, visuals)

        self.assertLess(context.index("00:00:02"), context.index("00:00:05"))
        self.assertLess(context.index("00:00:05"), context.index("00:00:10"))
        self.assertIn("屏幕文字：输入→输出", context)

    def test_multimodal_context_removes_exact_duplicate_visuals(self):
        visuals = [
            {
                "timestamp_seconds": 5,
                "timestamp": "00:00:05",
                "description": "显示同一页面",
                "visible_text": "设置",
                "uncertainty": "",
            },
            {
                "timestamp_seconds": 10,
                "timestamp": "00:00:10",
                "description": "显示同一页面",
                "visible_text": "设置",
                "uncertainty": "",
            },
        ]

        context = video_summary.build_multimodal_context("", visuals)

        self.assertIn("00:00:05", context)
        self.assertNotIn("00:00:10", context)

    def test_multimodal_context_keeps_only_new_lines_from_incremental_slide(self):
        visuals = [
            {
                "timestamp_seconds": 5,
                "timestamp": "00:00:05",
                "description": "显示第一步",
                "visible_text": "标题\n选项 A",
                "uncertainty": "",
            },
            {
                "timestamp_seconds": 10,
                "timestamp": "00:00:10",
                "description": "增加第二步",
                "visible_text": "标题\n选项 A\n选项 B",
                "uncertainty": "",
            },
        ]

        context = video_summary.build_multimodal_context("", visuals)

        self.assertEqual(context.count("选项 A"), 1)
        self.assertEqual(context.count("选项 B"), 1)

    def test_compaction_splits_semicolon_text_and_removes_subtitle_overlap(self):
        visuals = [
            {
                "timestamp_seconds": 5,
                "timestamp": "00:00:05",
                "description": "显示步骤",
                "visible_text": "固定页眉；开始执行；参数 A",
                "importance": "medium",
                "uncertainty": "",
            },
            {
                "timestamp_seconds": 10,
                "timestamp": "00:00:10",
                "description": "显示下一步",
                "visible_text": "固定页眉；开始执行；参数 B",
                "importance": "medium",
                "uncertainty": "",
            },
            {
                "timestamp_seconds": 15,
                "timestamp": "00:00:15",
                "description": "显示完成",
                "visible_text": "固定页眉；开始执行；参数 C",
                "importance": "medium",
                "uncertainty": "",
            },
        ]

        compacted = video_summary.compact_visual_material(
            visuals,
            "[00:00:05] 现在开始执行\n",
        )
        combined = "\n".join(item["visible_text"] for item in compacted)

        self.assertNotIn("固定页眉", combined)
        self.assertNotIn("开始执行", combined)
        self.assertIn("参数 A", combined)

    def test_multimodal_budget_preserves_transcript_and_limits_visuals(self):
        transcript = "\n".join(
            f"[00:00:{index:02d}] 第 {index} 条字幕内容"
            for index in range(30)
        )
        visuals = [
            {
                "timestamp_seconds": index,
                "timestamp": video_summary.format_timestamp(index),
                "description": "画面新增信息" * 20,
                "visible_text": f"参数 {index} " * 20,
                "importance": "high",
                "uncertainty": "",
            }
            for index in range(20)
        ]

        context = video_summary.build_multimodal_context(
            transcript,
            visuals,
            max_chars=len(transcript) + 500,
        )
        base_context = video_summary.build_multimodal_context(transcript, [])
        visual_budget = video_summary.determine_visual_char_budget(
            len(transcript) + 500,
            len(base_context),
        )

        self.assertIn("第 29 条字幕内容", context)
        self.assertLessEqual(len(context), len(base_context) + visual_budget)
        self.assertLess(context.count("] 画面："), len(visuals))

    def test_long_transcript_keeps_visual_budget_and_splits_later(self):
        transcript = "\n".join(
            f"[00:00:{index:02d}] 第 {index} 条字幕" + "完整内容" * 30
            for index in range(20)
        )
        visuals = [
            {
                "timestamp_seconds": 10,
                "timestamp": "00:00:10",
                "description": "展示字幕中没有提到的系统架构图" + "节点关系" * 100,
                "visible_text": "入口服务 → 任务队列 → 分析服务",
                "importance": "high",
                "uncertainty": "",
            }
        ]

        context = video_summary.build_multimodal_context(
            transcript,
            visuals,
            max_chars=600,
        )
        chunks = video_summary.split_text_balanced(context, 600)

        self.assertIn("第 19 条字幕", context)
        self.assertIn("] 画面：", context)
        self.assertGreater(len(context), 600)
        self.assertGreater(len(chunks), 1)

    def test_multimodal_budget_keeps_exact_command_evidence(self):
        visuals = [
            {
                "timestamp_seconds": index,
                "timestamp": video_summary.format_timestamp(index),
                "description": f"第 {index} 个画面" + "说明" * 20,
                "visible_text": (
                    "$ npx skills add owner/repo --skill example"
                    if index == 8
                    else f"普通文字 {index}" * 10
                ),
                "importance": "high",
                "uncertainty": "",
            }
            for index in range(10)
        ]

        context = video_summary.build_multimodal_context("", visuals, max_chars=650)

        self.assertIn("npx skills add owner/repo --skill example", context)

    def test_parses_json_inside_markdown_fence(self):
        payload = video_summary.parse_json_object('```json\n{"frames": []}\n```')

        self.assertEqual(payload, {"frames": []})

    def test_analyzes_frames_and_writes_completed_artifact(self):
        response = types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(
                        content=(
                            '{"frames":[{"index":1,"description":"首页",'
                            '"visible_text":"开始","importance":"high","uncertainty":""}]}'
                        )
                    )
                )
            ]
        )
        completions = types.SimpleNamespace(create=lambda **_kwargs: response)
        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))
        config = video_summary.VisionConfig("ollama", "http://localhost/v1", "qwen", 30)

        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            frame_path = output_dir / "frame.jpg"
            frame_path.write_bytes(b"image")
            frames = [video_summary.FrameCandidate(frame_path, 0, "periodic")]

            results = video_summary.analyze_keyframes(
                client,
                config,
                frames,
                output_dir,
                1,
                analysis_signature="analysis-signature",
            )
            artifact = video_summary.parse_json_object((output_dir / "visual_context.json").read_text())

        self.assertEqual(results[0]["description"], "首页")
        self.assertEqual(artifact["status"], "completed")
        self.assertEqual(artifact["analysis_signature"], "analysis-signature")

    def test_visual_resume_requires_matching_analysis_signature(self):
        config = video_summary.VisionConfig("ollama", "http://localhost/v1", "qwen", 30)
        frames = [
            {
                "timestamp_seconds": 0,
                "timestamp": "00:00:00",
                "image": "frame.jpg",
                "frame_source": "periodic",
                "description": "旧画面结果",
                "visible_text": "",
                "importance": "medium",
                "uncertainty": "",
            }
        ]

        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            video_summary.write_visual_outputs(
                output_dir,
                config,
                frames,
                "processing",
                analysis_signature="old-signature",
            )
            matching = video_summary.load_visual_results(
                output_dir,
                config.model,
                "old-signature",
            )
            stale = video_summary.load_visual_results(
                output_dir,
                config.model,
                "new-signature",
            )
            artifact = json.loads(
                (output_dir / "visual_context.json").read_text(encoding="utf-8")
            )

        self.assertEqual(matching, frames)
        self.assertEqual(stale, [])
        self.assertEqual(artifact["analysis_signature"], "old-signature")

    def test_visual_failure_is_strict_and_keeps_failed_artifact(self):
        def fail(**_kwargs):
            raise RuntimeError("model stopped")

        completions = types.SimpleNamespace(create=fail)
        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))
        config = video_summary.VisionConfig("ollama", "http://localhost/v1", "qwen", 30)

        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            frame_path = output_dir / "frame.jpg"
            frame_path.write_bytes(b"image")
            frames = [video_summary.FrameCandidate(frame_path, 0, "periodic")]

            with patch.object(video_summary.time, "sleep"), self.assertRaises(video_summary.AppError):
                video_summary.analyze_keyframes(client, config, frames, output_dir, 1)
            artifact = video_summary.parse_json_object((output_dir / "visual_context.json").read_text())

        self.assertEqual(artifact["status"], "failed")
        self.assertIn("model stopped", artifact["error"])

    def test_visual_resume_only_requests_missing_frames(self):
        calls = []

        def create(**kwargs):
            calls.append(kwargs)
            return types.SimpleNamespace(
                choices=[
                    types.SimpleNamespace(
                        message=types.SimpleNamespace(
                            content=(
                                '{"frames":[{"index":1,"description":"第二页",'
                                '"visible_text":"","importance":"medium","uncertainty":""}]}'
                            )
                        )
                    )
                ]
            )

        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
        )
        config = video_summary.VisionConfig("ollama", "http://localhost/v1", "qwen", 30)
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            first_path = output_dir / "first.jpg"
            second_path = output_dir / "second.jpg"
            first_path.write_bytes(b"first")
            second_path.write_bytes(b"second")
            frames = [
                video_summary.FrameCandidate(first_path, 0, "periodic"),
                video_summary.FrameCandidate(second_path, 5, "scene"),
            ]
            existing = [
                {
                    "timestamp_seconds": 0,
                    "timestamp": "00:00:00",
                    "image": "first.jpg",
                    "frame_source": "periodic",
                    "description": "第一页",
                    "visible_text": "",
                    "importance": "medium",
                    "uncertainty": "",
                }
            ]

            results = video_summary.analyze_keyframes(
                client,
                config,
                frames,
                output_dir,
                1,
                existing_results=existing,
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual([item["description"] for item in results], ["第一页", "第二页"])

    def test_visual_batch_falls_back_to_smaller_requests(self):
        calls = []

        def create(**kwargs):
            image_count = sum(
                item.get("type") == "image_url"
                for item in kwargs["messages"][1]["content"]
            )
            calls.append(image_count)
            if image_count > 1:
                raise RuntimeError("context length exceeded")
            return types.SimpleNamespace(
                choices=[
                    types.SimpleNamespace(
                        message=types.SimpleNamespace(
                            content=(
                                '{"frames":[{"index":1,"description":"单帧结果",'
                                '"visible_text":"","importance":"medium","uncertainty":""}]}'
                            )
                        )
                    )
                ]
            )

        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
        )
        config = video_summary.VisionConfig("ollama", "http://localhost/v1", "qwen", 30)
        with tempfile.TemporaryDirectory() as temp_name, patch.object(video_summary.time, "sleep"):
            output_dir = Path(temp_name)
            frames = []
            for index in range(2):
                path = output_dir / f"{index}.jpg"
                path.write_bytes(b"image")
                frames.append(video_summary.FrameCandidate(path, index * 5, "periodic"))

            results = video_summary.analyze_keyframes(
                client,
                config,
                frames,
                output_dir,
                2,
            )
            artifact = video_summary.parse_json_object(
                (output_dir / "visual_context.json").read_text()
            )

        self.assertEqual(len(results), 2)
        self.assertEqual(calls, [2, 2, 1, 1])
        self.assertEqual(artifact["metrics"]["fallback_splits"], 1)

    def test_visual_batch_remembers_successful_smaller_size(self):
        calls = []

        def create(**kwargs):
            image_count = sum(
                item.get("type") == "image_url"
                for item in kwargs["messages"][1]["content"]
            )
            calls.append(image_count)
            if image_count > 2:
                raise RuntimeError("context length exceeded")
            frames = ",".join(
                f'{{"index":{index},"description":"结果 {index}",'
                '"visible_text":"","importance":"medium","uncertainty":""}'
                for index in range(1, image_count + 1)
            )
            return types.SimpleNamespace(
                choices=[
                    types.SimpleNamespace(
                        message=types.SimpleNamespace(content=f'{{"frames":[{frames}]}}')
                    )
                ]
            )

        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
        )
        config = video_summary.VisionConfig("ollama", "http://localhost/v1", "qwen", 30)
        with tempfile.TemporaryDirectory() as temp_name, patch.object(video_summary.time, "sleep"):
            output_dir = Path(temp_name)
            frame_items = []
            for index in range(6):
                path = output_dir / f"{index}.jpg"
                path.write_bytes(b"image")
                frame_items.append(video_summary.FrameCandidate(path, index * 5, "periodic"))

            results = video_summary.analyze_keyframes(
                client,
                config,
                frame_items,
                output_dir,
                4,
            )
            artifact = video_summary.parse_json_object(
                (output_dir / "visual_context.json").read_text()
            )

        self.assertEqual(len(results), 6)
        self.assertEqual(calls, [4, 4, 2, 2, 2])
        self.assertEqual(artifact["metrics"]["effective_batch_size"], 2)


class TemporaryVideoCleanupTests(unittest.TestCase):
    def test_download_failure_removes_only_current_prefix_artifacts(self):
        result = subprocess.CompletedProcess(
            args=["yt-dlp"],
            returncode=1,
            stdout="",
            stderr="download failed",
        )

        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            current_part = output_dir / "vision-source-123.mp4.part"
            current_fragment = output_dir / "vision-source-123.f137.mp4"
            unrelated = output_dir / "vision-source-older.mp4"
            current_part.write_bytes(b"partial")
            current_fragment.write_bytes(b"fragment")
            unrelated.write_bytes(b"keep")

            with patch.object(video_summary, "require_tool"), patch.object(
                video_summary.time, "time_ns", return_value=123
            ), patch.object(
                video_summary, "run_command", return_value=result
            ), self.assertRaises(video_summary.AppError):
                video_summary.download_video(
                    "https://example.com/video",
                    output_dir,
                    None,
                )

            self.assertFalse(current_part.exists())
            self.assertFalse(current_fragment.exists())
            self.assertTrue(unrelated.exists())

    def test_missing_download_candidate_removes_partial_artifacts(self):
        result = subprocess.CompletedProcess(
            args=["yt-dlp"],
            returncode=0,
            stdout="",
            stderr="",
        )

        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            partial = output_dir / "vision-source-456.mp4.part"
            partial.write_bytes(b"partial")

            with patch.object(video_summary, "require_tool"), patch.object(
                video_summary.time, "time_ns", return_value=456
            ), patch.object(
                video_summary, "run_command", return_value=result
            ), self.assertRaises(video_summary.AppError) as context:
                video_summary.download_video(
                    "https://example.com/video",
                    output_dir,
                    None,
                )

            self.assertEqual(context.exception.code, "video_not_found")
            self.assertFalse(partial.exists())

    def test_early_failure_removes_video_downloaded_for_current_run(self):
        with tempfile.TemporaryDirectory() as temp_name:
            args = video_summary.parse_args(
                [
                    "https://example.com/video",
                    "--with-vision",
                    "--output",
                    temp_name,
                ]
            )
            summary_config = video_summary.SummaryConfig(
                provider="api",
                api_key="key",
                base_url="https://api.openai.com/v1",
                model="summary-model",
                timeout=30,
            )
            vision_config = video_summary.VisionConfig(
                "key",
                "https://api.openai.com/v1",
                "vision-model",
                30,
            )
            meta = video_summary.VideoMeta(
                "video",
                "id",
                "https://example.com/video",
                duration=60,
            )
            downloaded = {}

            def create_downloaded_video(_url, output_dir, _cookies):
                path = output_dir / "vision-source-current.mp4"
                path.write_bytes(b"video")
                downloaded["path"] = path
                return path

            with patch.object(video_summary, "load_dotenv"), patch.object(
                video_summary, "parse_args", return_value=args
            ), patch.object(
                video_summary, "resolve_summary_config", return_value=summary_config
            ), patch.object(
                video_summary, "create_summary_client", return_value=object()
            ), patch.object(
                video_summary, "resolve_cookie_config", return_value=video_summary.CookieConfig()
            ), patch.object(
                video_summary, "require_tool"
            ), patch.object(
                video_summary, "load_meta", return_value=meta
            ), patch.object(
                video_summary, "resolve_vision_config", return_value=vision_config
            ), patch.object(
                video_summary, "create_vision_client", return_value=object()
            ), patch.object(
                video_summary, "download_video", side_effect=create_downloaded_video
            ), patch.object(
                video_summary, "download_subtitle", return_value=None
            ), patch.object(
                video_summary,
                "extract_audio_from_video",
                side_effect=video_summary.AppError("audio_failed", "音频抽取失败"),
            ):
                result = video_summary.main()

            self.assertEqual(result, 1)
            self.assertFalse(downloaded["path"].exists())


if __name__ == "__main__":
    unittest.main()
