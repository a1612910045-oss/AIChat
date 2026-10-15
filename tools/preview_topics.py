"""Exercise the native topic UI with synthetic content; no capture, network or sending."""
import sys
from pathlib import Path
from unittest.mock import Mock, patch

sys.stdout.reconfigure(encoding="utf-8")
PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "source"))

from PySide6.QtCore import QTimer
from app import i18n, settings
from app.chatgpt_account import ChatGPTAccount
from app.overlay import Overlay
from core.chatgpt_auth import auth

TOPICS = {"mode": "topics", "topic_hint": "周末安排",
          "candidates": ["周末想出去走走吗", "周末想在家休息还是出去玩", "有空一起喝杯咖啡吗"],
          "best_index": 1, "reason": "问一个容易回答的轻松选择，不急着敲定安排", "answers": {}}
REPLY = {"candidates": ["行啊，几点", "可以，明天聊", "好，明天见"], "best_index": 0,
         "answers": {"best_action": {"choice": "make_plan"}, "true_intent": {"choice": "casual_chat"},
                     "she_needs": {"choice": "action"}, "danger_level": {"score": 1}}}


def main():
    account = {"client_id": "demo", "connected": True, "enabled": True, "email": "demo@example.invalid"}
    with patch.object(auth, "status", return_value=account), patch.object(auth, "accounts", return_value=[account]), \
         patch.object(ChatGPTAccount, "_initial_models"), patch.multiple(
             settings, has_key=lambda: True, language=lambda: "zh", gpt_model=lambda: "demo-gpt",
             relationship=lambda: "friends", style=lambda: "", context=lambda: 10,
             reply_target=lambda: False, debug_view=lambda: False):
        i18n.reload("zh")
        filled, requests = Mock(), []

        def topics(direction):
            requests.append(("topics", direction))
            ov.set_generation_mode("topics")
            ov.set_busy(True)

        ov = Overlay(on_fill=filled, on_generate=lambda: requests.append(("reply", "")),
                     on_generate_topics=topics)
        ov.win.setWindowTitle("GPT Chat Helper · 合成话题预览")
        ov.set_snapshot("示例会话", [])
        ov.topicInput.setText("  周末安排  ")
        ov.topicButton.click()
        assert requests == [("topics", "周末安排")]
        assert not ov.generateButton.isEnabled() and not ov.topicButton.isEnabled()
        assert not ov.topicInput.isEnabled()
        ov.generateButton.click()
        assert len(requests) == 1
        ov.show(TOPICS)
        assert ov.generateButton.isEnabled() and ov.topicButton.isEnabled() and ov.topicInput.isEnabled()
        assert ov.intent.isHidden() and ov.tension.isHidden()
        assert ov.summary.text() == TOPICS["reason"]
        ov.cards[0].fillButton.click()
        filled.assert_called_once_with(TOPICS["candidates"][1])
        ov.show(REPLY)
        assert not ov.intent.isHidden() and not ov.tension.isHidden()
        ov.show(TOPICS)
        ov.topicInput.setText("周末安排")
        ov.set_capture(False)
        ov.set_status("话题已生成，选一句作为开场，可修改后发送", "success")

        def finish():
            assert ov.win.grab().save(str(PROJECT / "topic-generation-preview.png"), "PNG")
            print("Native topic UI, busy state, recommendation fill and reply-mode restoration verified.")
            ov.app.quit()

        QTimer.singleShot(350, finish)
        ov.run()


if __name__ == "__main__":
    main()
