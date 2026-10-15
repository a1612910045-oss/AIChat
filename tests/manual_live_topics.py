"""Run explicitly after authorization; verifies topic generation using synthetic input."""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "source"))

from app import settings
from core.chatgpt_auth import auth
from core.engine import analyze


def main():
    manifest_path = PROJECT / "deployment.json"
    manifest = json.loads(manifest_path.read_text("utf-8-sig"))
    settings._CONFIG = (PROJECT / manifest["entrypoint"]).parent / "config.json"
    if not auth.status()["enabled"] or not settings.gpt_model():
        raise SystemExit("请先在程序中完成 ChatGPT 登录和模型选择。")
    model = settings.gpt_model()
    print("Testing proactive openings with selected GPT:", model, flush=True)
    result = analyze([], "friends", model=model, mode="topics", topic_hint="周末安排",
                     style="简短自然，不用客套话")
    verification = {"synthetic_chat_test": True, "empty_visible_history": True, "model": model,
                    "mode": result["mode"], "topic_hint": result["topic_hint"],
                    "candidates": result["candidates"], "best_index": result["best_index"],
                    "reason": result["reason"]}
    assert len(result["candidates"]) == 3 and result["mode"] == "topics"
    (PROJECT / "live-topics-verification.json").write_text(
        json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest["live_topics_verified"] = True
    manifest["topics_tested_model"] = model
    manifest["live_topics_verification_artifact"] = "live-topics-verification.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(verification, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
