#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「添加到主屏幕」用的图标 (纯标准库, 不需要 Pillow)。

输出: assets/icon-180.png (iOS apple-touch-icon) / icon-192.png / icon-512.png
设计: 满幅蓝色方底 + 3 根递增白色/金色柱子(看板意象)。满幅不透明底是为了
同时满足 Android maskable(圆形裁切也不露白角) 和 iOS(系统自己会切圆角)。

重新生成: python3 make_icons.py
"""
import os
import struct
import zlib

BASE = os.path.dirname(os.path.abspath(__file__))
BG = (0x2b, 0x5a, 0xed)          # --accent
BAR = (0xff, 0xff, 0xff)
HOT = (0xff, 0xd1, 0x66)         # 最高那根用暖色, 呼应"热度"

# 版式(相对边长): 3 根柱子, 底对齐, 高度递增
BARS = [(0.18, BAR, 0.92), (0.30, BAR, 0.92), (0.45, HOT, 1.0)]
BAR_W, GAP = 0.125, 0.075
BASE_Y, BASE_H = 0.745, 0.028


def _cov(lo, hi, a, b):
    """线段 [a,b) 与 [lo,hi) 的重叠比例 (精确覆盖率, 用作抗锯齿)"""
    o = min(hi, b) - max(lo, a)
    return max(0.0, o) / (hi - lo) if hi > lo else 0.0


def render(size):
    """返回 RGBA bytearray: 满幅底色 + 抗锯齿柱子 + 底部基准线"""
    px = bytearray(size * size * 4)
    total = 3 * BAR_W + 2 * GAP
    x0 = (1.0 - total) / 2.0
    rects = []
    for i, (h, col, alpha) in enumerate(BARS):
        left = x0 + i * (BAR_W + GAP)
        rects.append((left, left + BAR_W, BASE_Y - h, BASE_Y, col, alpha))
    rects.append((x0 - 0.035, x0 + total + 0.035, BASE_Y, BASE_Y + BASE_H, BAR, 0.38))
    for y in range(size):
        fy0, fy1 = y / size, (y + 1) / size
        for x in range(size):
            fx0, fx1 = x / size, (x + 1) / size
            r, g, b = BG
            for (l, rr, t, bt, col, alpha) in rects:
                cv = _cov(fx0, fx1, l, rr) * _cov(fy0, fy1, t, bt)
                if cv <= 0:
                    continue
                a = cv * alpha
                r = r * (1 - a) + col[0] * a
                g = g * (1 - a) + col[1] * a
                b = b * (1 - a) + col[2] * a
            i = (y * size + x) * 4
            px[i] = int(r + 0.5)
            px[i + 1] = int(g + 0.5)
            px[i + 2] = int(b + 0.5)
            px[i + 3] = 255
    return px


def write_png(path, size, px):
    raw = b"".join(b"\x00" + bytes(px[y * size * 4:(y + 1) * size * 4]) for y in range(size))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def main():
    out = os.path.join(BASE, "assets")
    os.makedirs(out, exist_ok=True)
    for size in (180, 192, 512):
        p = os.path.join(out, f"icon-{size}.png")
        write_png(p, size, render(size))
        print(f"{p}  {os.path.getsize(p)} bytes")


if __name__ == "__main__":
    main()
