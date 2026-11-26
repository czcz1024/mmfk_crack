# -*- coding: utf-8 -*-
"""监控电脑上已经打开的窗口，识别猫猫方块，并在这个窗口里点击。

    python watch_window.py

启动后选择窗口，例如「Xiaomi 12 · NectarLink」。
不使用 adb，也不改原来的 watch_phone.py。

能截到窗口自己的画面时，标注盖在投屏窗口上，鼠标可以点穿。
按钮和文字贴在投屏窗口旁边，跟着它移动。
截出来是黑的（Qt 投屏窗口常见），就在竖条外侧显示带标注的预览。
自动点击始终打在投屏窗口的格子上。
"""

import ctypes
import sys
import threading
import time
from ctypes import wintypes

import cv2
import numpy as np
import tkinter as tk
from PIL import Image, ImageDraw, ImageTk

import watch_phone
from solve_cats import draw_badge, load_font, scheme_color, setup_console
from watch_phone import (
    HintPane,
    SharedState,
    allow_display_sleep,
    compose_view,
    fit_to_screen,
    frame_digest,
    make_hint_pages,
    prevent_display_sleep,
    recognize_loop,
)

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32
try:
    dwmapi = ctypes.windll.dwmapi
except Exception:
    dwmapi = None

if dwmapi is not None:
    dwmapi.DwmGetWindowAttribute.argtypes = [
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020
BI_RGB = 0
DIB_RGB_COLORS = 0
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SW_RESTORE = 9
SW_SHOWNOACTIVATE = 4
SW_HIDE = 0
HWND_TOPMOST = -1
GA_ROOT = 2
DWMWA_CLOAKED = 14
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

PROBE_BLACK_FRAMES = 3
CAPTURE_INTERVAL = 0.2
BAR_W = 118
GAP = 8


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte),
        ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat", ctypes.c_byte),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("mi", MOUSEINPUT)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.WindowFromPoint.argtypes = [POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_void_p]
user32.keybd_event.restype = None
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND,
    wintypes.HDC,
    ctypes.POINTER(POINT),
    ctypes.POINTER(SIZE),
    wintypes.HDC,
    ctypes.POINTER(POINT),
    wintypes.DWORD,
    ctypes.POINTER(BLENDFUNCTION),
    wintypes.DWORD,
]
user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
gdi32.BitBlt.argtypes = [
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC,
    ctypes.POINTER(BITMAPINFO),
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.GetDIBits.argtypes = [
    wintypes.HDC,
    wintypes.HBITMAP,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.c_void_p,
    ctypes.POINTER(BITMAPINFO),
    wintypes.UINT,
]
gdi32.GetDIBits.restype = ctypes.c_int


def enable_dpi():
    """按显示器真实像素取坐标，避免缩放后点击偏一截。"""
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def window_root(hwnd):
    if not hwnd:
        return 0
    root = user32.GetAncestor(hwnd, GA_ROOT)
    return int(root or hwnd)


def window_title(hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value.strip()


def is_cloaked(hwnd):
    if dwmapi is None:
        return False
    cloaked = ctypes.c_int(0)
    hr = dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
    )
    return hr == 0 and cloaked.value != 0


def client_screen(hwnd):
    rect = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    pt = POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return None
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width < 40 or height < 40:
        return None
    return int(pt.x), int(pt.y), width, height


def window_screen(hwnd):
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        return None
    return int(rect.left), int(rect.top), width, height


def _bitmap_bgr(hdc_mem, hbmp, width, height):
    stride = ((width * 3 + 3) // 4) * 4
    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = height
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 24
    info.bmiHeader.biCompression = BI_RGB
    info.bmiHeader.biSizeImage = stride * height
    buf = ctypes.create_string_buffer(stride * height)
    rows = gdi32.GetDIBits(
        hdc_mem, hbmp, 0, height, buf, ctypes.byref(info), DIB_RGB_COLORS
    )
    if rows == 0:
        return None
    raw = np.frombuffer(buf, dtype=np.uint8).reshape(height, stride)
    bgr = raw[:, : width * 3].reshape(height, width, 3)
    return np.flipud(bgr).copy()


def _grab_dc(width, height, paint):
    if width <= 0 or height <= 0:
        return None
    screen = user32.GetDC(0)
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, width, height)
    old = gdi32.SelectObject(mem, bmp)
    try:
        if not paint(mem, screen):
            return None
        return _bitmap_bgr(mem, bmp, width, height)
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(0, screen)


def grab_screen(x, y, width, height):
    def paint(mem, screen):
        return bool(gdi32.BitBlt(mem, 0, 0, width, height, screen, x, y, SRCCOPY))

    return _grab_dc(width, height, paint)


def grab_print_client(hwnd):
    bounds = window_screen(hwnd)
    client = client_screen(hwnd)
    if bounds is None or client is None:
        return None
    wx, wy, ww, wh = bounds
    cx, cy, cw, ch = client

    def paint(mem, _screen):
        return bool(user32.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT))

    full = _grab_dc(ww, wh, paint)
    if full is None:
        return None
    ih, iw = full.shape[:2]
    x0 = int(round((cx - wx) * iw / float(ww)))
    y0 = int(round((cy - wy) * ih / float(wh)))
    x1 = int(round(x0 + cw * iw / float(ww)))
    y1 = int(round(y0 + ch * ih / float(wh)))
    x0 = max(0, min(iw - 1, x0))
    y0 = max(0, min(ih - 1, y0))
    x1 = max(x0 + 1, min(iw, x1))
    y1 = max(y0 + 1, min(ih, y1))
    return full[y0:y1, x0:x1].copy()


def mostly_black(img):
    """Qt 窗口用 PrintWindow 时，客户区经常是一整块黑。"""
    if img is None or img.size == 0:
        return True
    dark = np.mean(img, axis=2) < 12
    return float(np.mean(dark)) > 0.85


def list_windows():
    found = []
    my_pid = kernel32.GetCurrentProcessId()

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if is_cloaked(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == my_pid:
            return True
        title = window_title(hwnd)
        if not title:
            return True
        client = client_screen(hwnd)
        if client is None:
            return True
        found.append((int(hwnd), title, client[2], client[3]))
        return True

    enum = WNDENUMPROC(callback)
    user32.EnumWindows(enum, 0)
    def rank(item):
        title = item[1]
        if "NectarLink" in title and ("·" in title or "•" in title):
            return 0
        if "NectarLink" in title:
            return 1
        return 2

    found.sort(key=lambda item: (rank(item), item[1].casefold()))
    return found


def to_absolute(x, y):
    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = max(1, user32.GetSystemMetrics(SM_CXVIRTUALSCREEN) - 1)
    vh = max(1, user32.GetSystemMetrics(SM_CYVIRTUALSCREEN) - 1)
    ax = int(round((x - vx) * 65535 / float(vw)))
    ay = int(round((y - vy) * 65535 / float(vh)))
    return max(0, min(65535, ax)), max(0, min(65535, ay))


def send_click(x, y):
    ax, ay = to_absolute(x, y)
    flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    events = (flags, flags | MOUSEEVENTF_LEFTDOWN, flags | MOUSEEVENTF_LEFTUP)
    arr = (INPUT * len(events))()
    for index, flag in enumerate(events):
        arr[index].type = INPUT_MOUSE
        arr[index].mi.dx = ax
        arr[index].mi.dy = ay
        arr[index].mi.dwFlags = flag
    sent = user32.SendInput(len(arr), arr, ctypes.sizeof(INPUT))
    return sent == len(arr)


def raise_window(hwnd):
    """把投屏窗口拿到最前。不能把点击线程挂到界面线程上，否则两边会一起卡住。"""
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.keybd_event(VK_MENU, 0, 0, None)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, None)


def point_hits(hwnd, x, y, ignore=()):
    hit = user32.WindowFromPoint(POINT(int(x), int(y)))
    if not hit:
        return False
    if int(hit) in ignore:
        return True
    return window_root(hit) == int(hwnd)


def client_exposed(hwnd, ignore=()):
    """投屏窗口的中间没有被别的窗口挡住。"""
    if not hwnd or not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
        return False
    client = client_screen(hwnd)
    if client is None:
        return False
    x, y, w, h = client
    return point_hits(hwnd, x + w // 2, y + h // 2, ignore)


class Target(object):
    def __init__(self, hwnd, title):
        self.hwnd = int(hwnd)
        self.title = title
        self.lock = threading.Lock()
        self.mode = "probe"
        self.blank_hits = 0
        self.origin = (0, 0)
        self.client_size = (0, 0)
        self.frame_size = (0, 0)
        self.dead = False
        self.covered = False
        self.overlay_hwnd = 0

    def get_mode(self):
        with self.lock:
            return self.mode

    def note_black(self):
        with self.lock:
            if self.mode != "probe":
                return self.mode
            self.blank_hits += 1
            if self.blank_hits >= PROBE_BLACK_FRAMES:
                self.mode = "screen"
                print("窗口自身截图是黑的，改为截取屏幕上这块区域，标注显示在旁边")
            return self.mode

    def note_print(self):
        with self.lock:
            if self.mode != "print":
                print("窗口自身截图可用，标注盖在投屏窗口上")
            self.mode = "print"
            self.blank_hits = 0

    def set_geometry(self, origin, client_size, frame_shape, covered):
        with self.lock:
            self.origin = (int(origin[0]), int(origin[1]))
            self.client_size = (int(client_size[0]), int(client_size[1]))
            self.frame_size = (int(frame_shape[1]), int(frame_shape[0]))
            self.covered = bool(covered)

    def set_covered(self, covered):
        with self.lock:
            self.covered = bool(covered)

    def snapshot_geometry(self):
        with self.lock:
            return self.origin, self.client_size, self.frame_size, self.covered, self.mode, self.dead

    def mark_dead(self):
        with self.lock:
            self.dead = True

    def to_screen(self, fx, fy, frame_shape):
        with self.lock:
            ox, oy = self.origin
            cw, ch = self.client_size
        fh, fw = frame_shape[:2]
        if fw <= 0 or fh <= 0 or cw <= 0 or ch <= 0:
            return None
        return (
            int(round(ox + float(fx) * cw / float(fw))),
            int(round(oy + float(fy) * ch / float(fh))),
        )


def capture_loop(target, state):
    while state.running():
        if not user32.IsWindow(target.hwnd):
            print("窗口已关闭")
            target.mark_dead()
            state.set_status("窗口已关闭")
            state.stop()
            return
        if user32.IsIconic(target.hwnd):
            state.set_status("窗口已最小化")
            time.sleep(CAPTURE_INTERVAL)
            continue
        client = client_screen(target.hwnd)
        if client is None:
            time.sleep(CAPTURE_INTERVAL)
            continue
        cx, cy, cw, ch = client
        mode = target.get_mode()
        frame = None
        covered = not point_hits(
            target.hwnd,
            cx + cw // 2,
            cy + ch // 2,
            (target.overlay_hwnd,) if target.overlay_hwnd else (),
        )
        if mode != "screen":
            printed = grab_print_client(target.hwnd)
            if printed is not None and not mostly_black(printed):
                target.note_print()
                frame = printed
            elif mode == "print":
                target.set_covered(covered)
                time.sleep(CAPTURE_INTERVAL)
                continue
            else:
                mode = target.note_black()
                if mode != "screen":
                    time.sleep(CAPTURE_INTERVAL)
                    continue
        if frame is None:
            if covered:
                target.set_covered(True)
                time.sleep(CAPTURE_INTERVAL)
                continue
            shot = grab_screen(cx, cy, cw, ch)
            if shot is None:
                time.sleep(CAPTURE_INTERVAL)
                continue
            frame = shot
            covered = False
        target.set_geometry((cx, cy), (cw, ch), frame.shape, covered)
        state.set_frame(frame, frame_digest(frame))
        time.sleep(CAPTURE_INTERVAL)


def render_marks(frame_shape, board, solutions):
    """只画猫的标记，背景全透明，用来盖在投屏窗口上。"""
    height, width = frame_shape[:2]
    if width <= 0 or height <= 0 or not solutions or board is None:
        return None
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    side = min(board[0][0][2], board[0][0][3])
    n = len(board)
    owners = [[[] for _ in range(n)] for _ in range(n)]
    for index, cats in enumerate(solutions):
        for row, col in cats:
            if 0 <= row < n and 0 <= col < n:
                owners[row][col].append(index)
    for row in range(n):
        for col in range(n):
            ids = owners[row][col]
            if not ids:
                continue
            x, y, w, h = board[row][col]
            cx = x + w / 2.0
            cy = y + h / 2.0
            count = len(ids)
            radius = side * (0.30 if count == 1 else 0.16)
            if count > 4:
                radius = side * 0.12
            font = load_font(max(12, int(radius * 1.15)))
            if count == 1 and len(solutions) == 1:
                draw_badge(draw, cx, cy, radius, scheme_color(0), "猫", font)
                continue
            gap = radius * 2.25
            total = gap * count
            left = cx - total / 2.0 + gap / 2.0
            for offset, scheme_id in enumerate(ids):
                draw_badge(
                    draw,
                    left + offset * gap,
                    cy,
                    radius,
                    scheme_color(scheme_id),
                    str(scheme_id + 1),
                    font,
                )
    return overlay


def _marks_key(ox, oy, cw, ch, frame_shape, board, solutions):
    """用格子内容做缓存键。不能用 id(solutions)，每次刷新都会换成一个新列表。"""
    try:
        boxes = tuple(
            tuple(tuple(int(v) for v in cell) for cell in row)
            for row in board
        )
        found = tuple(
            tuple(tuple(int(v) for v in cell) for cell in cats)
            for cats in solutions
        )
    except (TypeError, ValueError):
        return None
    return (
        int(ox),
        int(oy),
        int(cw),
        int(ch),
        int(frame_shape[1]),
        int(frame_shape[0]),
        boxes,
        found,
    )


def premultiply_bgra(image):
    arr = np.array(image, dtype=np.uint8)
    alpha = arr[:, :, 3:4].astype(np.float32) / 255.0
    rgb = arr[:, :, :3].astype(np.float32) * alpha
    bgra = np.zeros_like(arr)
    bgra[:, :, 0] = rgb[:, :, 2]
    bgra[:, :, 1] = rgb[:, :, 1]
    bgra[:, :, 2] = rgb[:, :, 0]
    bgra[:, :, 3] = arr[:, :, 3]
    return np.ascontiguousarray(bgra)


class MarkOverlay(object):
    def __init__(self):
        self.hwnd = None
        self._class = "MmfkMarkOverlay"
        self._last_key = None
        self._visible = False
        self._suspended = False
        self._lock = threading.RLock()
        self._register()

    def _register(self):
        wc = WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(user32.DefWindowProcW, ctypes.c_void_p)
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = self._class
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom and kernel32.GetLastError() != 1410:
            print("标注层注册失败: %s" % kernel32.GetLastError())
        ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST
        self.hwnd = user32.CreateWindowExW(
            ex,
            self._class,
            "猫猫方块标注",
            WS_POPUP,
            0,
            0,
            0,
            0,
            None,
            None,
            wc.hInstance,
            None,
        )
        if not self.hwnd:
            print("标注层创建失败: %s" % kernel32.GetLastError())

    def hide(self):
        with self._lock:
            self._hide_locked()

    def _hide_locked(self):
        if self.hwnd and self._visible:
            user32.ShowWindow(self.hwnd, SW_HIDE)
            self._visible = False

    def is_suspended(self):
        with self._lock:
            return self._suspended

    def suspend(self):
        """只做标记。真正藏窗口必须在界面线程，后台线程直接 ShowWindow 会和界面互相死锁。"""
        with self._lock:
            self._suspended = True

    def wait_hidden(self, timeout=0.6):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if not self._visible:
                    return True
            time.sleep(0.02)
        return False

    def resume(self):
        with self._lock:
            self._suspended = False

    def destroy(self):
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None

    def update(self, origin, client_size, frame_shape, board, solutions):
        with self._lock:
            if self._suspended or not self.hwnd:
                return
            ox, oy = origin
            cw, ch = client_size
            if cw < 40 or ch < 40 or not solutions or board is None:
                self._hide_locked()
                return
            key = _marks_key(ox, oy, cw, ch, frame_shape, board, solutions)
            if key is None:
                self._hide_locked()
                return
            if key == self._last_key and self._visible:
                return
            image = render_marks(frame_shape, board, solutions)
            if image is None:
                self._hide_locked()
                return
            if image.size != (cw, ch):
                image = image.resize((cw, ch), Image.BILINEAR)
            bgra = premultiply_bgra(image)
            if self._present(ox, oy, bgra):
                self._last_key = key
                self._visible = True
            else:
                self._hide_locked()

    def _present(self, x, y, bgra):
        height, width = bgra.shape[:2]
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB
        bits = ctypes.c_void_p()
        screen = user32.GetDC(0)
        mem = gdi32.CreateCompatibleDC(screen)
        bmp = gdi32.CreateDIBSection(
            screen, ctypes.byref(info), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
        )
        if not bmp or not bits:
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(0, screen)
            return False
        old = gdi32.SelectObject(mem, bmp)
        try:
            ctypes.memmove(bits, bgra.ctypes.data, width * height * 4)
            dst = POINT(int(x), int(y))
            size = SIZE(width, height)
            src = POINT(0, 0)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            ok = user32.UpdateLayeredWindow(
                self.hwnd,
                screen,
                ctypes.byref(dst),
                ctypes.byref(size),
                mem,
                ctypes.byref(src),
                0,
                ctypes.byref(blend),
                ULW_ALPHA,
            )
            if not ok:
                print("标注层刷新失败: %s" % kernel32.GetLastError())
            return bool(ok)
        finally:
            gdi32.SelectObject(mem, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(0, screen)


def click_cell(target, overlay, x, y):
    if not user32.IsWindow(target.hwnd) or user32.IsIconic(target.hwnd):
        return False
    overlay.suspend()
    try:
        overlay.wait_hidden()
        raise_window(target.hwnd)
        time.sleep(0.05)
        ignore = (int(overlay.hwnd),) if overlay.hwnd else ()
        if not point_hits(target.hwnd, x, y, ignore):
            raise_window(target.hwnd)
            time.sleep(0.08)
            if not point_hits(target.hwnd, x, y, ignore):
                return False
        if not send_click(x, y):
            return False
        time.sleep(0.12)
        return send_click(x, y)
    finally:
        overlay.resume()


def play_loop(state, app, target):
    try:
        pack = state.get_play_pack()
        if pack is None or not pack["solutions"]:
            state.set_note("请先重新识别")
            return
        solution = [tuple(cell) for cell in pack["solutions"][0]]
        labels = pack["labels"]
        print("自动点击：打在窗口「%s」上，先点第 1 套，共 %d 套" % (target.title, len(pack["solutions"])))
        if len(pack["solutions"]) > 1:
            state.set_note("有多套，先点第1套")
        must_resolve = False
        empty_tries = 0
        last_cell = None
        wrong_rounds = 0
        extra_banned = set()
        accepted = set()
        resolve_streak = 0
        while not state.play_should_stop():
            pack = state.get_play_pack()
            if pack is None:
                state.set_note("没有解密结果，已停止")
                return
            frame = pack["frame"]
            board = pack["board"]
            n = pack["n"]
            marked = pack["marked"]
            if watch_phone.is_celebration_screen(frame):
                state.set_note("这一局已经结束")
                print("自动点击：过关了，停止点击")
                return
            confirmed, banned = watch_phone._marks_on_board(frame, board)
            confirmed = confirmed | accepted
            banned = banned | extra_banned
            if must_resolve or watch_phone._solution_conflicts(solution, confirmed, banned):
                resolve_streak += 1
                if resolve_streak > 3:
                    state.set_note("局面变了，已停止")
                    print("自动点击：连续重新解密仍对不上，停止")
                    return
                resolved = watch_phone._resolve_after_miss(
                    state, frame, labels, board, n, marked, confirmed, banned
                )
                if resolved is None:
                    return
                solution, labels, board = resolved
                must_resolve = False
                time.sleep(0.05)
                continue
            resolve_streak = 0
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
            point = target.to_screen(box[0] + box[2] / 2.0, box[1] + box[3] / 2.0, frame.shape)
            if point is None:
                state.set_note("还没有窗口位置")
                return
            sx, sy = point
            state.set_note("正在点 %d/%d" % (len(confirmed) + 1, n))
            print(
                "双击第 %d 行第 %d 列，画面 %dx%d → 屏幕 (%d,%d)"
                % (row + 1, col + 1, frame.shape[1], frame.shape[0], sx, sy)
            )
            if not click_cell(target, app.overlay, sx, sy):
                empty_tries += 1
                print("点击没有落在投屏窗口上，第 %d 次" % empty_tries)
                if empty_tries > 2:
                    state.set_note("没点到窗口，已停止")
                    print("请让投屏窗口露在最前面。")
                    return
                state.set_note("没点到窗口，再试一次")
                time.sleep(0.2)
                continue
            kind = watch_phone._wait_cell(state, row, col, 2.6)
            if state.play_should_stop():
                break
            if kind == watch_phone.hint_cats.CAT:
                empty_tries = 0
                accepted.add((row, col))
                state.set_note("这一格正确")
                print("第 %d 行第 %d 列是猫" % (row + 1, col + 1))
                time.sleep(0.25)
                continue
            if kind == watch_phone.hint_cats.EX:
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
                print("连续没点中。请确认游戏在这个窗口里，并且窗口没有被挡住。")
                return
            state.set_note("没点中，再试一次")
        if state.has_solution() and state.running():
            state.set_note("已停止")
    except Exception as exc:
        print("自动点击失败: %s" % exc)
        state.set_note(str(exc)[:36])
    finally:
        state.end_play()
        app.set_play_text("自动点击")


class App(object):
    def __init__(self, root, target, state):
        self.root = root
        self.target = target
        self.state = state
        self.closed = False
        self.photo = None
        self.preview_ready = False
        self.hint_visible = False
        self.overlay = MarkOverlay()
        self.target.overlay_hwnd = int(self.overlay.hwnd or 0)
        self.root.title("猫猫方块 · %s" % target.title)
        self.root.configure(bg="#1e2428")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Escape>", lambda _event: self.close())
        self.root.bind("<q>", lambda _event: self.close())
        self.root.bind("<Q>", lambda _event: self.close())
        self.root.bind("<r>", lambda _event: self._click_rerecognize())
        self.root.bind("<R>", lambda _event: self._click_rerecognize())
        self.root.bind("<Left>", lambda _event: self.hint_pane.prev_page())
        self.root.bind("<Right>", lambda _event: self.hint_pane.next_page())

        self._geom = None
        self._side = None
        self._preview_size = (0, 0)
        self.strip = tk.Frame(self.root, bg="#1e2428", width=BAR_W)
        self.strip.pack_propagate(False)
        self._button(self.strip, "重新识别", self._click_rerecognize, "#3d8bfd").pack(
            fill=tk.X, padx=6, pady=(8, 4)
        )
        self.hint_btn = self._button(self.strip, "出提示", self._click_hint, "#e23b45")
        self.hint_btn.pack(fill=tk.X, padx=6, pady=4)
        self.play_btn = self._button(self.strip, "自动点击", self._click_play, "#1f8a4c")
        self.play_btn.pack(fill=tk.X, padx=6, pady=4)
        self.mode_label = tk.Label(
            self.strip,
            text="正在确认怎么截这个窗口…",
            bg="#1e2428",
            fg="#d0d4d8",
            font=("Microsoft YaHei UI", 9),
            anchor=tk.NW,
            justify=tk.LEFT,
            wraplength=BAR_W - 16,
        )
        self.mode_label.pack(fill=tk.X, padx=8, pady=(10, 4))
        self._button(self.strip, "关闭", self.close, "#2c3338").pack(
            fill=tk.X, padx=6, pady=(4, 8), side=tk.BOTTOM
        )
        self.status_label = tk.Label(
            self.strip,
            text="正在截取窗口…",
            bg="#14181c",
            fg="white",
            font=("Microsoft YaHei UI", 9),
            anchor=tk.NW,
            justify=tk.LEFT,
            wraplength=BAR_W - 16,
        )
        self.status_label.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)

        self.body = tk.Frame(self.root, bg="black")
        self.preview = tk.Label(self.body, bg="black")
        self.hint_pane = HintPane(self.body)
        self._mode_text_shown = None
        self._status_text_shown = None
        self.root.overrideredirect(True)
        try:
            self.root.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        self.root.withdraw()

    def _button(self, parent, text, command, color):
        return tk.Button(
            parent,
            text=text,
            command=command,
            font=("Microsoft YaHei UI", 10),
            bg=color,
            fg="white",
            activebackground=color,
            activeforeground="white",
            relief=tk.FLAT,
            padx=4,
            pady=6,
            cursor="hand2",
        )

    def _strip_hwnd(self):
        return window_root(int(self.root.winfo_id()))

    def _pack_side(self, side, show_body):
        """竖条本身贴着投屏窗口。预览和提示只在需要时出现在更外侧。"""
        key = (side, bool(show_body))
        if key == getattr(self, "_pack_key", None):
            return
        self._pack_key = key
        self._side = side
        self.strip.pack_forget()
        self.body.pack_forget()
        if side == "right":
            self.strip.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            if show_body:
                self.body.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        else:
            self.strip.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)
            if show_body:
                self.body.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

    def _follow_target(self):
        bounds = window_screen(self.target.hwnd)
        if bounds is None:
            return
        wx, wy, ww, wh = bounds
        vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        extra_w = 0
        extra_h = wh
        if self.preview_ready:
            extra_w += self._preview_size[0]
            extra_h = max(extra_h, self._preview_size[1])
        if self.hint_visible:
            extra_w += 760
            extra_h = max(extra_h, 640)
        room_right = (vx + vw) - (wx + ww)
        room_left = wx - vx
        side = "right" if room_right >= room_left else "left"
        room = room_right if side == "right" else room_left
        width = BAR_W + min(extra_w, max(0, room - BAR_W))
        height = min(vh - 8, max(wh, extra_h))
        if side == "right":
            x = wx + ww
        else:
            x = wx - width
        y = wy
        if y + height > vy + vh:
            y = max(vy, vy + vh - height)
        x, y, width, height = int(x), int(y), int(width), int(height)
        self._pack_side(side, extra_w > 0)
        hwnd = self._strip_hwnd()
        actual = window_screen(hwnd) if hwnd else None
        if actual is not None:
            ax, ay, aw, ah = actual
            if (
                abs(ax - x) < 3
                and abs(ay - y) < 3
                and abs(aw - width) < 3
                and abs(ah - height) < 3
            ):
                return
        self.root.geometry("%dx%d+%d+%d" % (width, height, x, y))
        self.root.update_idletasks()
        hwnd = self._strip_hwnd()
        if hwnd:
            user32.SetWindowPos(
                hwnd,
                None,
                x,
                y,
                width,
                height,
                SWP_NOZORDER | SWP_NOACTIVATE,
            )

    def _ignore_hwnds(self):
        found = []
        if self.target.overlay_hwnd:
            found.append(int(self.target.overlay_hwnd))
        hwnd = self._strip_hwnd()
        if hwnd:
            found.append(int(hwnd))
        return tuple(found)

    def _target_in_front(self):
        return client_exposed(self.target.hwnd, self._ignore_hwnds())

    def _hide_strip(self):
        self.overlay.hide()
        try:
            if self.root.state() != "withdrawn":
                self.root.withdraw()
        except tk.TclError:
            pass

    def _show_strip(self):
        hidden = True
        try:
            hidden = self.root.state() == "withdrawn"
        except tk.TclError:
            pass
        self._follow_target()
        if hidden:
            self.root.deiconify()
            self.root.update_idletasks()
            self._follow_target()

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

    def _click_rerecognize(self):
        if not self.closed:
            self.state.request_rerecognize()

    def _click_play(self):
        if self.closed:
            return
        if self.state.play_is_running():
            self.state.request_play_stop()
            self.play_btn.configure(text="正在停止…")
            return
        if not self.state.has_solution():
            self.state.set_status("还没有解密结果")
            self.play_btn.configure(text="还没解密")
            self.root.after(1500, lambda: self.set_play_text("自动点击"))
            return
        if not self.state.begin_play():
            return
        self.play_btn.configure(text="停止点击")
        threading.Thread(
            target=play_loop, args=(self.state, self, self.target), name="play", daemon=True
        ).start()

    def _click_hint(self):
        if self.closed:
            return
        if self.hint_visible:
            self._hide_hint()
            return
        self.hint_btn.configure(state=tk.DISABLED, text="正在出提示…")
        frame, _digest = self.state.get_frame_snapshot()
        if frame is None:
            self._hint_message("还没有画面。")
            return

        def work():
            error = None
            pages = None
            try:
                pages = make_hint_pages(frame)
            except Exception as exc:
                error = str(exc)

            def apply():
                if self.closed:
                    return
                if pages:
                    self._show_hint_pages(pages)
                else:
                    self._hint_message(error or "出提示失败")

            try:
                self.root.after(0, apply)
            except tk.TclError:
                pass

        threading.Thread(target=work, name="hint", daemon=True).start()

    def _show_hint_pages(self, pages):
        self._ensure_hint_column()
        self.hint_pane.set_pages(pages)
        self.hint_btn.configure(state=tk.NORMAL, text="关闭提示")

    def _hint_message(self, text):
        self._ensure_hint_column()
        self.hint_pane.show_message(text)
        self.hint_btn.configure(state=tk.NORMAL, text="关闭提示")

    def _ensure_hint_column(self):
        if self.hint_visible:
            return
        self.hint_pane.frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.hint_visible = True
        self._follow_target()

    def _hide_hint(self):
        if not self.hint_visible:
            return
        self.hint_pane.frame.pack_forget()
        self.hint_visible = False
        self.hint_btn.configure(state=tk.NORMAL, text="出提示")
        self._follow_target()

    def _show_preview(self, img):
        view = fit_to_screen(img)
        rgb = cv2.cvtColor(view, cv2.COLOR_BGR2RGB)
        self.photo = ImageTk.PhotoImage(Image.fromarray(rgb))
        self.preview.configure(image=self.photo)
        if not self.preview.winfo_ismapped():
            self.preview.pack(side=tk.LEFT)
        height, width = view.shape[:2]
        size = (width, height)
        if not self.preview_ready or size != self._preview_size:
            self.preview_ready = True
            self._preview_size = size
            self._follow_target()

    def _mode_text(self, mode, covered, dead):
        if dead:
            return "窗口已关闭"
        if mode == "probe":
            return "正在确认怎么截这个窗口…"
        if mode == "print" and covered:
            return "投屏被挡住了。露出来以后，标注会盖回画面上。"
        if mode == "print":
            return "标注在投屏上，可以点穿。"
        if covered:
            return "投屏被挡住了，请把它露出来。"
        return "截不到窗口本身，标注显示在外侧。"

    def tick(self):
        if self.closed:
            return
        try:
            self._tick_once()
        except Exception as exc:
            print("界面刷新失败: %s" % exc)
        if not self.closed:
            try:
                self.root.after(50, self.tick)
            except tk.TclError:
                pass

    def _tick_once(self):
        _origin, _client_size, _frame_size, _covered, _mode, dead = self.target.snapshot_geometry()
        if dead or not self._target_in_front():
            self._hide_strip()
            return
        self._show_strip()
        origin, client_size, _frame_size, covered, mode, dead = self.target.snapshot_geometry()
        mode_text = self._mode_text(mode, covered, dead)
        if mode_text != self._mode_text_shown:
            self._mode_text_shown = mode_text
            self.mode_label.configure(text=mode_text)
        snap = self.state.snapshot_for_display()
        status_text = snap["status"] or ""
        if status_text != self._status_text_shown:
            self._status_text_shown = status_text
            self.status_label.configure(text=status_text)
        self._follow_target()
        if self.overlay.is_suspended():
            self.overlay.hide()
            return
        use_overlay = (
            mode == "print"
            and not covered
            and not dead
            and snap["frame"] is not None
            and snap["solutions"]
            and snap["board"] is not None
        )
        show_preview = (
            snap["frame"] is not None
            and not dead
            and not covered
            and mode == "screen"
        )
        if use_overlay:
            if self.preview.winfo_ismapped():
                self.preview.pack_forget()
                self.preview_ready = False
                self._preview_size = (0, 0)
            self.overlay.update(
                origin, client_size, snap["frame"].shape, snap["board"], snap["solutions"]
            )
        else:
            self.overlay.hide()
            if show_preview:
                self._show_preview(compose_view(snap))
            elif self.preview.winfo_ismapped() and not self.hint_visible:
                self.preview.pack_forget()
                self.preview_ready = False
                self._preview_size = (0, 0)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.state.stop()
        self.overlay.destroy()
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def pick_window(root):
    """列表就做在主窗口上。主窗口一旦 withdraw，再弹出的子窗口在 Windows 上经常是隐形的。"""
    chosen = {"hwnd": None, "title": None}
    root.title("选择要监控的窗口")
    root.configure(bg="#1e2428")
    root.minsize(520, 320)
    root.update_idletasks()
    width, height = 720, 480
    x = max(0, (root.winfo_screenwidth() - width) // 2)
    y = max(0, (root.winfo_screenheight() - height) // 2)
    root.geometry("%dx%d+%d+%d" % (width, height, x, y))
    root.deiconify()
    root.lift()
    root.attributes("-topmost", True)
    root.focus_force()

    tk.Label(
        root,
        text="选 NectarLink 的投屏窗口，例如「Xiaomi 12 · NectarLink」。",
        bg="#1e2428",
        fg="white",
        font=("Microsoft YaHei UI", 12),
        anchor=tk.W,
    ).pack(fill=tk.X, padx=12, pady=(12, 4))
    tk.Label(
        root,
        text="双击一行，或选中后点「开始」。",
        bg="#1e2428",
        fg="#c8c8c8",
        font=("Microsoft YaHei UI", 10),
        anchor=tk.W,
    ).pack(fill=tk.X, padx=12, pady=(0, 8))

    frame = tk.Frame(root, bg="#1e2428")
    frame.pack(fill=tk.BOTH, expand=True, padx=12)
    scroll = tk.Scrollbar(frame)
    scroll.pack(side=tk.RIGHT, fill=tk.Y)
    box = tk.Listbox(
        frame,
        yscrollcommand=scroll.set,
        font=("Microsoft YaHei UI", 11),
        bg="#14181c",
        fg="white",
        selectbackground="#3d8bfd",
        selectforeground="white",
        activestyle="none",
        highlightthickness=0,
        relief=tk.FLAT,
    )
    box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scroll.config(command=box.yview)
    items = []

    def refill():
        items[:] = list_windows()
        box.delete(0, tk.END)
        select_at = None
        for index, (hwnd, title, width, height) in enumerate(items):
            box.insert(tk.END, "%s    %d×%d" % (title, width, height))
            mirror = "NectarLink" in title and ("·" in title or "•" in title)
            if select_at is None and (mirror or "NectarLink" in title):
                select_at = index
        if items and select_at is None:
            select_at = 0
        if select_at is not None:
            box.selection_set(select_at)
            box.see(select_at)
            box.activate(select_at)
        print("找到 %d 个窗口" % len(items), flush=True)

    def finish():
        root.attributes("-topmost", False)
        root.withdraw()
        root.quit()

    def accept(_event=None):
        selection = box.curselection()
        if not selection:
            return
        hwnd, title, _w, _h = items[selection[0]]
        chosen["hwnd"] = hwnd
        chosen["title"] = title
        finish()

    def cancel():
        finish()

    box.bind("<Double-Button-1>", accept)
    buttons = tk.Frame(root, bg="#1e2428")
    buttons.pack(fill=tk.X, padx=12, pady=12)
    tk.Button(
        buttons,
        text="刷新",
        command=refill,
        font=("Microsoft YaHei UI", 11),
        bg="#2c3338",
        fg="white",
        activebackground="#3d454c",
        activeforeground="white",
        relief=tk.FLAT,
        padx=14,
        pady=6,
    ).pack(side=tk.LEFT)
    tk.Button(
        buttons,
        text="开始",
        command=accept,
        font=("Microsoft YaHei UI", 11),
        bg="#1f8a4c",
        fg="white",
        activebackground="#18723e",
        activeforeground="white",
        relief=tk.FLAT,
        padx=18,
        pady=6,
    ).pack(side=tk.RIGHT)
    tk.Button(
        buttons,
        text="取消",
        command=cancel,
        font=("Microsoft YaHei UI", 11),
        bg="#2c3338",
        fg="white",
        activebackground="#3d454c",
        activeforeground="white",
        relief=tk.FLAT,
        padx=14,
        pady=6,
    ).pack(side=tk.RIGHT, padx=8)
    root.protocol("WM_DELETE_WINDOW", cancel)
    print("窗口列表已打开。没看到的话，到任务栏点一下这个窗口。", flush=True)
    root.after(50, refill)
    root.mainloop()
    for child in list(root.winfo_children()):
        child.destroy()
    try:
        root.protocol("WM_DELETE_WINDOW", lambda: None)
    except tk.TclError:
        pass
    return chosen["hwnd"], chosen["title"]


def watch(root, hwnd, title):
    prevent_display_sleep()
    state = SharedState()
    state.set_status("正在截取窗口…")
    target = Target(hwnd, title)
    app = App(root, target, state)
    threading.Thread(target=capture_loop, args=(target, state), name="capture", daemon=True).start()
    threading.Thread(target=recognize_loop, args=(state,), name="recognize", daemon=True).start()
    app.tick()
    try:
        root.mainloop()
    finally:
        state.stop()
        app.overlay.destroy()
        allow_display_sleep()


def main(argv):
    setup_console()
    if sys.platform != "win32":
        print("只支持 Windows")
        return 1
    print("正在打开窗口列表…", flush=True)
    enable_dpi()
    if ctypes.sizeof(INPUT) not in (28, 40):
        print("鼠标输入结构尺寸异常: %s" % ctypes.sizeof(INPUT))
        return 1
    root = tk.Tk()
    hwnd, title = pick_window(root)
    if not hwnd:
        root.destroy()
        print("没有选择窗口")
        return 0
    print("监控窗口: %s" % title)
    watch(root, hwnd, title)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
