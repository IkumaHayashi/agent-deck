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


class FrontendTemplateTest(unittest.TestCase):
    def test_new_page_uses_external_frontend_assets(self):
        page = server.render(host="localhost:8787")

        self.assertIn('/static/new.css?v=', page)
        self.assertIn('/static/new.js?v=', page)
        self.assertIn('data-panel="reviews-panel"', page)
        self.assertIn('<details id="prompt-details" open>', page)
        self.assertNotIn("{static_version}", page)

    def test_template_path_cannot_escape_template_directory(self):
        with self.assertRaisesRegex(ValueError, "テンプレート名"):
            server.load_template("../server.py")

    def test_empty_session_list_stays_on_list_page(self):
        handler = object.__new__(server.Handler)
        handler.client_address = ("127.0.0.1", 12345)
        handler.path = "/"
        handler.headers = {}
        with (
            mock.patch.object(server, "managed_sessions", return_value=[]),
            mock.patch.object(handler, "_page") as page,
            mock.patch.object(handler, "_redirect") as redirect,
        ):
            handler.do_GET()

        redirect.assert_not_called()
        self.assertIn("セッション一覧 - Agent Deck", page.call_args.args[0])

    def test_startup_question_is_not_hidden_by_empty_conversation(self):
        self.assertIn(
            "if (!messages.length && !activity && !question && !auth)",
            server.TERMINAL_PAGE,
        )


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

    def test_codex_custom_tool_output_image_is_saved_for_chat_rendering(self):
        image = b"\x89PNG\r\n\x1a\n" + b"custom-tool-image"
        item = {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "output": [{
                    "type": "input_image",
                    "image_url": "data:image/png;base64," + base64.b64encode(image).decode(),
                }],
            },
        }
        with tempfile.TemporaryDirectory() as upload_dir, mock.patch.object(
            server, "UPLOAD_DIR", upload_dir
        ):
            parts = server.assistant_parts(item, "codex")
            files = os.listdir(os.path.join(upload_dir, "codex-images"))

        self.assertEqual(1, len(files))
        self.assertEqual("assistant", parts[0]["role"])
        self.assertRegex(
            parts[0]["text"],
            r"^添付画像: .*/codex-images/codex-[0-9a-f]{16}\.png$",
        )

    def test_codex_file_citation_is_saved_for_chat_rendering(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as upload_dir:
            source = os.path.join(source_dir, "確認用 PDF.pdf")
            with open(source, "wb") as output:
                output.write(b"%PDF-1.4\ntest")
            item = {
                "type": "response_item",
                "payload": {
                    "role": "assistant",
                    "content": [{
                        "type": "output_text",
                        "text": (
                            "確認用PDF："
                            f':codex-file-citation{{path="{source}" purpose="output"}}'
                        ),
                    }],
                },
            }
            with mock.patch.object(server, "UPLOAD_DIR", upload_dir):
                parts = server.assistant_parts(item, "codex")

            self.assertRegex(
                parts[0]["text"],
                r"^確認用PDF：\n\n添付ファイル: .*/codex-files/codex-[0-9a-f]{16}-確認用_PDF\.pdf$",
            )
            saved = parts[0]["text"].split("添付ファイル: ", 1)[1]
            with open(saved, "rb") as copied:
                self.assertEqual(b"%PDF-1.4\ntest", copied.read())

    def test_missing_codex_file_citation_remains_visible(self):
        citation = ':codex-file-citation{path="/missing/sample.pdf" purpose="output"}'
        self.assertEqual(citation, server.materialize_codex_file_citations(citation))


class ShellCommandTest(unittest.TestCase):
    def test_launches_managed_tmux_shell_with_multiline_command(self):
        result = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            tempfile.TemporaryDirectory() as cwd,
            mock.patch.object(server, "HOME", os.path.realpath(cwd)),
            mock.patch.object(server, "tmux_run", return_value=result) as tmux,
            mock.patch.object(server, "invalidate_session_cache"),
        ):
            session = server.launch_shell_command(cwd, "npm install\nnpm test")

        self.assertRegex(session, r"^agent-shell-\d{8}-\d{6}-[0-9a-f]{8}$")
        launch = tmux.call_args_list[0].args
        self.assertEqual("new-session", launch[0])
        self.assertEqual(os.path.realpath(cwd), launch[launch.index("-c") + 1])
        self.assertIn("npm install\nnpm test", launch[-1])

    def test_reports_tmux_shell_launch_failure(self):
        result = SimpleNamespace(returncode=1, stdout="", stderr="start failed")
        with (
            tempfile.TemporaryDirectory() as cwd,
            mock.patch.object(server, "HOME", os.path.realpath(cwd)),
            mock.patch.object(server, "tmux_run", return_value=result),
        ):
            with self.assertRaisesRegex(RuntimeError, "start failed"):
                server.launch_shell_command(cwd, "npm install")


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


class PullRequestDiffTest(unittest.TestCase):
    def test_normalizes_number_and_github_url(self):
        self.assertEqual("123", server.normalize_pr_selector("123"))
        self.assertEqual(
            "https://github.com/example/repo/pull/456",
            server.normalize_pr_selector("https://github.com/example/repo/pull/456"),
        )

    def test_rejects_non_github_selector(self):
        with self.assertRaisesRegex(ValueError, "PR番号"):
            server.normalize_pr_selector("https://example.com/repo/pull/1")

    def test_fetches_current_branch_pull_request_and_patch(self):
        metadata = {
            "number": 42,
            "title": "差分ペインを追加",
            "url": "https://github.com/example/repo/pull/42",
            "state": "OPEN",
            "baseRefName": "main",
            "headRefName": "feature/review",
            "files": [{"path": "server.py", "additions": 10, "deletions": 2}],
        }
        results = [
            SimpleNamespace(returncode=0, stdout=json.dumps(metadata), stderr=""),
            SimpleNamespace(returncode=0, stdout="diff --git a/server.py b/server.py\n", stderr=""),
        ]
        with (
            tempfile.TemporaryDirectory() as cwd,
            mock.patch.object(server, "find_bin", return_value="/usr/bin/gh"),
            mock.patch.object(server.subprocess, "run", side_effect=results) as run,
        ):
            result = server.pull_request_diff(cwd)

        self.assertEqual(42, result["number"])
        self.assertIn("diff --git", result["patch"])
        self.assertEqual(
            ["/usr/bin/gh", "pr", "view", "--json",
             "number,title,url,state,baseRefName,headRefName,files"],
            run.call_args_list[0].args[0],
        )
        self.assertEqual(
            ["/usr/bin/gh", "pr", "diff", "42", "--patch"],
            run.call_args_list[1].args[0],
        )

    def test_missing_current_branch_pull_request_has_friendly_error(self):
        failed = SimpleNamespace(returncode=1, stdout="", stderr="no pull requests found")
        with (
            tempfile.TemporaryDirectory() as cwd,
            mock.patch.object(server.subprocess, "run", return_value=failed),
        ):
            with self.assertRaisesRegex(LookupError, "現在のブランチ"):
                server.pull_request_diff(cwd)

    def test_normalizes_github_remote_urls(self):
        self.assertEqual(
            "example/repo", server.github_repo_name("git@github.com:Example/Repo.git")
        )
        self.assertEqual(
            "example/repo", server.github_repo_name("https://github.com/Example/Repo.git")
        )
        self.assertEqual("", server.github_repo_name("https://gitlab.com/example/repo.git"))

    def test_review_requests_include_matching_local_project(self):
        payload = [{
            "number": 7,
            "title": "レビュー対象",
            "url": "https://github.com/example/repo/pull/7",
            "repository": {"nameWithOwner": "Example/Repo"},
            "author": {"login": "octocat"},
            "updatedAt": "2026-08-17T00:00:00Z",
            "isDraft": False,
        }]
        completed = SimpleNamespace(
            returncode=0, stdout=json.dumps(payload), stderr=""
        )
        with (
            mock.patch.object(server, "find_bin", return_value="/usr/bin/gh"),
            mock.patch.object(server.subprocess, "run", return_value=completed) as run,
            mock.patch.object(
                server, "local_github_repositories",
                return_value={"example/repo": "/tmp/repo"},
            ),
        ):
            items = server.github_review_requests()

        self.assertEqual("/tmp/repo", items[0]["cwd"])
        self.assertEqual("Example/Repo", items[0]["repositoryName"])
        self.assertEqual("--review-requested=@me", run.call_args.args[0][3])

    def test_pull_request_target_requires_configured_local_repository(self):
        with mock.patch.object(server, "local_github_repositories", return_value={}):
            with self.assertRaisesRegex(LookupError, "Agent Deck"):
                server.pull_request_target("https://github.com/example/repo/pull/12")


class SessionPinTest(unittest.TestCase):
    def test_pinned_session_is_rendered_before_active_unpinned_session(self):
        sessions = [
            self._session("agent-pinned", pinned=True),
            self._session("agent-active", pinned=False),
            self._session("agent-other", pinned=False),
        ]
        with mock.patch.object(server, "managed_sessions", return_value=sessions):
            sidebar = server.build_sidebar("agent-active")

        self.assertLess(
            sidebar.index("session=agent-pinned"),
            sidebar.index("session=agent-active"),
        )

    def test_active_session_keeps_its_original_position(self):
        sessions = [
            self._session("agent-newer"),
            self._session("agent-active"),
            self._session("agent-older"),
        ]
        with mock.patch.object(server, "managed_sessions", return_value=sessions):
            sidebar = server.build_sidebar("agent-active")

        self.assertLess(
            sidebar.index("session=agent-newer"),
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

    def test_pull_request_metadata_is_saved(self):
        calls = []
        with mock.patch.object(
            server, "tmux_run",
            side_effect=lambda *args: calls.append(args) or SimpleNamespace(),
        ):
            server.set_session_metadata(
                "agent-new", pull_request="https://github.com/example/repo/pull/42"
            )

        self.assertIn(
            (
                "set-option", "-t", "agent-new", "@launcher_pull_request",
                "https://github.com/example/repo/pull/42",
            ),
            calls,
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

    def test_parses_codex_app_sign_in_choices(self):
        screen = """
• Calling codex_apps.gmail.get_profile({})


  Gmail

  Sign in to Gmail on ChatGPT to use it in Codex.

  URL
  https://chatgpt.com/apps/gmail/connector_example

  Sign in to this app in your browser, then return here.


  › 1. Open sign-in URL
    2. Back
  Use tab / ↑ ↓ to move, enter to select, esc to close
"""
        self.assertEqual({
            "question": (
                "Gmail Sign in to Gmail on ChatGPT to use it in Codex. URL "
                "https://chatgpt.com/apps/gmail/connector_example "
                "Sign in to this app in your browser, then return here."
            ),
            "choices": [
                {"number": 1, "label": "Open sign-in URL", "description": ""},
                {"number": 2, "label": "Back", "description": ""},
            ],
        }, server.parse_codex_question_screen(screen))

    def test_quoted_codex_app_dialog_is_not_a_question(self):
        screen = """
  › 1. Open sign-in URL
    2. Back
  Use tab / ↑ ↓ to move, enter to select, esc to close

• 認証画面は上記の内容でした。
"""
        self.assertIsNone(server.parse_codex_question_screen(screen))


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


class WaitClassifierTest(unittest.TestCase):
    def setUp(self):
        server.WAIT_CLASS_CACHE.clear()
        server.WAIT_CLASS_PENDING.clear()

    @mock.patch.object(server, "wait_classifier_context", return_value="直近の会話")
    @mock.patch.object(server.subprocess, "run")
    def test_uses_configured_model(self, run, _context):
        run.return_value = SimpleNamespace(returncode=0, stdout="完了\n")

        with mock.patch.object(server, "WAIT_CLASS_MODEL", "sonnet"):
            server.run_wait_classifier("/tmp/session.jsonl", "claude", (1, 2, "claude"))

        args, kwargs = run.call_args
        self.assertEqual("sonnet", args[0][3])
        self.assertEqual("/tmp", kwargs["cwd"])
        self.assertEqual("完了", server.WAIT_CLASS_CACHE["/tmp/session.jsonl"]["label"])


class ScreenRunningTest(unittest.TestCase):
    def test_codex_ignores_spinner_before_completed_short_response(self):
        screen = "\n".join(
            [
                "• Working (24s • esc to interrupt)",
                "• 完了しました。",
                "────────────────────────────────────────",
                "› Write tests for @filename",
            ]
        )

        self.assertFalse(server.screen_is_running(screen, "codex"))

    def test_codex_detects_spinner_after_last_completed_response(self):
        screen = "\n".join(
            [
                "• 前の回答です。",
                "────────────────────────────────────────",
                "› 新しい依頼",
                "• 調べています。",
                "• Working (3s • esc to interrupt)",
                "› Improve documentation in @filename",
            ]
        )

        self.assertTrue(server.screen_is_running(screen, "codex"))

    def test_codex_does_not_treat_quoted_interrupt_text_as_spinner(self):
        screen = "\n".join(
            [
                "────────────────────────────────────────",
                "• `esc to interrupt`という文字列について説明しました。",
                "────────────────────────────────────────",
                "› Write tests for @filename",
            ]
        )

        self.assertFalse(server.screen_is_running(screen, "codex"))

    def test_claude_keeps_existing_full_screen_detection(self):
        screen = "Running…\n" + "\n".join(
            f"idle line {index}" for index in range(24)
        )

        self.assertTrue(server.screen_is_running(screen, "claude"))


class ScreenBackgroundTest(unittest.TestCase):
    def test_monitor_keeps_watch_status(self):
        screen = "auto mode on · 1 monitor"

        self.assertEqual("監視中", server.screen_background_label(screen))

    def test_background_terminal_does_not_override_wait_status(self):
        screen = "1 background terminal running · /ps to view · /stop to close"

        self.assertEqual("", server.screen_background_label(screen))

    def test_local_agent_does_not_override_wait_status(self):
        screen = "1 local agent running"

        self.assertEqual("", server.screen_background_label(screen))


if __name__ == "__main__":
    unittest.main()
