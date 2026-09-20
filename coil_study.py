#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「蓄势 vs 爆发」研究: 放量爆发后买入真的容易跌吗? 能不能提前看出在积蓄能量?

数据: data/klines_<最新日期>.json (scan_volume.py 抓的全市场日K缓存, 310 只 × 120 交易日)
口径: 与看板「量价验证」一致 —— 信号日收盘确认, 次日开盘买入, 持有 h 日按收盘卖出, 未计成本。
      量能用成交量(手)算比值(成交额≈量×价, 同一只券内部比值等价; 缓存里成交额是 0)。

用法: python3 coil_study.py            # 打印分层统计
      python3 coil_study.py --json     # 额外输出 json 供其他脚本用
"""
import json
import os
import statistics
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
FWD = (1, 3, 5, 10, 20)


def load_klines():
    files = sorted(f for f in os.listdir(os.path.join(BASE, "data"))
                   if f.startswith("klines_") and f.endswith(".json"))
    d = json.load(open(os.path.join(BASE, "data", files[-1]), encoding="utf-8"))
    return d.get("date"), d.get("klines") or {}


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def win(xs):
    xs = [x for x in xs if x is not None]
    return 100.0 * sum(1 for x in xs if x > 0) / len(xs) if xs else None


def features(k, i):
    """只用 i 及之前的数据算特征(无未来函数)"""
    closes = [r[2] for r in k]
    highs = [r[3] for r in k]
    lows = [r[4] for r in k]
    vols = [r[5] for r in k]
    v_base = med(vols[i - 20:i])                     # 前20日中位量(不含当日)
    if not v_base or v_base <= 0:
        return None
    a60 = max(0, i - 59)
    hi60, lo60 = max(highs[a60:i + 1]), min(lows[a60:i + 1])
    hi20, lo20 = max(highs[i - 19:i + 1]), min(lows[i - 19:i + 1])
    if hi60 <= lo60:
        return None
    r = vols[i] / v_base                             # 量比(对前20日中位量)
    chg = (closes[i] / closes[i - 1] - 1) * 100
    pos60 = (closes[i] - lo60) / (hi60 - lo60)       # 收盘在近60日高低区间的位置
    range20 = (hi20 - lo20) / closes[i]              # 20日振幅(相对价格)
    vol_trend = (med(vols[i - 9:i + 1]) or 0) / (med(vols[i - 29:i - 9]) or 1)   # 近10日 vs 更早20日
    amp20 = avg([abs(closes[j] / closes[j - 1] - 1) for j in range(i - 19, i + 1)])  # 日均波动
    # 连续缩量天数(量 < 0.8 × 前20日中位量)
    shrink = 0
    for j in range(i, 20, -1):
        vb = med(vols[j - 20:j])
        if vb and vols[j] < 0.8 * vb:
            shrink += 1
        else:
            break
    return {"r": r, "chg": chg, "pos60": pos60, "range20": range20,
            "vol_trend": vol_trend, "amp20": amp20, "shrink": shrink,
            "close": closes[i]}


def fwd_returns(k, i, entry_i):
    """次日开盘买入 -> 持有 h 日收盘卖出"""
    entry = k[entry_i][1]
    if not entry or entry <= 0:
        return None
    out = {}
    for h in FWD:
        j = entry_i + h - 1
        out[h] = (k[j][2] / entry - 1) * 100 if j < len(k) else None
    # 持有到 T+5 期间的最大浮亏 / 最大浮盈
    win_rows = [r for r in k[entry_i:entry_i + 5]]
    if win_rows:
        out["dd5"] = (min(r[4] for r in win_rows) / entry - 1) * 100
        out["mfe5"] = (max(r[3] for r in win_rows) / entry - 1) * 100
    else:
        out["dd5"] = out["mfe5"] = None
    # 爆发后 5 日内是否出现 +5% / -5%
    seg = k[entry_i:entry_i + 5]
    prev = k[entry_i - 1][2] if entry_i >= 1 else None
    out["up5_in5"] = any(prev and (r[2] / prev - 1) * 100 >= 5 for r in seg)
    out["dn5_in5"] = any(prev and (r[2] / prev - 1) * 100 <= -5 for r in seg)
    return out


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def coil_score(f):
    """蓄势分 0~100: 位置低 + 量能萎缩 + 振幅压缩 + 波动压缩 + 量能不升 + 缩量够久。

    权重是按"这些特征在历史样本里能不能压低回撤/提高后续收益"拍的, 不是拟合出来的;
    改动权重后请重跑本脚本看分层结果(coil_study.py 会打印按分数分层的表现)。
    """
    s = 0.0
    s += 25 * clamp((0.45 - f["pos60"]) / 0.45)              # 位置: pos60=0 满分, >=0.45 零分
    s += 25 * clamp((0.95 - f["r"]) / 0.55)                  # 量能: 今量 <=0.4×中位满分, >=0.95 零分
    s += 20 * clamp((0.13 - f["range20"]) / 0.10)            # 20日振幅: <=3% 满分, >=13% 零分
    s += 10 * clamp((0.018 - f["amp20"]) / 0.013)            # 日均波动: <=0.5% 满分
    s += 10 * clamp((1.05 - f["vol_trend"]) / 0.35)          # 近10日量能 vs 更早: 不升才给分
    s += 10 * clamp(f["shrink"] / 10.0)                      # 连续缩量天数: 10 天满分
    return round(s, 1)


def ignition(f):
    """点火: 前期安静的低位券, 今天温和放量收涨(1.5~3x) —— 历史里唯一正期望的那格"""
    return (1.5 <= f["r"] <= 3.0 and f["chg"] > 0 and f["pos60"] <= 0.35
            and f["vol_trend"] <= 1.2 and f["range20"] <= 0.12)


def risky(f):
    """风险: 高位 + 放量 + 大阳 = 筹码交换区, 别在次日开盘追"""
    return f["pos60"] >= 0.65 and f["r"] >= 1.5 and f["chg"] >= 3


def today_list(date, klines, top=18):
    out = []
    for code, k in klines.items():
        if len(k) < 45:
            continue
        f = features(k, len(k) - 1)
        if not f:
            continue
        f["code"], f["close"], f["score"] = code, f["close"], coil_score(f)
        f["ign"] = ignition(f)
        f["risk"] = risky(f)
        out.append(f)
    return sorted(out, key=lambda x: -x["score"])[:top]


def walk(klines):
    """遍历所有券日, 产出 (features, forward) 样本"""
    rows = []
    for code, k in klines.items():
        if len(k) < 45:
            continue
        for i in range(21, len(k) - 20):          # 前面留20根算基准, 后面留20根算前瞻
            f = features(k, i)
            if not f:
                continue
            fr = fwd_returns(k, i, i + 1)
            if not fr or fr.get(20) is None:
                continue
            rows.append((code, k[i][0], f, fr))
    return rows


def report(name, rows, idx):
    if not rows:
        print(f"{name:28s} 样本 0")
        return
    def g(h):
        return [r[idx].get(h) for r in rows]
    line = (f"{name:28s} {len(rows):6d} "
            + " ".join(f"{avg(g(h)):+6.2f}/{win(g(h)):4.0f}%" for h in FWD)
            + f"  最大浮亏 {avg(g('dd5')):+6.2f}%  "
            + f"未来5日内涨≥5% {100.0*sum(1 for r in rows if r[idx]['up5_in5'])/len(rows):5.1f}%")
    print(line)


def main():
    date, klines = load_klines()
    print(f"数据: klines_{date}.json · {len(klines)} 只券")
    rows = walk(klines)
    print(f"样本(券日): {len(rows)} 个\n")
    print("口径: 信号日收盘确认 → 次日开盘买入 → 持有 h 日收盘卖出; 每格 = 均值/胜率")
    print(f"{'分组':28s} {'样本':>6s} " + " ".join(f"{'T+'+str(h):>10s}" for h in FWD)
          + "  风险与后续")
    print("-" * 132)

    def sel(fn):
        return [r for r in rows if fn(r[2], r[1])]

    base = rows
    surge = sel(lambda f, d: f["r"] >= 2)
    surge_low = sel(lambda f, d: f["r"] >= 2 and f["pos60"] <= 0.35)
    surge_mid = sel(lambda f, d: f["r"] >= 2 and 0.35 < f["pos60"] < 0.65)
    surge_high = sel(lambda f, d: f["r"] >= 2 and f["pos60"] >= 0.65)
    surge_nh = sel(lambda f, d: f["r"] >= 2 and f["chg"] >= 3)
    quiet_low = sel(lambda f, d: f["pos60"] <= 0.35 and f["r"] <= 0.8)
    coil = sel(lambda f, d: f["pos60"] <= 0.35 and f["r"] <= 0.8
               and f["range20"] <= 0.10 and f["vol_trend"] <= 1.1)
    coil_tight = sel(lambda f, d: f["pos60"] <= 0.35 and f["r"] <= 0.6
                     and f["range20"] <= 0.08 and f["vol_trend"] <= 1.0)
    coil_ign = sel(lambda f, d: 1.5 <= f["r"] <= 3 and f["chg"] > 0 and f["pos60"] <= 0.35)
    hot_high = sel(lambda f, d: f["pos60"] >= 0.65 and f["r"] >= 1.5 and f["chg"] >= 3)

    for nm, rs in [("全样本基准", base), ("放量爆发 r≥2 (全部)", surge),
                   ("  ├ 爆发+低位 pos60≤.35", surge_low),
                   ("  ├ 爆发+中位", surge_mid),
                   ("  ├ 爆发+高位 pos60≥.65", surge_high),
                   ("  └ 爆发+当日涨≥3%(追涨)", surge_nh),
                   ("缩量+低位 (安静)", quiet_low),
                   ("蓄势: 缩量+低位+窄幅+量不升", coil),
                   ("  └ 更严: 极缩量+极窄幅", coil_tight),
                   ("蓄势后首次温和放量1.5~3x", coil_ign),
                   ("高位+放量+涨≥3% (最热)", hot_high)]:
        report(nm, rs, 3)

    print("\n【蓄势分本身有没有用?】按蓄势分分层(无未来函数, 每日可算)")
    rows_s = [(c, d, dict(f, score=coil_score(f)), fr) for c, d, f, fr in rows]
    for lo, hi in [(70, 101), (55, 70), (40, 55), (0, 40)]:
        rs = [r for r in rows_s if lo <= r[2]["score"] < hi]
        if rs:
            report(f"蓄势分 {lo}~{hi if hi < 101 else ''}", rs, 3)

    print("\n【点火 vs 追高】")
    report("点火(低位温和放量1.5~3x收涨)",
           [r for r in rows_s if ignition(r[2])], 3)
    report("风险(高位+放量+涨≥3%)",
           [r for r in rows_s if risky(r[2])], 3)

    print("\n【关键问题】蓄势状态之后, 未来几天会不会爆发? (条件概率)")
    for nm, rs in [("全样本", base), ("蓄势(缩量+低位+窄幅)", coil), ("更严的蓄势", coil_tight)]:
        if not rs:
            continue
        print(f"  {nm:22s} 样本 {len(rs):6d} · 未来5日内出现 放量(r≥2)+收涨 的概率 "
              f"{100.0*sum(1 for r in rs if r[3]['up5_in5'])/len(rs):5.1f}%"
              f" · 未来5日内涨≥5% {100.0*sum(1 for r in rs if r[3]['up5_in5'])/len(rs):5.1f}%")

    print("\n【爆发日当天特征 vs 次日结果】按量比分层看 T+1 (次日开盘买 → 次日收盘)")
    for lo, hi in [(0.5, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 3.0), (3.0, 5.0), (5.0, 999)]:
        rs = sel(lambda f, d, lo=lo, hi=hi: lo <= f["r"] < hi)
        if rs:
            t1 = [r[3][1] for r in rs]
            print(f"  量比 {lo:>4.1f}~{hi if hi<999 else '∞':>4}  样本 {len(rs):6d}  "
                  f"T+1 {avg(t1):+6.2f}%  胜率 {win(t1):4.0f}%  中位 {med(t1):+6.2f}%  "
                  f"次日跌的比例 {100.0*sum(1 for x in t1 if x<0)/len(t1):5.1f}%")

    if "--today" in sys.argv:
        print("\n【今日蓄势榜】(蓄势分前 18 名, 越靠前=越低位/越缩量/越窄幅)")
        print(f"  {'代码':8s}{'收盘':>8s}{'蓄势分':>7s}{'量比':>7s}{'pos60':>7s}{'振幅20':>8s}{'波动':>7s}{'缩量天':>7s} 标记")
        for f in today_list(date, klines):
            mark = " ⚡点火" if f["ign"] else (" ⚠追高区" if f["risk"] else "")
            print(f"  {f['code']:8s}{f['close']:8.2f}{f['score']:7.1f}{f['r']:7.2f}"
                  f"{f['pos60']:7.2f}{f['range20']*100:7.1f}%{f['amp20']*100:6.2f}%{f['shrink']:7d}{mark}")

    if "--json" in sys.argv:
        out = {"date": date, "samples": len(rows)}
        print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
