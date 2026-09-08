import importlib.util
import json
import sys
import unittest
from urllib import error as urlerror
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "serve_vault.py"
SPEC = importlib.util.spec_from_file_location("serve_vault", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ProviderConfigTests(unittest.TestCase):
    def test_missing_usage_is_unknown_and_total_is_derived_only_from_reported_counts(self):
        self.assertEqual(MODULE.token_usage(11, 7)["total_tokens"], 18)
        missing = MODULE.token_usage(None, None)
        self.assertIsNone(missing["total_tokens"])
        self.assertFalse(missing["usage_complete"])
        summed = MODULE.sum_usage(MODULE.token_usage(11, 7), missing)
        self.assertIsNone(summed["prompt_tokens"])
        self.assertFalse(summed["usage_complete"])

    def test_openai_missing_usage_does_not_become_zero(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"output_text":"Analyst answer"}'
        with mock.patch.object(MODULE.urlrequest, "urlopen", return_value=response):
            _, usage, _ = MODULE.call_openai("system", "question", {"api_key": "test", "model": "gpt-5-mini"})
        self.assertIsNone(usage["total_tokens"])
        self.assertFalse(usage["usage_complete"])

    def test_verify_llm_config_makes_a_minimal_model_check(self):
        config = {"provider": "openai", "model": "gpt-5-mini", "url": "", "api_key": "sk-test"}
        with mock.patch.object(MODULE, "call_llm", return_value=("OK", {"total_tokens": 2}, 4.5)) as call:
            result = MODULE.verify_llm_config(config)

        self.assertEqual(result["provider"], "openai")
        self.assertEqual(result["model"], "gpt-5-mini")
        self.assertEqual(result["model_ms"], 4.5)
        call.assert_called_once_with(
            "You are a connection verifier. Reply with exactly: OK",
            "Connection test. Reply with exactly: OK",
            config,
        )

    def test_chatgpt_alias_maps_to_openai(self):
        config = MODULE.llm_config_from_payload({"provider_config": {"provider": "chatgpt", "api_key": "sk-test"}})

        self.assertEqual(config["provider"], "openai")
        self.assertEqual(config["api_key"], "sk-test")

    def test_anthropic_provider_defaults_model(self):
        config = MODULE.llm_config_from_payload({"provider_config": {"provider": "anthropic", "api_key": "sk-ant-test"}})

        self.assertEqual(config["provider"], "anthropic")
        self.assertEqual(config["model"], MODULE.default_llm_model("anthropic"))

    def test_local_llm_alias_maps_to_ollama(self):
        config = MODULE.llm_config_from_payload({"provider_config": {"provider": "local llm", "model": "llama3.2:3b"}})

        self.assertEqual(config["provider"], "ollama")
        self.assertEqual(config["model"], "llama3.2:3b")

    def test_response_cache_key_does_not_include_api_key(self):
        base = {"provider": "openai", "model": "gpt-5-mini", "url": "", "api_key": "sk-one"}
        changed_key = {**base, "api_key": "sk-two"}

        first = MODULE.response_cache_key("auto", "system", "user", base)
        second = MODULE.response_cache_key("auto", "system", "user", changed_key)

        self.assertEqual(first, second)

    def test_response_cache_key_does_include_provider(self):
        openai = {"provider": "openai", "model": "gpt-5-mini", "url": "", "api_key": ""}
        anthropic = {"provider": "anthropic", "model": "claude-sonnet-5", "url": "", "api_key": ""}

        first = MODULE.response_cache_key("auto", "system", "user", openai)
        second = MODULE.response_cache_key("auto", "system", "user", anthropic)

        self.assertNotEqual(first, second)

    def test_openai_key_must_come_from_request_config(self):
        with mock.patch.dict(MODULE.os.environ, {"OPENAI_API_KEY": "sk-env"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "in the UI"):
                MODULE.call_openai("system", "user", {"provider": "openai", "model": "gpt-5-mini", "api_key": "", "url": ""})

    def test_anthropic_key_must_come_from_request_config(self):
        with mock.patch.dict(MODULE.os.environ, {"ANTHROPIC_API_KEY": "sk-ant-env"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "in the UI"):
                MODULE.call_anthropic("system", "user", {"provider": "anthropic", "model": "claude-sonnet-5", "api_key": "", "url": ""})

    def test_ollama_404_names_missing_model(self):
        response = BytesResponse(b'{"error":"model not found"}')
        error = urlerror.HTTPError("http://127.0.0.1:11434/api/chat", 404, "Not Found", {}, response)

        with mock.patch.object(MODULE.urlrequest, "urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "Ollama model `missing-model` was not found"):
                MODULE.call_ollama(
                    "system",
                    "user",
                    {"provider": "ollama", "model": "missing-model", "url": "http://127.0.0.1:11434", "api_key": ""},
                )

    def test_anthropic_call_uses_ephemeral_key_and_parses_text(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "content": [{"type": "text", "text": "answer from anthropic"}],
                        "usage": {"input_tokens": 11, "output_tokens": 7},
                    }
                ).encode("utf-8")

        captured = {}

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse()

        with mock.patch.object(MODULE.urlrequest, "urlopen", side_effect=fake_urlopen):
            answer, usage, _model_ms = MODULE.call_anthropic(
                "system prompt",
                "user prompt",
                {"provider": "anthropic", "model": "claude-sonnet-5", "api_key": "sk-ant-test", "url": ""},
            )

        self.assertEqual(answer, "answer from anthropic")
        self.assertEqual(usage["total_tokens"], 18)
        self.assertEqual(captured["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(captured["headers"]["X-api-key"], "sk-ant-test")
        self.assertEqual(captured["body"]["model"], "claude-sonnet-5")
        self.assertEqual(captured["body"]["system"], "system prompt")
        self.assertEqual(captured["body"]["messages"][0]["content"], "user prompt")


class BytesResponse:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data

    def close(self):
        return None


if __name__ == "__main__":
    unittest.main()
