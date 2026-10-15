"""Non-secret preferences. OAuth credentials live in Windows-protected storage."""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

_ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
_CONFIG = _ROOT / "config.json"
_LOCK = threading.RLock()


def _data() -> dict:
    try:
        data = json.loads(_CONFIG.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def relationship() -> str:
    return str(_data().get("relationship") or "friends")


def context() -> int:
    try:
        return max(3, min(30, int(_data().get("context", 10))))
    except (ValueError, TypeError):
        return 10


def style() -> str:
    return str(_data().get("style") or "")


def language() -> str:
    return str(_data().get("lang") or "zh")


def reply_target() -> bool:
    return bool(_data().get("reply_target", False))


def debug_view() -> bool:
    return bool(_data().get("debug_view", False))


def check_update() -> bool:
    return False


def gpt_model() -> str:
    from core.chatgpt_auth import auth
    data = _data()
    if data.get("model_client_id") != auth.status()["client_id"]:
        return ""
    return str(data.get("chatgpt_model") or "")


def has_key() -> bool:
    from core.chatgpt_auth import auth
    return auth.status()["enabled"] and bool(gpt_model())


def save(relationship_text: str | None = None, context_n: int | None = None, *,
         reply_target_on: bool | None = None, style_text: str | None = None,
         debug_view_on: bool | None = None, lang_text: str | None = None,
         model_text: str | None = None, model_client_id: str | None = None) -> None:
    with _LOCK:
        existing = _data()
        allowed = {"relationship", "context", "style", "reply_target", "debug_view", "lang",
                   "chatgpt_model", "model_client_id"}
        data = {name: value for name, value in existing.items() if name in allowed}
        updates = {"relationship": relationship_text, "context": context_n,
                   "reply_target": reply_target_on, "style": style_text,
                   "debug_view": debug_view_on, "lang": lang_text,
                   "chatgpt_model": model_text, "model_client_id": model_client_id}
        data.update({name: value for name, value in updates.items() if value is not None})
        temp = _CONFIG.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, _CONFIG)
