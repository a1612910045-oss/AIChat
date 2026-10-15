"""Old-message snapshots and explicit generation without automatic capture."""
import queue
import unittest
from collections import deque
from unittest.mock import Mock, patch

import numpy as np

import main as controller
from app import chatapps, worker


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.frame = np.full((200, 400, 3), 240, dtype=np.uint8)
        self.area = (0, 50, 400, 170, np.array([240, 240, 240]), 0)
        self.capture = Mock()
        self.capture.alive.return_value = True
        self.capture.settled.return_value = self.frame
        self.reader = Mock()
        self.reader.read.return_value = [("her", None, "以前问的事情有结果了吗", 30),
                                         ("me", None, "我再查一下", 80)]
        self.reader.new_lines.return_value = []  # Both messages were already seen.

    def snapshot(self, title="朋友", readers=None, allow_empty=False):
        with patch.object(worker, "Capture", return_value=self.capture), \
             patch.object(worker, "Reader", return_value=self.reader), \
             patch.object(worker, "unminimize"), patch.object(worker, "chat_area", return_value=self.area), \
             patch.object(worker, "read_title", return_value=title):
            return worker.take_snapshot(42, chatapps.get(None), readers or {}, allow_empty=allow_empty)

    def test_already_seen_messages_are_kept_even_if_last_message_is_mine(self):
        title, visible, rect = self.snapshot()
        self.assertEqual(title, "朋友")
        self.assertEqual([row[0] for row in visible], ["her", "me"])
        self.assertEqual(len(visible), 2)
        self.assertEqual(rect, self.area[:4])
        self.capture.stop.assert_called_once()

    def test_uses_fresh_capture_for_every_explicit_request(self):
        self.snapshot()
        self.snapshot()
        self.assertEqual(self.capture.stop.call_count, 2)
        self.assertEqual(self.reader.read.call_count, 2)

    def test_missing_title_does_not_reuse_previous_conversation(self):
        with self.assertRaisesRegex(RuntimeError, "会话名称"):
            self.snapshot(title="", readers={"旧会话": self.reader})
        self.reader.read.assert_not_called()
        self.capture.stop.assert_called_once()

    def test_empty_visible_history_is_reported_and_capture_is_closed(self):
        self.reader.read.return_value = []
        with self.assertRaisesRegex(RuntimeError, "聊天文字"):
            self.snapshot()
        self.capture.stop.assert_called_once()

    def test_topics_accept_empty_history_but_still_require_actual_chat_title(self):
        self.reader.read.return_value = []
        title, visible, rect = self.snapshot(allow_empty=True)
        self.assertEqual((title, visible, rect), ("朋友", [], self.area[:4]))
        with self.assertRaisesRegex(RuntimeError, "会话名称"):
            self.snapshot(title="", allow_empty=True)

    def test_worker_accepts_explicit_request_while_automatic_capture_is_paused(self):
        requests, output = queue.Queue(), queue.Queue()
        requests.put({"id": 7, "hwnd": 42, "app_key": chatapps.get(None).key})
        enabled = Mock()
        enabled.is_set.return_value = False
        enabled.wait.side_effect = StopIteration("stop test loop")
        visible = [("her", None, "以前的消息")]
        with patch.object(worker, "take_snapshot", return_value=("朋友", visible, (0, 50, 400, 170))), \
             patch.object(worker.ctypes.windll.user32, "SetProcessDPIAware"), \
             self.assertRaises(StopIteration):
            worker.run(output, 42, enabled, Mock(), requests=requests)
        self.assertEqual(output.get_nowait()[:4], ("snapshot", 7, "朋友", visible))
        enabled.set.assert_not_called()


class ManualControllerTests(unittest.TestCase):
    def setUp(self):
        self.original_state = dict(controller.state)
        self.original_chats = dict(controller.chats)
        controller.state.update(busy=False, rerun=None, chat="旧会话", session=5,
                                manual_id=0, manual_pending=None, manual_deadline=0,
                                manual_mode="reply", manual_hint="", active_mode="reply", active_hint="")
        controller.chats.clear()
        self.overlay = Mock()
        self.overlay.current_chat.return_value = "旧会话"
        self.events = queue.Queue()
        self.requests = queue.Queue()
        self.capture_on = Mock()
        for name, value in (("ov", self.overlay), ("q", self.events),
                            ("snapshot_requests", self.requests), ("child", Mock()),
                            ("capture_on", self.capture_on)):
            patcher = patch.object(controller, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ready = patch.object(controller.settings, "has_key", return_value=True)
        self.ready.start()
        self.addCleanup(self.ready.stop)

    def tearDown(self):
        controller.state.clear()
        controller.state.update(self.original_state)
        controller.chats.clear()
        controller.chats.update(self.original_chats)

    def request(self):
        app = chatapps.get(None)
        with patch.object(controller, "find_chat_hwnd", return_value=(42, app)):
            controller.on_generate()
        return self.requests.get_nowait()

    def test_paused_capture_stays_paused_when_requesting_once(self):
        request = self.request()
        self.assertEqual(request["hwnd"], 42)
        self.capture_on.set.assert_not_called()
        self.assertEqual(controller.state["manual_pending"], request["id"])
        self.overlay.set_busy.assert_called_with(True)

    def test_repeated_click_does_not_queue_duplicate_inference(self):
        self.request()
        controller.on_generate()
        self.assertTrue(self.requests.empty())

    def test_topic_request_uses_fresh_current_chat_and_keeps_capture_paused(self):
        with patch.object(controller, "find_chat_hwnd", return_value=(42, chatapps.get(None))):
            controller.on_generate_topics("  周末安排  ")
        request = self.requests.get_nowait()
        self.assertTrue(request["allow_empty"])
        self.capture_on.set.assert_not_called()
        self.events.put(("snapshot", request["id"], "新会话", [], (1, 2, 3, 4), 42, "wechat"))
        with patch.object(controller, "start_analyze") as analyze:
            controller.drain()
        analyze.assert_called_once_with("新会话", [], mode="topics", topic_hint="周末安排")
        self.overlay.set_generation_mode.assert_called_with("topics")

    def test_switching_buttons_while_snapshot_pending_does_not_replace_topic_request(self):
        with patch.object(controller, "find_chat_hwnd", return_value=(42, chatapps.get(None))):
            controller.on_generate_topics("电影")
        self.requests.get_nowait()
        controller.on_generate()
        controller.on_generate_topics("游戏")
        self.assertTrue(self.requests.empty())
        self.assertEqual((controller.state["manual_mode"], controller.state["manual_hint"]), ("topics", "电影"))

    def test_topic_target_change_preserves_mode_and_direction_even_without_history(self):
        chat = controller.chat_of("朋友")
        chat["result"] = {"mode": "topics", "topic_hint": "电影"}
        with patch.object(controller, "start_analyze") as analyze:
            controller.on_target_change("朋友", "小李")
        analyze.assert_called_once_with("朋友", [], "topics", "电影")

    def test_background_analysis_forwards_topic_mode_and_direction(self):
        completed = queue.Queue()
        with patch.object(controller, "analyze", return_value={"mode": "topics"}) as analyze, \
             patch.object(controller, "results", completed):
            controller.analyze_bg([], "朋友", 3, None, 5, "topics", "最近有什么电影")
        self.assertEqual(analyze.call_args.kwargs["mode"], "topics")
        self.assertEqual(analyze.call_args.kwargs["topic_hint"], "最近有什么电影")
        self.assertEqual(completed.get_nowait(), ("ok", {"mode": "topics"}, "朋友", 3, 5))

    def test_own_new_message_during_topic_generation_keeps_buttons_busy_until_stale_result_returns(self):
        controller.state.update(busy=True, active_mode="topics")
        self.events.put(("lines", "旧会话", [("me", None, "我刚发的新消息")], (1, 2, 3, 4)))
        controller.drain()
        self.overlay.set_busy.assert_called_with(True)
        completed = queue.Queue()
        completed.put(("ok", {"mode": "topics"}, "旧会话", 0, 5))
        with patch.object(controller, "results", completed), patch.object(controller, "update_result", queue.Queue()):
            controller.tick()
        self.assertFalse(controller.state["busy"])
        self.overlay.set_busy.assert_called_with(False)
        self.overlay.show.assert_not_called()

    def test_snapshot_generates_for_actual_window_including_own_last_message(self):
        request = self.request()
        visible = [("her", "朋友", "旧消息还没处理呢"), ("me", None, "让我看看")]
        controller.chat_of("旧会话")["history"] = deque([("her", "不要使用这段缓存", None)])
        self.events.put(("snapshot", request["id"], "新会话", visible, (1, 2, 3, 4), 42, "wechat"))
        with patch.object(controller, "start_analyze") as analyze:
            controller.drain()
        analyze.assert_called_once_with("新会话", [("her", "旧消息还没处理呢", "朋友"), ("me", "让我看看", None)])
        self.assertEqual(controller.state["chat"], "新会话")
        self.overlay.set_snapshot.assert_called_once_with("新会话", visible)

    def test_late_snapshot_after_account_change_is_ignored(self):
        request = self.request()
        controller.on_account_changed()
        self.events.put(("snapshot", request["id"], "新会话", [("her", None, "内容")], (1, 2, 3, 4), 42, "wechat"))
        with patch.object(controller, "start_analyze") as analyze:
            controller.drain()
        analyze.assert_not_called()
        self.overlay.set_snapshot.assert_not_called()

    def test_new_messages_do_not_start_second_job_while_snapshot_is_pending(self):
        self.request()
        self.events.put(("lines", "旧会话", [("her", None, "另一条新消息")], (1, 2, 3, 4)))
        with patch.object(controller, "start_analyze") as analyze:
            controller.drain()
        analyze.assert_not_called()

    def test_snapshot_failure_releases_button(self):
        request = self.request()
        self.events.put(("snapshot_error", request["id"], "未读到聊天文字"))
        controller.drain()
        self.assertIsNone(controller.state["manual_pending"])
        self.overlay.set_busy.assert_called_with(False)
        self.overlay.set_status.assert_called_with("未读到聊天文字", "error")

    def test_closed_chat_window_is_reported_without_queuing(self):
        with patch.object(controller, "find_chat_hwnd", side_effect=RuntimeError("closed")):
            controller.on_generate()
        self.assertTrue(self.requests.empty())
        self.assertIsNone(controller.state["manual_pending"])

    def test_timeout_releases_button_without_model_request(self):
        controller.state.update(manual_pending=1, manual_deadline=1)
        with patch.object(controller, "drain"), patch.object(controller.time, "monotonic", return_value=100), \
             patch.object(controller, "update_result", queue.Queue()), patch.object(controller, "results", queue.Queue()):
            controller.tick()
        self.assertIsNone(controller.state["manual_pending"])
        self.overlay.set_busy.assert_called_with(False)
        self.overlay.set_status.assert_called_with("读取聊天超时，请确认会话窗口已打开后重试。", "error")


if __name__ == "__main__":
    unittest.main()
