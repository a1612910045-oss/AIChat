# -*- coding: utf-8 -*-
"""子进程：截图 → 定位消息区 → OCR 头部会话名和消息 → 按会话去重，全在这边跑。
一次 OCR 250~800ms，放父进程的 Qt 主线程界面就僵了。
只往队列里丢纯 tuple/str（底色 bg 是 numpy，留在这边不过队列）。帧全程内存，绝不落盘。"""
import ctypes
import queue
import time
import traceback

import numpy as np

from app import chatapps
from app.capture import Capture, chat_area, unminimize
from app.ocr import Reader, read_title, similar


def _err(q):
    """异常压成一行发给父进程，子进程的 stderr 一般没人看得见。"""
    q.put(("status", " ".join(traceback.format_exc().split())[-200:]))


def _packet(full, area, title, reader, lines):
    """调试视图的一帧：整帧缩到长边 ≤1100 再走队列（原帧 2560 宽裸传要 20MB），只在内存里传，不落盘。
    整数步长切片够用，不引新依赖；框和坐标照发原始值，画的那边按 scale 折算。"""
    k = max(1, -(-max(full.shape[:2]) // 1100))
    small = np.ascontiguousarray(full[::k, ::k])
    return {"w": small.shape[1], "h": small.shape[0], "rgb": small.tobytes(), "scale": k,
            "area": tuple(int(v) for v in area[:4]) if area else None,
            "pane_top": int(area[5]) if area else 0, "title": title,
            "boxes": reader.last_boxes if reader else [],
            "lines": [(w, n, t) for w, n, t, _ in lines],
            "ocr_ms": reader.last_ms if reader else 0, "ts": time.time()}


def take_snapshot(hwnd, app, readers, timeout=8, allow_empty=False):
    """Fresh WGC/OCR read, including already-seen messages; independent of auto capture."""
    unminimize(hwnd)
    cap = Capture(hwnd, app=app)
    try:
        deadline = time.monotonic() + timeout
        full = None
        while time.monotonic() < deadline:
            if not cap.alive():
                raise RuntimeError("聊天窗口已关闭，请重新打开后生成。")
            full = cap.settled()
            if full is not None:
                break
            time.sleep(0.05)
        if full is None:
            raise RuntimeError("未能读取聊天画面，请保持会话窗口打开后重试。")
        area = chat_area(full, app=app)
        if area is None:
            raise RuntimeError("未找到带输入框的聊天区域，请打开目标会话；联系人列表不能用于生成。")
        x0, y0, x1, y1, bg, pane_top = area
        title = read_title(full[pane_top:y0, x0:x1], app)
        if not title:
            raise RuntimeError("未识别到会话名称，请打开要回复的会话后重试。")
        title = next((name for name in readers if similar(name, title)), title)
        if title not in readers:
            readers[title] = Reader(app)
        reader = readers[title]
        lines = reader.read(full[y0:y1, x0:x1], bg)
        if not lines and not allow_empty:
            raise RuntimeError("未读到聊天文字，请把要回复的历史消息显示在窗口中。")
        # Update the automatic deduplication checkpoint without filtering the manual input.
        reader.new_lines(lines)
        return title, [(who, name, text) for who, name, text, _ in lines], (x0, y0, x1, y1)
    finally:
        cap.stop()


def run(q, hwnd, enabled, debug_on, app_key=None, requests=None):
    """enabled 置位=采集，清掉=暂停。暂停时停掉 WGC 会话（Windows 那圈黄色采集边框也跟着没了），
    恢复时重开一个；readers 一直留着，去重状态不丢，恢复后不会把屏幕上的旧消息再报一遍。
    debug_on 置位才往队列里送整帧（一帧 2~3MB），关着一点额外活都不干。"""
    ctypes.windll.user32.SetProcessDPIAware()
    app = chatapps.get(app_key)
    cap = None
    readers = {}  # {会话名: Reader}，一个会话一套去重状态
    title, head = "", None  # 当前会话名 / 上一帧的头部像素
    last_area = None  # 上次发给父进程的 4 元组，变了才再发一次
    warned = False  # 消息区识别失败是否已经报过，拖窗口时别每帧刷一条
    while True:
        request = None
        if requests is not None:
            try:
                request = requests.get_nowait()
            except queue.Empty:
                pass
        if request is not None:
            requested_app = chatapps.get(request["app_key"])
            if hwnd != request["hwnd"] or requested_app is not app:
                if cap is not None:
                    cap.stop()
                    cap = None
                if requested_app is not app:
                    readers = {}
                hwnd, app = request["hwnd"], requested_app
                title, head, last_area = "", None, None
            try:
                title, visible, rect = take_snapshot(hwnd, app, readers, allow_empty=request.get("allow_empty", False))
                head = None
                q.put(("snapshot", request["id"], title, visible, rect, hwnd, app.key))
            except Exception as error:
                # Do not include exception bodies containing captured chat text.
                reason = str(error) if isinstance(error, RuntimeError) else "读取聊天失败，请调整窗口后重试。"
                q.put(("snapshot_error", request["id"], reason))
            continue
        if not enabled.is_set():
            if cap is not None:
                cap.stop()
                cap = None
                q.put(("paused",))
            enabled.wait(0.1)  # Keep accepting explicit one-shot requests while paused.
            continue
        if cap is None:
            try:
                cap = Capture(hwnd, app=app)
            except Exception as e:
                q.put(("dead", "无法开始采集：" + (" ".join(str(e).split())[:120] or type(e).__name__)))
                enabled.clear()  # 自己清掉，下一圈就去等着，别一秒重试几十次
                continue
            q.put(("resumed",))
        if not cap.alive():
            break
        try:
            unminimize(hwnd)
            full = cap.settled()
            if full is not None:
                reader, lines = None, []  # 调试视图要用，消息区没认出来时就是空的
                area = chat_area(full, app=app)
                if area is None:
                    if not warned:
                        q.put(("unavailable", "未找到带输入框的聊天区域，请打开目标会话；联系人列表不能用于生成。"))
                        warned = True
                    cap.area = None
                    title, head, last_area = "", None, None
                else:
                    warned = False
                    cap.area = area  # 采集线程拿它做 diff
                    x0, y0, x1, y1, bg, y_pane = area
                    rect = (x0, y0, x1, y1)
                    if rect != last_area:
                        q.put(("area", rect))
                        last_area = rect
                    crop = full[y_pane:y0, x0:x1]  # 头部：会话名在这里
                    if head is None or not np.array_equal(crop, head):  # 名字没动就别白跑一次 OCR
                        head = crop
                        name = read_title(crop, app)
                        # OCR 抖一下（「小分队」↔「小分认」）不能分裂出一个新会话
                        name = next((k for k in readers if similar(k, name)), name) if name else ""
                        if not name:
                            q.put(("unavailable", "未识别到会话名称，请保持目标会话打开后重试。"))
                            title, head = "", None
                            continue
                        if name != title:
                            title = name
                            q.put(("chat", title))
                    reader = readers.setdefault(title, Reader(app))
                    lines = reader.read(full[y0:y1, x0:x1], bg)
                    new = reader.new_lines(lines)
                    if new:
                        q.put(("lines", title, new, rect))
                if debug_on.is_set():
                    q.put(("debug", _packet(full, area, title, reader, lines)))
        except Exception:
            _err(q)  # 一帧出错不退出
        time.sleep(0.05)
    q.put(("dead", "采集停了（聊天窗口关了？）"))
    try:
        cap.wait()  # 采集线程若是报错死的，这里把错抛出来
    except Exception:
        _err(q)
