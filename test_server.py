import importlib.util
import base64
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


MODULE_PATH = os.path.join(os.path.dirname(__file__), "server.py")
SPEC = importlib.util.spec_from_file_location("launcher_server", MODULE_PATH)
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class CodexSessionTest(unittest.TestCase):
    def setUp(self):
        server.CODEX_HEAD_CACHE.clear()

    def test_incomplete_session_head_is_not_cached(self):
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".jsonl") as source:
            self.assertEqual("", server.codex_session_head(source.name)["id"])
            source.write(json.dumps({
                "type": "session_meta",
                "payload": {
                    "id": "019fd08a-e352-7a22-9aa5-0b5d0de94eba",
                    "cwd": "/tmp/project",
                    "source": "cli",
                    "thread_source": "user",
                },
            }) + "\n")
            source.flush()
            self.assertEqual(
                "019fd08a-e352-7a22-9aa5-0b5d0de94eba",
                server.codex_session_head(source.name)["id"],
            )

    def test_pane_agent_skips_guardian_log(self):
        guardian = "019fd08a-e3ee-7462-ad86-e427701a464d"
        main = "019fd08a-e352-7a22-9aa5-0b5d0de94eba"
        ps_result = SimpleNamespace(
            stdout="123 1 /Users/demo/.local/bin/codex prompt\n"
        )
        lsof_result = SimpleNamespace(stdout=(
            f"codex 123 1u REG /tmp/rollout-now-{guardian}.jsonl\n"
            f"codex 123 2u REG /tmp/rollout-now-{main}.jsonl\n"
        ))
        heads = {
            guardian: {"thread_source": "subagent", "subagent": True},
            main: {"thread_source": "user", "subagent": False},
        }
        with (
            mock.patch.object(server.subprocess, "run", side_effect=[ps_result, lsof_result]),
            mock.patch.object(server, "find_log_by_id", side_effect=lambda _tool, sid: sid),
            mock.patch.object(server, "codex_session_head", side_effect=lambda path: heads[path]),
        ):
            agent = server.pane_agent({"tty_name": "ttys001"})
        self.assertEqual(main, agent["explicit_id"])

    def test_codex_internal_context_is_not_shown_as_user_message(self):
        internal = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": (
                        '<codex_internal_context source="goal">\n'
                        "Continue working toward the active thread goal.\n"
                        "</codex_internal_context>"
                    ),
                }],
            },
        }
        user = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "監視を続けて"}],
            },
        }

        self.assertIsNone(server.user_message_entry(internal, "codex"))
        self.assertEqual(
            {"role": "user", "text": "監視を続けて"},
            server.user_message_entry(user, "codex"),
        )

    def test_codex_tool_result_image_is_saved_for_chat_rendering(self):
        image = b"\x89PNG\r\n\x1a\n" + b"test-image"
        item = {
            "type": "event_msg",
            "payload": {
                "type": "mcp_tool_call_end",
                "result": {"Ok": {"content": [{
                    "type": "image",
                    "data": base64.b64encode(image).decode(),
                    "detail": "original",
                }]}},
            },
        }
        with tempfile.TemporaryDirectory() as upload_dir, mock.patch.object(
            server, "UPLOAD_DIR", upload_dir
        ):
            first = server.assistant_parts(item, "codex")
            second = server.assistant_parts(item, "codex")
            files = os.listdir(os.path.join(upload_dir, "codex-images"))

        self.assertEqual(first, second)
        self.assertEqual(1, len(files))
        self.assertEqual("assistant", first[0]["role"])
        self.assertRegex(
            first[0]["text"],
            r"^添付画像: .*/codex-images/codex-[0-9a-f]{16}\.png$",
        )

    def test_codex_tool_result_rejects_invalid_image_data(self):
        item = {
            "type": "event_msg",
            "payload": {
                "type": "mcp_tool_call_end",
                "result": {"Ok": {"content": [
                    {"type": "image", "data": base64.b64encode(b"not-image").decode()},
                    {"type": "image", "data": "invalid-base64"},
                ]}},
            },
        }
        with tempfile.TemporaryDirectory() as upload_dir, mock.patch.object(
            server, "UPLOAD_DIR", upload_dir
        ):
            self.assertEqual([], server.assistant_parts(item, "codex"))
        self.assertFalse(os.path.exists(os.path.join(upload_dir, "codex-images")))


class SessionArtifactTest(unittest.TestCase):
    def test_create_command_with_environment_variable_is_detected(self):
        command = (
            "cd /tmp/repo && SKIP_REVIEW_GATE=1 "
            "gh pr create --base develop"
        )

        self.assertEqual(["pr"], server.GH_CREATE_RE.findall(command))

    def test_create_command_mentioned_in_argument_is_not_detected(self):
        command = "rg 'SKIP_REVIEW_GATE=1 gh pr create' README.md"

        self.assertEqual([], server.GH_CREATE_RE.findall(command))


class LocalTranscriptionTest(unittest.TestCase):
    def test_voice_recording_falls_back_to_default_constraints(self):
        self.assertIn("channelCount: {{ideal: 1}}", server.TERMINAL_PAGE)
        self.assertIn("getUserMedia({{audio: true}})", server.TERMINAL_PAGE)

    def test_voice_recording_transcribes_pcm_in_one_second_chunks(self):
        self.assertIn("const voiceChunkDuration = 1000", server.TERMINAL_PAGE)
        self.assertIn("createScriptProcessor(4096, 1, 1)", server.TERMINAL_PAGE)
        self.assertIn('type: "audio/wav"', server.TERMINAL_PAGE)
        self.assertIn("queueVoiceTranscription(blob)", server.TERMINAL_PAGE)
        self.assertIn("voicePendingJob = {{blob, finalResult}}", server.TERMINAL_PAGE)

    def test_voice_input_separates_committed_and_provisional_text(self):
        self.assertIn("commonPrefix(voicePreviousHypothesis, text)", server.TERMINAL_PAGE)
        self.assertIn('"認識中: " + provisional', server.TERMINAL_PAGE)
        self.assertIn("queueVoiceTranscription(blob, true)", server.TERMINAL_PAGE)
        self.assertIn("event.preventDefault()", server.TERMINAL_PAGE)
        self.assertIn("text.startsWith(voiceCommittedText)", server.TERMINAL_PAGE)

    def test_builds_prompt_from_current_session_context(self):
        item = {
            "cwd": "/projects/agent-deck",
            "tool": "codex",
            "log_path": "/tmp/session.jsonl",
            "artifacts": [{"repo": "lc-infrastructure"}],
        }
        messages = [{
            "role": "user",
            "text": "`MicroCMS` のWebhookを libe-city で確認して",
        }]
        with (
            mock.patch.object(
                server, "SPEECH_TERMS", ["Agent Deck（エージェントデッキ）", "Codex"]
            ),
            mock.patch.object(server, "session_messages", return_value=messages),
        ):
            prompt = server.speech_context_prompt(item)

        self.assertIn("Agent Deck", prompt)
        self.assertIn("lc-infrastructure", prompt)
        self.assertIn("MicroCMS", prompt)
        self.assertIn("libe-city", prompt)

    def test_rejects_unsupported_audio_type(self):
        with self.assertRaisesRegex(ValueError, "WebM"):
            server.transcribe_audio(b"audio", "application/octet-stream")

    def test_reports_missing_local_runtime(self):
        with mock.patch.object(server, "SPEECH_PYTHON", "/missing/python"):
            with self.assertRaisesRegex(RuntimeError, "未セットアップ"):
                server.transcribe_audio(b"audio", "audio/webm")

    def test_transcribes_and_removes_temporary_audio(self):
        with tempfile.TemporaryDirectory() as workdir:
            with (
                mock.patch.object(server, "TRANSCRIPTION_DIR", workdir),
                mock.patch.object(server, "SPEECH_PYTHON", __file__),
                mock.patch.object(
                    server, "_run_speech_worker",
                    return_value={"text": " テスト入力です。 "},
                ) as run_worker,
            ):
                text = server.transcribe_audio(
                    b"audio", "audio/webm;codecs=opus", "Agent Deck"
                )
                remaining = os.listdir(workdir)
                prompt = run_worker.call_args.args[1]

        self.assertEqual("テスト入力です。", text)
        self.assertEqual([], remaining)
        self.assertEqual("Agent Deck", prompt)

    def test_rejects_common_silence_hallucination(self):
        with tempfile.TemporaryDirectory() as workdir:
            with (
                mock.patch.object(server, "TRANSCRIPTION_DIR", workdir),
                mock.patch.object(server, "SPEECH_PYTHON", __file__),
                mock.patch.object(
                    server, "_run_speech_worker",
                    return_value={"text": "ご視聴ありがとうございました。"},
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "マイクに近づいて"):
                    server.transcribe_audio(b"audio", "audio/webm")


class SessionPinTest(unittest.TestCase):
    def test_pinned_session_is_rendered_before_active_unpinned_session(self):
        sessions = [
            self._session("agent-pinned", pinned=True),
            self._session("agent-active", pinned=False),
            self._session("agent-other", pinned=False),
        ]
        with (
            mock.patch.object(server, "managed_sessions", return_value=sessions),
            mock.patch.object(server, "wezterm_panes", return_value=[]),
        ):
            sidebar = server.build_sidebar("agent-active")

        self.assertLess(
            sidebar.index("session=agent-pinned"),
            sidebar.index("session=agent-active"),
        )

    def test_pinned_metadata_is_restored_on_restarted_session(self):
        calls = []
        with mock.patch.object(
            server, "tmux_run",
            side_effect=lambda *args: calls.append(args) or SimpleNamespace(),
        ):
            server.set_session_metadata(
                "agent-new", "summary", "session-id", False, "note", True
            )

        self.assertIn(
            ("set-option", "-t", "agent-new", "@launcher_pinned", "1"), calls
        )

    @staticmethod
    def _session(name, pinned=False):
        return {
            "name": name, "tool": "codex", "cwd": "/tmp/project",
            "summary": "summary", "last_message": "summary", "note": "",
            "running": False, "background": "", "context": None,
            "artifacts": [], "pinned": pinned,
        }


class VersionUpdateTest(unittest.TestCase):
    def test_semver_is_compared_numerically(self):
        self.assertEqual((1, 10, 2), server.version_tuple("v1.10.2"))
        self.assertGreater(server.version_tuple("0.10.0"), server.version_tuple("0.9.9"))
        self.assertIsNone(server.version_tuple("latest"))

    def test_update_is_rejected_when_worktree_has_local_changes(self):
        dirty = SimpleNamespace(returncode=0, stdout=" M server.py\n", stderr="")
        with mock.patch.object(server.subprocess, "run", return_value=dirty) as run:
            with self.assertRaisesRegex(RuntimeError, "ローカル変更"):
                server.install_release("0.2.0")

        run.assert_called_once()

    def test_invalid_release_tag_is_rejected_before_git_is_called(self):
        with mock.patch.object(server.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "バージョンが不正"):
                server.install_release("main")

        run.assert_not_called()


class ClaudeProjectDirTest(unittest.TestCase):
    def test_non_ascii_and_punctuation_are_replaced(self):
        self.assertEqual(
            "-Users-xxx-Dropbox-----",
            server.claude_project_dir("/Users/xxx/Dropbox (個人)"),
        )

    def test_conversation_log_path_uses_encoded_project_dir(self):
        session_id = "019fd08a-e352-7a22-9aa5-0b5d0de94eba"
        cwd = "/Users/xxx/Dropbox (個人)"
        with tempfile.TemporaryDirectory() as home, mock.patch.object(
            server, "HOME", home
        ):
            project = os.path.join(
                home, ".claude", "projects", server.claude_project_dir(cwd)
            )
            os.makedirs(project)
            log_path = os.path.join(project, f"{session_id}.jsonl")
            with open(log_path, "w"):
                pass

            self.assertEqual(
                log_path,
                server.conversation_log_path("claude", cwd, session_id),
            )

    def test_resume_candidates_uses_encoded_project_dir(self):
        session_id = "019fd08a-e352-7a22-9aa5-0b5d0de94eba"
        cwd = "/Users/xxx/Dropbox (個人)"
        with tempfile.TemporaryDirectory() as home, (
            mock.patch.object(server, "HOME", home)
        ), mock.patch.object(server, "log_meta", return_value={}):
            project = os.path.join(
                home, ".claude", "projects", server.claude_project_dir(cwd)
            )
            os.makedirs(project)
            log_path = os.path.join(project, f"{session_id}.jsonl")
            with open(log_path, "w"):
                pass

            candidates = server.resume_candidates("claude", cwd)

        self.assertEqual([session_id], [item["id"] for item in candidates])
        self.assertEqual(log_path, candidates[0]["path"])


class ClaudeShellCommandTest(unittest.TestCase):
    def test_user_shell_command_is_rendered_as_markdown(self):
        item = {
            "type": "user",
            "message": {"content": (
                "<user_shell_command>\n"
                "<command>gh auth login --web</command>\n"
                "<result>Exit code: 0\nOutput:\nAuthentication complete.</result>\n"
                "</user_shell_command>"
            )},
        }

        self.assertEqual({
            "role": "user",
            "text": (
                "```sh\n$ gh auth login --web\n```\n\n"
                "```\nExit code: 0\nOutput:\nAuthentication complete.\n```"
            ),
        }, server.user_message_entry(item, "claude"))
        self.assertEqual(
            "$ gh auth login --web",
            server.user_summary_text(item, "claude"),
        )

    def test_running_github_device_auth_is_rendered(self):
        screen = """
! First copy your one-time code: ABCD-1234
Open this URL to continue in your web browser: https://github.com/login/device
(11s)
"""
        self.assertEqual(
            "**GitHub認証待ちです**\n\n"
            "ワンタイムコード: `ABCD-1234`\n\n"
            "[GitHubの認証ページを開く](https://github.com/login/device)",
            server.parse_shell_auth_screen(screen),
        )

    def test_running_codex_github_auth_is_read_from_tmux_screen(self):
        screen = SimpleNamespace(
            returncode=0,
            stdout=(
                "! First copy your one-time code: ABCD-1234\n"
                "Open this URL to continue in your web browser:\n"
                "https://github.com/login/device\n"
            ),
        )
        with mock.patch.object(server, "tmux_run", return_value=screen):
            auth = server.pending_shell_auth("agent-test", "codex")

        self.assertIn("ABCD-1234", auth)
        self.assertIn("https://github.com/login/device", auth)

    def test_completed_github_device_auth_is_not_rendered_as_pending(self):
        screen = """
! First copy your one-time code: ABCD-1234
Open this URL to continue in your web browser: https://github.com/login/device
Authentication complete.
"""
        self.assertEqual("", server.parse_shell_auth_screen(screen))


class CodexQuestionTest(unittest.TestCase):
    def test_parses_codex_choices_and_wrapped_description(self):
        screen = """
• Calling Browser tool

  Field 1/1
  Allow Browser use to use full CDP access on http://localhost:3005

  › 1. Allow         Run the tool and continue.
    2. Always allow  Run the tool and remember this choice for future tool
                     calls.
    3. Cancel        Cancel this tool call
  enter to submit | esc to cancel
"""
        self.assertEqual({
            "question": "Allow Browser use to use full CDP access on http://localhost:3005",
            "choices": [
                {"number": 1, "label": "Allow", "description": "Run the tool and continue."},
                {
                    "number": 2,
                    "label": "Always allow",
                    "description": "Run the tool and remember this choice for future tool calls.",
                },
                {"number": 3, "label": "Cancel", "description": "Cancel this tool call"},
            ],
        }, server.parse_codex_question_screen(screen))


class ClaudeQuestionTest(unittest.TestCase):
    MCP_DIALOG = """\
[Screen Reader Mode: on via flag]
New MCP server found in this project: mfc_ca
MCP servers may execute code or access system resources. All tool calls require
approval. Learn more in the MCP documentation.
1. Use this MCP server
2. Use this and all future MCP servers in this project
3. Continue without using this MCP server
Enter selection [1-3], or Escape to cancel:
Enter to confirm · Esc to cancel
"""

    def test_parses_startup_mcp_dialog(self):
        result = server.parse_question_screen(self.MCP_DIALOG)
        self.assertEqual(
            "New MCP server found in this project: mfc_ca MCP servers may "
            "execute code or access system resources. All tool calls require "
            "approval. Learn more in the MCP documentation.",
            result["question"],
        )
        self.assertEqual(
            ["Use this MCP server",
             "Use this and all future MCP servers in this project",
             "Continue without using this MCP server"],
            [choice["label"] for choice in result["choices"]],
        )

    def test_quoted_dialog_text_is_not_a_question(self):
        # 会話に引用されたダイアログ風テキストは、下に本文やフッターが
        # 続くので選択待ちとして拾わない
        screen = self.MCP_DIALOG.replace(
            "Enter to confirm · Esc to cancel",
            "以上です。これは選択プロンプトの引用ですね。\n"
            "auto mode on (shift+tab to cycle)\n/rc\n$",
        )
        self.assertIsNone(server.parse_question_screen(screen))

    def test_real_dialog_wins_over_quoted_text_above(self):
        screen = (
            "claude: 復唱します:\n"
            "1. Use this MCP server\n"
            "2. Use this and all future MCP servers in this project\n"
            "3. Continue without using this MCP server\n"
            "Enter selection [1-3], or Escape to cancel:\n"
            "以上です。\n"
            "☐ りんごとみかんどちらが好き？\n"
            "1. りんご — 甘酸っぱい\n"
            "2. みかん — ジューシー\n"
            "Enter selection [1-2], or Escape to cancel:\n"
        )
        result = server.parse_question_screen(screen)
        self.assertEqual("りんごとみかんどちらが好き？", result["question"])
        self.assertEqual(
            ["りんご", "みかん"],
            [choice["label"] for choice in result["choices"]],
        )


if __name__ == "__main__":
    unittest.main()
