from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from model_settings import (
    ModelSettings,
    ModelSettingsConflict,
    ModelSettingsCorrupt,
    ModelSettingsUnavailable,
    _protect,
    _unprotect,
)


@unittest.skipUnless(os.name == "nt", "Windows DPAPI is required")
class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name) / "states"
        self.settings = ModelSettings(self.state_dir)
        self.secret = "sk-test-settings-private"
        self.config = {
            "base_url": "https://api.deepseek.com",
            "api_key": self.secret,
            "model": "deepseek-flash",
        }

    def save(self, revision=0, provider="DeepSeek", config=None):
        return self.settings.update({
            "expected_revision": revision, "action": "save", "provider": provider,
            "config": config or self.config,
        })

    def test_dpapi_roundtrip_and_missing_get_does_not_write(self):
        self.assertEqual(_unprotect(_protect(self.secret)), self.secret)
        self.assertEqual(self.settings.get(), {
            "revision": 0, "active_provider": None, "profiles": [],
            "storage": "windows-user-encrypted",
        })
        self.assertFalse(self.settings.path.exists())
        saved = self.save()
        self.assertEqual(saved["profiles"], [{"provider": "DeepSeek", **self.config}])
        raw = self.settings.path.read_text(encoding="utf-8")
        self.assertNotIn(self.secret, raw)
        self.assertIn("api_key_dpapi", raw)
        self.assertEqual(ModelSettings(self.state_dir).get(), saved)

    def test_overwrite_another_provider_and_remove_default_without_reselecting(self):
        first = self.save()
        second = self.save(1, "OpenRouter", {
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-openrouter-private",
            "model": "example/model",
        })
        self.assertEqual(second["active_provider"], "OpenRouter")
        self.assertEqual(len(second["profiles"]), 2)
        removed = self.settings.update({
            "expected_revision": 2, "action": "remove", "provider": "OpenRouter",
        })
        self.assertIsNone(removed["active_provider"])
        self.assertEqual(removed["profiles"], first["profiles"])
        self.assertEqual(ModelSettings(self.state_dir).get(), removed)
        overwritten = self.save(3, config={**self.config, "api_key": "sk-new-private"})
        self.assertEqual(overwritten["active_provider"], "DeepSeek")
        self.assertEqual(overwritten["profiles"][0]["api_key"], "sk-new-private")
        self.assertNotIn("sk-new-private", self.settings.path.read_text(encoding="utf-8"))

    def test_conflict_invalid_input_and_atomic_failure_keep_old_file(self):
        self.save()
        old = self.settings.path.read_bytes()
        with self.assertRaises(ModelSettingsConflict) as caught:
            self.save(0)
        self.assertEqual(caught.exception.current_revision, 1)
        for bad in (
            {**self.config, "model": "x" * 201},
            {**self.config, "api_key": "bad\nkey"},
            {**self.config, "base_url": "http://example.com/v1"},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.save(1, config=bad)
        with patch("model_settings.os.replace", side_effect=OSError("simulated")):
            with self.assertRaises(ModelSettingsUnavailable):
                self.save(1, config={**self.config, "model": "other"})
        self.assertEqual(self.settings.path.read_bytes(), old)
        self.assertEqual(list(self.state_dir.glob(".model-settings-*.tmp")), [])

    def test_corrupt_json_and_failed_decryption_never_reset(self):
        self.save()
        payload = json.loads(self.settings.path.read_text(encoding="utf-8"))
        payload["profiles"][0]["api_key_dpapi"] = base64.b64encode(b"not a DPAPI blob").decode("ascii")
        broken = json.dumps(payload).encode("utf-8")
        self.settings.path.write_bytes(broken)
        with self.assertRaises(ModelSettingsCorrupt):
            self.settings.get()
        with self.assertRaises(ModelSettingsCorrupt):
            self.save(1)
        self.assertEqual(self.settings.path.read_bytes(), broken)
        self.settings.path.write_bytes(b"{broken JSON")
        with self.assertRaises(ModelSettingsCorrupt):
            self.settings.get()
        self.assertEqual(self.settings.path.read_bytes(), b"{broken JSON")


if __name__ == "__main__":
    unittest.main()
