import os
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import claude_runner


class _FakeStdin:
    def write(self, _data):
        return None

    async def drain(self):
        return None

    def close(self):
        return None


class _FakeStdout:
    async def readline(self):
        return b""


class _FakeStderr:
    async def read(self):
        return b""


class _FakeProc:
    def __init__(self):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout()
        self.stderr = _FakeStderr()
        self.returncode = 0

    async def wait(self):
        return 0


class ClaudeRunnerEnvTests(IsolatedAsyncioTestCase):
    async def test_run_claude_sets_proxy_env_and_unsets_model_overrides(self):
        captured = {}

        async def fake_create_subprocess_exec(*cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs["env"]
            return _FakeProc()

        env_updates = {
            "ANTHROPIC_BASE_URL": "https://cc.honoursoft.cn",
            "ANTHROPIC_AUTH_TOKEN": "test-token",
            "API_TIMEOUT_MS": "3000000",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "ANTHROPIC_MODEL": "should-be-removed",
            "ANTHROPIC_SMALL_FAST_MODEL": "should-be-removed",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "should-be-removed",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "should-be-removed",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "should-be-removed",
        }

        with (
            patch.dict(os.environ, env_updates, clear=False),
            patch("asyncio.create_subprocess_exec", fake_create_subprocess_exec),
        ):
            await claude_runner.run_claude("hi")

        child_env = captured["env"]
        self.assertEqual(child_env["ANTHROPIC_BASE_URL"], "https://cc.honoursoft.cn")
        self.assertEqual(child_env["ANTHROPIC_AUTH_TOKEN"], "test-token")
        self.assertEqual(child_env["API_TIMEOUT_MS"], "3000000")
        self.assertEqual(child_env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1")
        self.assertNotIn("ANTHROPIC_MODEL", child_env)
        self.assertNotIn("ANTHROPIC_SMALL_FAST_MODEL", child_env)
        self.assertNotIn("ANTHROPIC_DEFAULT_SONNET_MODEL", child_env)
        self.assertNotIn("ANTHROPIC_DEFAULT_OPUS_MODEL", child_env)
        self.assertNotIn("ANTHROPIC_DEFAULT_HAIKU_MODEL", child_env)
