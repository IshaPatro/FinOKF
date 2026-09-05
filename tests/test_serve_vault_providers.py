import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "serve_vault.py"
SPEC = importlib.util.spec_from_file_location("serve_vault_providers", MODULE_PATH)
serve_vault = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(serve_vault)


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class ProviderRequestTests(unittest.TestCase):
    def test_openai_uses_request_key_and_disables_response_storage(self):
        payload = {
            "output_text": "OpenAI answer",
            "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
        }
        with patch.object(serve_vault.urlrequest, "urlopen", return_value=FakeResponse(payload)) as urlopen:
            answer, usage, _elapsed = serve_vault.call_openai("system", "question", "tab-only-openai-key", "gpt-test")

        request = urlopen.call_args.args[0]
        request_body = json.loads(request.data)
        self.assertEqual(answer, "OpenAI answer")
        self.assertEqual(usage["total_tokens"], 6)
        self.assertEqual(request.get_header("Authorization"), "Bearer tab-only-openai-key")
        self.assertEqual(request_body["model"], "gpt-test")
        self.assertFalse(request_body["store"])
        self.assertNotIn("tab-only-openai-key", request.data.decode("utf-8"))

    def test_anthropic_uses_request_key_and_messages_shape(self):
        payload = {
            "content": [{"type": "text", "text": "Anthropic answer"}],
            "usage": {"input_tokens": 5, "output_tokens": 3},
        }
        with patch.object(serve_vault.urlrequest, "urlopen", return_value=FakeResponse(payload)) as urlopen:
            answer, usage, _elapsed = serve_vault.call_anthropic("system", "question", "tab-only-anthropic-key", "claude-test")

        request = urlopen.call_args.args[0]
        request_body = json.loads(request.data)
        self.assertEqual(answer, "Anthropic answer")
        self.assertEqual(usage["total_tokens"], 8)
        self.assertEqual(request.get_header("X-api-key"), "tab-only-anthropic-key")
        self.assertEqual(request.get_header("Anthropic-version"), "2023-06-01")
        self.assertEqual(request_body["model"], "claude-test")
        self.assertEqual(request_body["system"], "system")
        self.assertEqual(request_body["messages"], [{"role": "user", "content": "question"}])
        self.assertNotIn("tab-only-anthropic-key", request.data.decode("utf-8"))

    def test_provider_models_keep_remote_and_local_defaults_separate(self):
        with patch.object(serve_vault, "LLM_PROVIDER", "ollama"), patch.object(serve_vault, "LLM_MODEL", "custom-local"):
            self.assertEqual(serve_vault.provider_model("ollama"), "custom-local")
            self.assertEqual(serve_vault.provider_model("openai"), serve_vault.OPENAI_MODEL)
            self.assertEqual(serve_vault.provider_model("anthropic"), serve_vault.ANTHROPIC_MODEL)


if __name__ == "__main__":
    unittest.main()
