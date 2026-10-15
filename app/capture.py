# -*- coding: utf-8 -*-
"""找聊天窗口 + Windows Graphics Capture 盯着它 + 从帧里定位消息区。帧全程内存，绝不落盘。"""
import ctypes
import os
import time
from ctypes import wintypes

import numpy as np

from app import chatapps

u32 = ctypes.windll.user32


def find_chat_hwnd(want=None):
    """枚举可见顶层窗口，按进程名认出聊天软件，返回 (hwnd, ChatApp)。
    微信：同进程还有工具窗和看图窗，面积可能更大，所以按标题挑主窗口。
    KakaoTalk：一个对话一个窗口，主列表窗（标题「카카오톡」）不是目标，挑剩下最大的那个。
    want=某个 ChatApp 时只找它，None 时先到先得（两个都开着就按 APPS 顺序）。"""
    k32 = ctypes.windll.kernel32
    found = []

    def exe_of(pid):
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        buf, size = ctypes.create_unicode_buffer(1024), ctypes.c_uint(1024)
        ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        k32.CloseHandle(h)
        return os.path.basename(buf.value).lower() if ok else ""

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd):
            return True
        pid = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        app = chatapps.by_exe(exe_of(pid.value))
        if app and (want is None or app is want):
            title = ctypes.create_unicode_buffer(256)
            u32.GetWindowTextW(hwnd, title, 256)
            rect = wintypes.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(rect))
            area = (rect.right - rect.left) * (rect.bottom - rect.top)
            found.append((hwnd, title.value, area, app))
        return True

    u32.EnumWindows(cb, 0)
    if not found:
        raise RuntimeError("没找到聊天窗口，开着吗？")
    for app in chatapps.APPS.values():
        mine = [f for f in found if f[3] is app]
        if not mine:
            continue
        if app.main_title:
            hit = next((f for f in mine if f[1] == app.main_title), None)
            if hit:
                return hit[0], app
        rooms = [f for f in mine if f[1] not in app.skip_titles] or mine
        best = max(rooms, key=lambda f: f[2])
        return best[0], app
    raise RuntimeError("没找到聊天窗口，开着吗？")


def find_wechat_hwnd():
    """老名字：只返回 hwnd，给还没改过来的调用方用。"""
    return find_chat_hwnd()[0]


def unminimize(hwnd):
    """Windows 不渲染最小化的窗口，什么截图法都拿不到画面。发现被最小化就无激活还原，再压到所有窗口最底下——
    看着跟收起来一样，但 DWM 继续画。不抢焦点、不动大小位置。返回是否动了手。"""
    if not u32.IsIconic(hwnd):
        return False
    u32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
    u32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x13)  # HWND_BOTTOM, SWP_NOSIZE|SWP_NOMOVE|SWP_NOACTIVATE
    return True


def _legacy_chat_area(full, header_h=60):
    """消息列表区 (x0, y_top, x1, y_in, 面板底色, y_pane)，全靠像素锚点，不写死坐标，深浅主题通用：
    - 面板底色 = 右半边最常见的颜色（抽样算，全量 np.unique 在 2560 宽的图上要半秒）
    - 面板左/右边界 = 第一/最后一根「底色占比 > 30%」的列（联系人列表是另一种底色，占比 0）
    - y_pane = 面板第一行；会话名就印在 y_pane~y_top 这条头部里（公告条也在里面）
    - 横向分隔线 = 整行单色且非底色；输入框顶 y_in = 面板 45% 高度以下第一根；
      公告条下面那根（有的话）= 消息区顶 y_top，没有就用 header_h
    认不出（窗口太小 / 拖到一半布局没铺好）返回 None。
    ponytail: 输入框拉高超过面板一半会认错；header_h 按 100% DPI 给的，缩放了按比例调。"""
    H, W = full.shape[:2]
    right = full[::8, W // 2::8].reshape(-1, 3)
    vals, cnt = np.unique(right, axis=0, return_counts=True)
    bg = vals[cnt.argmax()]
    isbg = np.abs(full.astype(int) - bg).sum(-1) <= 6
    col = isbg[H // 4: H * 3 // 4].mean(0)
    x0 = int(np.argmax(col > 0.3))
    x1 = W - int(np.argmax(col[::-1] > 0.3))
    row = isbg[:, x0:x1].mean(1)
    y0 = int(np.argmax(row > 0.9))
    y1 = H - int(np.argmax(row[::-1] > 0.9))
    band = full[y0:y1, x0:x1].astype(int)
    seps = y0 + np.where((band.std(axis=(1, 2)) < 4) & (row[y0:y1] < 0.1))[0]
    seps = [int(s) for i, s in enumerate(seps) if i == 0 or s - seps[i - 1] > 3]
    below = [s for s in seps if s > y0 + 0.45 * (y1 - y0)]
    y_in = below[0] if below else y1
    above = [s for s in seps if y0 + header_h < s < y_in - 50]
    y_top = above[-1] if above else y0 + header_h
    if x1 - x0 < 100 or y_in - y_top < 40:
        return None
    return x0, y_top, x1, y_in, bg, y0


def _horizontal_rules(full):
    """Thin, long horizontal strokes near the right edge, independent of pane colors."""
    height, width = full.shape[:2]
    pixels = full.astype(np.int16)
    minimum = max(100, int(width * 0.3))
    right_margin = max(32, int(width * 0.06))
    rules = []
    for gap in (1, 2, 3):
        middle = pixels[gap:-gap]
        above, below = pixels[:-2 * gap], pixels[2 * gap:]
        stroke = ((np.abs(middle - above).max(2) >= 3) &
                  (np.abs(middle - below).max(2) >= 3))
        # Text, icons and avatars cannot form a long run of one line color.
        flat = np.abs(middle[:, 1:] - middle[:, :-1]).max(2) <= 2
        stroke[:, 1:] &= flat
        stroke[:, :-1] &= flat
        for row in np.flatnonzero(stroke.sum(1) >= minimum):
            edges = np.diff(np.pad(stroke[row].astype(np.int8), (1, 1)))
            starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
            for left, right in zip(starts, ends):
                if right - left >= minimum and right >= width - right_margin:
                    item = (int(row + gap), int(left), int(right))
                    if not any(abs(item[0] - old[0]) <= 3 and abs(item[1] - old[1]) <= 6
                               for old in rules):
                        rules.append(item)
    return sorted(rules)


def chat_area(full, header_h=60, app=None):
    """Locate a conversation using its header divider and composer top border.

    A roster has no message composer. Never treat its bottom window edge as one.
    Header/composer anchors also isolate the right pane when sidebar colors match it.
    """
    if full.ndim != 3 or full.shape[2] != 3 or min(full.shape[:2]) < 100:
        return None
    if app is not None and app.key != "wechat":
        return _legacy_chat_area(full, header_h)
    height, width = full.shape[:2]
    rules = _horizontal_rules(full)
    alignment = max(24, int(width * 0.035))
    for input_y, input_left, input_right in rules:
        if not 0.4 * height < input_y < 0.9 * height:
            continue
        headers = [(y, left, right) for y, left, right in rules
                   if max(20, height * 0.04) < y < min(height * 0.35, input_y - 60)
                   and abs(left - input_left) <= alignment
                   and abs(right - input_right) <= alignment]
        if not headers:
            continue
        header_y, left, right = headers[-1]
        # A rounded composer border begins a few pixels inside the actual pane.
        x0, x1 = min(left, input_left), max(right, input_right)
        if input_y - header_y < 40 or x1 - x0 < 100:
            continue
        crop = full[header_y + 1:input_y, x0:x1]
        values, counts = np.unique(crop[::8, ::8].reshape(-1, 3), axis=0, return_counts=True)
        bg = values[counts.argmax()]
        background = (np.abs(full[:header_y, x0:x1].astype(np.int16) - bg).sum(2) <= 6).mean(1)
        rows = np.flatnonzero(background > 0.75)
        pane_top = int(rows[0]) if rows.size else max(0, header_y - header_h)
        return x0, header_y + 1, x1, input_y, bg, pane_top
    return None


class Capture:
    """WGC 盯窗口。采集线程只做「跟上一帧比」；settled() 在画面停稳后交出整帧，中间帧（滚动动画、
    新消息滑入的半截气泡）全跳过。动图表情永远停不稳，所以最多等 max_wait 秒照样交。"""

    def __init__(self, hwnd, settle=0.25, max_wait=1.0, app=None):
        from windows_capture import WindowsCapture

        self.settle, self.max_wait = settle, max_wait
        self.app = app
        self.shape = self.area = self.last = self.pending = None
        self.t = self.t0 = 0.0
        # 包装层默认 cursor_capture=True，会去调 SetIsCursorCaptureEnabled。
        # 这个属性要 Win10 2004（build 19041）才有，1909 及更早直接抛 CursorConfigUnsupported。
        # 显式 None 走系统默认，不去切换；draw_border 同理。
        cap = WindowsCapture(cursor_capture=None, draw_border=None, window_hwnd=hwnd)
        cap.event(self.on_frame_arrived)
        cap.event(self.on_closed)
        self.ctl = cap.start_free_threaded()

    def on_frame_arrived(self, frame, control):
        full = np.ascontiguousarray(frame.frame_buffer[:, :, :3][:, :, ::-1])  # BGRA → RGB；缓冲区回调后就没了，必须拷
        if full.max() == 0:
            return
        if self.area is None or full.shape != self.shape:
            self.shape, self.area = full.shape, chat_area(full, app=self.app)
        # ponytail: diff 不含头部——公告条会滚动，带上它就永远停不稳。切会话时消息区必然也变，照样出帧。
        if self.area is None:
            # Deliver invalid layouts too, so the worker can report a roster instead of timing out.
            chat = full
        else:
            x0, y0, x1, y1 = self.area[:4]
            chat = full[y0:y1, x0:x1]
        if self.last is not None and np.array_equal(chat, self.last):
            return
        self.last = chat
        if self.pending is None:
            self.t0 = time.perf_counter()
        self.pending, self.t = full, time.perf_counter()

    def on_closed(self):
        pass

    def settled(self):
        """停稳了就返回整帧，否则 None。"""
        if self.pending is None:
            return None
        now = time.perf_counter()
        if now - self.t < self.settle and now - self.t0 < self.max_wait:
            return None
        full, self.pending = self.pending, None
        return full

    def alive(self):
        return not self.ctl.is_finished()

    def stop(self):
        self.ctl.stop()

    def wait(self):
        self.ctl.wait()  # 采集线程若是报错死的，这里把错误抛出来
