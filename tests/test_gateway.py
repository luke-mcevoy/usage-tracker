"""Tests for the OpenAI-compatible HTTP gateway."""

import copy
import json
import unittest
from unittest import mock

from dispatcher import config as config_mod
from gateway.backends import (BackendError, gemini_to_openai_message,
                              messages_to_gemini)
from gateway.router import (available_backends, build_chain, chat_completions,
                            list_models, parse_model)


def gw_config(backends=None):
    config = copy.deepcopy(config_mod.DEFAULT_CONFIG)
    config["gateway"]["backends"] = backends if backends is not None else [
        {"id": "gemini", "kind": "gemini", "quota": "gemini",
         "model": "gemini-2.5-flash", "api_key_env": "GEMINI_API_KEY"},
        {"id": "groq", "kind": "openai", "quota": None,
         "base_url": "https://api.groq.com/openai/v1",
         "model": "llama-3.3-70b", "api_key_env": "GROQ_API_KEY"},
    ]
    return config


GEMINI_OK = {
    "ok": True, "plan": "api key",
    "windows": [{"key": "daily", "label": "daily", "used_percent": 2.0,
                 "resets_at": 1787803200}],
}


class ParseModelTest(unittest.TestCase):
    def test_auto(self):
        self.assertEqual(parse_model("auto"), (None, None))
        self.assertEqual(parse_model(""), (None, None))
        self.assertEqual(parse_model(None), (None, None))

    def test_pin_and_override(self):
        self.assertEqual(parse_model("gemini"), ("gemini", None))
        self.assertEqual(parse_model("gemini/gemini-2.0-flash"),
                         ("gemini", "gemini-2.0-flash"))


class GeminiTranslateTest(unittest.TestCase):
    def test_system_and_turns(self):
        body = messages_to_gemini([
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "2+2?"},
        ])
        self.assertEqual(body["systemInstruction"]["parts"][0]["text"], "be brief")
        self.assertEqual([c["role"] for c in body["contents"]],
                         ["user", "model", "user"])
        self.assertEqual(body["contents"][-1]["parts"][0]["text"], "2+2?")

    def test_multimodal_text_parts(self):
        body = messages_to_gemini([
            {"role": "user", "content": [
                {"type": "text", "text": "hello "},
                {"type": "text", "text": "world"},
            ]},
        ])
        self.assertEqual(body["contents"][0]["parts"][0]["text"], "hello world")

    def test_response_text(self):
        data = {"candidates": [{"content": {"parts": [{"text": "four"}]}}]}
        self.assertEqual(gemini_to_openai_message(data), "four")


class ChainTest(unittest.TestCase):
    def test_auto_picks_quota_backend_before_payg(self):
        backends = [
            {"id": "gemini", "kind": "gemini", "quota": "gemini", "model": "g"},
            {"id": "groq", "kind": "openai", "base_url": "http://x", "model": "l"},
        ]
        services = {"gemini": GEMINI_OK}
        chain, reason = build_chain("auto", backends, services, gw_config())
        self.assertEqual([b["id"] for b in chain], ["gemini", "groq"])
        self.assertIn("gemini", reason)

    def test_pin_skips_router(self):
        backends = [
            {"id": "gemini", "kind": "gemini", "quota": "gemini", "model": "g"},
            {"id": "groq", "kind": "openai", "base_url": "http://x", "model": "l"},
        ]
        chain, reason = build_chain("groq", backends, {"gemini": GEMINI_OK}, gw_config())
        self.assertEqual([b["id"] for b in chain], ["groq"])
        self.assertIn("pinned", reason)

    def test_unknown_pin(self):
        chain, reason = build_chain("nope", [{"id": "gemini", "kind": "gemini"}], {}, gw_config())
        self.assertEqual(chain, [])
        self.assertIn("unknown model", reason)


class CompletionsTest(unittest.TestCase):
    def test_fallback_on_retryable_error(self):
        config = gw_config()
        calls = []

        def complete(backend, messages, model, api_key, extras=None):
            calls.append(backend["id"])
            if backend["id"] == "gemini":
                raise BackendError(429, "rate limited", retryable=True)
            return {"content": "ok from groq", "model": model,
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

        with mock.patch("gateway.router.read_key", return_value="secret-key"):
            status, payload = chat_completions(
                {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
                {"gemini": GEMINI_OK}, config, complete_fn=complete)

        self.assertEqual(status, 200)
        self.assertEqual(payload["choices"][0]["message"]["content"], "ok from groq")
        self.assertEqual(payload["router"]["backend"], "groq")
        self.assertEqual(calls, ["gemini", "groq"])
        dumped = str(payload)
        self.assertNotIn("secret-key", dumped)

    def test_missing_messages(self):
        status, payload = chat_completions({"model": "auto"}, {}, gw_config())
        self.assertEqual(status, 400)
        self.assertIn("messages", payload["error"]["message"])

    def test_list_models_includes_auto(self):
        with mock.patch("gateway.router.read_key", return_value="k"):
            listing = list_models(gw_config())
        ids = [m["id"] for m in listing["data"]]
        self.assertIn("auto", ids)
        self.assertIn("gemini", ids)

    def test_unwraps_json_token(self):
        from gateway.keys import _unwrap_secret
        wrapped = json.dumps({
            "serverName": "x",
            "token": {"accessToken": "AIzaSyTEST", "tokenType": "Bearer"},
            "updatedAt": 1,
        })
        self.assertEqual(_unwrap_secret(wrapped), "AIzaSyTEST")
        self.assertEqual(_unwrap_secret("raw-key"), "raw-key")

    def test_available_backends_skip_missing_keys(self):
        with mock.patch("gateway.router.read_key", return_value=None):
            self.assertEqual(available_backends(gw_config()), [])


if __name__ == "__main__":
    unittest.main()
