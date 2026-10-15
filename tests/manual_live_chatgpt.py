"""Run explicitly after browser authorization; uses synthetic chat and plan quota."""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "source"))

from app import settings
from core.chatgpt_auth import auth
from core.engine import analyze

manifest_path = PROJECT / "deployment.json"
manifest = json.loads(manifest_path.read_text("utf-8-sig"))
settings._CONFIG = (PROJECT / manifest["entrypoint"]).parent / "config.json"
state = auth.status()
manifest["chatgpt_login_verified"] = bool(state["connected"] and state["enabled"])
manifest["live_gpt_response_verified"] = False
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
model = settings.gpt_model()
if not state["enabled"] or not model:
    raise SystemExit("请先在程序中完成 ChatGPT 登录和模型选择。")

chat = "明天下午三点一起喝杯咖啡怎么样？"
assert "三点" in chat  # The UTF-8 file avoids PowerShell's ASCII stdin conversion.
print("Testing selected GPT:", model, flush=True)
result = analyze([("her", chat)], "friends", model=model, context=3,
                 style="简短自然，不用客套话")
verification = {"synthetic_chat_test": True, "input": chat, "model": model,
                "candidates": result["candidates"], "best_index": result["best_index"],
                "answers": result["answers"]}
(PROJECT / "live-verification.json").write_text(
    json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8")
manifest["live_gpt_response_verified"] = True
manifest["tested_model"] = model
manifest["live_verification_artifact"] = "live-verification.json"
manifest.pop("live_test_error", None)
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(verification, ensure_ascii=False, indent=2), flush=True)
