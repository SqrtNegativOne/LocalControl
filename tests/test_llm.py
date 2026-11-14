"""Tests for localcontrol.llm: response_format compatibility and malformed replies."""

import json
import time
import unittest
from unittest import mock

from localcontrol.llm import Llama, LLMError, Ollama

SCHEMA = {"type": "object", "properties": {"allowed": {"type": "boolean"}}}
CHAT_OK = {"choices": [{"message": {"content": json.dumps({"allowed": True})}}]}


class LlamaResponseFormatTests(unittest.TestCase):
    def setUp(self):
        self.llama = Llama("http://127.0.0.1:9931", "m")
        # Pre-seed the model-id cache so chat_json doesn't probe /v1/models.
        self.llama._model_id_cache = ("m", time.time())

    def test_sends_spec_nested_json_schema(self):
        seen = []

        def fake(path, body=None, timeout=None):
            seen.append((path, body))
            return CHAT_OK

        with mock.patch.object(self.llama, "_request", side_effect=fake):
            self.assertEqual(self.llama.chat_json("system", "user", SCHEMA),
                             {"allowed": True})

        self.assertEqual(seen[0][0], "/v1/chat/completions")
        fmt = seen[0][1]["response_format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertEqual(fmt["json_schema"]["schema"], SCHEMA)

    def test_falls_back_to_flat_schema_on_http_400(self):
        seen = []

        def fake(path, body=None, timeout=None):
            seen.append(body["response_format"])
            if len(seen) == 1:
                raise LLMError("llama.app HTTP 400: unsupported response_format")
            return CHAT_OK

        with mock.patch.object(self.llama, "_request", side_effect=fake):
            self.llama.chat_json("system", "user", SCHEMA)

        self.assertIn("json_schema", seen[0])
        self.assertEqual(seen[1], {"type": "json_schema", "schema": SCHEMA})

    def test_does_not_retry_unrelated_errors(self):
        calls = []

        def fake(path, body=None, timeout=None):
            calls.append(path)
            raise LLMError("llama.app HTTP 500: boom")

        with mock.patch.object(self.llama, "_request", side_effect=fake), \
             self.assertRaises(LLMError):
            self.llama.chat_json("system", "user", SCHEMA)
        self.assertEqual(calls, ["/v1/chat/completions"])

    def test_malformed_models_list_returns_empty(self):
        for payload in ([], None, {"data": None}, {"data": "nope"}, {"data": [1, {}]}):
            with self.subTest(payload=payload), \
                 mock.patch.object(self.llama, "_request", return_value=payload):
                self.assertEqual(self.llama._models(), [])


class OllamaStatusTests(unittest.TestCase):
    def test_missing_name_does_not_raise(self):
        ollama = Ollama("http://127.0.0.1:11434", "m")
        tags = {"models": [{"name": "m:latest"}, {"size": 1}, "junk"]}
        with mock.patch.object(ollama, "_request", return_value=tags):
            self.assertEqual(ollama.status(), (True, "m ready"))

    def test_non_dict_tags_does_not_raise(self):
        ollama = Ollama("http://127.0.0.1:11434", "m")
        with mock.patch.object(ollama, "_request", return_value=[]):
            ok, message = ollama.status()
        self.assertFalse(ok)
        self.assertIn("not pulled", message)


class LlamaChoiceTests(unittest.TestCase):
    def setUp(self):
        self.llama = Llama("http://127.0.0.1:9931", "gemma3:4b")

    @staticmethod
    def model(model_id: str, params: int, loaded: bool = False,
              outputs: list[str] | None = None) -> dict:
        return {
            "id": model_id,
            "status": {"value": "loaded" if loaded else "unloaded"},
            "architecture": {"output_modalities": outputs or ["text"]},
            "meta": {"n_params": params},
        }

    def test_configured_model_wins(self):
        models = [self.model("big", 8_000_000_000),
                  self.model("gemma3:4b", 4_000_000_000)]
        self.assertEqual(self.llama._chosen(models)["id"], "gemma3:4b")

    def test_largest_text_model_wins_without_a_config_match(self):
        models = [self.model("tiny", 1_000_000_000),
                  self.model("Qwen3-4B", 4_000_000_000)]
        self.assertEqual(self.llama._chosen(models)["id"], "Qwen3-4B")

    def test_loaded_model_wins_over_a_larger_unloaded_one(self):
        models = [self.model("tiny", 1_000_000_000, loaded=True),
                  self.model("Qwen3-4B", 4_000_000_000)]
        self.assertEqual(self.llama._chosen(models)["id"], "tiny")

    def test_decision_model_is_never_chosen(self):
        models = [self.model("Kev", 800_000_000, outputs=["decisions"]),
                  self.model("Qwen3-4B", 4_000_000_000)]
        self.assertEqual(self.llama._chosen(models)["id"], "Qwen3-4B")

    def test_unloaded_models_compare_by_parameter_count_in_the_id(self):
        models = [
            {"id": "ggml-org/gemma-3-1b-it-qat-GGUF:Q4_0",
             "status": {"value": "unloaded"},
             "architecture": {"output_modalities": ["text"]}},
            {"id": "unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M",
             "status": {"value": "unloaded"},
             "architecture": {"output_modalities": ["text"]}},
        ]
        self.assertEqual(self.llama._chosen(models)["id"],
                         "unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M")

    def test_missing_parameter_counts_fall_back_to_order(self):
        models = [{"id": "first", "architecture": {"output_modalities": ["text"]}},
                  {"id": "second", "architecture": {"output_modalities": ["text"]}}]
        self.assertEqual(self.llama._chosen(models)["id"], "first")


if __name__ == "__main__":
    unittest.main()
