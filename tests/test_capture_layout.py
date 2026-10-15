"""A roster is never a conversation; OCR and fill coordinates use the right pane only."""
import queue
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import main as controller
from app import capture, chatapps, worker
from app import ocr
from app.ocr import Reader


def layout(dark=False, same_sidebar=False, standalone=False, scale=1):
    height, width = 600, 920
    left = 8 if standalone else 310
    bg = (30, 30, 31) if dark else (245, 245, 245)
    side = bg if same_sidebar else ((46, 46, 46) if dark else (235, 235, 235))
    frame = np.full((height, width, 3), bg, dtype=np.uint8)
    frame[:, :left] = side
    frame[31, :] = (80, 80, 80) if dark else (210, 210, 210)
    frame[80, left:width - 5] = (50, 50, 51) if dark else (220, 220, 220)
    inset = 14 if standalone else 0
    frame[430, left + inset:width - 5 - inset] = (50, 50, 51) if dark else (220, 220, 220)
    frame[580, left + inset:width - 5 - inset] = (50, 50, 51) if dark else (220, 220, 220)
    if scale != 1:
        frame = np.repeat(np.repeat(frame, scale, axis=0), scale, axis=1)
    return frame


def roster():
    frame = np.full((567, 426, 3), (46, 46, 46), dtype=np.uint8)
    frame[:, :61] = (40, 40, 40)
    frame[:32] = (40, 40, 40)
    # Search field stops before the add button; selected contact is green like my bubble.
    frame[44:75, 74:372] = (56, 56, 56)
    frame[85:145, 61:426] = (7, 166, 107)
    for top in (150, 215, 280, 345, 410, 475):
        frame[top:top + 40, 74:114] = (170, 170, 170)
    frame[562] = (40, 40, 40)
    return frame


def ocr_fixture(dark=False):
    frame = layout(dark=dark, same_sidebar=True)
    image = Image.fromarray(frame)
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 20)
    ink = (185, 185, 185) if dark else (35, 35, 35)
    draw.text((330, 44), "测试朋友", font=font, fill=ink)
    draw.rectangle((65, 102, 305, 145), fill=(149, 236, 105))
    draw.text((85, 112), "kk机器人AI", font=font, fill=(25, 25, 25))
    draw.text((85, 185), "干饭群", font=font, fill=ink)
    draw.text((85, 255), "公众号", font=font, fill=ink)
    draw.rectangle((580, 130, 890, 175), fill=(149, 236, 105))
    draw.text((596, 140), "又进入到学习开智期了", font=font, fill=(25, 25, 25))
    other_bg = (47, 47, 48) if dark else (255, 255, 255)
    draw.rectangle((370, 235, 590, 280), fill=other_bg)
    draw.text((386, 245), "你准备学什么", font=font, fill=ink)
    draw.text((350, 480), "未发送草稿", font=font, fill=ink)
    return np.array(image)


class ChatAreaTests(unittest.TestCase):
    def test_roster_with_green_selection_is_rejected(self):
        self.assertIsNone(capture.chat_area(roster()))

    def test_matching_sidebar_and_pane_colors_do_not_include_contacts(self):
        for dark in (False, True):
            for scale in (1, 2):
                with self.subTest(dark=dark, scale=scale):
                    area = capture.chat_area(layout(dark, same_sidebar=True, scale=scale))
                    self.assertIsNotNone(area)
                    x0, y0, x1, y1 = area[:4]
                    self.assertGreaterEqual(x0, 310 * scale)
                    self.assertLessEqual(x0, 310 * scale + 2)
                    self.assertGreaterEqual(x1, 915 * scale - 2)
                    self.assertLessEqual(x1, 915 * scale)
                    self.assertLessEqual(abs(y0 - 81 * scale), scale)
                    self.assertEqual(y1, 430 * scale)

    def test_narrow_standalone_chat_keeps_real_input_border(self):
        frame = layout(dark=True, standalone=True)[:, :426].copy()
        frame[80] = (30, 30, 31)
        frame[430] = (30, 30, 31)
        frame[80, 8:420] = (50, 50, 51)
        frame[430, 22:404] = (50, 50, 51)
        area = capture.chat_area(frame)
        self.assertIsNotNone(area)
        self.assertGreaterEqual(area[0], 8)
        self.assertLessEqual(area[0], 10)
        self.assertGreaterEqual(area[2], 418)
        self.assertLessEqual(area[2], 420)
        self.assertEqual((area[1], area[3]), (81, 430))

    def test_no_input_divider_never_falls_back_to_window_bottom(self):
        frame = layout()
        frame[430] = (245, 245, 245)
        self.assertIsNone(capture.chat_area(frame))

    def test_roster_snapshot_is_blocked_before_ocr_even_for_topics(self):
        cap = Mock()
        cap.alive.return_value = True
        cap.settled.return_value = roster()
        with patch.object(worker, "Capture", return_value=cap), patch.object(worker, "unminimize"), \
             patch.object(worker, "read_title") as title, patch.object(worker, "Reader") as reader:
            for allow_empty in (False, True):
                with self.assertRaisesRegex(RuntimeError, "联系人列表"):
                    worker.take_snapshot(42, chatapps.WECHAT, {}, allow_empty=allow_empty)
        title.assert_not_called()
        reader.assert_not_called()

    def test_unrecognized_layout_reaches_worker_instead_of_silent_timeout(self):
        cap = capture.Capture.__new__(capture.Capture)
        cap.app = chatapps.WECHAT
        cap.shape = cap.area = cap.last = cap.pending = None
        cap.t = cap.t0 = 0
        rgb = roster()
        frame = Mock(frame_buffer=np.dstack((rgb[:, :, ::-1], np.full(rgb.shape[:2], 255, np.uint8))))
        cap.on_frame_arrived(frame, None)
        self.assertIsNone(cap.area)
        self.assertTrue(np.array_equal(cap.pending, rgb))

    def test_automatic_capture_of_roster_does_not_emit_messages_or_run_ocr(self):
        cap = Mock()
        cap.alive.return_value = True
        cap.settled.return_value = roster()
        events = queue.Queue()
        enabled = Mock()
        enabled.is_set.return_value = True
        with patch.object(worker, "Capture", return_value=cap), patch.object(worker, "unminimize"), \
             patch.object(worker.ctypes.windll.user32, "SetProcessDPIAware"), \
             patch.object(worker, "read_title") as title, patch.object(worker, "Reader") as reader, \
             patch.object(worker.time, "sleep", side_effect=StopIteration), self.assertRaises(StopIteration):
            worker.run(events, 42, enabled, Mock())
        packets = list(events.queue)
        self.assertTrue(any(p[0] == "unavailable" for p in packets))
        self.assertFalse(any(p[0] == "lines" for p in packets))
        title.assert_not_called()
        reader.assert_not_called()

    def test_real_ocr_reads_bubbles_and_ignores_sidebar_and_unsent_draft(self):
        for dark in (False, True):
            with self.subTest(dark=dark):
                frame = ocr_fixture(dark)
                x0, y0, x1, y1, bg, _ = capture.chat_area(frame)
                lines = Reader(chatapps.WECHAT).read(frame[y0:y1, x0:x1], bg)
                self.assertEqual([line[0] for line in lines], ["me", "her"])
                text = "\n".join(line[2] for line in lines)
                self.assertIn("学习开智期", text)
                self.assertIn("准备学什么", text)
                for label in ("kk机器人AI", "干饭群", "公众号", "未发送草稿"):
                    self.assertNotIn(label, text)

    def test_short_latin_title_and_messages_work_through_snapshot(self):
        frame = ocr_fixture(dark=True)
        image = Image.fromarray(frame)
        draw = ImageDraw.Draw(image)
        draw.rectangle((310, 32, 915, 79), fill=(30, 30, 31))
        draw.text((360, 44), "YY", font=ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 14),
                  fill=(224, 224, 224))
        cap = Mock()
        cap.alive.return_value = True
        cap.settled.return_value = np.array(image)
        with patch.object(worker, "Capture", return_value=cap), patch.object(worker, "unminimize"):
            title, lines, _ = worker.take_snapshot(42, chatapps.WECHAT, {})
        self.assertEqual(title, "YY")
        self.assertEqual([r[0] for r in lines], ["me", "her"])


class TitleTests(unittest.TestCase):
    def test_initial_ocr_miss_can_recover_title_without_reusing_an_old_name(self):
        header = np.full((48, 375, 3), (30, 30, 31), np.uint8)
        box = [[98, 32], [158, 32], [158, 62], [98, 62]]
        engine = Mock(side_effect=[(None, None), ([[box, "YY", 0.97]], None)])
        with patch.object(ocr, "_engine", return_value=engine):
            self.assertEqual(ocr.read_title(header), "YY")

    def test_navigation_and_right_hand_controls_cannot_become_a_title(self):
        header = np.full((48, 375, 3), (30, 30, 31), np.uint8)

        def controls(crop, **kwargs):
            width = crop.shape[1]
            right = [[width * 0.8, 12], [width * 0.9, 12], [width * 0.9, 28], [width * 0.8, 28]]
            arrow = [[20, 12], [30, 12], [30, 28], [20, 28]]
            return [[right, "call", 0.95], [arrow, "<", 0.95]], None

        with patch.object(ocr, "_engine", return_value=controls):
            self.assertEqual(ocr.read_title(header), "")


class InvalidPanelControllerTests(unittest.TestCase):
    def test_roster_invalidates_old_coordinates_and_pending_gpt_result(self):
        original = dict(controller.state)
        try:
            controller.state.update(area=(1, 2, 3, 4), busy=True, session=5,
                                    manual_pending=7, rerun=("朋友", []))
            events = queue.Queue()
            events.put(("unavailable", "请打开会话"))
            ov = Mock()
            with patch.object(controller, "q", events, create=True), patch.object(controller, "ov", ov, create=True):
                controller.drain()
            self.assertIsNone(controller.state["area"])
            self.assertIsNone(controller.state["manual_pending"])
            self.assertFalse(controller.state["busy"])
            self.assertEqual(controller.state["session"], 6)
            ov.invalidate_replies.assert_called_once()
        finally:
            controller.state.clear()
            controller.state.update(original)


if __name__ == "__main__":
    unittest.main()
