#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量能基准线斜率标定（一次性探针，非生产代码）

目的: 给「详情页K线标题的趋势标签」选窗口和阈值。
口径与 index.html 保持一致:
  klineAmount(kl, 10): 有真实成交额用 r[6], 否则 volume*(h+l+c)/3*10 (转债1手=10张), 单位亿, 再 toFixed(3)
  baseLine(amt, 20, minN=3): 第 i 天取「前20天(不含当日)」均值
输出: 相对斜率分布、各窗口/阈值的符号翻转率、以及分状态的前瞻收益。
"""
import json
import statistics as st
from pathlib import Path

DATA = Path(__file__).parent / "data" / "klines_2026-09-18.json"
BASE_N = 20          # 量能基准窗口, 同 baseLine(arr,20)
FWD = 5              # 前瞻收益天数
WINDOWS = [5, 10, 15, 20]
THRESHOLDS = [0.0, 0.25, 0.5, 0.75, 1.0, 1.5]


def amount_yi(rows):
    """复刻前端 klineAmount: 返回成交额序列(亿)
    注意 toFixed(3): 前端把每日成交额量化到 3 位小数(10万元)。低成交额的券(日均几千万)
    上这一步会引入 ~0.3% 的相对量化噪声, 探针必须一起复刻, 否则算出来的斜率跟前端对不上。
    """
    out = []
    for r in rows:
        vol, h, l, c = float(r[5]), float(r[3]), float(r[4]), float(r[2])
        real = float(r[6]) if len(r) > 6 else 0.0
        yi = real if real > 0 else vol * (h + l + c) / 3 * 10
        out.append(round(yi / 1e8, 3))
    return out


def base_line(amt, n=BASE_N, min_n=3):
    """复刻前端 baseLine: 前 n 日均值(不含当日), 同样 toFixed(4)"""
    bl = []
    for i in range(len(amt)):
        w = amt[max(0, i - n):i]
        bl.append(round(sum(w) / len(w), 4) if len(w) >= min_n else None)
    return bl


def ols_slope(ys):
    """最小二乘斜率 + R² (x = 0..n-1 交易日)"""
    n = len(ys)
    mx = (n - 1) / 2
    my = sum(ys) / n
    sxy = sum((i - mx) * (y - my) for i, y in enumerate(ys))
    sxx = sum((i - mx) ** 2 for i in range(n))
    if sxx == 0:
        return 0.0, 0.0
    slope = sxy / sxx
    sst = sum((y - my) ** 2 for y in ys)
    ssr = sum((my + slope * (i - mx) - y) ** 2 for i, y in enumerate(ys))
    return slope, (1 - ssr / sst if sst > 0 else 0.0)


def main():
    d = json.loads(DATA.read_text())
    klines = d["klines"]

    # 每只债: 基准线 -> 各窗口相对斜率(%/交易日) + R²
    recs = []   # (code, i, {win: (rel_slope, r2)}, fwd_ret, price_chg5)
    for code, rows in klines.items():
        if len(rows) < BASE_N + max(WINDOWS) + FWD + 1:
            continue
        closes = [float(r[2]) for r in rows]
        bl = base_line(amount_yi(rows))
        for i in range(BASE_N + max(WINDOWS), len(rows) - FWD):
            win = bl[i - max(WINDOWS) + 1:i + 1]
            if any(v is None for v in win):
                continue
            per = {}
            for w in WINDOWS:
                ys = win[-w:]
                s, r2 = ols_slope(ys)
                mean = sum(ys) / len(ys)
                per[w] = (s / mean * 100 if mean > 0 else 0.0, r2)
            r_fwd = closes[i + FWD] / closes[i] - 1
            r_back = closes[i] / closes[i - 5] - 1 if i >= 5 else None
            recs.append((code, i, per, r_fwd, r_back))

    print(f"样本: {len(recs)} 个 (债 × 交易日) 观测点, 覆盖 {len(klines)} 只债\n")

    # 1) 相对斜率分布: 阈值要卡在什么量级
    print("== 相对斜率分布 (%/交易日) ==")
    print(f"{'窗口':>4} {'P10':>7} {'P25':>7} {'P50':>7} {'P75':>7} {'P90':>7} {'P95':>7} {'|x|>0.5占比':>11}")
    for w in WINDOWS:
        v = sorted(abs(r[2][w][0]) for r in recs)
        q = lambda p: v[int(p * (len(v) - 1))]
        share = sum(1 for x in v if x > 0.5) / len(v) * 100
        print(f"{w:>4} {q(.1):>7.2f} {q(.25):>7.2f} {q(.5):>7.2f} {q(.75):>7.2f} {q(.9):>7.2f} {q(.95):>7.2f} {share:>10.1f}%")

    # 2) 符号翻转率: 箭头今天↑明天↓的比例(越低越稳)
    print("\n== 次日状态翻转率 (同一只债相邻两日) ==")
    by_code = {}
    for code, i, per, *_ in recs:
        by_code.setdefault(code, []).append((i, per))
    for c in by_code:
        by_code[c].sort()
    print(f"{'窗口':>4} " + " ".join(f"{'t=' + str(t):>8}" for t in THRESHOLDS))
    for w in WINDOWS:
        row = []
        for t in THRESHOLDS:
            chg = tot = 0
            for seq in by_code.values():
                prev = None
                for _, per in seq:
                    v = per[w][0]
                    state = 1 if v > t else (-1 if v < -t else 0)
                    if prev is not None:
                        tot += 1
                        chg += state != prev
                    prev = state
            row.append(chg / tot * 100 if tot else 0)
        print(f"{w:>4} " + " ".join(f"{x:>7.1f}%" for x in row))

    # 2b) 死区 vs 滞回: 想「箭头别乱跳」, 哪种更稳
    print("\n== 走平占比 / 滞回翻转率 (窗口10) ==")
    print(f"{'阈值':>6} {'走平占比':>9} {'死区翻转率':>11} {'滞回翻转率':>11} {'平滑3日翻转率':>13}")
    for t in [0.0, 0.25, 0.5, 0.75, 1.0]:
        flat = total = 0
        band_flip = band_tot = 0
        hyst_flip = hyst_tot = 0
        smooth_flip = smooth_tot = 0
        for seq in by_code.values():
            prev_state = prev_hyst = None
            slopes = [per[10][0] for _, per in seq]
            smooth = [sum(slopes[max(0, j - 2):j + 1]) / len(slopes[max(0, j - 2):j + 1])
                      for j in range(len(slopes))]
            prev_smooth = None
            hyst = 0
            for j, v in enumerate(slopes):
                total += 1
                flat += abs(v) <= t
                state = 1 if v > t else (-1 if v < -t else 0)
                if prev_state is not None:
                    band_tot += 1
                    band_flip += state != prev_state
                if t > 0:
                    # 滞回: 只有越过对侧阈值才翻, 中间地带保持原状态
                    if prev_hyst is None:
                        hyst = 1 if v > 0 else -1
                    elif hyst == 1 and v < -t:
                        hyst = -1
                    elif hyst == -1 and v > t:
                        hyst = 1
                    if prev_hyst is not None:
                        hyst_tot += 1
                        hyst_flip += hyst != prev_hyst
                    prev_hyst = hyst
                sstate = 1 if smooth[j] > t else (-1 if smooth[j] < -t else 0)
                if prev_smooth is not None:
                    smooth_tot += 1
                    smooth_flip += sstate != prev_smooth
                prev_smooth = sstate
                prev_state = state
        f = lambda a, b: f"{a / b * 100:>10.1f}%" if b else "         -"
        print(f"{t:>6.2f} {flat / total * 100:>8.1f}% {f(band_flip, band_tot)} "
              f"{f(hyst_flip, hyst_tot)} {f(smooth_flip, smooth_tot)}")

    # 3) 分状态前瞻5日收益: 趋势标签有没有区分度
    print(f"\n== 分状态前瞻{FWD}日收益 (中位数) ==")
    for w in WINDOWS:
        for t in [0.0, 0.5, 1.0]:
            buckets = {-1: [], 0: [], 1: []}
            for _, _, per, r_fwd, _ in recs:
                v = per[w][0]
                buckets[1 if v > t else (-1 if v < -t else 0)].append(r_fwd)
            cells = []
            for k, name in [(1, "上行"), (0, "走平"), (-1, "下行")]:
                b = buckets[k]
                cells.append(f"{name} n={len(b):>5} 中位{st.median(b)*100:>6.2f}%" if b else f"{name} n=0")
            print(f"  窗口{w:>2} 阈值{t:.2f}: " + " | ".join(cells))

    # 4) 量能趋势 × 价格趋势(四象限)
    print(f"\n== 量能趋势 × 前5日价格方向 -> 前瞻{FWD}日收益中位数 (窗口10, 阈值0.5) ==")
    w, t = 10, 0.5
    quad = {}
    for _, _, per, r_fwd, r_back in recs:
        if r_back is None:
            continue
        v = per[w][0]
        vs = 1 if v > t else (-1 if v < -t else 0)
        ps = 1 if r_back > 0 else -1
        quad.setdefault((ps, vs), []).append(r_fwd)
    label = {(1, 1): "价涨+量能↑", (1, 0): "价涨+量能→", (1, -1): "价涨+量能↓",
             (-1, 1): "价跌+量能↑", (-1, 0): "价跌+量能→", (-1, -1): "价跌+量能↓"}
    for k, name in label.items():
        b = quad.get(k, [])
        if b:
            print(f"  {name:<12} n={len(b):>5}  中位 {st.median(b)*100:>6.2f}%  胜率 {sum(1 for x in b if x > 0)/len(b)*100:>5.1f}%")


if __name__ == "__main__":
    main()
