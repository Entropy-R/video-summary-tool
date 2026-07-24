import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


if importlib.util.find_spec("dotenv") is None:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None
    sys.modules["dotenv"] = dotenv

import video_summary


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

        self.assertIn("第 29 条字幕内容", context)
        self.assertLessEqual(len(context), len(transcript) + 500)
        self.assertLess(context.count("] 画面："), len(visuals))

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

            results = video_summary.analyze_keyframes(client, config, frames, output_dir, 1)
            artifact = video_summary.parse_json_object((output_dir / "visual_context.json").read_text())

        self.assertEqual(results[0]["description"], "首页")
        self.assertEqual(artifact["status"], "completed")

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


if __name__ == "__main__":
    unittest.main()
