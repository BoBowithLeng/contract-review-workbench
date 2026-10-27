"""Local model profiles; API keys are encrypted for the current Windows user.

The reader's same-origin page receives decrypted keys in memory so its existing
review/discussion requests can keep their unchanged, explicit config contract.
This module never reads model credentials from elsewhere on the machine.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import json
import os
import tempfile
import threading
import unicodedata
from ctypes import wintypes
from pathlib import Path
from typing import Any

from model_client import _endpoint
from model_presets import PRESETS


STORAGE = "windows-user-encrypted"
MAX_FILE_BYTES = 128 * 1024


class ModelSettingsConflict(RuntimeError):
    def __init__(self, current_revision: int):
        super().__init__("模型设置已变化，请刷新后重试。")
        self.current_revision = current_revision


class ModelSettingsCorrupt(RuntimeError):
    """Saved data or its user-bound ciphertext cannot be read safely."""


class ModelSettingsUnavailable(RuntimeError):
    """Windows user encryption or the settings file is unavailable."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _windows_api():
    if os.name != "nt":
        raise ModelSettingsUnavailable("当前系统无法保存加密的模型设置，请在本次使用时填写。")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.c_wchar_p, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.POINTER(ctypes.c_wchar_p), ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _input_blob(value: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(value)
    return _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _protect(value: str) -> str:
    crypt32, kernel32 = _windows_api()
    plain, buffer = _input_blob(value.encode("utf-8"))
    encrypted = _DataBlob()
    try:
        if not crypt32.CryptProtectData(ctypes.byref(plain), None, None, None, None, 1, ctypes.byref(encrypted)):
            raise ModelSettingsUnavailable("Windows 当前用户加密失败，原设置未更改。")
        result = ctypes.string_at(encrypted.pbData, encrypted.cbData)
        return base64.b64encode(result).decode("ascii")
    finally:
        if encrypted.pbData:
            kernel32.LocalFree(encrypted.pbData)
        del buffer


def _unprotect(ciphertext: str) -> str:
    try:
        raw = base64.b64decode(ciphertext, validate=True)
        if not raw:
            raise ValueError
    except (ValueError, UnicodeEncodeError, binascii.Error):
        raise ModelSettingsCorrupt("保存的 API Key 数据损坏，原设置文件已保留。") from None
    crypt32, kernel32 = _windows_api()
    encrypted, buffer = _input_blob(raw)
    plain = _DataBlob()
    try:
        if not crypt32.CryptUnprotectData(ctypes.byref(encrypted), None, None, None, None, 1, ctypes.byref(plain)):
            raise ModelSettingsCorrupt("当前 Windows 账户无法读取保存的 API Key，原设置文件已保留。")
        try:
            return ctypes.string_at(plain.pbData, plain.cbData).decode("utf-8")
        except UnicodeDecodeError:
            raise ModelSettingsCorrupt("保存的 API Key 内容损坏，原设置文件已保留。") from None
    finally:
        if plain.pbData:
            kernel32.LocalFree(plain.pbData)
        del buffer


def _clean_config(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"base_url", "api_key", "model"}:
        raise ValueError("模型设置不完整，请填写服务地址、API Key 和模型名称。")
    base_url, key, model = raw["base_url"], raw["api_key"], raw["model"]
    if not isinstance(base_url, str) or len(base_url) > 2000:
        raise ValueError("服务地址格式不正确，请检查后重试。")
    _endpoint(base_url)
    if (not isinstance(key, str) or not key.strip() or len(key) > 4096
            or any(unicodedata.category(char).startswith("C") for char in key)):
        raise ValueError("API Key 不能为空、超过4096字，或包含特殊控制字符。")
    if not isinstance(model, str):
        raise ValueError("模型名称不能为空。")
    model = model.strip()
    if (not model or len(model) > 200
            or any(unicodedata.category(char).startswith("C") for char in model)):
        raise ValueError("模型名称最多200字，不能包含特殊控制字符。")
    return {"base_url": base_url.strip(), "api_key": key.strip(), "model": model}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".model-settings-", suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


class ModelSettings:
    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "model-settings.json"
        self.lock = threading.RLock()

    def _load(self) -> dict[str, Any]:
        _windows_api()
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return {"revision": 0, "active_provider": None, "profiles": {}}
        except OSError:
            raise ModelSettingsUnavailable("模型设置文件暂时无法读取，原设置未更改。") from None
        if len(raw) > MAX_FILE_BYTES:
            raise ModelSettingsCorrupt("模型设置文件超过容量上限，暂时无法读取。")
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict) or set(payload) != {"revision", "active_provider", "profiles"}:
                raise ValueError
            revision = payload["revision"]
            active = payload["active_provider"]
            rows = payload["profiles"]
            if (not isinstance(revision, int) or isinstance(revision, bool) or revision < 0
                    or not isinstance(rows, list) or len(rows) > len(PRESETS)):
                raise ValueError
            profiles: dict[str, dict[str, str]] = {}
            for row in rows:
                if not isinstance(row, dict) or set(row) != {"provider", "base_url", "model", "api_key_dpapi"}:
                    raise ValueError
                provider = row["provider"]
                if not isinstance(provider, str) or provider not in PRESETS or provider in profiles:
                    raise ValueError
                if not isinstance(row["api_key_dpapi"], str):
                    raise ValueError
                config = _clean_config({
                    "base_url": row["base_url"],
                    "model": row["model"],
                    "api_key": _unprotect(row["api_key_dpapi"]),
                })
                profiles[provider] = config
            if active is not None and (not isinstance(active, str) or active not in profiles):
                raise ValueError
            return {"revision": revision, "active_provider": active, "profiles": profiles}
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            raise ModelSettingsCorrupt("模型设置文件损坏，暂时无法读取。") from None

    @staticmethod
    def _public(state: dict[str, Any]) -> dict[str, Any]:
        profiles = state["profiles"]
        return {
            "revision": state["revision"],
            "active_provider": state["active_provider"],
            "profiles": [{"provider": provider, **profiles[provider]} for provider in PRESETS if provider in profiles],
            "storage": STORAGE,
        }

    def get(self) -> dict[str, Any]:
        with self.lock:
            return self._public(self._load())

    def update(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict) or set(body) - {"expected_revision", "action", "provider", "config"}:
            raise ValueError("模型设置请求格式不正确。")
        expected = body.get("expected_revision")
        action = body.get("action")
        provider = body.get("provider")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            raise ValueError("请求中的设置版本有误，请刷新后重试。")
        if action not in {"save", "remove"}:
            raise ValueError("请选择保存或移除模型设置。")
        if not isinstance(provider, str) or provider not in PRESETS:
            raise ValueError("服务商选择有误，请重新选择。")
        if action == "save":
            if set(body) != {"expected_revision", "action", "provider", "config"}:
                raise ValueError("请填写完整的服务地址、API Key 和模型名称。")
            config = _clean_config(body["config"])
        elif set(body) != {"expected_revision", "action", "provider"}:
            raise ValueError("移除设置时不需要提交模型配置。")
        with self.lock:
            state = self._load()
            if state["revision"] != expected:
                raise ModelSettingsConflict(state["revision"])
            profiles = dict(state["profiles"])
            if action == "save":
                profiles[provider] = config
                active = provider
            else:
                if provider not in profiles:
                    raise ValueError("此服务商没有已保存的设置。")
                profiles.pop(provider)
                active = None if state["active_provider"] == provider else state["active_provider"]
            encrypted = {
                "revision": expected + 1,
                "active_provider": active,
                "profiles": [
                    {"provider": item, "base_url": profiles[item]["base_url"], "model": profiles[item]["model"],
                     "api_key_dpapi": _protect(profiles[item]["api_key"])}
                    for item in PRESETS if item in profiles
                ],
            }
            try:
                _atomic_json(self.path, encrypted)
            except OSError:
                raise ModelSettingsUnavailable("模型设置未能保存，原设置未更改。") from None
            return self._public({"revision": expected + 1, "active_provider": active, "profiles": profiles})


__all__ = ["ModelSettings", "ModelSettingsConflict", "ModelSettingsCorrupt", "ModelSettingsUnavailable"]
