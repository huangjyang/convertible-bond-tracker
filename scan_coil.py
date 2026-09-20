#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""蓄势扫描: 全市场可转债的「蓄势分 / 点火 / 追高区」+ 分层回测证据。

思路(证据见 coil_study.py / 蓄势研究_*.md):
    - 「蓄势」= 位置低 + 量能萎缩 + 振幅压缩 + 波动压缩 + 量能不升 + 缩量够久。
      它是**防守/等待**指标: 历史高分档收益≈0, 但最大浮亏只有低分档的一半。
    - 「点火」= 低位 + 窄幅 + 温和放量(1.5~3×) + 收涨 + 前期量能不升。
      这是历史里唯一稳定正期望的一格(T+20 +0.70% / 胜率 51% / 平均最大浮亏 -1.81%)。
    - 「追高」= 高位 + 放量 + 涨≥3% (T+20 -2.76% / 胜率 35% / 平均最大浮亏 -6.70%)。

数据源: data/klines_<日期>.json(全市场日K, 由 scan_volume.py 维护) + 东财 clist 实时快照(名字/正股/指标)。
产出:   data/coil_scan_<日期>.json

用法: python3 scan_coil.py            # 扫描并写 json
      python3 scan_coil.py --quiet    # 少打印
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
DATA_DIR = os.path.join(BASE, "data")

import coil_study as cs        # noqa: E402  特征/蓄势分/点火/追高 定义(单一来源)
import fetch_daily as fd       # noqa: E402  全市场列表(名字/正股/指标)
import scan_volume as sv       # noqa: E402  日K缓存读取

TOP_N = 40          # 蓄势榜展示条数
FWD = cs.FWD        # (1,3,5,10,20)


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _win(xs):
    xs = [x for x in xs if x is not None]
    return 100.0 * sum(1 for x in xs if x > 0) / len(xs) if xs else None


def _med(xs):
    xs = [x for x in xs if x is not None]
    return cs.med(xs)


def layer_stats(rows, name, sel):
    """一行回测证据: 每档的 T+h 均值/胜率 + 平均最大浮亏"""
    rs = [r for r in rows if sel(r[2])]
    if not rs:
        return None
    cells = []
    for h in FWD:
        v = [r[3].get(h) for r in rs]
        cells.append([round(_avg(v), 2) if _avg(v) is not None else None,
                      round(_win(v), 1) if _win(v) is not None else None,
                      round(_med(v), 2) if _med(v) is not None else None])
    dd = _avg([r[3].get("dd5") for r in rs])
    return {"name": name, "n": len(rs), "cells": cells,
            "dd": round(dd, 2) if dd is not None else None}


def backtest_layers(klines, samples=None):
    """把 coil_study 的分层结论算成前端能直接画表的结构"""
    rows = samples if samples is not None else cs.walk(klines)
    defs = [
        ("全样本基准", lambda f: True),
        ("放量爆发 r≥2", lambda f: f["r"] >= 2),
        ("　├ 爆发+低位 pos60≤.35", lambda f: f["r"] >= 2 and f["pos60"] <= 0.35),
        ("　└ 爆发+高位 pos60≥.65", lambda f: f["r"] >= 2 and f["pos60"] >= 0.65),
        ("蓄势: 缩量+低位+窄幅", lambda f: f["pos60"] <= 0.35 and f["r"] <= 0.8
            and f["range20"] <= 0.10 and f["vol_trend"] <= 1.1),
        ("更严: 极缩量+极窄幅", lambda f: f["pos60"] <= 0.35 and f["r"] <= 0.6
            and f["range20"] <= 0.08 and f["vol_trend"] <= 1.0),
        ("点火: 低位+窄幅+温和放量收涨", cs.ignition),
        ("风险: 高位+放量+涨≥3%", cs.risky),
    ]
    out = []
    for nm, fn in defs:
        st = layer_stats(rows, nm, fn)
        if st:
            out.append(st)
    return out, len(rows)


def scan(quiet=False):
    date, klines = sv.load_cached_klines()
    if not klines:
        print("没有日K缓存: 先跑一次 scan_volume.py", file=sys.stderr)
        return 1
    t0 = time.time()
    basics = sv.load_basics()                     # 评级/规模/上市日
    live = {}
    try:
        for r in fd.fetch_cb_universe():          # 名字/正股/溢价/换手(实时快照)
            p = fd.parse_cb_row(r)
            if p:
                live[p["code"]] = p
    except Exception as e:
        print(f"⚠ 东财快照取不到({e}), 名字/正股字段会缺", file=sys.stderr)

    bonds = []
    for code, k in klines.items():
        if len(k) < 45:
            continue
        f = cs.features(k, len(k) - 1)
        if not f:
            continue
        L = live.get(code, {})
        B = basics.get(code, {})
        score = cs.coil_score(f)
        ign, risk = cs.ignition(f), cs.risky(f)
        tags = []
        if ign:
            tags.append("点火")
        if risk:
            tags.append("追高区")
        if score >= 70:
            tags.append("高蓄势")
        if f["shrink"] >= 5:
            tags.append(f"连续缩量{f['shrink']}天")
        if f["pos60"] <= 0.1:
            tags.append("极低位")
        if f["vol_trend"] <= 0.9:
            tags.append("量能持续萎缩")
        bonds.append({
            "code": code, "name": L.get("name") or B.get("name") or code,
            "close": round(k[-1][2], 3), "price": round(k[-1][2], 3),   # price=close(归因/弹窗要 price)
            "chg": round(f["chg"], 2),
            "date": k[-1][0],
            "ratio": round(f["r"], 2), "pos60": round(f["pos60"], 3),
            "range20": round(f["range20"], 4), "amp20": round(f["amp20"], 4),
            "vol_trend": round(f["vol_trend"], 2), "shrink": f["shrink"],
            "score": score, "ign": ign, "risk": risk, "tags": tags,
            # 下面这些给弹窗/归因用(来自东财快照 + 静态缓存)
            "stock_code": L.get("stock_code"), "stock_name": L.get("stock_name"),
            "stock_chg": L.get("stock_chg"), "stock_price": L.get("stock_price"),
            "turnover_yi": round(L["turnover"] / 1e8, 2) if L.get("turnover") else None,
            "turnover_rate": L.get("turnover_rate"), "premium": L.get("premium"),
            "convert_price": L.get("convert_price"), "convert_value": L.get("convert_value"),
            "redeem_trigger": L.get("redeem_trigger"), "put_trigger": L.get("put_trigger"),
            "maturity_redeem": L.get("maturity_redeem"),
            "rating": B.get("rating"), "scale": B.get("scale"),
            "listing_date": B.get("listing_date"), "expire_date": B.get("expire_date"),
            "is_max60": f["r"] >= 2 and k[-1][5] >= max(r[5] for r in k[-60:]),
        })
    bonds.sort(key=lambda x: -x["score"])
    ign_list = [b["code"] for b in bonds if b["ign"]]
    risk_list = [b["code"] for b in bonds if b["risk"]]

    layers, nsamples = backtest_layers(klines)
    out = {
        "date": date,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "params": {"pos_low": 0.35, "shrink_ratio": 0.8, "range_max": 0.10,
                   "ignition_ratio": [1.5, 3.0], "risk_pos": 0.65},
        "counts": {"universe": len(klines), "scored": len(bonds),
                   "coil70": sum(1 for b in bonds if b["score"] >= 70),
                   "ignition": len(ign_list), "risk": len(risk_list)},
        "groups": {"点火": ign_list, "蓄势TOP": [b["code"] for b in bonds[:TOP_N]],
                   "追高区": risk_list},
        "bonds": bonds,
        "validation": {"horizons": list(FWD), "samples": nsamples, "layers": layers},
    }
    p = os.path.join(DATA_DIR, f"coil_scan_{date}.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    if not quiet:
        print(f"扫描日 {date}: 打分 {len(bonds)} 只 · 蓄势分≥70 {out['counts']['coil70']} 只 · "
              f"点火 {len(ign_list)} 只 · 追高区 {len(risk_list)} 只")
        print("  点火: " + ("、".join(
            f"{b['name']}({b['code']}) 量比{b['ratio']:.2f} 涨{b['chg']:+.1f}%"
            for b in bonds if b["ign"]) or "无"))
        print(f"  蓄势分 Top5: " + "、".join(
            f"{b['name']}({b['score']:.0f})" for b in bonds[:5]))
        print(f"  → {os.path.relpath(p, BASE)}  (用時 {time.time()-t0:.1f}s)")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    return scan(quiet=args.quiet)


if __name__ == "__main__":
    sys.exit(main())
