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


if __name__ == "__main__":
    unittest.main()
