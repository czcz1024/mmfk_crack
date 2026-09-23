# -*- coding: utf-8 -*-
"""识别《猫猫方块》截图并解密。格子大小不写死，每张图单独测量。

解密规则：每行、每列、每种颜色恰好一只猫，猫与猫不能相邻（含斜角）。
有多套都成立的摆法时，在同一张图上用不同颜色的编号标出。

做法：
1. 用背景色把方块从画面里分离出来，找近似正方形的轮廓。
2. 按边长分组。选总面积最大、又能排成完整方阵的那一组，躲开教程小图和按钮。
3. 用这一组方块的中心距算出行距、列距，再在棋盘区域内按缝隙把每一格切出来。
4. 每格取中间颜色；按相邻格子的色差合并成恰好 N 块连通色区（一种颜色一块区域）。

用法：
    python solve_cats.py 截图1.jpg 截图2.jpg
"""

import ctypes
import math
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# 同色格子的色差上限。这四张图里，不同颜色的最近色差大约是 47。
COLOR_MERGE_DIST = 22.0
MAX_SCHEMES = 8
SCHEME_COLORS = (
    (196, 40, 32),
    (25, 95, 210),
    (16, 140, 72),
    (214, 120, 16),
    (128, 48, 176),
    (0, 140, 150),
    (170, 40, 110),
    (70, 70, 70),
)


def setup_console():
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def load_bgr(path):
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("无法读取图片: %s" % path)
    return img


def save_bgr(path, img):
    ext = Path(path).suffix or ".jpg"
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        raise RuntimeError("无法保存图片: %s" % path)
    buf.tofile(str(path))


def load_font(size):
    for path in (
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def neutralize_dark_banner(img):
    """投屏横幅或深色提示条会破坏背景估计和格子缝，先铺成下方一行的颜色。"""
    h, w = img.shape[:2]
    row_means = img.reshape(h, -1).mean(axis=1)
    body = float(np.median(row_means[h // 5: (4 * h) // 5]))
    limit = max(12, h // 6)
    i = 0
    while i < limit and row_means[i] >= body - 28:
        i += 1
    while i < limit and row_means[i] < body - 28:
        i += 1
    cut = i
    if cut < 8:
        return img
    out = img.copy()
    out[:cut] = img[min(cut, h - 1)]
    return out


def estimate_background(img):
    """估计画面背景色。顶栏若是深色通知/状态条，会丢掉，避免把浅色缝也当成前景。"""
    h, w = img.shape[:2]
    t = max(4, min(h, w) // 80)
    strips = [img[-t:, :], img[:, :t], img[:, -t:]]
    top = img[:t, :]
    top_mean = float(np.mean(top))
    side_mean = float(np.mean(np.concatenate([s.reshape(-1, 3) for s in strips], axis=0)))
    # 顶栏明显更暗时（投屏横幅、程序自己的深色提示条），不用它估背景。
    if top_mean >= side_mean - 25:
        strips.insert(0, top)
    pixels = np.concatenate([s.reshape(-1, 3) for s in strips], axis=0)
    return np.median(pixels, axis=0).astype(np.uint8)


def foreground_mask(img):
    bg = estimate_background(img).reshape(1, 1, 3)
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)
    bg_lab = cv2.cvtColor(bg, cv2.COLOR_BGR2LAB).astype(np.float32)[0, 0]
    dist = np.linalg.norm(lab - bg_lab, axis=2)
    dist_u8 = np.clip(dist, 0, 255).astype(np.uint8)
    otsu, _ = cv2.threshold(dist_u8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    thr = float(np.clip(otsu, 12, 45))
    return dist >= thr


def find_squares(mask):
    h, w = mask.shape
    min_side = max(10, int(min(h, w) * 0.012))
    binary = mask.astype(np.uint8) * 255
    # 投屏 JPEG 容易把一格撕成几块，先轻度闭运算粘回去。
    k = max(3, int(min_side * 0.12) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    squares = []
    for contour in contours:
        x, y, bw, bh = cv2.boundingRect(contour)
        if bw < min_side or bh < min_side:
            continue
        aspect = bw / float(bh)
        if aspect < 0.75 or aspect > 1.33:
            continue
        area = cv2.contourArea(contour)
        if area < bw * bh * 0.55:
            continue
        squares.append((x, y, bw, bh))
    return squares


def group_by_size(squares):
    ordered = sorted(squares, key=lambda s: (s[2] + s[3]) / 2.0)
    groups = []
    for square in ordered:
        side = (square[2] + square[3]) / 2.0
        if not groups:
            groups.append([square])
            continue
        current = np.median([(s[2] + s[3]) / 2.0 for s in groups[-1]])
        # 投屏压缩后同局格子边长会抖一点，分组稍微放宽。
        if side > float(current) * 1.28:
            groups.append([square])
        else:
            groups[-1].append(square)
    return groups


def measure_pitch(coords, min_step, max_step=None, prefer=None):
    vals = np.sort(np.asarray(coords, dtype=np.float64))
    diffs = np.diff(vals)
    steps = diffs[diffs > min_step]
    if max_step is not None:
        steps = steps[steps < max_step]
    if prefer is not None and len(steps) > 0:
        near = steps[(steps > prefer * 0.75) & (steps < prefer * 1.40)]
        if len(near) > 0:
            return float(np.median(near))
    if len(steps) == 0:
        return None
    return float(np.median(steps))


def _square_score(square):
    x, y, w, h = square
    return w * h


def _synthesize_cell(row, col, origin_x, origin_y, pitch_x, pitch_y, side):
    cx = origin_x + col * pitch_x
    cy = origin_y + row * pitch_y
    half = side / 2.0
    x = int(round(cx - half))
    y = int(round(cy - half))
    s = max(1, int(round(side)))
    return (x, y, s, s)


def lattice_from_squares(squares, prefer_n=None):
    """用方块中心距推出行列。允许缺几格（局中叉/压缩），按间距补全后取最大方阵。"""
    if len(squares) < 4:
        return None
    sides = np.array([(w + h) / 2.0 for _, _, w, h in squares], dtype=np.float64)
    side = float(np.median(sides))
    cx = np.array([x + w / 2.0 for x, _, w, _ in squares])
    cy = np.array([y + h / 2.0 for _, y, _, h in squares])
    min_step = side * 0.45
    max_step = side * 1.75
    pitch_x = measure_pitch(cx, min_step, max_step, prefer=side)
    pitch_y = measure_pitch(cy, min_step, max_step, prefer=side)
    if pitch_x is None or pitch_y is None:
        return None
    if pitch_x < side * 0.7 or pitch_y < side * 0.7:
        return None
    if pitch_x > side * 1.65 or pitch_y > side * 1.65:
        return None
    origin_x = float(cx.min())
    origin_y = float(cy.min())
    col_idx = np.rint((cx - origin_x) / pitch_x).astype(int)
    row_idx = np.rint((cy - origin_y) / pitch_y).astype(int)
    occ = {}
    for i, square in enumerate(squares):
        key = (int(row_idx[i]), int(col_idx[i]))
        prev = occ.get(key)
        if prev is None or _square_score(square) > _square_score(prev):
            occ[key] = square
    if len(occ) < 9:
        return None
    rows = sorted(set(k[0] for k in occ))
    cols = sorted(set(k[1] for k in occ))
    r_min, r_max = rows[0], rows[-1]
    c_min, c_max = cols[0], cols[-1]
    guess = int(round(math.sqrt(len(occ))))
    max_n = min(r_max - r_min + 1, c_max - c_min + 1, max(guess + 1, 3))
    if prefer_n is not None:
        max_n = max(max_n, prefer_n)
    order = list(range(max_n, 2, -1))
    if prefer_n is not None and prefer_n >= 3:
        order = [prefer_n] + [x for x in order if x != prefer_n]
    best = None
    best_key = None
    for n in order:
        if n > r_max - r_min + 1 or n > c_max - c_min + 1:
            continue
        # 比方块数量推断值更大的 N，必须几乎满格，防止顶栏多出一行变成 10×10。
        if prefer_n is not None and n == prefer_n:
            min_fill = 0.55
        elif n > guess:
            min_fill = 0.96
        elif n <= 4:
            min_fill = 0.92
        elif n <= 6:
            min_fill = 0.80
        else:
            min_fill = 0.72
        if abs(n * n - len(occ)) <= max(3, n):
            min_fill = min(min_fill, 0.68)
        for r0 in range(r_min, r_max - n + 2):
            for c0 in range(c_min, c_max - n + 2):
                hits = []
                for dr in range(n):
                    for dc in range(n):
                        cell = occ.get((r0 + dr, c0 + dc))
                        if cell is not None:
                            hits.append((dr, dc, cell))
                fill = len(hits) / float(n * n)
                if fill < min_fill:
                    continue
                # prefer_n 命中时优先于更大但无关的 N
                prefer_bonus = 1 if (prefer_n is not None and n == prefer_n) else 0
                key = (prefer_bonus, n, fill, len(hits))
                if best_key is not None and key <= best_key:
                    continue
                if hits:
                    ox = float(np.median([cell[0] + cell[2] / 2.0 - dc * pitch_x for _, dc, cell in hits]))
                    oy = float(np.median([cell[1] + cell[3] / 2.0 - dr * pitch_y for dr, _, cell in hits]))
                else:
                    ox, oy = origin_x + c0 * pitch_x, origin_y + r0 * pitch_y
                hit_map = {(dr, dc): cell for dr, dc, cell in hits}
                cells = []
                for dr in range(n):
                    row_cells = []
                    for dc in range(n):
                        cell = hit_map.get((dr, dc))
                        if cell is None:
                            cell = _synthesize_cell(dr, dc, ox, oy, pitch_x, pitch_y, side)
                        row_cells.append(cell)
                    cells.append(row_cells)
                best_key = key
                best = {
                    "cells": cells,
                    "side": side,
                    "pitch_x": pitch_x,
                    "pitch_y": pitch_y,
                    "n": n,
                    "fill": fill,
                }
        if best is not None and prefer_n is not None and best["n"] == prefer_n and best["fill"] >= 0.6:
            break
        if best is not None and prefer_n is None and best["n"] == n and best["fill"] >= 0.9:
            break
    return best


def diagnose_squares(squares):
    if not squares:
        return "无"
    parts = []
    for group in group_by_size(squares):
        side = float(np.median([(s[2] + s[3]) / 2.0 for s in group]))
        parts.append("%d个≈%.0fpx" % (len(group), side))
    return "；".join(parts)


def choose_lattice(squares, prefer_n=None):
    best = None
    best_score = -1.0
    for group in group_by_size(squares):
        found = lattice_from_squares(group, prefer_n=prefer_n)
        if found is None:
            continue
        fill = float(found.get("fill", 1.0))
        score = found["n"] * found["n"] * found["side"] * found["side"] * fill
        expected = found["n"] * found["n"]
        closeness = 1.0 - min(1.0, abs(len(group) - expected) / float(expected))
        score *= 0.6 + 0.4 * closeness
        if prefer_n is not None and found["n"] == prefer_n:
            score *= 4.0
        if score > best_score:
            best_score = score
            best = found
    return best


def runs_on_axis(mask, axis, min_len):
    density = mask.mean(axis=axis)
    active = density > 0.35
    ranges = []
    start = None
    for i, flag in enumerate(active):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            if i - start >= min_len:
                ranges.append((start, i))
            start = None
    if start is not None and len(active) - start >= min_len:
        ranges.append((start, len(active)))
    return ranges


def slice_board(mask, lattice):
    """在棋盘外接矩形里，按背景缝把每一格切出来。切分数量必须和方阵一致。"""
    cells = lattice["cells"]
    n = lattice["n"]
    xs = [cell[0] for row in cells for cell in row]
    ys = [cell[1] for row in cells for cell in row]
    rights = [cell[0] + cell[2] for row in cells for cell in row]
    bottoms = [cell[1] + cell[3] for row in cells for cell in row]
    x0 = max(0, min(xs) - 2)
    y0 = max(0, min(ys) - 2)
    x1 = min(mask.shape[1], max(rights) + 2)
    y1 = min(mask.shape[0], max(bottoms) + 2)
    roi = mask[y0:y1, x0:x1]
    min_len = max(8, int(lattice["side"] * 0.45))
    col_runs = runs_on_axis(roi, axis=0, min_len=min_len)
    row_runs = runs_on_axis(roi, axis=1, min_len=min_len)
    if len(col_runs) != n or len(row_runs) != n:
        return None
    sliced = []
    for top, bottom in row_runs:
        row = []
        for left, right in col_runs:
            row.append((x0 + left, y0 + top, right - left, bottom - top))
        sliced.append(row)
    return sliced


def _patch_median(img, x0, y0, x1, y1):
    if x1 <= x0 or y1 <= y0:
        return None
    patch = img[y0:y1, x0:x1]
    if patch.size == 0:
        return None
    return np.median(patch.reshape(-1, 3), axis=0).astype(np.float32)


def _is_mark_color(bgr):
    """白叉/高亮标记：很亮且几乎没有色度。"""
    pixel = np.array([[bgr]], dtype=np.uint8)
    lightness, a, b = cv2.cvtColor(pixel, cv2.COLOR_BGR2LAB)[0, 0].astype(np.float32)
    chroma = math.hypot(float(a) - 128.0, float(b) - 128.0)
    return lightness > 200 and chroma < 25


def sample_bgr(img, box):
    """取格子底色。优先边缘，并丢掉白叉等高亮采样，方便局中识别。"""
    x, y, w, h = box
    patches = [
        (x + int(w * 0.34), y + int(h * 0.06), x + int(w * 0.66), y + max(int(h * 0.06) + 1, int(h * 0.18))),
        (x + int(w * 0.06), y + int(h * 0.34), x + max(int(w * 0.06) + 1, int(w * 0.18)), y + int(h * 0.66)),
        (x + int(w * 0.82), y + int(h * 0.34), x + max(int(w * 0.82) + 1, int(w * 0.94)), y + int(h * 0.66)),
        (x + int(w * 0.34), y + int(h * 0.82), x + int(w * 0.66), y + max(int(h * 0.82) + 1, int(h * 0.94))),
        (x + int(w * 0.30), y + int(h * 0.30), x + max(int(w * 0.30) + 1, int(w * 0.70)), y + max(int(h * 0.30) + 1, int(h * 0.70))),
    ]
    colors = []
    for x0, y0, x1, y1 in patches:
        color = _patch_median(img, x0, y0, x1, y1)
        if color is None or _is_mark_color(color):
            continue
        colors.append(color)
    if not colors:
        return _patch_median(img, x, y, x + w, y + h)
    return np.median(np.stack(colors, axis=0), axis=0).astype(np.float32)


def cell_is_marked(img, box):
    """格子中间如果盖了猫或叉，会和边缘底色差很多。没标记时两边几乎一样。"""
    x, y, w, h = box
    edge = _patch_median(
        img,
        x + int(w * 0.34),
        y + int(h * 0.06),
        x + int(w * 0.66),
        y + max(int(h * 0.06) + 1, int(h * 0.20)),
    )
    center = _patch_median(
        img,
        x + int(w * 0.30),
        y + int(h * 0.30),
        x + int(w * 0.70),
        y + int(h * 0.70),
    )
    if edge is None or center is None:
        return False
    if _is_mark_color(center):
        return True
    return float(np.linalg.norm(center - edge)) > 32.0


def board_has_marks(img, board):
    for row in board:
        for box in row:
            if cell_is_marked(img, box):
                return True
    return False


def _bgr_to_lab_arr(samples):
    labs = []
    for sample in samples:
        pixel = np.clip(sample, 0, 255).astype(np.uint8).reshape(1, 1, 3)
        lab = cv2.cvtColor(pixel, cv2.COLOR_BGR2LAB)[0, 0].astype(np.float32)
        labs.append(lab)
    return np.asarray(labs, dtype=np.float32)


def cluster_colors(samples, n=None):
    """颜色聚类。

    棋盘是 N×N 时：只合并上下左右相邻的格子，按色区中心色差从小到大
    合并成恰好 N 块连通色区（符合「一种颜色一块连通区域」）。
    """
    count = len(samples)
    labs = _bgr_to_lab_arr(samples)

    parent = list(range(count))
    size = [1] * count
    sum_lab = [labs[i].copy() for i in range(count)]

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def unite(ri, rj):
        ri, rj = find(ri), find(rj)
        if ri == rj:
            return False
        if size[ri] < size[rj]:
            ri, rj = rj, ri
        parent[rj] = ri
        size[ri] += size[rj]
        sum_lab[ri] = sum_lab[ri] + sum_lab[rj]
        return True

    if n is None:
        pairs = []
        for i in range(count):
            for j in range(i + 1, count):
                pairs.append((float(np.linalg.norm(labs[i] - labs[j])), i, j))
        pairs.sort()
        for dist, i, j in pairs:
            if dist >= 20.0:
                break
            unite(i, j)
    else:
        side = int(round(math.sqrt(count)))
        if side * side != count:
            raise RuntimeError("样本数 %d 不是完全平方，无法按棋盘聚类" % count)
        clusters = count
        while clusters > n:
            best_d = None
            best_pair = None
            for r in range(side):
                for c in range(side):
                    i = r * side + c
                    for dr, dc in ((0, 1), (1, 0)):
                        rr, cc = r + dr, c + dc
                        if rr >= side or cc >= side:
                            continue
                        j = rr * side + cc
                        ri, rj = find(i), find(j)
                        if ri == rj:
                            continue
                        mi = sum_lab[ri] / float(size[ri])
                        mj = sum_lab[rj] / float(size[rj])
                        dist = float(np.linalg.norm(mi - mj))
                        if best_d is None or dist < best_d:
                            best_d = dist
                            best_pair = (ri, rj)
            if best_pair is None:
                break
            if unite(best_pair[0], best_pair[1]):
                clusters -= 1

    root_to_id = {}
    labels = np.zeros(count, dtype=int)
    for i in range(count):
        root = find(i)
        if root not in root_to_id:
            root_to_id[root] = len(root_to_id)
        labels[i] = root_to_id[root]

    bgr_sums = [np.zeros(3, dtype=np.float64) for _ in root_to_id]
    bgr_nums = [0] * len(root_to_id)
    for i, sample in enumerate(samples):
        idx = int(labels[i])
        bgr_sums[idx] += np.asarray(sample, dtype=np.float64)
        bgr_nums[idx] += 1
    means = [(bgr_sums[i] / float(bgr_nums[i])).astype(np.float32) for i in range(len(root_to_id))]
    return labels, means


def color_name(bgr):
    pixel = np.array([[bgr]], dtype=np.uint8)
    lightness, a, b = cv2.cvtColor(pixel, cv2.COLOR_BGR2LAB)[0, 0].astype(np.float32)
    a -= 128.0
    b -= 128.0
    chroma = math.hypot(float(a), float(b))
    if chroma < 18:
        if lightness > 200:
            return "白"
        if lightness < 70:
            return "黑"
        return "灰"
    hue = math.degrees(math.atan2(float(b), float(a)))
    if hue < -70:
        return "蓝"
    if hue < -15:
        name = "紫"
    elif hue < 18:
        name = "红"
    elif hue < 70:
        name = "橙"
    elif hue < 105:
        name = "黄"
    elif hue < 165:
        name = "绿"
    else:
        name = "青"
    if name in ("紫", "红") and lightness > 175:
        return "粉"
    return name


def unique_names(means):
    base = [color_name(m) for m in means]
    used = {}
    for name in base:
        used[name] = used.get(name, 0) + 1
    seen = {}
    names = []
    for name in base:
        if used[name] == 1:
            names.append(name)
            continue
        seen[name] = seen.get(name, 0) + 1
        names.append("%s%d" % (name, seen[name]))
    return names


def draw_recognition(img, board, names, labels):
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    base = Image.fromarray(rgb).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    side = board[0][0][2]
    font = load_font(max(12, int(side * 0.28)))
    width = max(2, int(side * 0.04))
    n = len(board)
    for r in range(n):
        for c in range(n):
            x, y, w, h = board[r][c]
            draw.rectangle((x, y, x + w - 1, y + h - 1), outline=(255, 255, 255, 230), width=width)
            draw.rectangle(
                (x + width, y + width, x + w - 1 - width, y + h - 1 - width),
                outline=(20, 90, 220, 255),
                width=max(1, width // 2),
            )
            text = names[int(labels[r, c])]
            cx = x + w / 2.0
            cy = y + h / 2.0
            bbox = draw.textbbox((0, 0), text, font=font, anchor="mm")
            pad = max(2, int(side * 0.04))
            draw.rectangle(
                (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad),
                fill=(255, 255, 255, 210),
            )
            draw.text((cx, cy), text, font=font, fill=(20, 40, 80, 255), anchor="mm")
    out = Image.alpha_composite(base, overlay).convert("RGB")
    return cv2.cvtColor(np.array(out), cv2.COLOR_RGB2BGR)


def _lab_dist_bgr(a, b):
    pa = np.clip(a, 0, 255).astype(np.uint8).reshape(1, 1, 3)
    pb = np.clip(b, 0, 255).astype(np.uint8).reshape(1, 1, 3)
    la = cv2.cvtColor(pa, cv2.COLOR_BGR2LAB)[0, 0].astype(np.float32)
    lb = cv2.cvtColor(pb, cv2.COLOR_BGR2LAB)[0, 0].astype(np.float32)
    return float(np.linalg.norm(la - lb))


def _cell_is_inactive(sample, bg):
    """顶栏/背景缝误检出来的格：接近背景，或几乎是白。"""
    if sample is None:
        return True
    if _is_mark_color(sample):
        return True
    if _lab_dist_bgr(sample, bg) < 18.0:
        return True
    pixel = np.clip(sample, 0, 255).astype(np.uint8).reshape(1, 1, 3)
    lightness, a, b = cv2.cvtColor(pixel, cv2.COLOR_BGR2LAB)[0, 0].astype(np.float32)
    chroma = math.hypot(float(a) - 128.0, float(b) - 128.0)
    return lightness > 215 and chroma < 22


def prune_ghost_lines(board, side):
    """丢掉明显偏瘦的鬼行/鬼列（间距估小一半时会出现）。"""
    n = len(board)
    if n < 3 or any(len(row) != n for row in board):
        return board
    keep_cols = [c for c in range(n) if float(np.median([board[r][c][2] for r in range(n)])) >= side * 0.58]
    keep_rows = [r for r in range(n) if float(np.median([board[r][c][3] for c in range(n)])) >= side * 0.58]
    if len(keep_rows) < 3 or len(keep_cols) < 3:
        return board
    if len(keep_rows) == len(keep_cols):
        return [[board[r][c] for c in keep_cols] for r in keep_rows]
    rows = list(range(n))
    cols = list(range(n))
    while len(rows) > 3 and len(cols) > 3:
        changed = False
        if rows[0] not in keep_rows:
            rows = rows[1:]
            changed = True
        elif rows[-1] not in keep_rows:
            rows = rows[:-1]
            changed = True
        if cols[0] not in keep_cols:
            cols = cols[1:]
            changed = True
        elif cols[-1] not in keep_cols:
            cols = cols[:-1]
            changed = True
        if not changed:
            break
        m = min(len(rows), len(cols))
        rows, cols = rows[:m], cols[:m]
    if len(rows) == len(cols) and 3 <= len(rows) < n:
        return [[board[r][c] for c in cols] for r in rows]
    return board


def trim_inactive_borders(img, board, bg):
    """去掉整行/整列都像背景的边（常见于顶栏被吃进棋盘）。"""
    if not board or not board[0]:
        return board
    cells = [list(row) for row in board]
    width = len(cells[0])
    if width < 1 or any(len(row) != width for row in cells):
        return board
    changed = True
    while changed and len(cells) >= 4 and width >= 4:
        changed = False
        height = len(cells)
        for row_idx in (0, height - 1):
            if len(cells[row_idx]) != width:
                return board
            samples = [sample_bgr(img, cells[row_idx][c]) for c in range(width)]
            inactive = sum(1 for s in samples if _cell_is_inactive(s, bg))
            if inactive >= max(2, int(math.ceil(width * 0.55))):
                cells = cells[1:] if row_idx == 0 else cells[:-1]
                changed = True
                break
        if changed:
            continue
        height = len(cells)
        for col_idx in (0, width - 1):
            if any(len(row) <= col_idx for row in cells):
                return board
            samples = [sample_bgr(img, cells[r][col_idx]) for r in range(height)]
            inactive = sum(1 for s in samples if _cell_is_inactive(s, bg))
            if inactive >= max(2, int(math.ceil(height * 0.55))):
                cells = [row[1:] if col_idx == 0 else row[:-1] for row in cells]
                width = len(cells[0]) if cells else 0
                if any(len(row) != width for row in cells):
                    return board
                changed = True
                break
    return cells


_PROGRESS_DIGIT_CACHE = {}
_PROGRESS_FONTS = (
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
)


def _progress_fraction_roi(img):
    """尽量裁出进度「x/N」：优先跟绿色分子数字（非整块色格），否则跟进度条右侧。"""
    h, w = img.shape[:2]
    y0, y1 = int(h * 0.12), int(h * 0.42)
    band = img[y0:y1, 0:int(w * 0.72)]
    if band.size == 0:
        return None
    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
    # 分子常是绿色；棋盘色块也绿，必须按「小数字」尺寸过滤，不能取最大面积。
    green = cv2.inRange(hsv, (35, 40, 40), (100, 255, 255))
    green = cv2.morphologyEx(green, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(green, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_h = max(12, int(h * 0.010))
    max_h = max(36, int(h * 0.026))
    digit_blobs = []
    for contour in contours:
        x, y, bw, bh = cv2.boundingRect(contour)
        area = float(cv2.contourArea(contour))
        if bh < min_h or bh > max_h:
            continue
        aspect = bw / float(bh)
        if aspect < 0.30 or aspect > 1.20:
            continue
        if area < 25 or area > bh * bh * 1.05:
            continue
        if y < band.shape[0] * 0.12:
            continue
        score = -abs(aspect - 0.68) * 4.0 - abs(area - bh * bh * 0.55) / (bh * bh + 1.0)
        digit_blobs.append((score, x, y, bw, bh))
    if digit_blobs:
        digit_blobs.sort(reverse=True)
        _, x, y, bw, bh = digit_blobs[0]
        pad = max(4, bh // 4)
        fx0 = max(0, x - pad)
        fx1 = min(band.shape[1], x + bw + int(bh * 3.4))
        fy0 = max(0, y - pad)
        fy1 = min(band.shape[0], y + bh + pad)
        roi = band[fy0:fy1, fx0:fx1]
        if roi.shape[1] >= 24 and roi.shape[0] >= 14:
            return roi

    # 退回：浅色细长进度槽右侧（排除整块浅色背景）。
    light = cv2.inRange(hsv, (0, 0, 170), (180, 80, 255))
    light = cv2.morphologyEx(light, cv2.MORPH_CLOSE, np.ones((3, 9), np.uint8))
    contours, _ = cv2.findContours(light, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    for contour in contours:
        x, y, bw, bh = cv2.boundingRect(contour)
        if bh < min_h or bh > max_h * 1.6:
            continue
        if bw < bh * 3.5 or bw > band.shape[1] * 0.55:
            continue
        if y < band.shape[0] * 0.12:
            continue
        score = float(bw) / (abs(bh - max_h * 0.7) + 1.0)
        if best is None or score > best[0]:
            best = (score, x, y, bw, bh)
    if best is None:
        return None
    _, x, y, bw, bh = best
    pad_y = max(4, bh // 2)
    fx0 = min(band.shape[1] - 4, x + bw + 1)
    fx1 = min(band.shape[1], x + bw + max(int(bh * 4.5), 48))
    fy0 = max(0, y - pad_y)
    fy1 = min(band.shape[0], y + bh + pad_y)
    return band[fy0:fy1, fx0:fx1]


def _render_progress_digit(digit, height, font_path):
    key = (digit, height, font_path)
    cached = _PROGRESS_DIGIT_CACHE.get(key)
    if cached is not None:
        return cached
    font = ImageFont.truetype(font_path, height)
    probe = Image.new("L", (8, 8), 255)
    draw = ImageDraw.Draw(probe)
    text = str(digit)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    canvas = Image.new("L", (max(tw + 6, height // 2), max(th + 6, height)), 255)
    draw = ImageDraw.Draw(canvas)
    draw.text((3 - bbox[0], 3 - bbox[1]), text, font=font, fill=0)
    arr = np.array(canvas)
    _PROGRESS_DIGIT_CACHE[key] = arr
    return arr


def _digit_to_black_on_white(crop):
    img = crop.copy().astype(np.uint8)
    border = np.concatenate([img[0, :], img[-1, :], img[:, 0], img[:, -1]])
    if float(border.mean()) < 127:
        img = 255 - img
    _, img = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if float(img.mean()) < 127:
        img = 255 - img
    return img


def _digit_hole_count(black_digit):
    mask = (black_digit < 128).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return 0
    return sum(1 for i in range(len(contours)) if hierarchy[0][i][3] >= 0)


def _digit_from_holes(black_digit):
    h0 = black_digit.shape[0]
    holes = _digit_hole_count(black_digit)
    top_ink = float((black_digit[: h0 // 2, :] < 128).mean())
    bot_ink = float((black_digit[h0 // 2 :, :] < 128).mean())
    if holes >= 2:
        return 8
    if holes == 1:
        if top_ink > bot_ink * 1.18:
            return 9
        if bot_ink > top_ink * 1.18:
            return 6
        return 0
    return None


def _classify_progress_digit(crop):
    src = _digit_to_black_on_white(crop)
    h0, w0 = src.shape[:2]
    aspect = w0 / float(h0)
    holes = _digit_hole_count(src)
    if aspect <= 0.42 and holes == 0:
        return 1

    best_d, best_s = None, -1.0
    for font_path in _PROGRESS_FONTS:
        if not Path(font_path).exists():
            continue
        for thr_h in (max(14, h0 - 2), h0, h0 + 4, h0 + 8):
            for digit in range(10):
                try:
                    templ = _render_progress_digit(digit, thr_h, font_path)
                except Exception:
                    continue
                scale = thr_h / float(h0)
                nw = max(3, int(round(w0 * scale)))
                resized = cv2.resize(src, (nw, thr_h), interpolation=cv2.INTER_AREA)
                _, resized = cv2.threshold(resized, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                if float(resized.mean()) < 127:
                    resized = 255 - resized
                pad = cv2.copyMakeBorder(resized, 8, 8, 8, 8, cv2.BORDER_CONSTANT, value=255)
                th, tw = templ.shape
                if pad.shape[0] < th + 1 or pad.shape[1] < tw + 1:
                    continue
                score = float(cv2.matchTemplate(pad, templ, cv2.TM_CCOEFF_NORMED).max())
                if score > best_s:
                    best_s, best_d = score, digit

    hole_guess = _digit_from_holes(src)
    no_hole = {1, 2, 3, 5, 7}
    if hole_guess is not None:
        if best_d is None or best_s < 0.72 or best_d in no_hole:
            return hole_guess
        if best_d != hole_guess and best_s < 0.85:
            return hole_guess
    if holes == 0 and best_d in {0, 4, 6, 8, 9} and best_s < 0.75 and aspect <= 0.45:
        return 1
    if best_d is None:
        return hole_guess
    return best_d


def _progress_digit_components(roi):
    """从进度 ROI 里取出斜杠右侧的数字连通域（左到右）。"""
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    green = cv2.inRange(hsv, (35, 40, 40), (100, 255, 255))
    gcontours, _ = cv2.findContours(green, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    gx = 0
    has_green = False
    for contour in gcontours:
        x, y, bw, bh = cv2.boundingRect(contour)
        if bh >= 12:
            gx = max(gx, x + bw)
            has_green = True
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    right = gray[:, max(0, gx - 2) :] if has_green else gray
    ink = ((right < 175) * 255).astype(np.uint8)
    ink = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    contours, _ = cv2.findContours(ink, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    items = []
    for contour in contours:
        x, y, bw, bh = cv2.boundingRect(contour)
        area = float(cv2.contourArea(contour))
        if bh < right.shape[0] * 0.40 or area < 10:
            continue
        if x > right.shape[1] * 0.72:
            continue
        items.append((x, ink[y : y + bh, x : x + bw], bw, bh, area))
    # 只按 x 排序；crop 是 ndarray，整元组比较会在非游戏画面炸。
    items.sort(key=lambda t: t[0])
    if not items:
        return []
    if has_green:
        # 绿色分子右侧：第一块通常是「/」
        return [crop for _, crop, _, _, _ in items[1:3]]
    # 无绿色分子：找细斜杠，取其右侧数字
    slash_idx = None
    for i, (x, crop, bw, bh, area) in enumerate(items):
        aspect = bw / float(bh)
        fill = area / float(bw * bh + 1)
        if aspect < 0.55 and fill < 0.40:
            slash_idx = i
            break
    if slash_idx is None:
        return []
    return [crop for _, crop, _, _, _ in items[slash_idx + 1 : slash_idx + 3]]


def read_board_size_n(img):
    """从进度「已找到/总猫数」读分母，得到棋盘边长 N（每行每列各一只猫）。读不到返回 None。"""
    try:
        roi = _progress_fraction_roi(img)
        if roi is None or roi.size == 0:
            return None
        crops = _progress_digit_components(roi)
        if not crops:
            return None
        digits = []
        for crop in crops:
            digit = _classify_progress_digit(crop)
            if digit is None:
                continue
            digits.append(int(digit))
        if not digits:
            return None
        if len(digits) == 1:
            n = digits[0]
        else:
            n = digits[0] * 10 + digits[1]
        if n < 3 or n > 20:
            return None
        return n
    except Exception:
        # 非游戏画面/杂乱 ROI 时安静失败，避免拖垮识别线程。
        return None


def is_celebration_screen(img):
    """过关动画：半透明暗罩 + 中央花环/彩纸，不能当新棋盘。"""
    if img is None or img.size == 0:
        return False
    h, w = img.shape[:2]
    if h < 80 or w < 80:
        return False
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mean_l = float(gray.mean())
    # 正常对局画面整体很亮（约 180~210）；过关暗罩通常压到很低。
    if mean_l >= 120:
        return False
    cy0, cy1 = int(h * 0.20), int(h * 0.80)
    cx0, cx1 = int(w * 0.10), int(w * 0.90)
    center = img[cy0:cy1, cx0:cx1]
    hsv = cv2.cvtColor(center, cv2.COLOR_BGR2HSV)
    dark = cv2.inRange(hsv, (0, 0, 0), (180, 255, 110))
    dark_ratio = float(cv2.countNonZero(dark)) / float(dark.size)
    # 暗罩覆盖中央大片；再叠加一点点金/彩也能确认。
    if mean_l < 100 and dark_ratio > 0.35:
        return True
    if mean_l < 130 and dark_ratio > 0.55:
        return True
    return False


def peek_board(img):
    """轻量探测：画面里有没有完整方阵棋盘。没有则返回 None，不做颜色聚类。"""
    if is_celebration_screen(img):
        return None
    work = neutralize_dark_banner(img)
    target_n = read_board_size_n(work)
    bg = estimate_background(work)
    mask = foreground_mask(work)
    squares = find_squares(mask)
    # 进度 N 与方块数量冲突时：差 1（常见 10 读成 11）且方块数接近完全平方，信格点。
    if target_n is not None and squares:
        group = max(group_by_size(squares), key=len)
        guess = int(round(math.sqrt(len(group))))
        if guess >= 4:
            if abs(guess - target_n) >= 3:
                target_n = None
            elif abs(guess - target_n) >= 1 and abs(len(group) - guess * guess) <= guess:
                target_n = None
    lattice = choose_lattice(squares, prefer_n=target_n)
    if lattice is None and target_n is not None:
        lattice = choose_lattice(squares, prefer_n=None)
    if lattice is None:
        return None
    board = slice_board(mask, lattice)
    sliced = board is not None
    if board is None:
        board = lattice["cells"]
    pruned = prune_ghost_lines(board, lattice["side"])
    trimmed = trim_inactive_borders(work, pruned, bg)
    if target_n is not None:
        if len(trimmed) == target_n and all(len(row) == target_n for row in trimmed):
            board = trimmed
        elif len(pruned) == target_n and all(len(row) == target_n for row in pruned):
            board = pruned
        elif lattice["n"] == target_n:
            board = lattice["cells"]
            sliced = False
        else:
            # 进度与格子对不上：退回无 prefer 的结果。
            lattice = choose_lattice(squares, prefer_n=None)
            if lattice is None:
                return None
            board = slice_board(mask, lattice) or lattice["cells"]
            board = trim_inactive_borders(work, prune_ghost_lines(board, lattice["side"]), bg)
            sliced = True
            target_n = None
    else:
        board = trimmed

    n = len(board)
    if n < 3 or any(len(row) != n for row in board):
        return None
    if target_n is not None and n != target_n:
        return None
    samples = [sample_bgr(work, board[r][c]) for r in range(n) for c in range(n)]
    if any(s is None for s in samples):
        return None
    return {
        "img": img,
        "work": work,
        "bg": bg,
        "mask": mask,
        "board": board,
        "samples": samples,
        "n": n,
        "side": lattice["side"],
        "pitch_x": lattice["pitch_x"],
        "pitch_y": lattice["pitch_y"],
        "sliced": sliced,
        "target_n": target_n,
    }


def finish_recognize(peek):
    """在 peek_board 结果上做颜色聚类，得到可解密的识别结果。"""
    n = peek["n"]
    samples = peek["samples"]
    labels, means = cluster_colors(samples, n=n)
    labels = labels.reshape(n, n)
    names = unique_names(means)
    if len(names) != n:
        raise RuntimeError("颜色数是 %d，棋盘是 %d×%d，对不上" % (len(names), n, n))
    return {
        "img": peek["img"],
        "board": peek["board"],
        "labels": labels,
        "names": names,
        "samples": samples,
        "n": n,
        "side": peek["side"],
        "pitch_x": peek["pitch_x"],
        "pitch_y": peek["pitch_y"],
        "sliced": peek["sliced"],
        "color_count": len(names),
    }


def recognize_bgr(img):
    peek = peek_board(img)
    if peek is None:
        work = neutralize_dark_banner(img)
        mask = foreground_mask(work)
        squares = find_squares(mask)
        raise RuntimeError(
            "没有找到完整棋盘，检测到的方块有 %d 个（分组: %s）"
            % (len(squares), diagnose_squares(squares))
        )
    return finish_recognize(peek)


def same_board_colors(samples_a, n_a, samples_b, n_b, thr=28.0, min_ratio=0.62):
    """两帧是不是同一局棋盘：尺寸相同，且多数格子底色接近。"""
    if samples_a is None or samples_b is None:
        return False
    if n_a != n_b or len(samples_a) != len(samples_b) or n_a * n_a != len(samples_a):
        return False
    close = 0
    for a, b in zip(samples_a, samples_b):
        if _lab_dist_bgr(a, b) < thr:
            close += 1
    return close >= int(math.ceil(len(samples_a) * min_ratio))


def recognize(path):
    return recognize_bgr(load_bgr(path))


def solve_all(labels):
    """找出所有满足规则的摆法。超过 MAX_SCHEMES 套时停止，并标明没有列全。"""
    n = int(labels.shape[0])
    solutions = []
    truncated = False

    def search(row, used_cols, used_colors, prev_col, path):
        nonlocal truncated
        if truncated:
            return
        if row == n:
            solutions.append(list(path))
            if len(solutions) > MAX_SCHEMES:
                solutions.pop()
                truncated = True
            return
        for col in range(n):
            if used_cols & (1 << col):
                continue
            if prev_col is not None and abs(col - prev_col) <= 1:
                continue
            color = int(labels[row, col])
            if used_colors & (1 << color):
                continue
            path.append((row, col))
            search(row + 1, used_cols | (1 << col), used_colors | (1 << color), col, path)
            path.pop()

    search(0, 0, 0, None, [])
    return solutions, truncated


def scheme_color(index):
    return SCHEME_COLORS[index % len(SCHEME_COLORS)]


def draw_badge(draw, cx, cy, radius, color, text, font):
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        fill=color + (235,),
        outline=(255, 255, 255, 255),
        width=max(2, int(radius * 0.16)),
    )
    draw.text((cx, cy), text, font=font, fill=(255, 255, 255, 255), anchor="mm")


def draw_solutions(img, board, solutions):
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    base = Image.fromarray(rgb).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    side = min(board[0][0][2], board[0][0][3])
    n = len(board)
    owners = [[[] for _ in range(n)] for _ in range(n)]
    for index, cats in enumerate(solutions):
        for row, col in cats:
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

    legend_font = load_font(max(16, int(side * 0.18)))
    board_bottom = max(cell[1] + cell[3] for row in board for cell in row)
    board_left = min(cell[0] for row in board for cell in row)
    lx = board_left
    ly = board_bottom + int(side * 0.18)
    if len(solutions) == 1:
        pieces = ["唯一方案"]
    else:
        pieces = ["方案%d" % (i + 1) for i in range(len(solutions))]
    for index, text in enumerate(pieces):
        color = scheme_color(0 if len(solutions) == 1 else index)
        radius = max(8, int(side * 0.09))
        draw_badge(draw, lx + radius, ly + radius, radius, color, "" if len(solutions) == 1 else str(index + 1), legend_font)
        draw.text((lx + radius * 2 + 6, ly + radius), text, font=legend_font, fill=color + (255,), anchor="lm")
        bbox = draw.textbbox((lx + radius * 2 + 6, ly + radius), text, font=legend_font, anchor="lm")
        lx = bbox[2] + int(side * 0.16)

    out = Image.alpha_composite(base, overlay).convert("RGB")
    return cv2.cvtColor(np.array(out), cv2.COLOR_RGB2BGR)


def format_scheme(cats):
    return "、".join("第%d行第%d列" % (row + 1, col + 1) for row, col in cats)


def report(path, result):
    n = result["n"]
    labels = result["labels"]
    names = result["names"]
    print("文件: %s" % path)
    print("图像: %d × %d" % (result["img"].shape[1], result["img"].shape[0]))
    print("棋盘: %d × %d" % (n, n))
    print("格子边长: %.0f 像素" % result["side"])
    print("列间距: %.0f 像素，行间距: %.0f 像素" % (result["pitch_x"], result["pitch_y"]))
    if result["sliced"]:
        print("切分: 按格子之间的缝切开")
    else:
        print("切分: 缝太浅，改用方块轮廓的外接矩形")
    print("颜色: %d 种" % result["color_count"])
    for r in range(n):
        print("  " + " ".join(names[int(labels[r, c])] for c in range(n)))
    out_path = Path(path).with_name(Path(path).stem + "_识别.jpg")
    save_bgr(out_path, draw_recognition(result["img"], result["board"], names, labels))
    print("已标出格子: %s" % out_path.name)

    solutions, truncated = solve_all(labels)
    if not solutions:
        print("没有满足规则的摆法。")
        return
    if truncated:
        print("至少有 %d 套方案，图上只标前 %d 套。" % (MAX_SCHEMES + 1, MAX_SCHEMES))
    elif len(solutions) == 1:
        print("方案: 1 套")
    else:
        print("方案: %d 套" % len(solutions))
    for index, cats in enumerate(solutions, 1):
        print("  方案%d: %s" % (index, format_scheme(cats)))
    if len(solutions) > 1:
        common = set(solutions[0])
        for cats in solutions[1:]:
            common &= set(cats)
        if common:
            print("  每套都有的猫: %s" % format_scheme(sorted(common)))
        else:
            print("  没有哪一格在每套方案里都是猫。")
    solved_path = Path(path).with_name(Path(path).stem + "_解密.jpg")
    save_bgr(solved_path, draw_solutions(result["img"], result["board"], solutions))
    print("已标出方案: %s" % solved_path.name)


def main(argv):
    setup_console()
    if len(argv) < 2:
        print("用法: python solve_cats.py 截图.jpg [更多截图.jpg]")
        return 1
    code = 0
    for raw in argv[1:]:
        print("=" * 40)
        try:
            report(Path(raw), recognize(Path(raw)))
        except Exception as exc:
            print("文件: %s" % raw)
            print("失败: %s" % exc)
            code = 1
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
