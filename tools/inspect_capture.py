"""Inspect one local frame in memory; no frame or chat transcript is written to disk."""
import base64
import ctypes
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
from PIL import Image
from app.capture import Capture, chat_area, find_chat_hwnd

ctypes.windll.user32.SetProcessDPIAware()
hwnd, app = find_chat_hwnd()
capture = Capture(hwnd, app=app)
try:
    deadline = time.monotonic() + 8
    frame = None
    while time.monotonic() < deadline:
        frame = capture.settled()
        if frame is not None:
            break
        time.sleep(0.05)
    if frame is None:
        raise SystemExit("No settled frame available")
    area = chat_area(frame, app=app)
    metadata = {"app": app.key, "hwnd": hwnd, "shape": list(frame.shape),
                "area": list(area[:4]) + [int(area[5])] if area else None}
    output = {"metadata": metadata}
    if "--image" in sys.argv:
        preview = Image.fromarray(frame)
        preview.thumbnail((1400, 1000))
        buffer = io.BytesIO()
        preview.save(buffer, format="PNG")
        output["image"] = base64.b64encode(buffer.getvalue()).decode("ascii")
    print(json.dumps(output))
finally:
    capture.stop()
