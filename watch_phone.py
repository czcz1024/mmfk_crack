# -*- coding: utf-8 -*-
"""从手机 ScreenStream 拉画面，识别棋盘并在电脑上显示解密结果。

手机和电脑在同一个 Wi-Fi。手机上的 ScreenStream 已经开始投屏后：

    python watch_phone.py http://192.168.1.8:8080

窗口里按 Q 或 Esc 退出。如果应用里设了密码：

    python watch_phone.py http://192.168.1.8:8080 --pin 1234
"""

import argparse
import base64
import hashlib
import http.client
import json
import os
import socket
import sys
from urllib.parse import urlparse

import tkinter as tk

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageTk

from solve_cats import (
    board_has_marks,
    draw_solutions,
    load_font,
    recognize_bgr,
    setup_console,
    solve_all,
)

WINDOW = "猫猫方块解密"


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


class Viewer(object):
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(WINDOW)
        self.closed = False
        self.photo = None
        self.label = tk.Label(self.root, bg="black")
        self.label.pack()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Escape>", lambda _event: self.close())
        self.root.bind("<q>", lambda _event: self.close())
        self.root.bind("<Q>", lambda _event: self.close())

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
        try:
            self.root.update_idletasks()
            self.root.update()
        except tk.TclError:
            self.closed = True
            return False
        return not self.closed


def watch(page_url, pin):
    viewer = Viewer()
    last_digest = None
    last_labels = None
    last_solutions = None
    shown = paint_status(np.full((640, 360, 3), 32, np.uint8), "正在连接手机…")
    if not viewer.show(shown):
        return
    while True:
        try:
            _conn, response = open_stream(page_url, pin)
            print("已连上画面")
            for payload in jpeg_frames(response):
                frame = decode_jpeg(payload)
                if frame is None:
                    continue
                digest = frame_digest(frame)
                if digest == last_digest:
                    if not viewer.show(shown):
                        return
                    continue
                last_digest = digest
                try:
                    found = recognize_bgr(frame)
                    if board_has_marks(frame, found["board"]):
                        if last_solutions and len(last_solutions[0]) == found["n"]:
                            shown = draw_solutions(frame, found["board"], last_solutions)
                            shown = paint_status(shown, "同一局，保持这次结果")
                        elif last_solutions:
                            pass
                        else:
                            shown = paint_status(frame, "格子已有标记，等新的一局再解密")
                    else:
                        label_key = found["labels"].tobytes()
                        if label_key != last_labels:
                            solutions, truncated = solve_all(found["labels"])
                            last_labels = label_key
                            last_solutions = solutions
                            if not solutions:
                                print("新的一局 %d×%d，没有满足规则的摆法" % (found["n"], found["n"]))
                            else:
                                extra = "，还有更多套" if truncated else ""
                                print("新的一局 %d×%d，%d 套方案%s" % (found["n"], found["n"], len(solutions), extra))
                        if last_solutions:
                            shown = draw_solutions(frame, found["board"], last_solutions)
                            title = "%d×%d，%d 套方案" % (found["n"], found["n"], len(last_solutions))
                            shown = paint_status(shown, title)
                        else:
                            shown = paint_status(frame, "看到棋盘，但规则下无解")
                except Exception:
                    if last_solutions is None:
                        shown = paint_status(frame, "当前画面没有棋盘")
                if not viewer.show(shown):
                    return
        except PermissionError as exc:
            print(exc)
            return
        except Exception as exc:
            print("连接中断: %s，正在重试" % exc)
            blank = np.full((640, 360, 3), 32, np.uint8)
            shown = paint_status(blank, "连接中断，正在重试")
            if not viewer.show(shown):
                return


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
