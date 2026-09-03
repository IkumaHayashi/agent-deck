import importlib.util
import base64
import io
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
# テスト中のセッション一覧取得が利用者の実データを更新しないよう隔離する。
TEST_RUNTIME_DIR = tempfile.TemporaryDirectory()
server.DATA_DIR = TEST_RUNTIME_DIR.name
server.SESSION_REGISTRY_PATH = os.path.join(TEST_RUNTIME_DIR.name, "sessions.json")


class SessionContextTest(unittest.TestCase):
    def test_fable_minor_version_uses_one_million_token_window(self):
        self.assertEqual(1_000_000, server.claude_context_window("claude-fable-5-1"))

    def test_fable_context_percentage_uses_one_million_token_window(self):
        record = {
            "type": "assistant",
            "message": {
                "model": "claude-fable-5-1",
                "usage": {
                    "input_tokens": 86,
                    "cache_creation_input_tokens": 603,
                    "cache_read_input_tokens": 192_543,
                    "output_tokens": 775,
                },
            },
        }
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as log:
            log.write(json.dumps(record) + "\n")
            log.flush()

            self.assertEqual(19, server.session_context(log.name, "claude"))


class FrontendTemplateTest(unittest.TestCase):
    def test_new_page_uses_external_frontend_assets(self):
        page = server.render()

        self.assertIn('/static/new.css?v=', page)
        self.assertIn('/static/new.js?v=', page)
        self.assertIn('data-panel="reviews-panel"', page)
        self.assertIn('<details id="prompt-details" open>', page)
        self.assertNotIn("{static_version}", page)

    def test_new_page_offers_image_picker_for_touch_devices(self):
        page = server.render()

        self.assertIn('id="prompt-image-picker"', page)
        self.assertIn('id="prompt-attach"', page)

    def test_terminal_page_offers_file_picker_for_touch_devices(self):
        # スマホには D&D もペーストもないので 📎 から選べる必要がある
        self.assertIn('id="file-picker" multiple hidden', server.TERMINAL_PAGE)
        self.assertIn('id="attach"', server.TERMINAL_PAGE)
        self.assertIn("filePicker.click()", server.TERMINAL_PAGE)

    def test_terminal_page_offers_custom_question_input(self):
        self.assertIn('if (question.custom)', server.TERMINAL_PAGE)
        self.assertIn('question.custom_prompt', server.TERMINAL_PAGE)
        self.assertIn('custom.placeholder = "自由入力"', server.TERMINAL_PAGE)
        self.assertIn('submit.textContent = "入力して送信"', server.TERMINAL_PAGE)

    def test_resume_conversation_can_be_filtered_by_id(self):
        conversation = {
            "cwd": "/Users/demo/project",
            "tool": "codex",
            "id": "019fd08a-e352-7a22-9aa5-0b5d0de94eba",
            "summary": "絞り込み対象の会話",
            "label": "08/19 12:34",
        }
        with (
            mock.patch.object(server, "recent_conversations", return_value=[conversation]),
            mock.patch.object(server, "resume_group_dir", return_value=conversation["cwd"]),
        ):
            page = server.render()

        self.assertIn('id="resume-id-filter"', page)
        self.assertIn(f'data-resume-id="{conversation["id"]}"', page)
        self.assertIn(f'ID: {conversation["id"]}', page)
        self.assertIn('class="resume-group"', page)

    def test_worktree_conversations_are_grouped_by_common_git_directory(self):
        result = SimpleNamespace(
            returncode=0,
            stdout="/Users/demo/project/.git\n",
            stderr="",
        )
        with mock.patch.object(server.subprocess, "run", return_value=result):
            group = server.resume_group_dir("/Users/demo/project-wt-feature")

        self.assertEqual("/Users/demo/project", group)

    def test_inbox_is_a_prompt_helper(self):
        with (
            mock.patch.object(server, "CW_ENABLED", True),
            mock.patch.object(server, "PINNED", [("demo", "/Users/demo/project")]),
        ):
            page = server.render()

        self.assertEqual(1, page.count('id="inbox-open"'))
        self.assertIn('class="prompt-actions"', page)
        self.assertIn('📥 受信箱から選ぶ', page)
        self.assertNotIn('id="inbox-project-select"', page)
        self.assertNotIn('data-panel="inbox-panel"', page)

    def test_github_review_does_not_use_initial_prompt(self):
        script_path = os.path.join(os.path.dirname(__file__), "static", "new.js")
        with open(script_path, encoding="utf-8") as source:
            script = source.read()

        self.assertIn('if (!f.elements.namedItem("pull_request"))', script)

    def test_github_review_ignores_prompt_on_server(self):
        handler = object.__new__(server.Handler)
        result = SimpleNamespace(returncode=0, stdout="Started session agent-review\n", stderr="")
        with (
            mock.patch.object(server, "validate_dir", return_value=("/tmp/project", "")),
            mock.patch.object(server.subprocess, "run", return_value=result) as run,
            mock.patch.object(server, "wait_for_new_session_id", return_value="session-id"),
            mock.patch.object(server, "set_session_metadata"),
            mock.patch.object(server, "invalidate_session_cache"),
            mock.patch.object(
                server, "pull_request_target",
                return_value={"cwd": "/tmp/project", "number": 42},
            ),
            mock.patch.object(
                server, "pull_request_worktree", return_value="/tmp/worktrees/project-pr-42"
            ) as worktree,
            mock.patch.object(handler, "_redirect"),
        ):
            handler._launch(
                "/tmp/project", prompt="通常起動用の指示",
                pull_request="https://github.com/example/repo/pull/42",
            )

        self.assertNotIn("通常起動用の指示", run.call_args.args[0])
        # PRレビューはPR headを取得したworktreeで起動する
        worktree.assert_called_once()
        self.assertIn("/tmp/worktrees/project-pr-42", run.call_args.args[0])

    def test_claude_resume_finds_moved_log_and_uses_its_latest_cwd(self):
        handler = object.__new__(server.Handler)
        session_id = "019fd08a-e352-7a22-9aa5-0b5d0de94eba"
        result = SimpleNamespace(
            returncode=0, stdout="Started session agent-resumed\n", stderr=""
        )

        def validate(path):
            return path, ""

        with (
            mock.patch.object(server, "validate_dir", side_effect=validate) as validate_mock,
            mock.patch.object(server, "conversation_log_path", return_value=""),
            mock.patch.object(server, "find_log_by_id", return_value="/tmp/moved.jsonl"),
            mock.patch.object(
                server, "claude_session_cwd", return_value="/tmp/project-worktree"
            ),
            mock.patch.object(server, "log_meta", return_value={"summary": "再開テスト"}),
            mock.patch.object(server.subprocess, "run", return_value=result) as run,
            mock.patch.object(server, "set_session_metadata"),
            mock.patch.object(server, "upsert_registered_session"),
            mock.patch.object(server, "invalidate_session_cache"),
            mock.patch.object(handler, "_redirect"),
        ):
            handler._launch("/tmp/project", tool="claude", resume=session_id)

        self.assertEqual(
            [mock.call("/tmp/project"), mock.call("/tmp/project-worktree")],
            validate_mock.call_args_list,
        )
        self.assertIn("/tmp/project-worktree", run.call_args.args[0])
        self.assertIn(session_id, run.call_args.args[0])

    def test_review_context_is_seeded_into_input_without_sending(self):
        # レビュー対象PRは入力欄への書き出しプリセットでAIへ伝える（自動送信はしない）
        self.assertIn("seedReviewContext()", server.TERMINAL_PAGE)
        self.assertIn("をレビューしています。", server.TERMINAL_PAGE)
        self.assertIn(
            'localStorage.getItem(reviewSeedKey) === linkedPullRequest',
            server.TERMINAL_PAGE,
        )

    def test_diff_selection_quote_does_not_name_the_pull_request(self):
        self.assertIn(
            'const parts = ["以下の選択行について確認してください。"]',
            server.TERMINAL_PAGE,
        )

    def test_diff_fetches_directory_comparison_without_pr_picker(self):
        self.assertIn(
            'encodeURIComponent(session) + "/diff"', server.TERMINAL_PAGE
        )
        self.assertNotIn('id="review-picker"', server.TERMINAL_PAGE)
        self.assertIn("デフォルトブランチとの差分", server.TERMINAL_PAGE)
        self.assertIn('linkedPullRequest ? "PRベースブランチ"', server.TERMINAL_PAGE)

    def test_review_autoload_starts_after_draft_storage_is_initialized(self):
        page = server.TERMINAL_PAGE

        self.assertLess(
            page.index('const draftKey = "draft:" + session;'),
            page.index('if (reviewOpenMode !== "never") loadDirectoryDiff();'),
        )

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

    def test_assistant_message_can_be_quoted_into_input(self):
        self.assertIn('id="selection-quote" hidden', server.TERMINAL_PAGE)
        self.assertIn(
            'chat.querySelectorAll(".message.assistant .bubble")',
            server.TERMINAL_PAGE,
        )
        self.assertIn("appendQuoteToInput(text)", server.TERMINAL_PAGE)
        self.assertIn('document.addEventListener("selectionchange"', server.TERMINAL_PAGE)
        self.assertNotIn("selectedQuoteText || item.text", server.TERMINAL_PAGE)
        self.assertIn('line ? "> " + line : ">"', server.TERMINAL_PAGE)

    def test_local_markdown_images_have_caption_and_paging(self):
        page = server.TERMINAL_PAGE

        self.assertIn('id="lightbox-prev"', page)
        self.assertIn('id="lightbox-next"', page)
        self.assertIn('id="lightbox-path"', page)
        self.assertIn('id="lightbox-description"', page)
        self.assertIn('id="lightbox-context"', page)
        self.assertIn('"/api/local-image?path=" + encodeURIComponent(markdownImagePath)', page)
        self.assertIn('fullPath.textContent = path', page)
        self.assertIn('img.dataset.description = alt', page)
        self.assertIn('img.dataset.context = context', page)
        self.assertIn('normalized.length > 160', page)
        self.assertIn('contextLine.textContent = "文章: " + context', page)
        self.assertIn('description.textContent = "説明: " + alt', page)
        self.assertIn('event.key === "ArrowRight"', page)
        self.assertIn('root.querySelectorAll("img.thumb")', page)
        self.assertIn('!event.target.closest("img, figcaption, button")', page)

    def test_resolve_local_image_accepts_real_png(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as image:
            image.write(b"\x89PNG\r\n\x1a\nimage-data")
            image.flush()

            path, content_type = server.resolve_local_image(image.name)

        self.assertEqual(os.path.realpath(image.name), path)
        self.assertEqual("image/png", content_type)

    def test_resolve_local_image_rejects_non_image_content(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as image:
            image.write(b"not-an-image")
            image.flush()

            with self.assertRaisesRegex(ValueError, "画像データ"):
                server.resolve_local_image(image.name)

    def test_resolve_local_image_uses_signature_when_extension_differs(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as image:
            image.write(b"\xff\xd8\xffjpeg-data")
            image.flush()

            _, content_type = server.resolve_local_image(image.name)

        self.assertEqual("image/jpeg", content_type)

    def test_uploaded_image_uses_signature_when_content_type_is_missing(self):
        image = b"\xff\xd8\xffjpeg-data"
        with tempfile.TemporaryDirectory() as upload_dir, mock.patch.object(
            server, "UPLOAD_DIR", upload_dir
        ):
            path = server.save_uploaded_image(image, "application/octet-stream", "agent-test")

            with open(path, "rb") as saved:
                self.assertEqual(image, saved.read())

        self.assertTrue(path.endswith(".jpg"))

    def test_terminal_drop_upload_keeps_visible_feedback(self):
        page = server.TERMINAL_PAGE

        self.assertIn("function draggedFiles(dataTransfer)", page)
        self.assertIn('type.toLowerCase() === "files"', page)
        self.assertIn('/\\.(?:png|jpe?g|gif|webp)$/i.test(file.name || "")', page)
        self.assertIn("input.scrollTop = input.scrollHeight", page)
        self.assertIn("statusMessageUntil = Number.POSITIVE_INFINITY", page)
        self.assertIn("statusMessageUntil = Date.now() + 5000", page)

    def test_terminal_shows_uploaded_images_before_send(self):
        page = server.TERMINAL_PAGE

        self.assertIn('id="attachment-preview"', page)
        self.assertIn("function renderInputAttachments()", page)
        self.assertIn('remove.className = "remove-attachment"', page)
        self.assertIn("autoGrow(); renderInputAttachments();", page)

    def test_quote_places_cursor_below_quoted_text(self):
        # removeAllRanges を引用より後に呼ぶと入力欄のカーソルが先頭へ戻る（Chrome）
        handler = server.TERMINAL_PAGE[
            server.TERMINAL_PAGE.index('selectionQuote.addEventListener("click"'):
        ]
        self.assertLess(
            handler.index("window.getSelection()?.removeAllRanges()"),
            handler.index("appendQuoteToInput(text)"),
        )
        # 引用が入力欄の表示域より長くてもカーソル位置まで送る
        self.assertIn("input.scrollTop = input.scrollHeight", server.TERMINAL_PAGE)

    def test_expanded_bubble_stays_open_when_new_messages_arrive(self):
        # 履歴APIは末尾300件しか返さないので、展開キーに位置(index)を使うと
        # 新着のたびにずれて「全文を表示」した吹き出しが畳み直されてしまう
        self.assertNotIn('"|" + index + "|"', server.TERMINAL_PAGE)
        self.assertIn(
            'item.text.slice(0, 80) + item.text.slice(-80)', server.TERMINAL_PAGE
        )
        # 最新の回答は全文のまま出る。新着で「最新」でなくなった瞬間に畳まれると
        # 読んでいる本文が閉じてしまうので、展開済みとして記録しておく
        self.assertIn("expandedBubbles.add(key)", server.TERMINAL_PAGE)

    def test_page_navigation_shows_loading_overlay(self):
        self.assertIn("#nav-loading", server.SIDEBAR_CSS)
        self.assertIn("function showNavLoading", server.SIDEBAR_JS)
        self.assertIn("function navigateTo", server.SIDEBAR_JS)
        # リンククリック（セッション切替・/newへの移動）で表示する
        self.assertIn("showNavLoading(navLoadingLabel(link.href))", server.SIDEBAR_JS)
        # bfcacheで戻ってきたときは消す
        self.assertIn('window.addEventListener("pageshow"', server.SIDEBAR_JS)
        # JSからの遷移（再起動・モデル変更・引き継ぎ等）もオーバーレイ付きで行う
        self.assertNotIn("location.href =", server.TERMINAL_PAGE)

    def test_new_messages_keep_scroll_position_while_reading_history(self):
        self.assertIn(
            "if (current < lastChatScrollTop - 2) followChat = false",
            server.TERMINAL_PAGE,
        )
        self.assertIn("const savedScrollTop = chat.scrollTop", server.TERMINAL_PAGE)
        self.assertIn(
            "chat.scrollTop = shouldFollow ? chat.scrollHeight : savedScrollTop",
            server.TERMINAL_PAGE,
        )


class SessionInputTest(unittest.TestCase):
    def test_extracts_only_uploaded_image_lines(self):
        prefix = server.UPLOAD_PATH_PREFIXES[0]
        image_path = f"{prefix}/uploads/agent-example/photo.jpg"
        value = "\n".join(
            [
                "確認してください",
                f"添付画像: {image_path}",
                "添付ファイル: /tmp/report.pdf",
                "添付画像: /tmp/unmanaged.jpg",
            ]
        )

        self.assertEqual([image_path], server.pasted_upload_image_paths(value))

    def test_waits_until_claude_converts_image_path(self):
        path = f"{server.UPLOAD_PATH_PREFIXES[0]}/uploads/agent-example/photo.jpg"
        screens = [f"入力中 {path}", "入力中 [Image #8]"]
        with (
            mock.patch.object(server, "capture_session", side_effect=screens) as capture,
            mock.patch.object(server.time, "monotonic", side_effect=[0.0, 0.1, 0.2]),
            mock.patch.object(server.time, "sleep") as sleep,
        ):
            server.wait_for_claude_image_paste("agent-example", [path], "[Image #7]")

        self.assertEqual(2, capture.call_count)
        sleep.assert_called_once_with(0.1)

    def test_image_send_uses_conversion_wait_before_enter(self):
        path = f"{server.UPLOAD_PATH_PREFIXES[0]}/uploads/agent-example/photo.jpg"

        def tmux_result(*args, **_kwargs):
            if args[:2] == ("show-option", "-qv"):
                return SimpleNamespace(returncode=0, stdout="claude\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(server, "tmux_run", side_effect=tmux_result) as tmux,
            mock.patch.object(server, "capture_session", return_value="[Image #4]"),
            mock.patch.object(server, "wait_for_claude_image_paste") as wait,
            mock.patch.object(server.time, "sleep"),
        ):
            server.send_session_text("agent-example", f"添付画像: {path}\n")

        wait.assert_called_once_with("agent-example", [path], "[Image #4]")
        self.assertEqual(
            ("send-keys", "-t", "agent-example", "Enter"),
            tmux.call_args_list[-2].args,
        )
        self.assertEqual(("delete-buffer",), tmux.call_args_list[-1].args[:1])


class WorktreeCleanupTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        server.SESSION_REGISTRY_FORGOTTEN.clear()
        self.repo = os.path.join(self.temp_dir.name, "repo")
        os.makedirs(self.repo)
        self.git("init", self.repo)
        readme = os.path.join(self.repo, "README.md")
        with open(readme, "w", encoding="utf-8") as output:
            output.write("test\n")
        self.git("-C", self.repo, "add", "README.md")
        self.git(
            "-C", self.repo, "-c", "user.name=Agent Deck",
            "-c", "user.email=agent-deck@example.com", "commit", "-m", "初期化",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def git(*args):
        return server.subprocess.run(
            [server.find_bin("git"), *args], check=True,
            capture_output=True, text=True,
        )

    def add_worktree(self, name="feature"):
        path = os.path.join(self.temp_dir.name, name)
        self.git("-C", self.repo, "worktree", "add", "-b", name, path)
        return path

    def test_main_worktree_is_not_cleanup_target(self):
        self.assertIsNone(server.linked_worktree_info(self.repo))
        self.assertFalse(server.remove_session_worktree(self.repo))
        self.assertTrue(os.path.isdir(self.repo))

    def test_clean_linked_worktree_is_removed(self):
        worktree = self.add_worktree()
        child = os.path.join(worktree, "src")
        os.makedirs(child)

        info = server.linked_worktree_info(child)
        removed = server.remove_session_worktree(child)

        self.assertEqual(os.path.realpath(worktree), info["path"])
        self.assertTrue(removed)
        self.assertFalse(os.path.exists(worktree))

    def test_worktree_in_use_by_another_session_is_kept(self):
        worktree = self.add_worktree()

        removed = server.remove_session_worktree(
            worktree, [os.path.join(worktree, "nested")]
        )

        self.assertFalse(removed)
        self.assertTrue(os.path.isdir(worktree))

    def test_dirty_worktree_is_kept(self):
        worktree = self.add_worktree()
        dirty_file = os.path.join(worktree, "untracked.txt")
        with open(dirty_file, "w", encoding="utf-8") as output:
            output.write("keep me\n")

        with self.assertRaisesRegex(RuntimeError, "modified or untracked"):
            server.remove_session_worktree(worktree)

        self.assertTrue(os.path.isfile(dirty_file))

    def test_session_is_terminated_before_cleanup_error_is_reported(self):
        panes = SimpleNamespace(
            returncode=0, stdout="agent-test\t/tmp/worktree\n", stderr=""
        )
        killed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            mock.patch.object(
                server, "tmux_run", side_effect=[panes, killed]
            ) as tmux,
            mock.patch.object(server, "invalidate_session_cache") as invalidate,
            mock.patch.object(server, "forget_registered_session") as forget,
            mock.patch.object(
                server, "remove_session_worktree",
                side_effect=RuntimeError("変更があります"),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "セッションは終了しましたが.*変更があります"
            ):
                server.terminate_session("agent-test", "/tmp/worktree")

        self.assertEqual(
            ("kill-session", "-t", "agent-test"), tmux.call_args_list[1].args
        )
        invalidate.assert_called_once_with()
        forget.assert_called_once_with("agent-test")

    def test_session_is_not_terminated_when_other_panes_cannot_be_checked(self):
        failed = SimpleNamespace(returncode=1, stdout="", stderr="tmux failed")
        with mock.patch.object(server, "tmux_run", return_value=failed) as tmux:
            with self.assertRaisesRegex(RuntimeError, "tmux failed"):
                server.terminate_session("agent-test", "/tmp/worktree")

        tmux.assert_called_once()


class SessionRestoreTest(unittest.TestCase):
    SESSION_ID = "019fd08a-e352-7a22-9aa5-0b5d0de94eba"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir_patch = mock.patch.object(server, "DATA_DIR", self.temp_dir.name)
        self.registry_patch = mock.patch.object(
            server, "SESSION_REGISTRY_PATH",
            os.path.join(self.temp_dir.name, "sessions.json"),
        )
        self.data_dir_patch.start()
        self.registry_patch.start()

    def tearDown(self):
        server.SESSION_REGISTRY_FORGOTTEN.clear()
        self.registry_patch.stop()
        self.data_dir_patch.stop()
        self.temp_dir.cleanup()

    def session(self, **overrides):
        item = {
            "name": "agent-20260828-120000-123",
            "tool": "codex",
            "cwd": self.temp_dir.name,
            "session_id": self.SESSION_ID,
            "summary": "再起動後も続ける作業",
            "note": "確認待ち",
            "position": "top",
            "pull_request": "https://github.com/example/repo/pull/42",
            "bypass": True,
            "restore_model": "gpt-5.6",
        }
        item.update(overrides)
        return item

    def test_registry_keeps_only_resumable_non_ephemeral_sessions(self):
        server.save_session_registry([
            self.session(),
            self.session(name="agent-ephemeral", ephemeral=True),
            self.session(name="agent-shell", tool="shell", session_id=""),
        ])

        items = server.load_session_registry()

        self.assertEqual(1, len(items))
        self.assertEqual(self.SESSION_ID, items[0]["session_id"])
        self.assertEqual("gpt-5.6", items[0]["model"])
        self.assertEqual(0o600, os.stat(server.SESSION_REGISTRY_PATH).st_mode & 0o777)

    def test_restores_missing_session_with_its_metadata_and_options(self):
        server.save_session_registry([self.session()])
        launched = SimpleNamespace(
            returncode=0,
            stdout="OK: restored (session agent-20260828-130000-456)\n",
            stderr="",
        )
        with (
            mock.patch.object(server, "live_registered_sessions", return_value=[]),
            mock.patch.object(server, "conversation_log_path", return_value="/tmp/log.jsonl"),
            mock.patch.object(server.subprocess, "run", return_value=launched) as run,
            mock.patch.object(server, "set_session_metadata") as metadata,
            mock.patch.object(server, "invalidate_session_cache"),
        ):
            report = server.restore_registered_sessions()

        command = run.call_args.args[0]
        self.assertIn("resume", command)
        self.assertIn(self.SESSION_ID, command)
        self.assertIn("--model", command)
        self.assertIn("gpt-5.6", command)
        self.assertIn("workspace-write", command)
        self.assertEqual(["agent-20260828-130000-456"], report["restored"])
        self.assertEqual([], report["failed"])
        metadata.assert_called_once()
        self.assertEqual(
            "agent-20260828-130000-456",
            server.load_session_registry()[0]["name"],
        )

    def test_existing_tmux_session_is_not_started_twice(self):
        item = self.session()
        server.save_session_registry([item])
        current = {**item, "name": "agent-20260828-140000-789"}
        with (
            mock.patch.object(server, "live_registered_sessions", return_value=[current]),
            mock.patch.object(server.subprocess, "run") as run,
            mock.patch.object(server, "invalidate_session_cache"),
        ):
            report = server.restore_registered_sessions()

        run.assert_not_called()
        self.assertEqual({"restored": [], "failed": []}, report)
        self.assertEqual(current["name"], server.load_session_registry()[0]["name"])

    def test_failed_restore_is_kept_for_the_next_startup(self):
        item = self.session()
        server.save_session_registry([item])
        failed = SimpleNamespace(returncode=1, stdout="", stderr="一時的な起動失敗")
        with (
            mock.patch.object(server, "live_registered_sessions", return_value=[]),
            mock.patch.object(server, "conversation_log_path", return_value="/tmp/log.jsonl"),
            mock.patch.object(server.subprocess, "run", return_value=failed),
            mock.patch.object(server, "invalidate_session_cache"),
        ):
            report = server.restore_registered_sessions()

        self.assertEqual([], report["restored"])
        self.assertIn("一時的な起動失敗", report["failed"][0]["error"])
        self.assertEqual(item["name"], server.load_session_registry()[0]["name"])

    def test_stale_snapshot_cannot_restore_an_explicitly_ended_session(self):
        item = self.session()
        server.save_session_registry([item])

        server.forget_registered_session(item["name"])
        server.save_session_registry([item])

        self.assertEqual([], server.load_session_registry())

    def test_server_startup_registers_live_sessions_before_restore(self):
        restorable = self.session()
        ephemeral = self.session(name="agent-ephemeral", ephemeral=True)
        with (
            mock.patch.object(
                server, "load_managed_sessions",
                return_value=[restorable, ephemeral],
            ) as load,
            mock.patch.object(server, "upsert_registered_session") as upsert,
        ):
            registered = server.register_live_sessions_for_restore()

        load.assert_called_once_with(persist=False)
        upsert.assert_called_once_with(restorable)
        self.assertEqual(1, registered)


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

    def test_live_session_id_prefers_newest_user_thread(self):
        # codex 0.151以降はターンごとに別rolloutへ書くことがあるため、
        # 開いているユーザー起点スレッドのうち最終更新が最新のものを選ぶ。
        with tempfile.TemporaryDirectory() as base:
            older = os.path.join(base, "older.jsonl")
            newer = os.path.join(base, "newer.jsonl")
            guardian = os.path.join(base, "guardian.jsonl")
            pending = os.path.join(base, "pending.jsonl")
            for path in (older, newer, guardian, pending):
                open(path, "w").close()
            os.utime(older, (1000, 1000))
            os.utime(newer, (2000, 2000))
            os.utime(guardian, (3000, 3000))
            os.utime(pending, (4000, 4000))
            paths = {
                "older-id": older, "newer-id": newer,
                "guardian-id": guardian, "pending-id": pending,
            }
            heads = {
                older: {"id": "older-id", "thread_source": "user", "subagent": False},
                newer: {"id": "newer-id", "thread_source": "user", "subagent": False},
                # 新形式のguardianはthread_sourceがguardian_reviewでsubagent扱い
                guardian: {
                    "id": "guardian-id", "thread_source": "guardian_review",
                    "subagent": True,
                },
                # session_metaがまだ書かれていないスレッドは候補にしない
                pending: {"id": "", "thread_source": "", "subagent": False},
            }
            with (
                mock.patch.object(
                    server, "find_log_by_id",
                    side_effect=lambda _tool, sid: paths.get(sid, ""),
                ),
                mock.patch.object(
                    server, "codex_session_head", side_effect=lambda path: heads[path]
                ),
            ):
                chosen = server.codex_live_session_id(
                    ["guardian-id", "older-id", "newer-id", "pending-id", "missing-id"]
                )
        self.assertEqual("newer-id", chosen)

    def test_session_messages_merges_previous_thread_logs(self):
        def message(role, text):
            return json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": role,
                    "content": [{
                        "type": "input_text" if role == "user" else "output_text",
                        "text": text,
                    }],
                },
            }) + "\n"

        with tempfile.TemporaryDirectory() as base:
            first = os.path.join(base, "first.jsonl")
            second = os.path.join(base, "second.jsonl")
            with open(first, "w") as out:
                out.write(message("user", "最初の質問"))
                out.write(message("assistant", "最初の回答"))
            with open(second, "w") as out:
                out.write(message("user", "続きの依頼"))
                out.write(message("assistant", "続きの回答"))

            merged = server.session_messages(second, "codex", history=[first])

        self.assertEqual(
            ["最初の質問", "最初の回答", "続きの依頼", "続きの回答"],
            [item["text"] for item in merged],
        )

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


class DirectoryDiffTest(unittest.TestCase):
    def test_normalizes_number_and_github_url(self):
        self.assertEqual("123", server.normalize_pr_selector("123"))
        self.assertEqual(
            "https://github.com/example/repo/pull/456",
            server.normalize_pr_selector("https://github.com/example/repo/pull/456"),
        )

    def test_rejects_non_github_selector(self):
        with self.assertRaisesRegex(ValueError, "PR番号"):
            server.normalize_pr_selector("https://example.com/repo/pull/1")

    def test_fetches_diff_from_default_branch_to_worktree(self):
        results = [
            SimpleNamespace(returncode=0, stdout="feature/review\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="base123\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="10\t2\tserver.py\0", stderr=""),
            SimpleNamespace(returncode=0, stdout="M\0server.py\0", stderr=""),
            SimpleNamespace(returncode=0, stdout="diff --git a/server.py b/server.py\n", stderr=""),
        ]
        with (
            tempfile.TemporaryDirectory() as cwd,
            mock.patch.object(server, "find_bin", return_value="/usr/bin/git"),
            mock.patch.object(
                server, "git_default_branch", return_value=("main", "origin/main")
            ),
            mock.patch.object(server.subprocess, "run", side_effect=results) as run,
        ):
            result = server.directory_diff(cwd)

        self.assertEqual("main", result["baseRefName"])
        self.assertEqual("feature/review", result["headRefName"])
        self.assertEqual(
            [{"path": "server.py", "additions": 10, "deletions": 2, "status": "M"}],
            result["files"],
        )
        self.assertIn("diff --git", result["patch"])
        self.assertEqual(
            ["/usr/bin/git", "branch", "--show-current"],
            run.call_args_list[0].args[0],
        )
        self.assertEqual(
            ["/usr/bin/git", "diff", "--name-status", "-z", "--no-ext-diff",
             "--find-renames", "base123", "--"],
            run.call_args_list[3].args[0],
        )
        self.assertEqual(
            ["/usr/bin/git", "diff", "--patch", "--no-ext-diff", "--find-renames",
             "base123", "--"],
            run.call_args_list[4].args[0],
        )

    def test_fetches_diff_from_requested_base_branch(self):
        results = [
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="base123\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        ]
        with (
            tempfile.TemporaryDirectory() as cwd,
            mock.patch.object(server, "find_bin", return_value="/usr/bin/git"),
            mock.patch.object(
                server,
                "git_branch_ref",
                return_value="origin/feature/stack-base",
            ) as branch_ref,
            mock.patch.object(server, "git_default_branch") as default_branch,
            mock.patch.object(server.subprocess, "run", side_effect=results) as run,
        ):
            result = server.directory_diff(cwd, "feature/stack-base")

        branch_ref.assert_called_once_with(cwd, "feature/stack-base")
        default_branch.assert_not_called()
        self.assertEqual("feature/stack-base", result["baseRefName"])
        self.assertEqual(
            ["/usr/bin/git", "merge-base", "origin/feature/stack-base", "HEAD"],
            run.call_args_list[1].args[0],
        )

    def test_pr_review_diff_uses_pull_request_base_branch(self):
        handler = object.__new__(server.Handler)
        handler.client_address = ("127.0.0.1", 12345)
        handler.path = "/api/sessions/agent-review/diff"
        handler.headers = {}
        item = {
            "name": "agent-review",
            "cwd": "/tmp/worktrees/project-pr-42",
            "pull_request": "https://github.com/example/repo/pull/42",
        }
        diff = {
            "baseRefName": "feature/stack-base",
            "headRefName": "HEAD (detached)",
            "files": [],
            "patch": "",
        }
        with (
            mock.patch.object(server, "managed_sessions", return_value=[item]),
            mock.patch.object(
                server,
                "pull_request_target",
                return_value={"baseRefName": "feature/stack-base"},
            ) as target,
            mock.patch.object(server, "directory_diff", return_value=diff) as directory,
            mock.patch.object(handler, "_json") as response,
        ):
            handler.do_GET()

        target.assert_called_once_with("https://github.com/example/repo/pull/42")
        directory.assert_called_once_with(
            "/tmp/worktrees/project-pr-42", "feature/stack-base"
        )
        response.assert_called_once_with(diff)

    def test_uses_remote_symbolic_default_branch(self):
        results = [
            SimpleNamespace(returncode=0, stdout="true\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="upstream\norigin\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="origin/develop\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="abc123\n", stderr=""),
        ]
        with (
            tempfile.TemporaryDirectory() as cwd,
            mock.patch.object(server, "find_bin", return_value="/usr/bin/git"),
            mock.patch.object(server.subprocess, "run", side_effect=results) as run,
        ):
            result = server.git_default_branch(cwd)

        self.assertEqual(("develop", "origin/develop"), result)
        self.assertEqual(
            ["/usr/bin/git", "symbolic-ref", "--quiet", "--short",
             "refs/remotes/origin/HEAD"],
            run.call_args_list[2].args[0],
        )

    def test_finds_remote_default_when_origin_head_is_not_configured(self):
        results = [
            SimpleNamespace(returncode=0, stdout="true\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="origin\n", stderr=""),
            SimpleNamespace(returncode=1, stdout="", stderr=""),
            SimpleNamespace(
                returncode=0,
                stdout="ref: refs/heads/trunk\tHEAD\nabc123\tHEAD\n",
                stderr="",
            ),
            SimpleNamespace(returncode=0, stdout="abc123\n", stderr=""),
        ]
        with (
            tempfile.TemporaryDirectory() as cwd,
            mock.patch.object(server, "find_bin", return_value="/usr/bin/git"),
            mock.patch.object(server.subprocess, "run", side_effect=results),
        ):
            result = server.git_default_branch(cwd)

        self.assertEqual(("trunk", "origin/trunk"), result)

    def test_numstat_uses_new_path_for_renamed_file(self):
        result = server._parse_git_numstat("3\t1\t\0old.py\0new.py\0")

        self.assertEqual(
            [{"path": "new.py", "additions": 3, "deletions": 1}], result
        )

    def test_name_status_maps_paths_and_uses_new_path_for_renames(self):
        result = server._parse_git_name_status(
            "A\0created.txt\0D\0del.txt\0M\0mod.txt\0R100\0old.txt\0new.txt\0"
        )

        self.assertEqual(
            {"created.txt": "A", "del.txt": "D", "mod.txt": "M", "new.txt": "R"},
            result,
        )

    def test_pull_request_worktree_creates_worktree_and_checks_out_pr(self):
        ok = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            tempfile.TemporaryDirectory() as data_dir,
            tempfile.TemporaryDirectory() as repo,
            mock.patch.object(
                server, "WORKTREES_DIR", os.path.join(data_dir, "worktrees")
            ),
            mock.patch.object(
                server, "find_bin", side_effect=lambda name, *a: f"/usr/bin/{name}"
            ),
            mock.patch.object(server.subprocess, "run", return_value=ok) as run,
        ):
            path = server.pull_request_worktree({"cwd": repo, "number": 7})

            expected = os.path.join(
                server.WORKTREES_DIR, os.path.basename(repo) + "-pr-7"
            )
        self.assertEqual(expected, path)
        self.assertEqual(
            ["/usr/bin/git", "worktree", "add", "--detach", expected],
            run.call_args_list[0].args[0],
        )
        self.assertEqual(repo, run.call_args_list[0].kwargs["cwd"])
        self.assertEqual(
            ["/usr/bin/gh", "pr", "checkout", "7", "--detach"],
            run.call_args_list[1].args[0],
        )
        self.assertEqual(expected, run.call_args_list[1].kwargs["cwd"])

    def test_pull_request_worktree_keeps_existing_worktree_on_update_failure(self):
        failed = SimpleNamespace(returncode=1, stdout="", stderr="dirty")
        with (
            tempfile.TemporaryDirectory() as data_dir,
            mock.patch.object(server, "WORKTREES_DIR", data_dir),
            mock.patch.object(server, "find_bin", return_value="/usr/bin/gh"),
            mock.patch.object(server.subprocess, "run", return_value=failed) as run,
        ):
            existing = os.path.join(data_dir, "repo-pr-7")
            os.makedirs(existing)
            path = server.pull_request_worktree(
                {"cwd": "/home/user/repo", "number": 7}
            )
        self.assertEqual(existing, path)
        # 既存worktreeなら worktree add は走らず、checkout失敗でもそのまま使う
        self.assertEqual(1, run.call_count)

    def test_non_git_directory_has_friendly_error(self):
        failed = SimpleNamespace(returncode=1, stdout="", stderr="not a git repository")
        with (
            tempfile.TemporaryDirectory() as cwd,
            mock.patch.object(server, "find_bin", return_value="/usr/bin/git"),
            mock.patch.object(server.subprocess, "run", return_value=failed),
        ):
            with self.assertRaisesRegex(LookupError, "Gitリポジトリ"):
                server.git_default_branch(cwd)

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


class SessionPositionTest(unittest.TestCase):
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

    def test_deferred_session_is_rendered_after_normal_session(self):
        sessions = [
            self._session("agent-later", position="later"),
            self._session("agent-normal", position="normal"),
        ]
        with mock.patch.object(server, "managed_sessions", return_value=sessions):
            sidebar = server.build_sidebar(None)

        self.assertLess(
            sidebar.index("session=agent-normal"),
            sidebar.index("後回し"),
        )
        self.assertLess(
            sidebar.index("後回し"),
            sidebar.index("session=agent-later"),
        )
        self.assertIn("1件", sidebar)

    def test_position_is_exclusive_even_when_legacy_pinned_is_true(self):
        session = self._session("agent-later", pinned=True, position="later")

        self.assertEqual("later", server.session_position(session))

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

    def test_deferred_metadata_is_restored_on_restarted_session(self):
        calls = []
        with mock.patch.object(
            server, "tmux_run",
            side_effect=lambda *args: calls.append(args) or SimpleNamespace(),
        ):
            server.set_session_metadata("agent-new", position="later")

        self.assertIn(
            ("set-option", "-t", "agent-new", "@launcher_position", "later"),
            calls,
        )
        self.assertNotIn(
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
    def _session(name, pinned=False, position=None):
        return {
            "name": name, "tool": "codex", "cwd": "/tmp/project",
            "summary": "summary", "last_message": "summary", "note": "",
            "running": False, "background": "", "context": None,
            "artifacts": [], "pinned": pinned, "position": position,
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
    def setUp(self):
        server.CLAUDE_CWD_CACHE.clear()
        server.CLAUDE_START_CACHE.clear()

    def test_claude_session_cwd_uses_latest_recorded_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = os.path.join(directory, "conversation.jsonl")
            with open(log_path, "w", encoding="utf-8") as output:
                output.write(json.dumps({"cwd": "/Users/xxx/project"}) + "\n")
                output.write(json.dumps({"cwd": "/Users/xxx/project-worktree"}) + "\n")

            self.assertEqual(
                "/Users/xxx/project-worktree", server.claude_session_cwd(log_path)
            )

            with open(log_path, "a", encoding="utf-8") as output:
                output.write(json.dumps({"cwd": "/Users/xxx/next-worktree"}) + "\n")

            self.assertEqual(
                "/Users/xxx/next-worktree", server.claude_session_cwd(log_path)
            )

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

    def test_resume_candidates_uses_session_start_from_log(self):
        session_id = "019fd08a-e352-7a22-9aa5-0b5d0de94eba"
        cwd = "/Users/xxx/project"
        started_at = "2026-08-20T08:03:21.000Z"
        expected = server.parse_timestamp(started_at)
        with tempfile.TemporaryDirectory() as home, (
            mock.patch.object(server, "HOME", home)
        ), mock.patch.object(server, "log_meta", return_value={}):
            project = os.path.join(
                home, ".claude", "projects", server.claude_project_dir(cwd)
            )
            os.makedirs(project)
            log_path = os.path.join(project, f"{session_id}.jsonl")
            with open(log_path, "w", encoding="utf-8") as output:
                output.write(json.dumps({
                    "type": "attachment",
                    "timestamp": started_at,
                    "sessionId": session_id,
                }) + "\n")
            delayed_file_time = expected + 180
            os.utime(log_path, (delayed_file_time, delayed_file_time))

            candidates = server.resume_candidates("claude", cwd)

        self.assertEqual(expected, candidates[0]["created"])


class ClaudeShellCommandTest(unittest.TestCase):
    def test_image_scale_metadata_is_not_shown_as_user_message(self):
        text = (
            "[Image: original 4032x3024, displayed at 2000x1500. "
            "Multiply coordinates by 2.02 to map to original image.]"
        )
        item = {
            "type": "user",
            "isMeta": True,
            "turnCompanion": True,
            "message": {"content": text},
        }

        self.assertIsNone(server.user_message_entry(item, "claude"))
        self.assertEqual(
            {"role": "user", "text": text},
            server.user_message_entry({"type": "user", "message": {"content": text}}, "claude"),
        )

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
            "multi": False,
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
            "multi": False,
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

    MULTI_DIALOG = """\
claude: どこが引っかかるかで直し方が変わるので、確認させてください。
←   ☐ 改善ポイント   ✔ Submit   →
どこが回答しにくいですか?（複数選択OK）
1. 絵文字が多すぎる — 10個から探すのが面倒
2. 2段階タップが面倒 — 数字と種類を別々に押すのが手間
3. Other
Enter selections (comma- or space-separated) [1-3] then Enter to Submit, or
Escape to cancel:
"""

    def test_parses_multi_select_dialog(self):
        result = server.parse_question_screen(self.MULTI_DIALOG)
        self.assertTrue(result["multi"])
        self.assertEqual("どこが回答しにくいですか?（複数選択OK）", result["question"])
        self.assertEqual(
            ["絵文字が多すぎる", "2段階タップが面倒", "Other"],
            [choice["label"] for choice in result["choices"]],
        )
        self.assertEqual(
            ["10個から探すのが面倒", "数字と種類を別々に押すのが手間", ""],
            [choice["description"] for choice in result["choices"]],
        )

    def test_single_select_dialog_is_not_multi(self):
        self.assertFalse(server.parse_question_screen(self.MCP_DIALOG)["multi"])

    # Claude Code 2.1.257 でプロンプトの文言が変わった。旧文言も resume した
    # 古いバージョンで出るので、両方を拾えることを確かめる。
    NEW_DIALOG = """\
claude: 実装に入る前に確認させてください。
←   ☐ 確定ポリシー   ✔ Submit   →
2日の自動確定で、どこまでを確定対象にしますか？
1. オムニ全部＋クレーは不足時のみ — 運用ルール通り
2. 当選は全部確定 — 取りこぼしゼロだが余剰枠は手動キャンセル
3. Other
4. Chat about this
Select with numbers [1-4]. Then Enter to submit or Escape to cancel:
"""

    NEW_MULTI_DIALOG = """\
←   ☐ 改善ポイント   ✔ Submit   →
どこが回答しにくいですか?（複数選択OK）
1. 絵文字が多すぎる — 10個から探すのが面倒
2. 2段階タップが面倒 — 数字と種類を別々に押すのが手間
3. Other
Select with numbers [1-3] (comma- or space-separated for several). Then Space
to toggle, Enter to submit or Escape to cancel:
"""

    def test_parses_new_single_select_prompt(self):
        result = server.parse_question_screen(self.NEW_DIALOG)
        self.assertFalse(result["multi"])
        self.assertEqual(
            "2日の自動確定で、どこまでを確定対象にしますか？", result["question"],
        )
        self.assertEqual(
            ["オムニ全部＋クレーは不足時のみ", "当選は全部確定", "Other",
             "Chat about this"],
            [choice["label"] for choice in result["choices"]],
        )
        self.assertEqual("運用ルール通り", result["choices"][0]["description"])

    def test_parses_new_multi_select_prompt(self):
        result = server.parse_question_screen(self.NEW_MULTI_DIALOG)
        self.assertTrue(result["multi"])
        self.assertEqual("どこが回答しにくいですか?（複数選択OK）", result["question"])
        self.assertEqual(
            ["絵文字が多すぎる", "2段階タップが面倒", "Other"],
            [choice["label"] for choice in result["choices"]],
        )

    def test_parses_other_custom_answer_prompt(self):
        screen = """\
←   ☐ 自動化範囲   ☐ 起票先   ✔ Submit   →
月次のタスクを、どの粒度で自動化しますか？
1. issue自動起票のみ — 毎月1日に起票
2. 費用サマリー付きで起票 — 前月比も記載
3. Other
4. Chat about this
Enter text for option 3 (Other), or Escape for the list:
"""

        self.assertEqual({
            "question": "月次のタスクを、どの粒度で自動化しますか？",
            "choices": [],
            "multi": False,
            "custom": True,
            "custom_prompt": "Other の内容を入力してください",
        }, server.parse_question_screen(screen))

    def test_quoted_other_custom_answer_prompt_is_not_a_question(self):
        screen = """\
Enter text for option 3 (Other), or Escape for the list:
この表示が出ていました。
"""

        self.assertIsNone(server.parse_question_screen(screen))

    def test_posts_other_custom_answer_as_question_text(self):
        body = "text=cron%E3%81%A7%E5%AE%9F%E8%A1%8C".encode()
        handler = object.__new__(server.Handler)
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.path = "/api/sessions/agent-test/answer"
        screen = "Enter text for option 3 (Other), or Escape for the list:\n"

        with (
            mock.patch.object(server, "valid_session", return_value=True),
            mock.patch.object(server, "capture_session", return_value=screen),
            mock.patch.object(server, "send_session_text") as send,
            mock.patch.object(handler, "_json") as response,
        ):
            handler.do_POST()

        send.assert_called_once_with("agent-test", "cronで実行")
        response.assert_called_once_with({"ok": True})

    def test_new_prompt_with_arrow_key_hint_is_parsed(self):
        screen = self.NEW_DIALOG.replace(
            "Select with numbers [1-4].",
            "Select with numbers [1-4] or up / down arrow keys.",
        )
        self.assertEqual(4, len(server.parse_question_screen(screen)["choices"]))

    def test_quoted_new_prompt_is_not_a_question(self):
        screen = self.NEW_DIALOG + "以上が前回の質問でした。\n"
        self.assertIsNone(server.parse_question_screen(screen))

    def test_quoted_multi_select_text_is_not_a_question(self):
        screen = self.MULTI_DIALOG + "以上が前回の質問でした。\n"
        self.assertIsNone(server.parse_question_screen(screen))

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

    def test_pending_question_prioritizes_dialog_over_waiting_spinner(self):
        screen = SimpleNamespace(
            returncode=0,
            stdout="""\
tool: Bash (gcloud logging read ...)
Waiting…
Permission Required: Bash command
Auto mode classifier requires confirmation for this command.

Latest blocked action: Blocked by classifier
Do you want to proceed?
1. Yes
2. Yes, and don’t ask again for
3. No
Enter selection [1-3], or Escape to cancel:
Esc to cancel · Tab to amend · ctrl+e to explain
""",
        )
        self.assertTrue(server.screen_is_running(screen.stdout, "claude"))

        with mock.patch.object(server, "tmux_run", return_value=screen):
            result = server.pending_question("agent-test", "claude")

        self.assertEqual(
            "Latest blocked action: Blocked by classifier Do you want to proceed?",
            result["question"],
        )
        self.assertEqual(
            ["Yes", "Yes, and don’t ask again for", "No"],
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
