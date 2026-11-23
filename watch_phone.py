# -*- coding: utf-8 -*-
"""从手机 ScreenStream 拉画面，识别棋盘并在电脑上显示解密结果。

手机和电脑在同一个 Wi-Fi。手机上的 ScreenStream 已经开始投屏后：

    python watch_phone.py http://192.168.1.8:8080

窗口里按 Q 或 Esc 退出。如果应用里设了密码：

    python watch_phone.py http://192.168.1.8:8080 --pin 1234

画面拉取、识别解密、窗口刷新分成三条线。后台约每 0.5 秒看一眼最新画面：
不是棋盘就清掉旧叠加；同局解出来后粘住结果（同尺寸一律不重算）；
离开棋盘、过关后空白新盘、或空白盘上新尺寸连续确认后，才自动识别下一局。
局中乱读成 3/6/8 等尺寸会忽略。手动「重新识别」/R 可强制重算。
「出提示」抓当前这一帧，把分步提示放在画面旁边，一次只显示一步，可左右切换。再点一次关闭。提示图可滚轮缩放、按住拖动。
启动后如果还没有 adb 配对，会先弹出配对框，不进入游戏画面。填手机上的 IP、配对端口和配对码即可；也可以选择只看、不自动点击。
「自动点击」用 adb 按当前方案逐格双击到手机上。单击会被游戏标成「不是猫」。点出猫就继续，点出叉就排除这一格、重新解密，再点新方案。再点一次按钮停止。
若一次解出多套方案，先当识别错重试；连续 3 次仍是多套，才按真多解显示。
"""

import argparse
import base64
import ctypes
import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

import tkinter as tk

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageTk

import hint_cats
from solve_cats import (
    board_has_marks,
    draw_recognition,
    draw_solutions,
    finish_recognize,
    is_celebration_screen,
    load_font,
    peek_board,
    same_board_colors,
    save_bgr,
    setup_console,
    solve_all,
)

WINDOW = "猫猫方块解密"
# 后台巡检间隔；同局已解则很快跳过，不会每次全量解密。
RECOGNIZE_INTERVAL = 0.5
# 同一失败局面冷却，避免遮挡时空转。
FAIL_COOLDOWN = 2.0
# 多套解法连续出现这么多次，才当真；否则当识别错重试。
MULTI_CONFIRM = 3
# 连续多少次看不到棋盘，才丢掉本局结果（避免闪帧误清）。
LEAVE_CLEAR_TICKS = 4
# 空白盘上连续多少次读到同一新尺寸，才当真换局（过滤 10→3 闪一下）。
SIZE_CHANGE_CONFIRM = 3
# 窗口刷新间隔（毫秒）。
DISPLAY_MS = 50

# Windows：阻止休眠/熄屏（SetThreadExecutionState）
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002


def prevent_display_sleep():
    """运行期间保持屏幕常亮，避免几分钟不动就熄屏。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED
        )
    except Exception:
        pass


def allow_display_sleep():
    """退出时恢复系统默认电源策略。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
    except Exception:
        pass


def random_id(length=16):
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    raw = os.urandom(length)
    return "".join(alphabet[b % len(alphabet)] for b in raw)


class SocketBuffer(object):
    def __init__(self, sock, leftover=b""):
        self.sock = sock
        self.buf = leftover

    def recv(self, size):
        if self.buf:
            out = self.buf[:size]
            self.buf = self.buf[size:]
            return out
        return self.sock.recv(size)

    def sendall(self, data):
        self.sock.sendall(data)

    def close(self):
        self.sock.close()


def read_exact(sock, size):
    buf = b""
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise ConnectionError("手机连接中断")
        buf += chunk
    return buf


def ws_send(sock, text):
    data = text.encode("utf-8")
    mask = os.urandom(4)
    header = bytearray([0x81])
    length = len(data)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(length.to_bytes(2, "big"))
    else:
        header.append(0x80 | 127)
        header.extend(length.to_bytes(8, "big"))
    header.extend(mask)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    sock.sendall(bytes(header) + masked)


def ws_recv(sock):
    while True:
        first, second = read_exact(sock, 2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(read_exact(sock, 2), "big")
        elif length == 127:
            length = int.from_bytes(read_exact(sock, 8), "big")
        if second & 0x80:
            mask = read_exact(sock, 4)
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(read_exact(sock, length)))
        else:
            payload = read_exact(sock, length)
        if opcode == 0x8:
            raise ConnectionError("手机端关闭了连接")
        if opcode == 0x9:
            mask = os.urandom(4)
            frame = bytearray([0x8A, 0x80 | len(payload)])
            frame.extend(mask)
            frame.extend(b ^ mask[i % 4] for i, b in enumerate(payload))
            sock.sendall(frame)
            continue
        if opcode in (0x1, 0x2):
            return payload.decode("utf-8")


def open_stream(page_url, pin):
    parsed = urlparse(page_url)
    if parsed.scheme not in ("http", ""):
        raise RuntimeError("目前只支持 http 地址，不要用 https")
    host = parsed.hostname
    port = parsed.port or 80
    if not host:
        raise RuntimeError("地址里没有主机名: %s" % page_url)
    client_id = random_id()
    sock = socket.create_connection((host, port), timeout=8)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    path = "/socket?clientId=%s" % client_id
    request = (
        "GET %s HTTP/1.1\r\n"
        "Host: %s:%d\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ) % (path, host, port, key)
    sock.sendall(request.encode("ascii"))
    header = b""
    while b"\r\n\r\n" not in header:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("连上手机后没有收到回应")
        header += chunk
        if len(header) > 65536:
            raise ConnectionError("手机返回的内容异常")
    head, leftover = header.split(b"\r\n\r\n", 1)
    status = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
    if "101" not in status:
        raise ConnectionError("手机没有接受连接: %s" % status)
    sock = SocketBuffer(sock, leftover)

    body = {"type": "CONNECT"}
    if pin:
        body["data"] = {"pin": pin}
    ws_send(sock, json.dumps(body))
    stream_address = None
    for _ in range(8):
        message = json.loads(ws_recv(sock))
        kind = message.get("type")
        if kind == "STREAM_ADDRESS":
            stream_address = message.get("data", {}).get("streamAddress")
            break
        if kind == "UNAUTHORIZED":
            raise PermissionError("手机拒绝了连接。如果设了密码，请加上 --pin")
    sock.close()
    if not stream_address:
        raise ConnectionError("没有拿到画面地址")
    if not stream_address.startswith("http"):
        slash = "" if stream_address.startswith("/") else "/"
        stream_address = "http://%s:%d%s%s" % (host, port, slash, stream_address)
    stream = urlparse(stream_address)
    query = stream.query
    if "clientId=" not in query:
        joiner = "&" if query else ""
        query = query + joiner + "clientId=" + client_id
    path = stream.path or "/"
    if query:
        path = path + "?" + query
    conn = http.client.HTTPConnection(stream.hostname, stream.port or port, timeout=15)
    conn.request("GET", path)
    response = conn.getresponse()
    if response.status != 200:
        conn.close()
        raise ConnectionError("画面请求失败: HTTP %s" % response.status)
    return conn, response


def jpeg_frames(response):
    buf = b""
    while True:
        chunk = response.read(16384)
        if not chunk:
            break
        buf += chunk
        while True:
            start = buf.find(b"\xff\xd8")
            if start < 0:
                buf = buf[-4:]
                break
            end = buf.find(b"\xff\xd9", start + 2)
            if end < 0:
                buf = buf[start:]
                break
            yield buf[start:end + 2]
            buf = buf[end + 2:]


def decode_jpeg(payload):
    data = np.frombuffer(payload, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    return img


def frame_digest(img):
    small = cv2.resize(img, (48, 96), interpolation=cv2.INTER_AREA)
    return hashlib.md5(small.tobytes()).hexdigest()


def fit_to_screen(img):
    if sys.platform == "win32":
        screen_h = ctypes_screen_height()
    else:
        screen_h = 900
    limit = int(screen_h * 0.86)
    h, w = img.shape[:2]
    if h <= limit:
        return img
    scale = limit / float(h)
    return cv2.resize(img, (max(1, int(w * scale)), limit), interpolation=cv2.INTER_AREA)


def ctypes_screen_height():
    import ctypes
    return int(ctypes.windll.user32.GetSystemMetrics(1))


def paint_status(img, text):
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    base = Image.fromarray(rgb).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = load_font(max(22, img.shape[1] // 28))
    bbox = draw.textbbox((0, 0), text, font=font)
    pad = 16
    draw.rectangle((0, 0, base.size[0], bbox[3] - bbox[1] + pad * 2), fill=(20, 24, 28, 210))
    draw.text((pad, pad), text, font=font, fill=(255, 255, 255, 255))
    out = Image.alpha_composite(base, overlay).convert("RGB")
    return cv2.cvtColor(np.array(out), cv2.COLOR_RGB2BGR)


class SharedState(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.frame = None
        self.digest = None
        self.status = "正在连接手机…"
        self.solutions = None
        self.board = None
        self.n = None
        self.marked = False
        self.labels = None
        self.note = ""
        self.adopt_solutions = None
        self.alive = True
        self.force_rerecognize = False
        self.play_running = False
        self.play_stop = threading.Event()

    def set_frame(self, frame, digest):
        with self.lock:
            self.frame = frame
            self.digest = digest

    def get_frame_snapshot(self):
        with self.lock:
            if self.frame is None:
                return None, None
            return self.frame.copy(), self.digest

    def set_status(self, text):
        with self.lock:
            self.status = text

    def set_result(self, solutions, board, n, marked, status):
        with self.lock:
            self.solutions = solutions
            self.board = board
            self.n = n
            self.marked = marked
            self.status = status

    def clear_result(self, status):
        with self.lock:
            self.solutions = None
            self.board = None
            self.n = None
            self.marked = False
            self.labels = None
            self.note = ""
            self.adopt_solutions = None
            self.status = status

    def set_labels(self, labels):
        with self.lock:
            self.labels = None if labels is None else np.array(labels, copy=True)

    def set_note(self, text):
        with self.lock:
            self.note = text or ""
            if self.solutions is not None and self.n is not None:
                title = _title(self.n, self.solutions, self.marked)
                self.status = ("%s｜%s" % (title, self.note)) if self.note else title

    def get_note(self):
        with self.lock:
            return self.note

    def publish_solutions(self, solutions, board, n, marked, labels):
        """自动点击重解后，立刻换上新方案，并让识别线程跟着改。"""
        with self.lock:
            self.solutions = list(solutions)
            self.board = board
            self.n = n
            self.marked = marked
            self.labels = None if labels is None else np.array(labels, copy=True)
            self.adopt_solutions = list(solutions)
            title = _title(n, self.solutions, marked)
            self.status = ("%s｜%s" % (title, self.note)) if self.note else title

    def take_adopt_solution(self):
        with self.lock:
            solutions = self.adopt_solutions
            self.adopt_solutions = None
            return solutions

    def has_solution(self):
        with self.lock:
            return self.solutions is not None and self.board is not None

    def play_is_running(self):
        with self.lock:
            return self.play_running

    def begin_play(self):
        with self.lock:
            if self.play_running:
                return False
            self.play_running = True
            self.play_stop.clear()
            return True

    def end_play(self):
        with self.lock:
            self.play_running = False

    def request_play_stop(self):
        self.play_stop.set()

    def play_should_stop(self):
        return self.play_stop.is_set() or not self.running()

    def get_play_pack(self):
        with self.lock:
            if self.frame is None or self.solutions is None or self.board is None or self.labels is None:
                return None
            return {
                "frame": self.frame.copy(),
                "board": self.board,
                "labels": self.labels.copy(),
                "n": self.n,
                "marked": self.marked,
                "solutions": list(self.solutions),
            }

    def request_rerecognize(self):
        """界面按钮：立刻清掉叠加，并让识别线程强制重跑。"""
        self.play_stop.set()
        with self.lock:
            self.force_rerecognize = True
            self.solutions = None
            self.board = None
            self.n = None
            self.marked = False
            self.labels = None
            self.note = ""
            self.adopt_solutions = None
            self.status = "手动重新识别…"

    def consume_rerecognize(self):
        with self.lock:
            if not self.force_rerecognize:
                return False
            self.force_rerecognize = False
            return True

    def rerecognize_pending(self):
        with self.lock:
            return self.force_rerecognize

    def snapshot_for_display(self):
        with self.lock:
            frame = None if self.frame is None else self.frame.copy()
            return {
                "frame": frame,
                "status": self.status,
                "solutions": None if self.solutions is None else list(self.solutions),
                "board": self.board,
                "n": self.n,
                "marked": self.marked,
            }

    def stop(self):
        with self.lock:
            self.alive = False

    def running(self):
        with self.lock:
            return self.alive


class HintPane(object):
    """旁边的提示图。滚轮缩放，按住左键拖动。"""

    def __init__(self, parent):
        self.frame = tk.Frame(parent, bg="#14181c")
        tools = tk.Frame(self.frame, bg="#1e2428")
        tools.pack(fill=tk.X, side=tk.TOP)
        for text, command in (
            ("放大", self.zoom_in),
            ("缩小", self.zoom_out),
            ("适应窗口", self.fit),
        ):
            tk.Button(
                tools,
                text=text,
                command=command,
                font=("Microsoft YaHei UI", 10),
                bg="#2c3338",
                fg="white",
                activebackground="#3d454c",
                activeforeground="white",
                relief=tk.FLAT,
                padx=10,
                pady=4,
                cursor="hand2",
            ).pack(side=tk.LEFT, padx=4, pady=4)
        self.prev_btn = tk.Button(
            tools,
            text="上一张",
            command=self.prev_page,
            font=("Microsoft YaHei UI", 10),
            bg="#2c3338",
            fg="white",
            activebackground="#3d454c",
            activeforeground="white",
            relief=tk.FLAT,
            padx=10,
            pady=4,
            cursor="hand2",
            state=tk.DISABLED,
        )
        self.prev_btn.pack(side=tk.LEFT, padx=(12, 4), pady=4)
        self.page_label = tk.Label(
            tools,
            text="",
            bg="#1e2428",
            fg="white",
            font=("Microsoft YaHei UI", 10),
        )
        self.page_label.pack(side=tk.LEFT, padx=4)
        self.next_btn = tk.Button(
            tools,
            text="下一张",
            command=self.next_page,
            font=("Microsoft YaHei UI", 10),
            bg="#2c3338",
            fg="white",
            activebackground="#3d454c",
            activeforeground="white",
            relief=tk.FLAT,
            padx=10,
            pady=4,
            cursor="hand2",
            state=tk.DISABLED,
        )
        self.next_btn.pack(side=tk.LEFT, padx=4, pady=4)
        tk.Label(
            tools,
            text="滚轮缩放，按住拖动",
            bg="#1e2428",
            fg="#c8c8c8",
            font=("Microsoft YaHei UI", 10),
        ).pack(side=tk.LEFT, padx=8)

        self.canvas = tk.Canvas(self.frame, bg="#14181c", highlightthickness=0, width=720, height=640)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.pages = []
        self.index = 0
        self.pil = None
        self.photo = None
        self.scale = 1.0
        self.off_x = 0.0
        self.off_y = 0.0
        self._need_fit = False
        self._drag = None
        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", lambda _event: self._end_drag())
        self.canvas.configure(cursor="hand2")

    def set_pages(self, pages):
        self.pages = list(pages)
        self.index = 0
        self._update_nav()
        if self.pages:
            self._show_index(0)

    def prev_page(self):
        if self.index <= 0:
            return
        self._show_index(self.index - 1)

    def next_page(self):
        if self.index + 1 >= len(self.pages):
            return
        self._show_index(self.index + 1)

    def _show_index(self, index):
        self.index = index
        self._update_nav()
        self._set_pil(self.pages[index])

    def _update_nav(self):
        count = len(self.pages)
        if count == 0:
            self.page_label.configure(text="")
            self.prev_btn.configure(state=tk.DISABLED)
            self.next_btn.configure(state=tk.DISABLED)
            return
        self.page_label.configure(text="%d / %d" % (self.index + 1, count))
        self.prev_btn.configure(state=tk.NORMAL if self.index > 0 else tk.DISABLED)
        self.next_btn.configure(state=tk.NORMAL if self.index + 1 < count else tk.DISABLED)

    def _set_pil(self, pil_image):
        self.pil = pil_image
        self._need_fit = True
        self._drag = None
        if self.canvas.winfo_width() > 20 and self.canvas.winfo_height() > 20:
            self.fit()
        else:
            self.canvas.after(30, self._fit_when_ready)

    def show_message(self, text):
        self.pages = []
        self.index = 0
        self._update_nav()
        self.pil = None
        self.photo = None
        self.canvas.delete("hint")
        self.canvas.delete("msg")
        self.canvas.create_text(
            24,
            24,
            text=text,
            anchor=tk.NW,
            fill="white",
            font=("Microsoft YaHei UI", 14),
            width=640,
            tags="msg",
        )

    def _fit_when_ready(self):
        if self.pil is None or not self._need_fit:
            return
        if self.canvas.winfo_width() <= 20:
            self.canvas.after(30, self._fit_when_ready)
            return
        self.fit()

    def fit(self):
        if self.pil is None:
            return
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        self.scale = min(cw / float(self.pil.width), ch / float(self.pil.height))
        self.off_x = (cw - self.pil.width * self.scale) / 2.0
        self.off_y = (ch - self.pil.height * self.scale) / 2.0
        self._need_fit = False
        self._redraw()

    def zoom_in(self):
        self._zoom_at(1.2, self.canvas.winfo_width() / 2.0, self.canvas.winfo_height() / 2.0)

    def zoom_out(self):
        self._zoom_at(1.0 / 1.2, self.canvas.winfo_width() / 2.0, self.canvas.winfo_height() / 2.0)

    def _on_configure(self, event):
        if self._need_fit and event.width > 20 and event.height > 20:
            self.fit()

    def _on_wheel(self, event):
        if self.pil is None or event.delta == 0:
            return
        factor = 1.12 if event.delta > 0 else (1.0 / 1.12)
        self._zoom_at(factor, event.x, event.y)

    def _zoom_at(self, factor, cx, cy):
        if self.pil is None:
            return
        new_scale = min(8.0, max(0.05, self.scale * factor))
        ix = (cx - self.off_x) / self.scale
        iy = (cy - self.off_y) / self.scale
        self.scale = new_scale
        self.off_x = cx - ix * self.scale
        self.off_y = cy - iy * self.scale
        self._need_fit = False
        self._redraw()

    def _on_press(self, event):
        self._drag = (event.x, event.y, self.off_x, self.off_y)
        self.canvas.configure(cursor="fleur")

    def _on_drag(self, event):
        if self._drag is None or self.pil is None:
            return
        x0, y0, ox, oy = self._drag
        self.off_x = ox + (event.x - x0)
        self.off_y = oy + (event.y - y0)
        self._redraw()

    def _end_drag(self):
        self._drag = None
        self.canvas.configure(cursor="hand2")

    def _redraw(self):
        if self.pil is None:
            return
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        x0 = max(0, int((-self.off_x) / self.scale))
        y0 = max(0, int((-self.off_y) / self.scale))
        x1 = min(self.pil.width, int((cw - self.off_x) / self.scale) + 1)
        y1 = min(self.pil.height, int((ch - self.off_y) / self.scale) + 1)
        if x1 <= x0 or y1 <= y0:
            self.canvas.delete("hint")
            return
        crop = self.pil.crop((x0, y0, x1, y1))
        dw = max(1, int(round((x1 - x0) * self.scale)))
        dh = max(1, int(round((y1 - y0) * self.scale)))
        shown = crop.resize((dw, dh), Image.BILINEAR)
        self.photo = ImageTk.PhotoImage(shown)
        px = self.off_x + x0 * self.scale
        py = self.off_y + y0 * self.scale
        self.canvas.delete("hint")
        self.canvas.delete("msg")
        self.canvas.create_image(px, py, anchor=tk.NW, image=self.photo, tags="hint")


def make_hint_pages(frame):
    found = hint_cats.recognize_board(frame)
    steps, _final, problem = hint_cats.deduce(found["labels"], found["marks"], found["names"])
    if not steps:
        if problem:
            raise RuntimeError("标记有矛盾：%s" % problem)
        raise RuntimeError("这一步没有能直接确定的新格子")
    return hint_cats.render_steps(found["img"], found["board"], steps)


class Viewer(object):
    def __init__(self, on_rerecognize=None, on_hint=None, on_play=None):
        self.root = tk.Tk()
        self.root.title(WINDOW)
        self.closed = False
        self.photo = None
        self.on_rerecognize = on_rerecognize
        self.on_hint = on_hint
        self.on_play = on_play
        self.hint_visible = False

        bar = tk.Frame(self.root, bg="#1e2428")
        bar.pack(fill=tk.X, side=tk.TOP)
        self.rerecognize_btn = tk.Button(
            bar,
            text="重新识别 (R)",
            command=self._click_rerecognize,
            font=("Microsoft YaHei UI", 11),
            bg="#3d8bfd",
            fg="white",
            activebackground="#2f6fd1",
            activeforeground="white",
            relief=tk.FLAT,
            padx=14,
            pady=6,
            cursor="hand2",
        )
        self.rerecognize_btn.pack(side=tk.LEFT, padx=8, pady=6)
        self.hint_btn = tk.Button(
            bar,
            text="出提示",
            command=self._click_hint,
            font=("Microsoft YaHei UI", 11),
            bg="#e23b45",
            fg="white",
            activebackground="#c42d36",
            activeforeground="white",
            relief=tk.FLAT,
            padx=14,
            pady=6,
            cursor="hand2",
        )
        self.hint_btn.pack(side=tk.LEFT, padx=4, pady=6)
        self.play_btn = tk.Button(
            bar,
            text="自动点击",
            command=self._click_play,
            font=("Microsoft YaHei UI", 11),
            bg="#1f8a4c",
            fg="white",
            activebackground="#18723e",
            activeforeground="white",
            relief=tk.FLAT,
            padx=14,
            pady=6,
            cursor="hand2",
        )
        if self.on_play is not None:
            self.play_btn.pack(side=tk.LEFT, padx=4, pady=6)

        self.body = tk.Frame(self.root, bg="black")
        self.body.pack(fill=tk.BOTH, expand=True)
        self.label = tk.Label(self.body, bg="black")
        self.label.pack(side=tk.LEFT)
        self.hint_pane = HintPane(self.body)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Escape>", lambda _event: self.close())
        self.root.bind("<q>", lambda _event: self.close())
        self.root.bind("<Q>", lambda _event: self.close())
        self.root.bind("<r>", lambda _event: self._click_rerecognize())
        self.root.bind("<R>", lambda _event: self._click_rerecognize())
        self.root.bind("<Left>", lambda _event: self.hint_pane.prev_page())
        self.root.bind("<Right>", lambda _event: self.hint_pane.next_page())

    def _click_rerecognize(self):
        if self.closed:
            return
        if self.on_rerecognize is not None:
            self.on_rerecognize()

    def _click_play(self):
        if self.closed or self.on_play is None:
            return
        self.on_play()

    def set_play_text(self, text):
        def apply():
            if self.closed:
                return
            try:
                self.play_btn.configure(text=text, state=tk.NORMAL)
            except tk.TclError:
                pass

        try:
            self.root.after(0, apply)
        except tk.TclError:
            pass

    def _click_hint(self):
        if self.closed or self.on_hint is None:
            return
        if self.hint_visible:
            self.hide_hint()
            return
        self.hint_btn.configure(state=tk.DISABLED, text="正在出提示…")
        self.on_hint()

    def hint_finished(self):
        if self.closed:
            return
        try:
            text = "关闭提示" if self.hint_visible else "出提示"
            self.hint_btn.configure(state=tk.NORMAL, text=text)
        except tk.TclError:
            pass

    def show_hint_pages(self, pages):
        if self.closed:
            return
        self._ensure_hint_column()
        self.hint_pane.set_pages(pages)

    def hide_hint(self):
        if self.closed or not self.hint_visible:
            return
        self.hint_pane.frame.pack_forget()
        self.hint_visible = False
        self.hint_pane.pages = []
        self.hint_pane.index = 0
        self.hint_pane._update_nav()
        geom = getattr(self, "_geom_before", None)
        if geom:
            self.root.geometry(geom)
        try:
            self.hint_btn.configure(state=tk.NORMAL, text="出提示")
        except tk.TclError:
            pass

    def show_hint_message(self, text):
        if self.closed:
            return
        self._ensure_hint_column()
        self.hint_pane.show_message(text)

    def _ensure_hint_column(self):
        if self.hint_visible:
            return
        self._geom_before = self.root.geometry()
        self.hint_pane.frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.hint_visible = True
        self.root.update_idletasks()
        cur_w = max(self.root.winfo_width(), 360)
        cur_h = max(self.root.winfo_height(), 640)
        screen_w = int(ctypes.windll.user32.GetSystemMetrics(0)) if sys.platform == "win32" else 1600
        target = min(screen_w - 16, cur_w + 780)
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        if x + target > screen_w:
            x = max(0, screen_w - target - 8)
        self.root.geometry("%dx%d+%d+%d" % (target, cur_h, x, y))

    def close(self):
        self.closed = True
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def show(self, img):
        if self.closed:
            return False
        view = fit_to_screen(img)
        rgb = cv2.cvtColor(view, cv2.COLOR_BGR2RGB)
        self.photo = ImageTk.PhotoImage(Image.fromarray(rgb))
        self.label.configure(image=self.photo)
        return not self.closed


def compose_view(snap):
    frame = snap["frame"]
    if frame is None:
        blank = np.full((640, 360, 3), 32, np.uint8)
        return paint_status(blank, snap["status"])
    solutions = snap["solutions"]
    board = snap["board"]
    if solutions and board is not None:
        try:
            shown = draw_solutions(frame, board, solutions)
            title = "%d×%d，%d 套方案" % (snap["n"], snap["n"], len(solutions))
            if snap["marked"]:
                title += "（局中）"
            return paint_status(shown, title)
        except Exception:
            pass
    return paint_status(frame, snap["status"])


def stream_loop(page_url, pin, state):
    while state.running():
        try:
            state.set_status("正在连接手机…")
            _conn, response = open_stream(page_url, pin)
            print("已连上画面")
            state.set_status("已连上，等待棋盘…")
            for payload in jpeg_frames(response):
                if not state.running():
                    return
                frame = decode_jpeg(payload)
                if frame is None:
                    continue
                state.set_frame(frame, frame_digest(frame))
        except PermissionError as exc:
            print(exc)
            state.set_status(str(exc))
            state.stop()
            return
        except Exception as exc:
            if not state.running():
                return
            print("连接中断: %s，正在重试" % exc)
            state.set_status("连接中断，正在重试")
            time.sleep(0.8)


def _title(n, solutions, marked):
    title = "%d×%d，%d 套方案" % (n, n, len(solutions))
    if marked:
        title += "（局中）"
    return title


def _clear_bag(bag):
    bag["solutions"] = None
    bag["board"] = None
    bag["samples"] = None
    bag["n"] = None
    bag["marked"] = False
    bag["fail_key"] = None
    bag["fail_until"] = 0.0
    bag["announced_n"] = None
    bag["multi_streak"] = 0
    bag["miss_streak"] = 0
    bag["size_mismatch_streak"] = 0
    bag["mismatch_n"] = None
    bag["saw_celebration"] = False
    bag["ignore_log_key"] = None


def _status_with_note(state, n, solutions, marked):
    title = _title(n, solutions, marked)
    note = state.get_note()
    if note:
        return "%s｜%s" % (title, note)
    return title


def _keep_locked(state, bag, marked=None):
    if marked is not None:
        bag["marked"] = marked or bag.get("marked")
    state.set_result(
        bag["solutions"],
        bag["board"],
        bag["n"],
        bag["marked"],
        _status_with_note(state, bag["n"], bag["solutions"], bag["marked"]),
    )


def recognize_loop(state):
    """漏斗：非棋盘→清叠加；同局已解→粘住；只有新盘/手动才重解密。"""
    bag = {
        "solutions": None,
        "board": None,
        "samples": None,
        "n": None,
        "marked": False,
        "fail_key": None,
        "fail_until": 0.0,
        "announced_n": None,
        "multi_streak": 0,
        "miss_streak": 0,
        "size_mismatch_streak": 0,
        "mismatch_n": None,
        "saw_celebration": False,
        "ignore_log_key": None,
    }
    last_msg = None
    while state.running():
        # 点了「重新识别」就少睡一会，少等半秒。
        time.sleep(0.05 if state.rerecognize_pending() else RECOGNIZE_INTERVAL)
        if not state.running():
            return
        force = state.consume_rerecognize()
        if force:
            print("手动触发重新识别")
            _clear_bag(bag)
            state.clear_result("手动重新识别…")
        adopted = state.take_adopt_solution()
        if adopted and not force and bag["solutions"] is not None:
            bag["solutions"] = adopted

        frame, _digest = state.get_frame_snapshot()
        if frame is None:
            continue

        # 过关动画：不当新局；记下「刚过关」，动画结束后空白新盘再放下旧解。
        if is_celebration_screen(frame):
            bag["miss_streak"] = 0
            if bag["solutions"] is not None:
                bag["saw_celebration"] = True
                state.set_status(
                    _title(bag["n"], bag["solutions"], bag.get("marked"))
                    + "（过关动画）"
                )
            else:
                state.clear_result("过关动画…")
            continue

        try:
            peek = peek_board(frame)
        except Exception as exc:
            msg = "peek_board: %s: %s" % (type(exc).__name__, exc)
            if msg != last_msg:
                print("识别探测失败: %s" % msg)
                last_msg = msg
            continue

        if peek is None:
            bag["miss_streak"] = int(bag.get("miss_streak") or 0) + 1
            if bag["solutions"] is not None and bag["miss_streak"] < LEAVE_CLEAR_TICKS:
                continue
            if bag["solutions"] is not None:
                print("离开棋盘，放下上次结果")
            _clear_bag(bag)
            state.clear_result("等待棋盘…")
            continue
        bag["miss_streak"] = 0

        if peek.get("target_n") and bag.get("announced_n") != peek["target_n"]:
            if bag["solutions"] is None:
                print("进度条读到棋盘 %d×%d" % (peek["target_n"], peek["target_n"]))
                bag["announced_n"] = peek["target_n"]

        samples = peek["samples"]
        n = peek["n"]
        marked = board_has_marks(frame, peek["board"])

        # 过关动画刚结束、又是空白盘 → 新的一局，放下旧解再识别。
        if (
            not force
            and bag["solutions"] is not None
            and bag.get("saw_celebration")
            and not marked
        ):
            print("过关后进入新盘，放下上次结果")
            _clear_bag(bag)
            state.clear_result("识别新的一局…")
            # bag 已空，下面按新局走

        # 本局已锁定解：局中粘住；空白新盘才换局。
        if not force and bag["solutions"] is not None:
            if n == bag["n"]:
                bag["size_mismatch_streak"] = 0
                bag["mismatch_n"] = None
                same = same_board_colors(samples, n, bag["samples"], bag["n"])
                if same or marked:
                    bag["board"] = peek["board"]
                    if same:
                        bag["samples"] = samples
                    _keep_locked(state, bag, marked=marked)
                    continue
                # 同尺寸、无标记、颜色明显变了 → 新开局
                print("同尺寸新开局，放下上次结果")
                _clear_bag(bag)
                state.clear_result("识别新的一局…")
            else:
                # 尺寸与锁定解不一致
                if marked:
                    # 局中乱读成 3/6/8/9：忽略，保持旧解
                    key = (bag["n"], n)
                    if bag.get("ignore_log_key") != key:
                        print(
                            "忽略尺寸抖动 %d→%d，保持已锁定的 %d×%d"
                            % (bag["n"], n, bag["n"], bag["n"])
                        )
                        bag["ignore_log_key"] = key
                    bag["size_mismatch_streak"] = 0
                    bag["mismatch_n"] = None
                    _keep_locked(state, bag, marked=True)
                    continue
                # 空白盘上的新尺寸：连续确认，避免闪一下误换局
                if bag.get("mismatch_n") != n:
                    bag["mismatch_n"] = n
                    bag["size_mismatch_streak"] = 1
                else:
                    bag["size_mismatch_streak"] = int(bag.get("size_mismatch_streak") or 0) + 1
                streak = bag["size_mismatch_streak"]
                if streak < SIZE_CHANGE_CONFIRM:
                    print(
                        "尺寸疑似 %d→%d，确认中 %d/%d"
                        % (bag["n"], n, streak, SIZE_CHANGE_CONFIRM)
                    )
                    _keep_locked(state, bag, marked=False)
                    continue
                print("确认新尺寸 %d×%d，放下上次结果" % (n, n))
                _clear_bag(bag)
                state.clear_result("识别新的一局…")

        # 局中画面（已有猫/叉）且本局还没锁定解：采样已被污染，再解只会刷无解日志。
        if not force and marked and bag["solutions"] is None:
            state.clear_result("局中画面未锁定解，请开局识别或按 R")
            continue

        # 无解冷却只按尺寸记，避免局中采样抖动换 key 刷屏。
        fail_key = n
        now = time.time()
        if (
            not force
            and bag.get("fail_key") == fail_key
            and now < bag.get("fail_until", 0)
        ):
            state.clear_result("看到棋盘，但规则下无解")
            continue

        try:
            found = finish_recognize(peek)
            solutions, truncated = solve_all(found["labels"])
        except Exception as exc:
            msg = "%s: %s" % (type(exc).__name__, exc)
            if msg != last_msg:
                print("识别失败: %s" % msg)
                last_msg = msg
            _clear_bag(bag)
            state.clear_result("看到棋盘，识别失败")
            continue

        stage = "局中" if marked else "新的一局"
        if not solutions:
            bag["fail_key"] = fail_key
            bag["fail_until"] = now + FAIL_COOLDOWN
            bag["multi_streak"] = 0
            print("%s %d×%d，没有满足规则的摆法" % (stage, n, n))
            dump = os.path.join(os.path.dirname(__file__) or ".", "_debug_unsolvable.jpg")
            try:
                save_bgr(
                    dump,
                    draw_recognition(frame, found["board"], found["names"], found["labels"]),
                )
                print("已保存无解识别图: %s" % dump)
                for r in range(n):
                    print(
                        "  "
                        + " ".join(
                            found["names"][int(found["labels"][r, c])] for c in range(n)
                        )
                    )
            except Exception as dump_exc:
                print("保存无解识别图时出错: %s" % dump_exc)
            state.clear_result("看到棋盘，但规则下无解")
            continue

        # 多套解法多半是色区识错；连续 MULTI_CONFIRM 次才当真。
        if len(solutions) > 1:
            bag["multi_streak"] = int(bag.get("multi_streak") or 0) + 1
            streak = bag["multi_streak"]
            if streak < MULTI_CONFIRM:
                print(
                    "%s %d×%d，%d 套方案，疑似识别错，重试 %d/%d"
                    % (stage, n, n, len(solutions), streak, MULTI_CONFIRM)
                )
                state.clear_result(
                    "多解疑似识别错，重试 %d/%d…" % (streak, MULTI_CONFIRM)
                )
                continue
            print(
                "%s %d×%d，连续 %d 次均为 %d 套，按多解处理"
                % (stage, n, n, streak, len(solutions))
            )
        else:
            bag["multi_streak"] = 0

        bag["solutions"] = solutions
        bag["board"] = found["board"]
        bag["samples"] = found["samples"]
        bag["n"] = n
        bag["marked"] = marked
        bag["fail_key"] = None
        bag["fail_until"] = 0.0
        last_msg = None
        extra = "，还有更多套" if truncated else ""
        print("%s %d×%d，%d 套方案%s" % (stage, n, n, len(solutions), extra))
        state.set_labels(found["labels"])
        state.set_result(
            solutions,
            found["board"],
            n,
            marked,
            _status_with_note(state, n, solutions, marked),
        )


def _adb_run(adb, args, timeout):
    command = [adb] + list(args)
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": timeout,
        "check": False,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000
    try:
        result = subprocess.run(command, **kwargs)
    except subprocess.TimeoutExpired:
        raise RuntimeError("adb 超时")
    except OSError as exc:
        raise RuntimeError("adb 启动失败：%s" % exc)
    out = (result.stdout or b"").decode("utf-8", "replace").strip()
    err = (result.stderr or b"").decode("utf-8", "replace").strip()
    return result.returncode, out, err


def find_adb():
    bundled = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "platform-tools", "adb.exe")
    if os.path.isfile(bundled):
        return bundled
    found = shutil.which("adb")
    if found:
        return found
    roots = [
        os.environ.get("ANDROID_HOME", ""),
        os.environ.get("ANDROID_SDK_ROOT", ""),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk"),
    ]
    for root in roots:
        if not root:
            continue
        path = os.path.join(root, "platform-tools", "adb.exe")
        if os.path.isfile(path):
            return path
    return None


def _adb_devices(adb):
    code, out, err = _adb_run(adb, ["devices"], 8)
    if code != 0:
        raise RuntimeError(err or out or "adb devices 失败")
    online = []
    unauthorized = False
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        if parts[1] == "device":
            online.append(parts[0])
        elif parts[1] == "unauthorized":
            unauthorized = True
    return online, unauthorized


def _read_screen_size(adb, serial):
    code, out, err = _adb_run(adb, ["-s", serial, "shell", "wm", "size"], 8)
    if code != 0:
        raise RuntimeError(err or out or "读不到手机分辨率")
    sizes = []
    for line in out.splitlines():
        if ":" not in line or "x" not in line.lower():
            continue
        tail = line.split(":", 1)[1]
        chunks = tail.replace("X", "x").split("x")
        if len(chunks) != 2:
            continue
        try:
            sizes.append((int(chunks[0].strip()), int(chunks[1].strip())))
        except ValueError:
            continue
    if not sizes:
        raise RuntimeError("读不到手机分辨率：%s" % out)
    return sizes[-1]


def phone_already_paired():
    """adb 设备列表里已经有一部在线手机，就不用再配对。"""
    adb = find_adb()
    if not adb:
        return False
    try:
        _adb_run(adb, ["start-server"], 20)
        online, _unauthorized = _adb_devices(adb)
    except Exception as exc:
        print("检查 adb 失败: %s" % exc)
        return False
    if online:
        print("已配对: %s" % ", ".join(online))
    return bool(online)


def run_pair(ip, port, code):
    """执行 adb pair。成功并且手机出现在设备列表里才返回 True。"""
    adb = find_adb()
    if not adb:
        return False, "找不到 adb"
    try:
        _adb_run(adb, ["start-server"], 20)
        code_rc, out, err = _adb_run(adb, ["pair", "%s:%s" % (ip, port), code], 30)
    except Exception as exc:
        return False, str(exc)
    text = "\n".join(part for part in (out, err) if part).strip()
    lower = text.lower()
    print(text or "adb pair 没有输出")
    if code_rc != 0 or "successfully paired" not in lower:
        if "protocol fault" in lower:
            return False, "配对没完成。请重新打开手机上的配对窗口，换新的端口和配对码。"
        if "wrong password" in lower or "connection was dropped" in lower:
            return False, "配对码不对，或配对窗口已经关掉。请重新打开后再试。"
        return False, (text or "配对失败")[:80]
    for _ in range(8):
        try:
            online, _unauthorized = _adb_devices(adb)
        except Exception:
            online = []
        if online:
            print("配对完成: %s" % ", ".join(online))
            return True, "已配对"
        time.sleep(0.4)
    return False, "配对命令成功，但手机还没连上。请看手机是否要点「允许」。"


class PairDialog(object):
    """进游戏画面之前的配对。关掉窗口表示不启动。"""

    def __init__(self, default_ip):
        self.result = None
        self.root = tk.Tk()
        self.root.title("无线调试配对")
        self.root.resizable(False, False)
        self.root.configure(bg="#1e2428")
        self.root.protocol("WM_DELETE_WINDOW", self._quit)

        wrap = tk.Frame(self.root, bg="#1e2428", padx=18, pady=16)
        wrap.pack()
        tk.Label(
            wrap,
            text="还没有配对。请在手机上打开「无线调试 → 使用配对码配对设备」，\n把窗口里的 IP、端口和配对码填在这里。配对窗口不要关。",
            bg="#1e2428",
            fg="white",
            font=("Microsoft YaHei UI", 10),
            justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 12))

        self.ip = tk.StringVar(value=default_ip or "")
        self.port = tk.StringVar()
        self.code = tk.StringVar()
        for row, label, variable in (
            (1, "IP", self.ip),
            (2, "端口", self.port),
            (3, "配对码", self.code),
        ):
            tk.Label(
                wrap,
                text=label,
                bg="#1e2428",
                fg="white",
                font=("Microsoft YaHei UI", 11),
            ).grid(row=row, column=0, sticky=tk.W, pady=4, padx=(0, 8))
            tk.Entry(
                wrap,
                textvariable=variable,
                font=("Microsoft YaHei UI", 11),
                width=28,
            ).grid(row=row, column=1, sticky=tk.W, pady=4)

        self.status = tk.Label(
            wrap,
            text="",
            bg="#1e2428",
            fg="#ffb4b4",
            font=("Microsoft YaHei UI", 10),
            wraplength=360,
            justify=tk.LEFT,
        )
        self.status.grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(10, 8))

        buttons = tk.Frame(wrap, bg="#1e2428")
        buttons.grid(row=5, column=0, columnspan=2, sticky=tk.E)
        self.pair_btn = tk.Button(
            buttons,
            text="配对",
            command=self._pair,
            font=("Microsoft YaHei UI", 11),
            bg="#1f8a4c",
            fg="white",
            activebackground="#18723e",
            activeforeground="white",
            relief=tk.FLAT,
            padx=14,
            pady=6,
            cursor="hand2",
        )
        self.pair_btn.pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(
            buttons,
            text="只看，不点击",
            command=self._skip,
            font=("Microsoft YaHei UI", 11),
            bg="#3d8bfd",
            fg="white",
            activebackground="#2f6fd1",
            activeforeground="white",
            relief=tk.FLAT,
            padx=14,
            pady=6,
            cursor="hand2",
        ).pack(side=tk.LEFT)
        if not find_adb():
            self.pair_btn.configure(state=tk.DISABLED)
            self.status.configure(text="找不到 adb，这次只能只看画面。")
        self.root.bind("<Return>", lambda _event: self._pair())
        self.root.update_idletasks()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        self.root.geometry("+%d+%d" % ((screen_w - width) // 2, (screen_h - height) // 3))

    def run(self):
        self.root.mainloop()
        return self.result

    def _quit(self):
        self.result = None
        self.root.destroy()

    def _skip(self):
        self.result = False
        self.root.destroy()

    def _pair(self):
        if str(self.pair_btn.cget("state")) == tk.DISABLED:
            return
        ip = self.ip.get().strip()
        port = self.port.get().strip()
        code = self.code.get().strip()
        if not ip or not port.isdigit() or not code.isdigit():
            self.status.configure(text="请填写 IP、端口和配对码，端口和配对码都是数字。")
            return
        self.pair_btn.configure(state=tk.DISABLED, text="正在配对…")
        self.status.configure(text="正在配对，请不要关掉手机上的配对窗口。", fg="#c8c8c8")

        def work():
            ok, message = run_pair(ip, port, code)

            def apply():
                try:
                    if not self.root.winfo_exists():
                        return
                except tk.TclError:
                    return
                if ok:
                    self.result = True
                    self.root.destroy()
                    return
                self.status.configure(text=message, fg="#ffb4b4")
                self.pair_btn.configure(state=tk.NORMAL, text="配对")

            try:
                self.root.after(0, apply)
            except tk.TclError:
                pass

        threading.Thread(target=work, name="pair", daemon=True).start()


def prepare_click(default_ip):
    """进游戏前配对。返回 True 允许自动点击，False 只看，None 表示用户关掉了窗口。"""
    if phone_already_paired():
        return True
    print("还没有 adb 配对，先弹出配对窗口")
    return PairDialog(default_ip).run()


def connect_adb(preferred, phone_host):
    """连上已经配对好的 adb。ScreenStream 本身不能把点击送回手机。"""
    adb = find_adb()
    if not adb:
        raise RuntimeError("找不到 adb，请安装 platform-tools")
    serial = (preferred or "").strip()
    if serial:
        host, _sep, port = serial.rpartition(":")
        if host and "." in host and port.isdigit():
            code, out, err = _adb_run(adb, ["connect", serial], 12)
            text = "%s %s" % (out, err)
            lower = text.lower()
            failed = "fail" in lower or "cannot" in lower or "unable" in lower or "refused" in lower
            if code != 0 or failed or "connected" not in lower:
                raise RuntimeError("adb 连不上 %s" % serial)
        return adb, serial, _read_screen_size(adb, serial)
    online, unauthorized = _adb_devices(adb)
    if not online and phone_host:
        _adb_run(adb, ["connect", "%s:5555" % phone_host], 12)
        online, unauthorized = _adb_devices(adb)
    if not online:
        if unauthorized:
            raise RuntimeError("请在手机上允许 USB 调试")
        raise RuntimeError("没有 adb 设备，请先 adb connect")
    if phone_host:
        for item in online:
            if item.startswith(phone_host):
                serial = item
                break
    if not serial:
        serial = online[0]
    return adb, serial, _read_screen_size(adb, serial)


def orient_screen(frame_w, frame_h, device_w, device_h):
    """画面和 wm size 的方向不一致时，对调宽高，让点击轴和画面对齐。"""
    if frame_w <= 0 or frame_h <= 0 or device_w <= 0 or device_h <= 0:
        return device_w, device_h
    direct = abs((frame_w / float(frame_h)) - (device_w / float(device_h)))
    swapped = abs((frame_w / float(frame_h)) - (device_h / float(device_w)))
    if swapped + 0.05 < direct:
        return device_h, device_w
    return device_w, device_h


def cell_center_tap(frame_shape, box, screen_wh):
    frame_h, frame_w = frame_shape[:2]
    device_w, device_h = orient_screen(frame_w, frame_h, screen_wh[0], screen_wh[1])
    x, y, w, h = box
    tx = int(round((x + w / 2.0) * device_w / float(frame_w)))
    ty = int(round((y + h / 2.0) * device_h / float(frame_h)))
    tx = max(0, min(device_w - 1, tx))
    ty = max(0, min(device_h - 1, ty))
    return tx, ty, device_w, device_h


def _cell_kind(frame, board, row, col):
    if frame is None or board is None:
        return None
    if row >= len(board) or col >= len(board[row]):
        return None
    kind, _color = hint_cats.classify_cell(frame, board[row][col])
    return kind


def _marks_on_board(frame, board):
    confirmed = set()
    banned = set()
    for row, line in enumerate(board):
        for col in range(len(line)):
            kind = _cell_kind(frame, board, row, col)
            if kind == hint_cats.CAT:
                confirmed.add((row, col))
            elif kind == hint_cats.EX:
                banned.add((row, col))
    return confirmed, banned


def _solution_conflicts(solution, confirmed, banned):
    cells = set(tuple(cell) for cell in solution)
    if confirmed - cells:
        return True
    if cells & banned:
        return True
    return False


def _reread_labels(frame):
    peek = peek_board(frame)
    if peek is None:
        return None
    return finish_recognize(peek)


def _resolve_after_miss(state, frame, labels, board, n, marked, confirmed, banned):
    solutions, _truncated = solve_all(labels, forced=confirmed, banned=banned)
    if not solutions:
        try:
            found = _reread_labels(frame)
        except Exception as exc:
            print("重新识别失败: %s" % exc)
            found = None
        if found is not None and found["n"] == n:
            labels = found["labels"]
            board = found["board"]
            solutions, _truncated = solve_all(labels, forced=confirmed, banned=banned)
        elif found is not None and found["n"] != n:
            state.set_note("棋盘变了，已停止")
            return None
    if not solutions:
        state.set_note("重新解密后无解，已停止")
        return None
    state.set_note("已重新解密")
    state.publish_solutions(solutions, board, n, marked, labels)
    print("重新解密：%d 套方案" % len(solutions))
    return list(solutions[0]), labels, board


def _wait_cell(state, row, col, timeout):
    """等这一格变成猫或叉。白闪一下再变成猫，仍算点对。"""
    deadline = time.time() + timeout
    time.sleep(0.35)
    saw_cat = 0
    saw_ex = 0
    while time.time() < deadline and not state.play_should_stop():
        pack = state.get_play_pack()
        frame = None if pack is None else pack["frame"]
        board = None if pack is None else pack["board"]
        kind = _cell_kind(frame, board, row, col)
        if kind == hint_cats.CAT:
            saw_cat += 1
            if saw_cat >= 2:
                return hint_cats.CAT
        elif kind == hint_cats.EX:
            saw_ex += 1
            saw_cat = 0
        else:
            saw_cat = 0
        time.sleep(0.12)
    if saw_cat >= 1:
        return hint_cats.CAT
    if saw_ex >= 2:
        return hint_cats.EX
    return hint_cats.EMPTY


def play_loop(state, viewer, page_url, adb_serial):
    try:
        parsed = urlparse(page_url)
        state.set_note("正在连接点击…")
        adb, serial, screen = connect_adb(adb_serial, parsed.hostname)
        pack = state.get_play_pack()
        if pack is None or not pack["solutions"]:
            state.set_note("请先重新识别")
            return
        solution = [tuple(cell) for cell in pack["solutions"][0]]
        labels = pack["labels"]
        print(
            "自动点击：%s，屏幕 %d×%d，先点第 1 套，共 %d 套"
            % (serial, screen[0], screen[1], len(pack["solutions"]))
        )
        if len(pack["solutions"]) > 1:
            state.set_note("有多套，先点第1套")
        must_resolve = False
        empty_tries = 0
        last_cell = None
        wrong_rounds = 0
        extra_banned = set()
        accepted = set()
        while not state.play_should_stop():
            pack = state.get_play_pack()
            if pack is None:
                state.set_note("没有解密结果，已停止")
                return
            frame = pack["frame"]
            board = pack["board"]
            n = pack["n"]
            marked = pack["marked"]
            confirmed, banned = _marks_on_board(frame, board)
            confirmed = confirmed | accepted
            banned = banned | extra_banned
            if must_resolve or _solution_conflicts(solution, confirmed, banned):
                resolved = _resolve_after_miss(
                    state, frame, labels, board, n, marked, confirmed, banned
                )
                if resolved is None:
                    return
                solution, labels, board = resolved
                must_resolve = False
                continue
            pending = [cell for cell in solution if cell not in confirmed]
            if not pending:
                state.set_note("已点完")
                print("自动点击：已点完")
                return
            row, col = pending[0]
            if (row, col) in banned:
                must_resolve = True
                continue
            if (row, col) != last_cell:
                empty_tries = 0
                last_cell = (row, col)
            box = board[row][col]
            tx, ty, device_w, device_h = cell_center_tap(frame.shape, box, screen)
            state.set_note("正在点 %d/%d" % (len(confirmed) + 1, n))
            print(
                "双击第 %d 行第 %d 列，画面 %dx%d → 手机 (%d,%d)，屏幕 %dx%d"
                % (row + 1, col + 1, frame.shape[1], frame.shape[0], tx, ty, device_w, device_h)
            )
            # 单击会被游戏标成「不是猫」。两次点击放在同一次 shell 里，间隔才算双击。
            tap = "input tap %d %d; sleep 0.12; input tap %d %d" % (tx, ty, tx, ty)
            code, out, err = _adb_run(adb, ["-s", serial, "shell", tap], 8)
            if code != 0:
                detail = (err or out or "点击失败")[:40]
                print("adb tap 失败: %s" % detail)
                state.set_note("点击失败，请允许模拟点击")
                return
            kind = _wait_cell(state, row, col, 2.6)
            if state.play_should_stop():
                break
            if kind == hint_cats.CAT:
                empty_tries = 0
                accepted.add((row, col))
                state.set_note("这一格正确")
                print("第 %d 行第 %d 列是猫" % (row + 1, col + 1))
                time.sleep(0.25)
                continue
            if kind == hint_cats.EX:
                empty_tries = 0
                wrong_rounds += 1
                extra_banned.add((row, col))
                print("第 %d 行第 %d 列是叉，准备重新解密" % (row + 1, col + 1))
                if wrong_rounds > n + 2:
                    state.set_note("多次点错，已停止")
                    return
                must_resolve = True
                state.set_note("这一格不是猫，重新解密")
                continue
            empty_tries += 1
            print("第 %d 行第 %d 列没有变化，第 %d 次" % (row + 1, col + 1, empty_tries))
            if empty_tries > 2:
                state.set_note("没点到格子，已停止")
                print("连续没点中。请确认无线调试，以及「USB调试（安全设置）」已打开。")
                return
            state.set_note("没点中，再试一次")
        if state.has_solution() and state.running():
            state.set_note("已停止")
    except Exception as exc:
        print("自动点击失败: %s" % exc)
        state.set_note(str(exc)[:36])
    finally:
        state.end_play()
        viewer.set_play_text("自动点击")


def watch(page_url, pin, adb_serial="", allow_click=True):
    prevent_display_sleep()
    state = SharedState()
    stream_thread = threading.Thread(
        target=stream_loop, args=(page_url, pin, state), name="stream", daemon=True
    )
    recog_thread = threading.Thread(
        target=recognize_loop, args=(state,), name="recognize", daemon=True
    )
    stream_thread.start()
    recog_thread.start()

    def request_hint():
        frame, _digest = state.get_frame_snapshot()
        if frame is None:
            viewer.hint_finished()
            viewer.show_hint_message("还没有画面，等连上手机后再试。")
            return

        def work():
            error = None
            pages = None
            try:
                pages = make_hint_pages(frame)
            except Exception as exc:
                error = str(exc)

            def apply():
                if viewer.closed:
                    return
                if pages:
                    viewer.show_hint_pages(pages)
                else:
                    viewer.show_hint_message(error or "出提示失败")
                viewer.hint_finished()

            try:
                viewer.root.after(0, apply)
            except tk.TclError:
                pass

        threading.Thread(target=work, name="hint", daemon=True).start()

    def toggle_play():
        if state.play_is_running():
            state.request_play_stop()
            viewer.set_play_text("正在停止…")
            return
        if not state.has_solution():
            print("自动点击：还没有解密结果")
            state.set_status("还没有解密结果")
            viewer.set_play_text("还没解密")

            def restore_play_text():
                if not state.play_is_running():
                    viewer.set_play_text("自动点击")

            try:
                viewer.root.after(1500, restore_play_text)
            except tk.TclError:
                pass
            return
        if not state.begin_play():
            return
        viewer.set_play_text("停止点击")
        threading.Thread(
            target=play_loop,
            args=(state, viewer, page_url, adb_serial),
            name="play",
            daemon=True,
        ).start()

    viewer = Viewer(
        on_rerecognize=state.request_rerecognize,
        on_hint=request_hint,
        on_play=toggle_play if allow_click else None,
    )
    blank = paint_status(np.full((640, 360, 3), 32, np.uint8), "正在连接手机…")
    viewer.show(blank)

    def tick():
        if viewer.closed or not state.running():
            state.stop()
            try:
                viewer.root.quit()
            except tk.TclError:
                pass
            return
        try:
            shown = compose_view(state.snapshot_for_display())
            if not viewer.show(shown):
                state.stop()
                viewer.root.quit()
                return
            viewer.root.after(DISPLAY_MS, tick)
        except tk.TclError:
            state.stop()

    viewer.root.after(DISPLAY_MS, tick)
    try:
        viewer.root.mainloop()
    finally:
        state.stop()
        allow_display_sleep()


def main(argv):
    setup_console()
    parser = argparse.ArgumentParser(description="从手机画面自动解密猫猫方块")
    parser.add_argument("url", help="ScreenStream 显示的地址，例如 http://192.168.1.8:8080")
    parser.add_argument("--pin", default="", help="ScreenStream 里设置的数字密码")
    parser.add_argument("--adb", default="", help="adb 序列号或 IP:端口，例如 192.168.1.8:5555")
    args = parser.parse_args(argv[1:])
    host = urlparse(args.url).hostname or ""
    allow_click = prepare_click(host)
    if allow_click is None:
        print("未配对，已退出")
        return 0
    if not allow_click:
        print("未配对，只看画面，不自动点击")
    watch(args.url, args.pin.strip(), args.adb.strip(), allow_click)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
