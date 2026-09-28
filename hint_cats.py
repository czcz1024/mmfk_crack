# -*- coding: utf-8 -*-
"""根据玩家已经标出的猫和叉，按人手推理给出下一步。

不改原来的整盘求解。格子从画面里现量，不写死分辨率。
每一格分成三种：空（还没标）、猫、叉。底色从格子边缘取，躲开中间的猫脸和白叉。

用法：
    python hint_cats.py 截图.png
    python hint_cats.py 截图.png --steps 1
"""

import itertools
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

import solve_cats as sc

EMPTY, CAT, EX = 0, 1, 2
MARK_NAME = {EMPTY: "空", CAT: "猫", EX: "叉"}


def classify_cell(img, box):
    """返回 (标记, 边缘底色)。标记是 EMPTY / CAT / EX。"""
    x, y, w, h = box
    if w < 4 or h < 4:
        return EMPTY, np.array([0, 0, 0], dtype=np.float32)

    ix0 = x + int(w * 0.18)
    iy0 = y + int(h * 0.18)
    ix1 = x + max(int(w * 0.18) + 1, int(w * 0.82))
    iy1 = y + max(int(h * 0.18) + 1, int(h * 0.82))
    patch = img[iy0:iy1, ix0:ix1]
    lab = cv2.cvtColor(patch, cv2.COLOR_BGR2LAB).astype(np.float32)
    lightness = lab[:, :, 0]
    chroma = np.hypot(lab[:, :, 1] - 128.0, lab[:, :, 2] - 128.0)
    white_ratio = float(((lightness > 190) & (chroma < 22)).mean())
    dark_ratio = float((lightness < 75).mean())
    hue = np.degrees(np.arctan2(lab[:, :, 2] - 128.0, lab[:, :, 1] - 128.0))
    orange = (chroma > 25) & (hue > 20) & (hue < 80) & (lightness > 80) & (lightness < 210)
    orange_ratio = float(orange.mean())

    # 猫脸有深色眼睛，也有橙色毛。白叉先别判，猫嘴也是白的。
    if dark_ratio > 0.015 and orange_ratio > 0.05:
        kind = CAT
    elif white_ratio > 0.18:
        kind = EX
    else:
        kind = EMPTY

    inset = max(1, int(min(w, h) * 0.04))
    thick = max(2, int(min(w, h) * 0.16))
    x0, y0 = x + inset, y + inset
    x1, y1 = x + w - inset, y + h - inset
    strips = [
        img[y0:y0 + thick, x0:x1],
        img[y1 - thick:y1, x0:x1],
        img[y0:y1, x0:x0 + thick],
        img[y0:y1, x1 - thick:x1],
    ]
    pixels = [strip.reshape(-1, 3) for strip in strips if strip.size]
    if not pixels:
        color = np.median(img[y:y + h, x:x + w].reshape(-1, 3), axis=0)
        return kind, color.astype(np.float32)
    pixels = np.concatenate(pixels, axis=0)
    pixel_lab = cv2.cvtColor(pixels.reshape(-1, 1, 3), cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(np.float32)
    pixel_chroma = np.hypot(pixel_lab[:, 1] - 128.0, pixel_lab[:, 2] - 128.0)
    keep = ~((pixel_lab[:, 0] > 190) & (pixel_chroma < 22))
    if int(keep.sum()) >= 8:
        color = np.median(pixels[keep], axis=0)
    else:
        color = np.median(pixels, axis=0)
    return kind, color.astype(np.float32)


def trim_dark_frame(img):
    """截图外圈如果是黑边，先裁掉，否则背景会被估成黑色，格子粘成一块。"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape
    if height < 40 or width < 40:
        return img, 0, 0
    center = float(np.median(gray[height // 3: 2 * height // 3, width // 3: 2 * width // 3]))
    if center < 80:
        return img, 0, 0

    def dark(values):
        return float(np.median(values)) < min(40.0, center - 50.0)

    top = 0
    while top < height // 6 and dark(gray[top]):
        top += 1
    bottom = 0
    while bottom < height // 6 and dark(gray[height - 1 - bottom]):
        bottom += 1
    left = 0
    while left < width // 6 and dark(gray[:, left]):
        left += 1
    right = 0
    while right < width // 6 and dark(gray[:, width - 1 - right]):
        right += 1
    if top + bottom >= height - 20 or left + right >= width - 20:
        return img, 0, 0
    if top == bottom == left == right == 0:
        return img, 0, 0
    y1 = height - bottom if bottom else height
    x1 = width - right if right else width
    return img[top:y1, left:x1], left, top


def recognize_board(img):
    """找出棋盘，并读出每格的底色和玩家标记。"""
    framed, origin_x, origin_y = trim_dark_frame(img)
    work = sc.neutralize_dark_banner(framed)
    mask = sc.foreground_mask(work)
    squares = sc.find_squares(mask)
    lattice = sc.choose_lattice(squares)
    if lattice is None:
        raise RuntimeError(
            "没有找到完整棋盘，检测到的方块有 %d 个（分组: %s）"
            % (len(squares), sc.diagnose_squares(squares))
        )
    local_board = sc.slice_board(mask, lattice)
    if local_board is None or len(local_board) != lattice["n"] or any(len(row) != lattice["n"] for row in local_board):
        local_board = lattice["cells"]
    board = [
        [(x + origin_x, y + origin_y, w, h) for x, y, w, h in row]
        for row in local_board
    ]
    n = len(board)
    if n < 3 or any(len(row) != n for row in board):
        raise RuntimeError("棋盘不是方阵")

    marks = np.zeros((n, n), dtype=np.int8)
    samples = []
    for row in range(n):
        for col in range(n):
            kind, color = classify_cell(work, local_board[row][col])
            marks[row, col] = kind
            samples.append(color)
    labels, means = sc.cluster_colors(samples, n=n)
    labels = labels.reshape(n, n)
    names = sc.unique_names(means)
    if len(names) != n:
        raise RuntimeError("颜色数是 %d，棋盘是 %d×%d，对不上" % (len(names), n, n))
    return {
        "img": img,
        "work": work,
        "board": board,
        "marks": marks,
        "labels": labels,
        "names": names,
        "n": n,
        "side": lattice["side"],
    }


def _cats(marks):
    found = []
    n = marks.shape[0]
    for row in range(n):
        for col in range(n):
            if marks[row, col] == CAT:
                found.append((row, col))
    return found


def contradiction(marks, labels, names=None):
    """已经标出的内容如果违反规则，返回一句原因。没有矛盾则返回 None。"""
    n = marks.shape[0]
    cats = _cats(marks)
    rows = [row for row, _ in cats]
    cols = [col for _, col in cats]
    if len(rows) != len(set(rows)):
        return "同一行有两只猫"
    if len(cols) != len(set(cols)):
        return "同一列有两只猫"
    colors = [int(labels[row, col]) for row, col in cats]
    if len(colors) != len(set(colors)):
        return "同一种颜色有两只猫"
    for i, (row, col) in enumerate(cats):
        for row2, col2 in cats[i + 1:]:
            if max(abs(row - row2), abs(col - col2)) <= 1:
                return "第%d行第%d列和第%d行第%d列的猫挨在一起" % (row + 1, col + 1, row2 + 1, col2 + 1)
    for row in range(n):
        if not np.any(marks[row] == CAT) and not np.any(marks[row] == EMPTY):
            return "第%d行没有可以放猫的格子" % (row + 1)
    for col in range(n):
        if not np.any(marks[:, col] == CAT) and not np.any(marks[:, col] == EMPTY):
            return "第%d列没有可以放猫的格子" % (col + 1)
    for color in range(n):
        cells = np.argwhere(labels == color)
        has_cat = False
        has_empty = False
        for row, col in cells:
            if marks[row, col] == CAT:
                has_cat = True
            elif marks[row, col] == EMPTY:
                has_empty = True
        if not has_cat and not has_empty:
            label = names[color] if names is not None else "颜色%d" % (color + 1)
            return "%s没有可以放猫的格子" % label
    return None


def _where(cells):
    ordered = sorted(cells)
    if len(ordered) > 8:
        return "其中 %d 格" % len(ordered)
    return "、".join("第%d行第%d列" % (row + 1, col + 1) for row, col in ordered)


def _open_of_color(marks, labels, color):
    cells = []
    n = marks.shape[0]
    for row in range(n):
        for col in range(n):
            if marks[row, col] == EMPTY and int(labels[row, col]) == int(color):
                cells.append((row, col))
    return cells


def _shape_name(cells):
    if len(cells) < 2:
        return ""
    rows = [row for row, _ in cells]
    cols = [col for _, col in cells]
    row_set = sorted(set(rows))
    col_set = sorted(set(cols))
    if len(row_set) == 1 and max(cols) - min(cols) + 1 == len(cells):
        return "%d格连成一行" % len(cells)
    if len(col_set) == 1 and max(rows) - min(rows) + 1 == len(cells):
        return "%d格连成一列" % len(cells)
    height = max(rows) - min(rows) + 1
    width = max(cols) - min(cols) + 1
    if len(cells) == 3 and height == 2 and width == 2:
        return "三格 L 形"
    if len(cells) == 4 and sorted((height, width)) == [2, 3]:
        straight = len(row_set) == 1 or len(col_set) == 1
        counts_row = [rows.count(row) for row in row_set]
        counts_col = [cols.count(col) for col in col_set]
        if 3 in counts_row or 3 in counts_col:
            return "四格 L 形"
        if not straight and counts_row == [2, 2]:
            return "S 形"
    if len(cells) == 2 and max(abs(rows[0] - rows[1]), abs(cols[0] - cols[1])) == 1:
        return "两格连在一起"
    return ""


def _forbidden(row, col, labels):
    """这一格如果是猫，哪些格子一定不能是猫。"""
    n = labels.shape[0]
    color = int(labels[row, col])
    bad = set()
    for rr in range(n):
        for cc in range(n):
            if rr == row and cc == col:
                continue
            same_line = rr == row or cc == col
            touch = max(abs(rr - row), abs(cc - col)) <= 1
            if same_line or touch or int(labels[rr, cc]) == color:
                bad.add((rr, cc))
    return bad


def common_exclusions(placements, marks, labels):
    if len(placements) < 2:
        return []
    common = None
    for row, col in placements:
        bad = _forbidden(row, col, labels)
        common = bad if common is None else (common & bad)
    place = set(placements)
    found = []
    for row, col in sorted(common or []):
        if (row, col) in place:
            continue
        if marks[row, col] != EMPTY:
            continue
        found.append((row, col))
    return found


def _color_reason(name, placements, excluded):
    shape = _shape_name(placements)
    rows = sorted({row for row, _ in placements})
    cols = sorted({col for _, col in placements})
    where = _where(excluded)
    if len(rows) == 1:
        head = "%s只出现在第%d行" % (name, rows[0] + 1)
        if shape:
            head += "，是%s" % shape
        return "%s。可以排除%s。" % (head, where)
    if len(cols) == 1:
        head = "%s只出现在第%d列" % (name, cols[0] + 1)
        if shape:
            head += "，是%s" % shape
        return "%s。可以排除%s。" % (head, where)
    if shape:
        return "%s只剩%s。不管猫放在其中哪一格，%s都会被排除。" % (name, shape, where)
    return "%s剩下 %d 格。不管猫放在哪一格，%s都会被排除。" % (name, len(placements), where)


def _line_list(indexes, unit):
    return "、".join("第%d%s" % (index + 1, unit) for index in indexes)


def _hint(reason, cells, style, focus, caption=None):
    return {
        "reason": reason,
        "cells": cells,
        "style": style,
        "focus": focus,
        "caption": caption or reason,
        "trial": False,
    }


def next_forced(marks, labels, names):
    """按从简单到复杂的顺序，找出当前能确定的一批格子。没有则返回 None。"""
    n = marks.shape[0]

    # 已经放上的猫先清场。否则下一只「只剩一格」的猫可能和它同一列。
    for row, col in _cats(marks):
        hit = []
        for rr in range(n):
            for cc in range(n):
                if marks[rr, cc] != EMPTY:
                    continue
                same = rr == row or cc == col or int(labels[rr, cc]) == int(labels[row, col])
                touch = max(abs(rr - row), abs(cc - col)) <= 1
                if same or touch:
                    hit.append((rr, cc, EX))
        if hit:
            return _hint(
                "第%d行第%d列是猫，同一行、同一列、同色，以及挨着的格子都不是猫：%s。"
                % (row + 1, col + 1, _where([(rr, cc) for rr, cc, _ in hit])),
                hit,
                "cat_clear",
                (row, col),
                "第%d行第%d列的猫，标着同一数字的格子都排除" % (row + 1, col + 1),
            )

    # 1–3. 只剩一格，就是猫。
    color_order = sorted(range(n), key=lambda color: len(_open_of_color(marks, labels, color)))
    for color in color_order:
        if np.any((labels == color) & (marks == CAT)):
            continue
        cells = _open_of_color(marks, labels, color)
        if len(cells) == 1:
            row, col = cells[0]
            return _hint(
                "%s只剩第%d行第%d列，这里是猫。" % (names[color], row + 1, col + 1),
                [(row, col, CAT)],
                "new_cat",
                (row, col),
            )
    for row in range(n):
        if np.any(marks[row] == CAT):
            continue
        cols = [col for col in range(n) if marks[row, col] == EMPTY]
        if len(cols) == 1:
            return _hint(
                "第%d行只剩第%d列还没排除，这里是猫。" % (row + 1, cols[0] + 1),
                [(row, cols[0], CAT)],
                "new_cat",
                (row, cols[0]),
            )
    for col in range(n):
        if np.any(marks[:, col] == CAT):
            continue
        rows = [row for row in range(n) if marks[row, col] == EMPTY]
        if len(rows) == 1:
            return _hint(
                "第%d列只剩第%d行还没排除，这里是猫。" % (col + 1, rows[0] + 1),
                [(rows[0], col, CAT)],
                "new_cat",
                (rows[0], col),
            )

    # 颜色只剩一块时，每种放法都会排除的格子。含整行/整列，以及连格、L、S 周围。
    for color in color_order:
        if np.any((labels == color) & (marks == CAT)):
            continue
        cells = _open_of_color(marks, labels, color)
        excluded = common_exclusions(cells, marks, labels)
        if excluded:
            return _hint(
                _color_reason(names[color], cells, excluded),
                [(row, col, EX) for row, col in excluded],
                "exclude",
                cells,
            )

    # 一行（或一列）剩下的格子如果都是同一种颜色，这种颜色在外面可以排除。
    for row in range(n):
        if np.any(marks[row] == CAT):
            continue
        cells = [(row, col) for col in range(n) if marks[row, col] == EMPTY]
        excluded = common_exclusions(cells, marks, labels)
        if not excluded:
            continue
        colors = sorted({int(labels[r, c]) for r, c in cells})
        if len(colors) == 1:
            reason = "第%d行剩下的格子都是%s，这种颜色在别的地方都不是猫：%s。" % (
                row + 1,
                names[colors[0]],
                _where(excluded),
            )
        else:
            reason = "第%d行剩下的每种放法都会排除这些格子：%s。" % (row + 1, _where(excluded))
        return _hint(reason, [(r, c, EX) for r, c in excluded], "exclude", cells)
    for col in range(n):
        if np.any(marks[:, col] == CAT):
            continue
        cells = [(row, col) for row in range(n) if marks[row, col] == EMPTY]
        excluded = common_exclusions(cells, marks, labels)
        if not excluded:
            continue
        colors = sorted({int(labels[r, c]) for r, c in cells})
        if len(colors) == 1:
            reason = "第%d列剩下的格子都是%s，这种颜色在别的地方都不是猫：%s。" % (
                col + 1,
                names[colors[0]],
                _where(excluded),
            )
        else:
            reason = "第%d列剩下的每种放法都会排除这些格子：%s。" % (col + 1, _where(excluded))
        return _hint(reason, [(r, c, EX) for r, c in excluded], "exclude", cells)

    locked = _locked_step(marks, labels, names)
    if locked is not None:
        return locked
    return None


def _locked_step(marks, labels, names):
    """k 行只用 k 种颜色，或 k 种颜色只落在 k 行里。列也一样。"""
    n = marks.shape[0]
    open_rows = [row for row in range(n) if not np.any(marks[row] == CAT)]
    open_cols = [col for col in range(n) if not np.any(marks[:, col] == CAT)]

    def row_colors(row):
        return {int(labels[row, col]) for col in range(n) if marks[row, col] == EMPTY}

    def col_colors(col):
        return {int(labels[row, col]) for row in range(n) if marks[row, col] == EMPTY}

    for k in range(2, len(open_rows)):
        for combo in itertools.combinations(open_rows, k):
            colors = set()
            for row in combo:
                colors |= row_colors(row)
            if len(colors) != k or any(len(row_colors(row)) == 0 for row in combo):
                continue
            hit = []
            for row in range(n):
                if row in combo:
                    continue
                for col in range(n):
                    if marks[row, col] != EMPTY:
                        continue
                    if int(labels[row, col]) in colors:
                        hit.append((row, col, EX))
            if hit:
                color_text = "、".join(names[color] for color in sorted(colors))
                reason = "%s这 %d 行剩下的格子只有%s。这几种颜色在这几行外面都不是猫：%s。" % (
                    _line_list(combo, "行"),
                    k,
                    color_text,
                    _where([(r, c) for r, c, _ in hit]),
                )
                focus = [
                    (row, col)
                    for row in combo
                    for col in range(n)
                    if marks[row, col] == EMPTY
                ]
                return _hint(reason, hit, "exclude", focus)

    for k in range(2, len(open_cols)):
        for combo in itertools.combinations(open_cols, k):
            colors = set()
            for col in combo:
                colors |= col_colors(col)
            if len(colors) != k or any(len(col_colors(col)) == 0 for col in combo):
                continue
            hit = []
            for col in range(n):
                if col in combo:
                    continue
                for row in range(n):
                    if marks[row, col] != EMPTY:
                        continue
                    if int(labels[row, col]) in colors:
                        hit.append((row, col, EX))
            if hit:
                color_text = "、".join(names[color] for color in sorted(colors))
                reason = "%s这 %d 列剩下的格子只有%s。这几种颜色在这几列外面都不是猫：%s。" % (
                    _line_list(combo, "列"),
                    k,
                    color_text,
                    _where([(r, c) for r, c, _ in hit]),
                )
                focus = [
                    (row, col)
                    for col in combo
                    for row in range(n)
                    if marks[row, col] == EMPTY
                ]
                return _hint(reason, hit, "exclude", focus)

    live_colors = []
    for color in range(n):
        if np.any((labels == color) & (marks == CAT)):
            continue
        if _open_of_color(marks, labels, color):
            live_colors.append(color)

    def color_rows(color):
        return {row for row, _ in _open_of_color(marks, labels, color)}

    def color_cols(color):
        return {col for _, col in _open_of_color(marks, labels, color)}

    for k in range(2, len(live_colors)):
        for combo in itertools.combinations(live_colors, k):
            rows = set()
            for color in combo:
                rows |= color_rows(color)
            if len(rows) != k or any(np.any(marks[row] == CAT) for row in rows):
                continue
            hit = []
            for row in rows:
                for col in range(n):
                    if marks[row, col] != EMPTY:
                        continue
                    if int(labels[row, col]) not in combo:
                        hit.append((row, col, EX))
            if hit:
                color_text = "、".join(names[color] for color in combo)
                reason = "%s只出现在%s。这几行里的其他颜色都不是猫：%s。" % (
                    color_text,
                    _line_list(sorted(rows), "行"),
                    _where([(r, c) for r, c, _ in hit]),
                )
                focus = [
                    (row, col)
                    for color in combo
                    for row, col in _open_of_color(marks, labels, color)
                ]
                return _hint(reason, hit, "exclude", focus)

    for k in range(2, len(live_colors)):
        for combo in itertools.combinations(live_colors, k):
            cols = set()
            for color in combo:
                cols |= color_cols(color)
            if len(cols) != k or any(np.any(marks[:, col] == CAT) for col in cols):
                continue
            hit = []
            for col in cols:
                for row in range(n):
                    if marks[row, col] != EMPTY:
                        continue
                    if int(labels[row, col]) not in combo:
                        hit.append((row, col, EX))
            if hit:
                color_text = "、".join(names[color] for color in combo)
                reason = "%s只出现在%s。这几列里的其他颜色都不是猫：%s。" % (
                    color_text,
                    _line_list(sorted(cols), "列"),
                    _where([(r, c) for r, c, _ in hit]),
                )
                focus = [
                    (row, col)
                    for color in combo
                    for row, col in _open_of_color(marks, labels, color)
                ]
                return _hint(reason, hit, "exclude", focus)
    return None


def _apply(marks, cells):
    for row, col, kind in cells:
        current = int(marks[row, col])
        if current == kind:
            continue
        if current != EMPTY:
            return "第%d行第%d列已经是%s，不能再标成%s" % (
                row + 1,
                col + 1,
                MARK_NAME[current],
                MARK_NAME[kind],
            )
        marks[row, col] = kind
    return None


def _forced_loop(marks, labels, names, limit):
    """只走必走的步骤，不试放。返回 (步骤列表, 矛盾原因)。"""
    steps = []
    for _ in range(limit):
        problem = contradiction(marks, labels, names)
        if problem:
            return steps, problem
        found = next_forced(marks, labels, names)
        if found is None:
            return steps, None
        problem = _apply(marks, found["cells"])
        if problem:
            return steps, problem
        steps.append(found)
    return steps, "步骤太多，先停下来"


def _trial_step(marks, labels, names):
    """没有必走步骤时，找一格：假设它是猫，会把某行、某列或某种颜色挤没。"""
    n = marks.shape[0]
    empties = [(row, col) for row in range(n) for col in range(n) if marks[row, col] == EMPTY]

    def sort_key(cell):
        row, col = cell
        color = int(labels[row, col])
        color_left = len(_open_of_color(marks, labels, color))
        row_left = int(np.sum(marks[row] == EMPTY))
        return (color_left, row_left, row, col)

    for row, col in sorted(empties, key=sort_key):
        trial = marks.copy()
        trial[row, col] = CAT
        _, problem = _forced_loop(trial, labels, names, limit=n * n)
        if problem:
            return _hint(
                "假设第%d行第%d列是猫，会让%s，所以这里不是猫。" % (row + 1, col + 1, problem),
                [(row, col, EX)],
                "trial",
                (row, col),
            )
    return None


def deduce(labels, marks, names, step_limit=None):
    """从玩家标记出发，一步步推出新的猫或叉。"""
    state = marks.copy()
    steps = []
    limit = step_limit if step_limit is not None else int(labels.size)
    problem = contradiction(state, labels, names)
    if problem:
        return steps, state, problem
    pending = None
    while len(steps) < limit:
        trial = False
        if pending is not None:
            found = pending
            pending = None
        else:
            found = next_forced(state, labels, names)
        if found is None:
            found = _trial_step(state, labels, names)
            trial = found is not None
        if found is None:
            break
        problem = _apply(state, found["cells"])
        if problem:
            return steps, state, problem
        step = dict(found)
        step["trial"] = trial
        if step["style"] == "new_cat" and not trial:
            nxt = next_forced(state, labels, names)
            same_cat = (
                nxt is not None
                and nxt["style"] == "cat_clear"
                and tuple(nxt["focus"]) == tuple(step["focus"])
            )
            if same_cat:
                problem = _apply(state, nxt["cells"])
                if problem:
                    return steps, state, problem
                step["cells"] = list(step["cells"]) + list(nxt["cells"])
                step["reason"] = step["reason"] + nxt["reason"]
                caption = step["caption"]
                if not caption.endswith("。"):
                    caption += "。"
                step["caption"] = caption + "标着数字的格子是这只猫排除的。"
            elif nxt is not None:
                pending = nxt
        steps.append(step)
        problem = contradiction(state, labels, names)
        if problem:
            return steps, state, problem
    return steps, state, None


def _finished(marks):
    n = marks.shape[0]
    cats = _cats(marks)
    return len(cats) == n


def format_grid(marks, labels, names):
    lines = []
    n = marks.shape[0]
    for row in range(n):
        bits = []
        for col in range(n):
            bits.append("%s%s" % (MARK_NAME[int(marks[row, col])], names[int(labels[row, col])]))
        lines.append("第%d行  %s" % (row + 1, " ".join(bits)))
    return "\n".join(lines)


# 当前这一步用同一种显眼颜色。前面已经排除的格子改成叉，不再留着数字。
ACCENT = (230, 30, 45)


def _wrap_text(text, font, max_width):
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    lines = []
    buf = ""
    for char in text:
        trial = buf + char
        if probe.textlength(trial, font=font) <= max_width:
            buf = trial
        else:
            if buf:
                lines.append(buf)
            buf = char
    if buf:
        lines.append(buf)
    return lines or [text]


def _board_crop(img, board, margin=8):
    height, width = img.shape[:2]
    x0 = min(cell[0] for row in board for cell in row) - margin
    y0 = min(cell[1] for row in board for cell in row) - margin
    x1 = max(cell[0] + cell[2] for row in board for cell in row) + margin
    y1 = max(cell[1] + cell[3] for row in board for cell in row) + margin
    x0 = max(0, int(x0))
    y0 = max(0, int(y0))
    x1 = min(width, int(x1))
    y1 = min(height, int(y1))
    crop = img[y0:y1, x0:x1]
    shifted = [
        [(x - x0, y - y0, w, h) for x, y, w, h in row]
        for row in board
    ]
    return crop, shifted


def _badge(draw, cx, cy, radius, rgb, text, font, text_rgb=(255, 255, 255)):
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        fill=rgb + (235,),
        outline=(255, 255, 255, 255),
        width=max(2, int(radius * 0.12)),
    )
    draw.text((cx, cy), text, font=font, fill=text_rgb + (255,), anchor="mm")


def _draw_x(draw, box):
    x, y, w, h = box
    pad = int(min(w, h) * 0.22)
    thick = max(4, int(min(w, h) * 0.14))
    start = (x + pad, y + pad)
    end = (x + w - pad, y + h - pad)
    start2 = (x + w - pad, y + pad)
    end2 = (x + pad, y + h - pad)
    for color, width in (((40, 40, 40, 255), thick + 5), ((255, 255, 255, 255), thick)):
        draw.line((start, end), fill=color, width=width)
        draw.line((start2, end2), fill=color, width=width)


def _panel_for_step(img, board, step, number, earlier_steps):
    """这一步用显眼颜色标新结论。前面排除过的格子只留一个叉。"""
    crop, local = _board_crop(img, board)
    base = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    side = min(local[0][0][2], local[0][0][3])
    radius = side * 0.34
    font = sc.load_font(max(14, int(radius * 1.15)))
    frame = max(3, int(side * 0.08))
    rgb = ACCENT

    def frame_cell(row, col):
        x, y, w, h = local[row][col]
        pad = 1
        draw.rounded_rectangle(
            (x + pad, y + pad, x + w - pad, y + h - pad),
            radius=max(4, int(side * 0.12)),
            outline=rgb + (255,),
            width=frame,
        )

    past_x = []
    past_cat = []
    for prev in earlier_steps:
        for row, col, kind in prev["cells"]:
            if kind == CAT:
                past_cat.append((row, col))
            else:
                past_x.append((row, col))
    for row, col in past_x:
        _draw_x(draw, local[row][col])
    for row, col in past_cat:
        x, y, w, h = local[row][col]
        _badge(draw, x + w / 2.0, y + h / 2.0, radius * 0.82, (255, 255, 255), "猫", font, (40, 40, 40))

    focus = step["focus"]
    if step["style"] in ("cat_clear", "new_cat") and not isinstance(focus, list):
        frame_cell(focus[0], focus[1])
    elif step["style"] == "exclude" and isinstance(focus, list):
        for row, col in focus:
            frame_cell(row, col)

    for row, col, kind in step["cells"]:
        x, y, w, h = local[row][col]
        text = "猫" if kind == CAT else str(number)
        _badge(draw, x + w / 2.0, y + h / 2.0, radius, rgb, text, font)

    panel = Image.alpha_composite(base, overlay).convert("RGB")
    caption = "%d. %s" % (number, step["caption"])
    cap_font = sc.load_font(max(16, int(side * 0.34)))
    lines = _wrap_text(caption, cap_font, panel.width - 16)
    line_h = int(cap_font.size * 1.35)
    cap_h = 8 + len(lines) * line_h + 6
    sheet = Image.new("RGB", (panel.width, panel.height + cap_h), (248, 244, 236))
    sheet.paste(panel, (0, 0))
    pen = ImageDraw.Draw(sheet)
    y = panel.height + 6
    for line in lines:
        pen.text((8, y), line, font=cap_font, fill=ACCENT)
        y += line_h
    return sheet


def render_steps(img, board, steps):
    """每一步单独一张图，后面的图会带上前面已经排除的叉。"""
    panels = []
    for index, step in enumerate(steps):
        panels.append(_panel_for_step(img, board, step, index + 1, steps[:index]))
    return panels


def draw_hints(img, board, steps):
    if not steps:
        return img
    panels = []
    for index, step in enumerate(steps):
        panels.append(_panel_for_step(img, board, step, index + 1, steps[:index]))
    columns = 2 if len(panels) > 1 else 1
    gap_x = 120
    gap_y = 40
    rows = (len(panels) + columns - 1) // columns
    col_w = [0] * columns
    row_h = [0] * rows
    for index, panel in enumerate(panels):
        col = index % columns
        row = index // columns
        col_w[col] = max(col_w[col], panel.width)
        row_h[row] = max(row_h[row], panel.height)
    width = sum(col_w) + gap_x * (columns + 1)
    height = sum(row_h) + gap_y * (rows + 1)
    sheet = Image.new("RGB", (width, height), (248, 244, 236))
    y = gap_y
    for row in range(rows):
        x = gap_x
        for col in range(columns):
            index = row * columns + col
            if index >= len(panels):
                break
            sheet.paste(panels[index], (x, y))
            x += col_w[col] + gap_x
        y += row_h[row] + gap_y
    return cv2.cvtColor(np.array(sheet), cv2.COLOR_RGB2BGR)


def report(path, step_limit):
    img = sc.load_bgr(path)
    found = recognize_board(img)
    marks = found["marks"]
    labels = found["labels"]
    names = found["names"]
    print("文件: %s" % path)
    print("棋盘: %d × %d，格子边长 %.0f 像素" % (found["n"], found["n"], found["side"]))
    print("玩家标记（猫/叉/空 + 颜色）:")
    print(format_grid(marks, labels, names))
    steps, final, problem = deduce(labels, marks, names, step_limit=step_limit)
    if not steps and problem:
        print("标记有矛盾：%s" % problem)
    else:
        if not steps:
            print("这一步没有能直接确定的新格子。")
        for index, step in enumerate(steps, start=1):
            title = "试放" if step["trial"] else "推理"
            print("%d. [%s] %s" % (index, title, step["reason"]))
        if problem:
            print("推到这里出现矛盾：%s" % problem)
            print("可能是前面的猫或叉标错了。")
        elif _finished(final):
            print("按这些步骤，猫都确定了。")
        elif step_limit is not None and len(steps) >= step_limit:
            print("只展示了前 %d 步。去掉 --steps 可以继续往后推。" % step_limit)
        else:
            empty = int(np.sum(final == EMPTY))
            print("必走的步骤走完了，还剩 %d 格没确定。再标一格之后可以重新运行。" % empty)
    out_path = Path(path).with_name(Path(path).stem + "_提示.jpg")
    sc.save_bgr(out_path, draw_hints(found["img"], found["board"], steps))
    print("已标出推荐: %s" % out_path.name)
    print("每一步一张小图。前面排除掉的格子留成叉，这一步新结论用同一种显眼颜色。")


def _check_shapes():
    """用手摆的小颜色块核对连格、L、S 周围会不会被排除。"""
    def run(cells, n=6):
        labels = np.arange(n * n, dtype=int).reshape(n, n) + 1
        for row, col in cells:
            labels[row, col] = 0
        marks = np.full((n, n), EMPTY, dtype=np.int8)
        # 其他颜色每格一种，避免它们干扰这一色的排除结果。颜色 0 只在 cells 上。
        return common_exclusions(cells, marks, labels)

    straight = run([(1, 0), (1, 1), (1, 2)])
    assert (0, 1) in straight and (2, 1) in straight
    assert (0, 0) not in straight and (0, 2) not in straight
    assert (1, 3) in straight and (1, 4) in straight

    ell = run([(0, 0), (0, 1), (1, 0)])
    assert (1, 1) in ell
    assert (0, 2) not in ell

    long_l = run([(0, 0), (1, 0), (2, 0), (2, 1)])
    assert (1, 1) in long_l
    assert (0, 1) not in long_l

    domino = run([(0, 0), (0, 1)])
    for cell in ((0, 2), (1, 0), (1, 1)):
        # 行号 0 的上一行不存在；(1,0)(1,1) 是下方两格。上方在 n 里用第 2 行当中间行再测一次。
        assert cell in domino or cell[0] < 0
    domino2 = run([(2, 2), (2, 3)])
    for cell in ((1, 2), (1, 3), (3, 2), (3, 3), (2, 1), (2, 4)):
        assert cell in domino2, cell

    skew = run([(0, 1), (0, 2), (1, 0), (1, 1)])
    assert (0, 0) in skew and (1, 2) in skew
    assert (0, 3) not in skew
    print("形状核对通过")


def main(argv):
    sc.setup_console()
    step_limit = None
    paths = []
    index = 1
    while index < len(argv):
        if argv[index] == "--steps":
            index += 1
            if index >= len(argv):
                print("用法: python hint_cats.py 截图.png [--steps 1]")
                return 1
            step_limit = int(argv[index])
        elif argv[index] == "--check":
            _check_shapes()
        else:
            paths.append(argv[index])
        index += 1
    if not paths and "--check" not in argv:
        print("用法: python hint_cats.py 截图.png [--steps 1]")
        return 1
    code = 0
    for raw in paths:
        print("=" * 40)
        try:
            report(Path(raw), step_limit)
        except Exception as exc:
            print("文件: %s" % raw)
            print("失败: %s" % exc)
            code = 1
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
