# -*- coding: utf-8 -*-
"""从手机 ScreenStream 拉画面，识别棋盘并在电脑上显示解密结果。

手机和电脑在同一个 Wi-Fi。手机上的 ScreenStream 已经开始投屏后：

    python watch_phone.py http://192.168.1.8:8080

窗口里按 Q 或 Esc 退出。如果应用里设了密码：

    python watch_phone.py http://192.168.1.8:8080 --pin 1234

画面拉取、识别解密、窗口刷新分成三条线。后台约每 0.5 秒看一眼最新画面：
不是棋盘就清掉旧叠加；同局已解过就跳过；新开局先丢掉旧解再解密。
窗口上的「重新识别」或按 R：丢掉当前结果，强制再识别一次（识别错了可手动重试）。
若一次解出多套方案，先当识别错重试；连续 3 次仍是多套，才按真多解显示。
"""

import argparse
import base64
import hashlib
import http.client
import json
import os
import socket
import sys
import threading
import time
from urllib.parse import urlparse

import tkinter as tk

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageTk

from solve_cats import (
    board_has_marks,
    draw_recognition,
    draw_solutions,
    finish_recognize,
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
# 窗口刷新间隔（毫秒）。
DISPLAY_MS = 50


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
        self.alive = True
        self.force_rerecognize = False

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
            self.status = status

    def request_rerecognize(self):
        """界面按钮：立刻清掉叠加，并让识别线程强制重跑。"""
        with self.lock:
            self.force_rerecognize = True
            self.solutions = None
            self.board = None
            self.n = None
            self.marked = False
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


class Viewer(object):
    def __init__(self, on_rerecognize=None):
        self.root = tk.Tk()
        self.root.title(WINDOW)
        self.closed = False
        self.photo = None
        self.on_rerecognize = on_rerecognize

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

        self.label = tk.Label(self.root, bg="black")
        self.label.pack()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Escape>", lambda _event: self.close())
        self.root.bind("<q>", lambda _event: self.close())
        self.root.bind("<Q>", lambda _event: self.close())
        self.root.bind("<r>", lambda _event: self._click_rerecognize())
        self.root.bind("<R>", lambda _event: self._click_rerecognize())

    def _click_rerecognize(self):
        if self.closed:
            return
        if self.on_rerecognize is not None:
            self.on_rerecognize()

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


def recognize_loop(state):
    """漏斗：非棋盘→清叠加；同局已解→跳过；新盘→丢掉旧解再解密；按钮可强制重跑。"""
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

        frame, _digest = state.get_frame_snapshot()
        if frame is None:
            continue

        peek = peek_board(frame)
        if peek is None:
            # 不是游戏棋盘：旧解一律丢掉，避免粘在微信等界面上。
            if bag["solutions"] is not None:
                print("离开棋盘，放下上次结果")
            _clear_bag(bag)
            state.clear_result("等待棋盘…")
            continue

        if peek.get("target_n") and bag.get("announced_n") != peek["target_n"]:
            print("进度条读到棋盘 %d×%d" % (peek["target_n"], peek["target_n"]))
            bag["announced_n"] = peek["target_n"]

        samples = peek["samples"]
        n = peek["n"]
        marked = board_has_marks(frame, peek["board"])

        # 同局且已有解：只更新格子坐标，不再聚类/解密（手动重识别会先清空 bag）。
        if (
            not force
            and bag["solutions"] is not None
            and same_board_colors(samples, n, bag["samples"], bag["n"])
        ):
            bag["board"] = peek["board"]
            bag["samples"] = samples
            bag["marked"] = marked or bag["marked"]
            state.set_result(
                bag["solutions"],
                bag["board"],
                bag["n"],
                bag["marked"],
                _title(bag["n"], bag["solutions"], bag["marked"]),
            )
            continue

        # 新的一局（或第一次 / 手动重跑）：旧结果直接丢掉。
        if bag["solutions"] is not None:
            print("检测到新的一局，放下上次结果")
            _clear_bag(bag)
            state.clear_result("识别新的一局…")

        fail_key = (n, hashlib.md5(np.round(np.asarray(samples) / 12.0).astype(np.int16).tobytes()).hexdigest())
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
        state.set_result(
            solutions,
            found["board"],
            n,
            marked,
            _title(n, solutions, marked),
        )


def watch(page_url, pin):
    state = SharedState()
    stream_thread = threading.Thread(
        target=stream_loop, args=(page_url, pin, state), name="stream", daemon=True
    )
    recog_thread = threading.Thread(
        target=recognize_loop, args=(state,), name="recognize", daemon=True
    )
    stream_thread.start()
    recog_thread.start()

    viewer = Viewer(on_rerecognize=state.request_rerecognize)
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


def main(argv):
    setup_console()
    parser = argparse.ArgumentParser(description="从手机画面自动解密猫猫方块")
    parser.add_argument("url", help="ScreenStream 显示的地址，例如 http://192.168.1.8:8080")
    parser.add_argument("--pin", default="", help="ScreenStream 里设置的数字密码")
    args = parser.parse_args(argv[1:])
    watch(args.url, args.pin.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
