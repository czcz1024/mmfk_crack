# -*- coding: utf-8 -*-
"""识别《猫猫方块》截图并解密。格子大小不写死，每张图单独测量。

解密规则：每行、每列、每种颜色恰好一只猫，猫与猫不能相邻（含斜角）。
有多套都成立的摆法时，在同一张图上用不同颜色的编号标出。

做法：
1. 用背景色把方块从画面里分离出来，找近似正方形的轮廓。
2. 按边长分组。选总面积最大、又能排成完整方阵的那一组，躲开教程小图和按钮。
3. 用这一组方块的中心距算出行距、列距，再在棋盘区域内按缝隙把每一格切出来。
4. 每格取中间颜色，色差很小的并成同一种颜色。

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


def estimate_background(img):
    h, w = img.shape[:2]
    t = max(4, min(h, w) // 80)
    strips = (img[:t, :], img[-t:, :], img[:, :t], img[:, -t:])
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
        if side > float(current) * 1.2:
            groups.append([square])
        else:
            groups[-1].append(square)
    return groups


def measure_pitch(coords, min_step):
    vals = np.sort(np.asarray(coords, dtype=np.float64))
    diffs = np.diff(vals)
    steps = diffs[diffs > min_step]
    if len(steps) == 0:
        return None
    return float(np.median(steps))


def lattice_from_squares(squares):
    """用方块中心距推出行列，只接受排满的正方形棋盘。"""
    if len(squares) < 4:
        return None
    sides = np.array([(w + h) / 2.0 for _, _, w, h in squares], dtype=np.float64)
    side = float(np.median(sides))
    cx = np.array([x + w / 2.0 for x, _, w, _ in squares])
    cy = np.array([y + h / 2.0 for _, y, _, h in squares])
    min_step = side * 0.45
    pitch_x = measure_pitch(cx, min_step)
    pitch_y = measure_pitch(cy, min_step)
    if pitch_x is None or pitch_y is None:
        return None
    col_idx = np.rint((cx - cx.min()) / pitch_x).astype(int)
    row_idx = np.rint((cy - cy.min()) / pitch_y).astype(int)
    occ = {}
    for i, square in enumerate(squares):
        key = (int(row_idx[i]), int(col_idx[i]))
        occ.setdefault(key, []).append(square)
    rows = sorted(set(k[0] for k in occ))
    cols = sorted(set(k[1] for k in occ))
    if len(rows) != len(cols) or len(rows) < 3:
        return None
    n = len(rows)
    row_map = {r: i for i, r in enumerate(rows)}
    col_map = {c: i for i, c in enumerate(cols)}
    cells = [[None] * n for _ in range(n)]
    for (r, c), hits in occ.items():
        if len(hits) != 1:
            return None
        cells[row_map[r]][col_map[c]] = hits[0]
    if any(cell is None for row in cells for cell in row):
        return None
    return {
        "cells": cells,
        "side": side,
        "pitch_x": pitch_x,
        "pitch_y": pitch_y,
        "n": n,
    }


def choose_lattice(squares):
    best = None
    best_score = -1.0
    for group in group_by_size(squares):
        found = lattice_from_squares(group)
        if found is None:
            continue
        score = found["n"] * found["side"] * found["side"]
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


def sample_bgr(img, box):
    x, y, w, h = box
    color = _patch_median(
        img,
        x + int(w * 0.3),
        y + int(h * 0.3),
        x + max(int(w * 0.3) + 1, int(w * 0.7)),
        y + max(int(h * 0.3) + 1, int(h * 0.7)),
    )
    if color is None:
        color = _patch_median(img, x, y, x + w, y + h)
    return color


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
    return float(np.linalg.norm(center - edge)) > 32.0


def board_has_marks(img, board):
    for row in board:
        for box in row:
            if cell_is_marked(img, box):
                return True
    return False


def cluster_colors(samples):
    count = len(samples)
    parent = list(range(count))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    pairs = []
    for i in range(count):
        for j in range(i + 1, count):
            dist = float(np.linalg.norm(samples[i] - samples[j]))
            pairs.append((dist, i, j))
    pairs.sort()
    for dist, i, j in pairs:
        if dist >= COLOR_MERGE_DIST:
            break
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    root_to_id = {}
    labels = np.zeros(count, dtype=int)
    sums = {}
    nums = {}
    for i, sample in enumerate(samples):
        root = find(i)
        if root not in root_to_id:
            root_to_id[root] = len(root_to_id)
            sums[root] = sample.copy()
            nums[root] = 1
        else:
            sums[root] += sample
            nums[root] += 1
        labels[i] = root_to_id[root]
    means = [None] * len(root_to_id)
    for root, idx in root_to_id.items():
        means[idx] = sums[root] / float(nums[root])
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


def recognize_bgr(img):
    mask = foreground_mask(img)
    squares = find_squares(mask)
    lattice = choose_lattice(squares)
    if lattice is None:
        raise RuntimeError("没有找到完整棋盘，检测到的方块有 %d 个" % len(squares))
    board = slice_board(mask, lattice)
    if board is None:
        board = lattice["cells"]
        sliced = False
    else:
        sliced = True
    n = len(board)
    samples = [sample_bgr(img, board[r][c]) for r in range(n) for c in range(n)]
    labels, means = cluster_colors(samples)
    labels = labels.reshape(n, n)
    names = unique_names(means)
    return {
        "img": img,
        "board": board,
        "labels": labels,
        "names": names,
        "n": n,
        "side": lattice["side"],
        "pitch_x": lattice["pitch_x"],
        "pitch_y": lattice["pitch_y"],
        "sliced": sliced,
        "color_count": len(names),
    }


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
