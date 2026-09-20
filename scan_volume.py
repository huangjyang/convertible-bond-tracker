#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
可转债「突然放量」扫描器 + 信号历史验证

对应盘面规律("先有量, 后有价"):
  1. 当日成交量显著高于自己前期的量(量比), 最好是近60日最大量;
  2. 放量之前必须是缩量的(前期安静), 否则只是持续活跃;
  3. 价格还没大涨、且处在近期相对低位 -> "量先起来"的早期阶段;
  4. 流动性够(成交额门槛), 剔除僵尸债与放量下跌。

数据源: 东财 clist(实时快照) + 腾讯日K(成交量, 手)
产出:
  data/klines_YYYY-MM-DD.json       当日全市场日K缓存(复用, 免重复抓取)
  data/volume_scan_YYYY-MM-DD.json  扫描结果

用法:
  python3 scan_volume.py                     # 扫描 + 榜单
  python3 scan_volume.py --validate          # 用缓存K线做信号前瞻收益验证
  python3 scan_volume.py --top 40 --show-all
"""
import argparse
import json
import os
import statistics
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import fetch_daily as F

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")

# ===== 口径常量 (改动后历史结果不可比) =====
LOOKBACK = 120         # 拉取日K长度
BASE_WIN = 20          # 量比基准窗口(不含当日)
SHORT_WIN = 5          # 短期量比窗口(不含当日)
QUIET_RATIO = 1.2      # 前期缩量: 前20日中位量 / 更早的60日中位量 < 该值
POS_WIN = 60           # 位置分位窗口
MIN_AMOUNT = 3e7       # 成交额门槛(元)
SURGE = 2.0            # 放量倍数门槛
FLAT_RANGE = 0.03      # 近20日振幅 < 3% 视为僵尸债(纯债/临近到期)
DROP_CHG = -3.0        # 跌幅超过该值且放量 -> 放量下跌(反向信号)
FWD_DAYS = (1, 3, 5, 10, 20)  # 验证用前瞻交易日


# ---------------------------------------------------------------- 抓取

def kline_cache_path(date):
    return os.path.join(DATA_DIR, f"klines_{date}.json")


def latest_snapshot_date():
    """data/ 里最新的热度榜快照日期(YYYY-MM-DD); 没有则 None"""
    try:
        files = sorted(f for f in os.listdir(DATA_DIR)
                       if len(f) == 15 and f.endswith(".json")
                       and f[:4].isdigit() and f[4] == "-")
    except OSError:
        return None
    return files[-1][:-5] if files else None


def cache_is_fresh(cache_date, snapshot_date):
    """缓存的日K日期是否已跟上最新快照。落后 = 有新的交易日还没扫过, 必须重抓,
    否则扫描结果会一直停在前一天(放量榜比热度榜慢一天)。"""
    if not cache_date:
        return False
    return (not snapshot_date) or cache_date >= snapshot_date


def load_or_fetch_klines(force=False, workers=8):
    """抓取(或复用)全市场日K; 返回 (trade_date, {code: kline}, universe快照)"""
    print("拉取全市场可转债快照 ...", file=sys.stderr)
    raw = F.fetch_cb_universe()
    bonds = [b for b in (F.parse_cb_row(r) for r in raw) if b]
    print(f"  全市场 {len(bonds)} 只", file=sys.stderr)

    if not force:
        cdate, ck = load_cached_klines()
        snap = latest_snapshot_date()
        if ck and cache_is_fresh(cdate, snap):
            print(f"  复用日K缓存 {cdate} ({len(ck)} 只); 加 --force 可重抓",
                  file=sys.stderr)
            return cdate, ck, bonds
        if ck:
            print(f"  日K缓存 {cdate} 落后于最新快照 {snap}, 自动重抓全市场日K ...",
                  file=sys.stderr)

    results, lock, done = {}, threading.Lock(), [0]

    def work(b):
        try:
            k = F.tx_kline(F.tx_bond_symbol(b["code"]), LOOKBACK)
        except Exception:
            k = []
        if k:
            with lock:
                results[b["code"]] = k
        with lock:
            done[0] += 1
            if done[0] % 60 == 0:
                print(f"  K线 {done[0]}/{len(bonds)}", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, bonds))

    if not results:
        return None, {}, bonds
    trade_date = max(k[-1][0] for k in results.values())
    results = {c: k for c, k in results.items() if k[-1][0] == trade_date}
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(kline_cache_path(trade_date), "w", encoding="utf-8") as f:
        json.dump({"date": trade_date, "klines": results}, f,
                  ensure_ascii=False, separators=(",", ":"))
    print(f"  扫描日 {trade_date}, 有效K线 {len(results)} 只 "
          f"-> {os.path.basename(kline_cache_path(trade_date))}", file=sys.stderr)
    return trade_date, results, bonds


def load_basics():
    """评级/发行规模/上市日 (来自 fetch_daily 的静态缓存, 缺失返回空)"""
    path = os.path.join(DATA_DIR, "bond_basic_cache.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("map") or {}
    except Exception:
        return {}


def load_cached_klines():
    files = sorted(f for f in os.listdir(DATA_DIR)
                   if f.startswith("klines_") and f.endswith(".json"))
    if not files:
        return None, {}
    p = os.path.join(DATA_DIR, files[-1])
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    return d.get("date"), d.get("klines") or {}


# ---------------------------------------------------------------- 指标

def window_stats(vols, i):
    """以 i 为当日, 计算量能指标; 返回 None 表示样本不足"""
    if i < 21:
        return None
    vol = vols[i]
    prior = vols[:i]
    base20 = statistics.median(prior[-BASE_WIN:])
    base60 = statistics.median(prior[-60:]) if len(prior) >= 40 else base20
    short5 = statistics.mean(prior[-SHORT_WIN:])
    if base20 <= 0 or short5 <= 0:
        return None
    return {
        "vol": vol,
        "base20": base20,
        "ratio20": vol / base20,
        "ratio5": vol / short5,
        "is_max60": vol >= max(prior[-60:]) if len(prior) >= 20 else False,
        "is_max20": vol >= max(prior[-20:]),
        "quiet_before": (base20 / base60) < QUIET_RATIO if base60 > 0 else False,
    }


def surge_days(vols, i, win=20):
    """截至 i(不含) 的近 win 日放量日个数"""
    n = 0
    for j in range(max(21, i - win), i):
        s = window_stats(vols, j)
        if s and s["ratio20"] >= SURGE:
            n += 1
    return n


def metrics(bond, kline):
    rows = [r for r in kline if r[5] and r[5] > 0]
    if len(rows) < 30:
        return None
    vols = [r[5] for r in rows]
    closes = [r[2] for r in rows]
    highs = [r[3] for r in rows]
    lows = [r[4] for r in rows]

    st = window_stats(vols, len(vols) - 1)
    if not st:
        return None

    close = closes[-1]
    win = min(POS_WIN, len(lows))
    px_lo, px_hi = min(lows[-win:]), max(highs[-win:])
    pos = (close - px_lo) / (px_hi - px_lo) if px_hi > px_lo else 0.5
    range20 = ((max(highs[-20:]) - min(lows[-20:])) / close) if len(highs) >= 20 else 1.0

    hi, lo = highs[-1], lows[-1]
    close_pos = (close - lo) / (hi - lo) if hi > lo else 0.5
    chg = bond.get("chg") or 0.0
    amount_yi = (bond.get("turnover") or 0.0) / 1e8
    scale = bond.get("scale")
    listed = bond.get("listing_date")

    # 前一日是否已经放量(区分"首日异动"与"持续放量")
    prev_surge = False
    if len(vols) >= 23:
        ps = window_stats(vols, len(vols) - 2)
        prev_surge = bool(ps and ps["ratio20"] >= SURGE)

    sd20 = surge_days(vols, len(vols) - 1)
    flat = range20 < FLAT_RANGE
    tags = []
    if st["ratio20"] >= 3:
        tags.append("巨量")
    elif st["ratio20"] >= 2:
        tags.append("明显放量")
    elif st["ratio20"] >= 1.5:
        tags.append("温和放量")
    if st["is_max60"]:
        tags.append("60日最大量")
    if st["quiet_before"]:
        tags.append("前期缩量")
    if pos <= 0.35:
        tags.append("相对低位")
    elif pos >= 0.75:
        tags.append("相对高位")
    if prev_surge:
        tags.append("连续放量")
    elif st["ratio20"] >= SURGE:
        tags.append("首日异动")
    if sd20 >= 3:
        tags.append("近期反复放量")
    if chg <= DROP_CHG and st["ratio20"] >= SURGE:
        tags.append("放量下跌")
    elif chg <= 0.5 and st["ratio20"] >= SURGE:
        tags.append("放量滞涨")
    if chg >= 5 and st["ratio20"] >= SURGE:
        tags.append("放量拉升")
    if flat:
        tags.append("价格几乎不动")
    if listed:
        try:
            from datetime import date as _d
            y, m_, dd = (int(x) for x in listed.split("-"))
            if (datetime.strptime(rows[-1][0], "%Y-%m-%d").date()
                    - _d(y, m_, dd)).days <= 60:
                tags.append("次新债")
        except Exception:
            pass

    if scale and scale <= 3:
        tags.append("小盘(<3亿)")
    score = 0.0
    score += min(st["ratio20"], 8.0) / 8.0 * 40
    score += 15 if st["is_max60"] else (8 if st["is_max20"] else 0)
    score += 10 if st["quiet_before"] else 0
    score += (1.0 - min(max(pos, 0.0), 1.0)) * 15
    if -4.0 <= chg <= 6.0:
        score += 10
    score += min(amount_yi / 2.0, 1.0) * 10
    # 扣分项: 放量下跌 / 僵尸债
    if chg <= DROP_CHG:
        score -= 25
    if flat:
        score -= 20

    return {
        "code": bond["code"], "name": bond["name"],
        "price": bond["price"], "chg": chg,
        "amount_yi": round(amount_yi, 2),
        "turnover_rate": bond.get("turnover_rate"),
        "ratio20": round(st["ratio20"], 2), "ratio5": round(st["ratio5"], 2),
        "is_max60": st["is_max60"], "is_max20": st["is_max20"],
        "quiet_before": st["quiet_before"], "prev_surge": prev_surge,
        "surge_days_20": sd20, "flat": flat,
        "pos60": round(pos, 3), "close_pos": round(close_pos, 3),
        "range20": round(range20, 4),
        "vol_hand": st["vol"], "base20_hand": round(st["base20"], 1),
        "chg5": round((close / closes[-6] - 1) * 100, 2) if len(closes) >= 6 else None,
        "chg20": round((close / closes[-21] - 1) * 100, 2) if len(closes) >= 21 else None,
        "scale": scale, "premium": bond.get("premium"),
        "rating": bond.get("rating"), "listing_date": listed,
        "stock_name": bond.get("stock_name"), "stock_code": bond.get("stock_code"),
        "stock_chg": bond.get("stock_chg"),
        # 下面这些快照里本来就有, 一并带上: 榜单外的券点开详情时不会一片 "-"
        "open": bond.get("open"), "high": hi, "low": lo, "pre_close": bond.get("pre_close"),
        "stock_price": bond.get("stock_price"),
        "convert_price": bond.get("convert_price"), "convert_value": bond.get("convert_value"),
        "pure_bond_premium": bond.get("pure_bond_premium"),
        "redeem_trigger": bond.get("redeem_trigger"), "put_trigger": bond.get("put_trigger"),
        "maturity_redeem": bond.get("maturity_redeem"),
        "expire_date": bond.get("expire_date"),
        "double_low": round(bond["price"] + bond["premium"], 1)
                      if bond.get("premium") is not None else None,
        "last_date": rows[-1][0], "tags": tags, "score": round(score, 1),
    }


def is_candidate(m):
    """主榜口径: 放量 + 非下跌 + 非僵尸 + 前期缩量或首日异动"""
    return (m["ratio20"] >= SURGE and m["chg"] > DROP_CHG and not m["flat"])


# ---------------------------------------------------------------- 扫描

def do_scan(args):
    trade_date, klines, bonds = load_or_fetch_klines(force=args.force,
                                                    workers=args.workers)
    if not trade_date:
        print("没有抓到K线数据", file=sys.stderr)
        return 1
    basics = load_basics()
    for b in bonds:
        info = basics.get(b["code"]) or {}
        b["rating"] = b.get("rating") or info.get("rating")
        b["scale"] = b.get("scale") or info.get("scale")
        b["listing_date"] = b.get("listing_date") or info.get("listing_date")
        b["expire_date"] = b.get("expire_date") or info.get("expire_date")   # 供前端算剩余年限
    by_code = {b["code"]: b for b in bonds}

    rows = []
    for code, k in klines.items():
        b = by_code.get(code)
        if not b:
            continue
        m = metrics(b, k)
        if m:
            rows.append(m)

    liq = [m for m in rows if m["amount_yi"] >= args.min_amount]
    liq.sort(key=lambda x: (-x["score"], -x["ratio20"]))
    cand = [m for m in liq if is_candidate(m)]
    grouped = {}
    for m in cand:
        key = ("放量下跌" if "放量下跌" in m["tags"] else
               "放量拉升" if m["chg"] >= 5 else
               "放量滞涨" if m["chg"] <= 0.5 else "放量温和上涨")
        grouped.setdefault(key, []).append(m)

    out = {
        "date": trade_date,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "params": {"lookback": LOOKBACK, "base_win": BASE_WIN, "surge": SURGE,
                   "min_amount_yi": args.min_amount, "quiet_ratio": QUIET_RATIO,
                   "flat_range": FLAT_RANGE},
        "counts": {"universe": len(bonds), "with_kline": len(rows),
                   "liquid": len(liq), "candidate": len(cand)},
        "groups": {k: [m["code"] for m in v] for k, v in grouped.items()},
        "bonds": liq,
    }
    path = os.path.join(DATA_DIR, f"volume_scan_{trade_date}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"\n扫描日 {trade_date}: 全市场{len(bonds)} -> 有效K线{len(rows)} "
          f"-> 成交额≥{args.min_amount}亿 {len(liq)} -> 放量候选 {len(cand)}")
    for k, v in sorted(grouped.items(), key=lambda x: -len(x[1])):
        print(f"   {k}: {len(v)} 只")
    print(f"已写入 {os.path.relpath(path, BASE)}\n")

    def table(items, title):
        if not items:
            return
        print(f"—— {title} ——")
        print(f"{'#':<3}{'代码':<8}{'名称':<10}{'现价':>8}{'涨幅%':>7}{'量比20':>8}"
              f"{'量比5':>7}{'额亿':>7}{'换手%':>7}{'60分位':>8}{'评分':>6}  标签")
        for i, m in enumerate(items, 1):
            print(f"{i:<3}{m['code']:<8}{m['name']:<10}{m['price']:>8.2f}"
                  f"{m['chg']:>7.2f}{m['ratio20']:>8.2f}{m['ratio5']:>7.2f}"
                  f"{m['amount_yi']:>7.2f}={(m['turnover_rate'] or 0):>6.1f}"
                  f"{m['pos60']:>8.2f}{m['score']:>6.1f}  {'/'.join(m['tags'])}")
        print()

    shown = cand if args.show_all else cand[:args.top]
    out["_groups"] = {
        "首选": [m for m in shown if m["pos60"] <= 0.35],
        "次选": [m for m in shown if m["pos60"] > 0.35],
        "回避": [m for m in liq if m["chg"] <= DROP_CHG and m["ratio20"] >= SURGE],
    }
    table([m for m in shown if m["pos60"] <= 0.35],
          "★ 首选: 放量 + 相对低位(60日分位≤0.35) —— 历史验证里唯一有正期望的组合")
    table([m for m in shown if m["pos60"] > 0.35],
          "次选: 放量但位置不低(注意: 追高型, 历史 T+20 均值为负)")
    table([m for m in liq if m["chg"] <= DROP_CHG and m["ratio20"] >= SURGE],
          "反向提示: 放量下跌(不参与)")
    return out


# ---------------------------------------------------------------- 验证

def do_validate(args):
    """两件事:
    A. 信号前瞻收益: "放量后买入"到底赚不赚(次日开盘买入口径)
    B. 事件研究: 大涨/大跌之前, 究竟有没有放量(检验"只有量上来价才涨")
    """
    trade_date, klines = load_cached_klines()
    if not klines:
        print("没有K线缓存, 先跑一次 scan_volume.py", file=sys.stderr)
        return 1
    print(f"用 {trade_date} 的K线缓存做验证: {len(klines)} 只 "
          f"(样本为最近{LOOKBACK}个交易日的全市场日K)\n")

    horizons = FWD_DAYS
    buckets = {}

    def add(name, ret):
        b = buckets.setdefault(name, {h: [] for h in horizons})
        for h in horizons:
            if ret.get(h) is not None:
                b[h].append(ret[h])

    # 事件研究计数
    ev = {"up": {"n": 0, "surge_on": 0, "surge_before1": 0, "surge_before3": 0,
                 "surge_before5": 0, "no_surge": 0, "ratios": []},
          "down": {"n": 0, "surge_on": 0, "surge_before1": 0, "surge_before3": 0,
                   "surge_before5": 0, "no_surge": 0, "ratios": []},
          "surge": {"n": 0, "hit": 0, "hitdown": 0},
          "nosurge": {"n": 0, "hit": 0, "hitdown": 0}}
    last_ratio = {}   # code -> {i: ratio20}

    for code, k in klines.items():
        rows = [r for r in k if r[5] and r[5] > 0]
        if len(rows) < 45:
            continue
        vols = [r[5] for r in rows]
        opens = [r[1] for r in rows]
        closes = [r[2] for r in rows]
        highs = [r[3] for r in rows]
        lows = [r[4] for r in rows]
        n = len(rows)
        ratios = {}
        for i in range(21, n):
            st = window_stats(vols, i)
            ratios[i] = st["ratio20"] if st else None

        for i in range(21, n - max(horizons)):
            st = window_stats(vols, i)
            if not st:
                continue
            # 次日开盘买入 -> 持有 h 个交易日
            ret = {}
            entry = opens[i + 1]
            for h in horizons:
                ret[h] = (closes[min(i + h, n - 1)] / entry - 1) * 100

            win = min(POS_WIN, i + 1)
            lo = min(lows[max(0, i - win + 1):i + 1])
            hi = max(highs[max(0, i - win + 1):i + 1])
            pos = (closes[i] - lo) / (hi - lo) if hi > lo else 0.5
            chg = (closes[i] / closes[i - 1] - 1) * 100
            base20 = st["base20"]
            b60 = statistics.median(vols[max(0, i - 60):i]) if i >= 40 else base20
            quiet = (base20 / b60) < QUIET_RATIO if b60 > 0 else False
            flat = ((max(highs[i - 19:i + 1]) - min(lows[i - 19:i + 1]))
                    / closes[i]) < FLAT_RANGE
            prev_ratio = ratios.get(i - 1) or 0.0
            new_high20 = closes[i] >= max(closes[i - 20:i])
            ma20 = statistics.mean(closes[i - 19:i + 1])
            above_ma = closes[i] > ma20
            r = st["ratio20"]

            add("全样本基准", ret)
            if r >= SURGE:
                add("放量(≥2倍)", ret)
                add("  ├ 巨量(≥3倍)", ret) if r >= 3 else add("  ├ 明显放量(2~3)", ret)
                if quiet:
                    add("  ├ +前期缩量", ret)
                if pos <= 0.35:
                    add("  ├ +相对低位", ret)
                if -4 <= chg <= 6:
                    add("  ├ +涨幅温和", ret)
                if st["is_max60"]:
                    add("  ├ +60日最大量", ret)
                if prev_ratio >= 1.5:
                    add("  ├ 连续放量(前一日也放)", ret)
                else:
                    add("  ├ 首日放量", ret)
                if new_high20:
                    add("  ├ 放量突破20日新高", ret)
                if above_ma:
                    add("  ├ 放量站上20日线", ret)
                if quiet and pos <= 0.35:
                    add("  ├ 缩量+低位", ret)
                if quiet and pos <= 0.35 and -4 <= chg <= 6:
                    add("  ├ 缩量+低位+涨幅温和", ret)
                if quiet and pos <= 0.35 and new_high20:
                    add("  ├ 缩量+低位+突破新高", ret)
                if chg <= DROP_CHG:
                    add("  ├ 放量下跌", ret)
                if flat:
                    add("  ├ 僵尸债放量", ret)
            elif r >= 1.5:
                add("温和放量(1.5~2)", ret)
            else:
                add("未放量(<2倍)", ret)
                if pos <= 0.35 and quiet:
                    add("  └ 缩量低位但无量", ret)

        # ---- 事件研究: 单日大涨/大跌日之前有没有放量(检验"量在价先") ----
        for i in range(21, n):
            rj = ratios.get(i)
            if rj is None:
                continue
            chg_i = (closes[i] / closes[i - 1] - 1) * 100
            before1 = ratios.get(i - 1) or 0.0
            before3 = max(ratios.get(x) or 0.0 for x in range(i - 3, i))
            before5 = max(ratios.get(x) or 0.0 for x in range(i - 5, i))
            fwd3 = (max(closes[i + 1:i + 4]) / closes[i] - 1) * 100 if i + 3 < n else None

            kind = "up" if chg_i >= 5 else ("down" if chg_i <= -5 else None)
            if kind:
                e = ev[kind]
                e["n"] += 1
                e["ratios"].append(rj)
                if rj >= SURGE:
                    e["surge_on"] += 1
                if before1 >= SURGE:
                    e["surge_before1"] += 1
                if before3 >= SURGE:
                    e["surge_before3"] += 1
                if before5 >= SURGE:
                    e["surge_before5"] += 1
                else:
                    e["no_surge"] += 1

            # 反向条件概率: 不同量价状态 -> 未来3日内出现±5%单日异动
            if fwd3 is not None:
                hi3, dd3 = fwd3 >= 5, fwd3 <= -5
                keys = ["surge" if rj >= SURGE else "nosurge"]
                if rj >= SURGE and pos <= 0.35:
                    keys.append("放量+低位")
                if rj >= SURGE and pos <= 0.35 and -4 <= chg <= 6:
                    keys.append("放量+低位+涨幅温和")
                if rj >= SURGE and pos >= 0.65:
                    keys.append("放量+高位")
                if rj < 1.2 and pos <= 0.35:
                    keys.append("缩量+低位")
                for key in keys:
                    c = ev.setdefault(key, {"n": 0, "hit": 0, "hitdown": 0})
                    c["n"] += 1
                    if hi3:
                        c["hit"] += 1
                    if dd3:
                        c["hitdown"] += 1

    # ---- 打印 A ----
    order = ["全样本基准", "放量(≥2倍)", "  ├ 巨量(≥3倍)", "  ├ 明显放量(2~3)",
             "  ├ +前期缩量", "  ├ +相对低位", "  ├ +涨幅温和", "  ├ +60日最大量",
             "  ├ 首日放量", "  ├ 连续放量(前一日也放)",
             "  ├ 放量突破20日新高", "  ├ 放量站上20日线",
             "  ├ 缩量+低位", "  ├ 缩量+低位+涨幅温和", "  ├ 缩量+低位+突破新高",
             "  ├ 放量下跌", "  ├ 僵尸债放量", "温和放量(1.5~2)", "未放量(<2倍)",
             "  └ 缩量低位但无量"]
    rows_a = []
    print("A. 信号前瞻收益 (当日收盘确认信号, 次日开盘买入; 单位%)")
    print(f"{'信号':<26}{'样本':>7}", end="")
    for h in horizons:
        print(f"{'T+'+str(h)+'均值':>11}{'胜率':>7}{'中位':>8}", end="")
    print()
    print("-" * (26 + 7 + len(horizons) * 26))
    for name in order:
        if name not in buckets:
            continue
        b = buckets[name]
        cnt = len(b[horizons[0]])
        cells = []
        print(f"{name:<26}{cnt:>7}", end="")
        for h in horizons:
            v = b[h]
            if not v:
                print(f"{'-':>11}{'-':>7}{'-':>8}", end="")
                cells.append(None)
                continue
            wr = sum(1 for x in v if x > 0) / len(v) * 100
            mu, md = statistics.mean(v), statistics.median(v)
            cells.append((round(mu, 2), round(wr, 1), round(md, 2)))
            print(f"{mu:>11.2f}{wr:>7.1f}{md:>8.2f}", end="")
        print()
        rows_a.append({"signal": name.strip(" ├└"), "n": cnt, "cells": cells})

    # ---- 打印 B ----
    rows_b = []
    print("\nB. 事件研究: 单日大涨(≥+5%)/大跌(≤-5%)当天与之前, 量能是什么状态?")
    print(f"{'事件':<12}{'样本':>6}{'当日量比中位':>13}{'当日放量':>10}"
          f"{'前1日放量':>11}{'前3日内放过量':>15}{'前5日完全无量':>15}")
    for kind, label in (("up", "单日涨≥5%"), ("down", "单日跌≥5%")):
        e = ev[kind]
        if not e["n"]:
            continue
        n_ = e["n"]
        med = statistics.median(e["ratios"]) if e["ratios"] else 0
        vals = (round(med, 2), round(e['surge_on']/n_*100, 1),
                round(e['surge_before1']/n_*100, 1),
                round(e['surge_before3']/n_*100, 1),
                round(e['no_surge']/n_*100, 1))
        print(f"{label:<12}{n_:>6}{vals[0]:>13.2f}{vals[1]:>8.1f}%"
              f"{vals[2]:>9.1f}%{vals[3]:>13.1f}%{vals[4]:>13.1f}%")
        rows_b.append({"event": label, "n": n_, "vals": vals})

    rows_c = []
    print("\nC. 反向条件概率: 不同量价状态之后, 未来3日内出现单日±5%异动的概率")
    print(f"{'状态':<22}{'样本':>8}{'3日内涨≥5%':>14}{'3日内跌≥5%':>14}")
    for key, label in (("surge", "放量日"), ("nosurge", "未放量日"),
                       ("放量+低位", "放量+相对低位"),
                       ("放量+低位+涨幅温和", "放量+低位+涨幅温和"),
                       ("放量+高位", "放量+相对高位"),
                       ("缩量+低位", "缩量+相对低位")):
        c = ev.get(key)
        if not c or not c["n"]:
            continue
        pu, pd = round(c['hit']/c['n']*100, 2), round(c['hitdown']/c['n']*100, 2)
        print(f"{label:<22}{c['n']:>8}{pu:>12.2f}%{pd:>12.2f}%")
        rows_c.append({"state": label, "n": c['n'], "up": pu, "down": pd})

    print("\n注: 放量=当日量比(对前20日中位量)≥2; 收益未计交易成本; "
          "样本仅覆盖缓存K线的最近约100个交易日, 属单一市场阶段, 结论只作概率参考。")
    return {"horizons": list(horizons), "a": rows_a, "b": rows_b, "c": rows_c,
            "date": trade_date, "universe": len(klines)}


def write_report(out, stats):
    """把扫描结果 + 历史验证结论写成 Markdown 日报"""
    date = out["date"]
    lines = [f"# 可转债「突然放量」扫描日报 {date}", "",
             f"生成时间: {out['generated_at']}　|　全市场 "
             f"{out['counts']['universe']} 只 → 有效K线 {out['counts']['with_kline']}"
             f" → 成交额达标 {out['counts']['liquid']}"
             f" → 放量候选 {out['counts']['candidate']}", ""]

    def tbl(title, items):
        if not items:
            return
        lines.append(f"## {title}")
        lines.append("")
        lines.append("| # | 代码 | 名称 | 现价 | 涨幅% | 量比20 | 量比5 | 成交额(亿) "
                     "| 换手% | 60日分位 | 评分 | 标签 |")
        lines.append("|---|------|------|------|-------|--------|-------|-----------"
                     "|-------|----------|------|------|")
        for i, m in enumerate(items, 1):
            lines.append(
                f"| {i} | {m['code']} | {m['name']} | {m['price']:.2f} "
                f"| {m['chg']:+.2f} | {m['ratio20']:.2f} | {m['ratio5']:.2f} "
                f"| {m['amount_yi']:.2f} | {(m['turnover_rate'] or 0):.1f} "
                f"| {m['pos60']:.2f} | {m['score']:.1f} "
                f"| {'/'.join(m['tags'])} |")
        lines.append("")

    g = out.get("_groups", {})
    tbl("★ 首选：放量 + 相对低位（历史验证里唯一正期望的组合）", g.get("首选"))
    tbl("次选：放量但位置不低（追高型，历史 T+20 均值为负）", g.get("次选"))
    tbl("回避：放量下跌", g.get("回避"))

    if stats and stats.get("a"):
        hs = stats["horizons"]
        lines += ["## 历史验证：这些信号到底赚不赚钱", "",
                  f"样本: {stats['universe']} 只转债最近 {LOOKBACK} 个交易日日K；"
                  "当日收盘确认信号、次日开盘买入；未计交易成本。", "",
                  "| 信号 | 样本 | "
                  + " | ".join(f"T+{h} 均值% / 胜率% / 中位%" for h in hs) + " |",
                  "|------|------|" + "------|" * len(hs)]
        for r in stats["a"]:
            cells = ["-" if not c else f"{c[0]:.2f} / {c[1]:.1f} / {c[2]:.2f}"
                     for c in r["cells"]]
            lines.append(f"| {r['signal']} | {r['n']} | " + " | ".join(cells) + " |")
        lines.append("")

        if stats.get("b"):
            lines += ["## 事件研究：大涨那天／之前，量能是什么状态", "",
                      "| 事件 | 样本 | 当日量比中位 | 当日放量 | 前1日放量 "
                      "| 前3日内放过量 | 前5日完全无量 |",
                      "|------|------|--------------|----------|------------"
                      "|----------------|----------------|"]
            for r in stats["b"]:
                v = r["vals"]
                lines.append(f"| {r['event']} | {r['n']} | {v[0]:.2f} | {v[1]:.1f}% "
                             f"| {v[2]:.1f}% | {v[3]:.1f}% | {v[4]:.1f}% |")
            lines.append("")

        if stats.get("c"):
            lines += ["## 条件概率：不同量价状态之后，3日内出现 ±5% 异动的概率", "",
                      "| 状态 | 样本 | 3日内涨≥5% | 3日内跌≥5% |",
                      "|------|------|------------|------------|"]
            for r in stats["c"]:
                lines.append(f"| {r['state']} | {r['n']} | {r['up']:.2f}% "
                             f"| {r['down']:.2f}% |")
            lines.append("")

    lines += ["## 口径与局限", "",
              f"- 放量定义: 当日成交量 / 前{BASE_WIN}日中位量 ≥ {SURGE}（量比20）；"
              f"剔除成交额<{out['params']['min_amount_yi']}亿、近20日振幅<"
              f"{FLAT_RANGE:.0%} 的僵尸债、以及放量下跌。",
              "- 60日分位: 现价在近60日高低区间中的位置，越低说明越没涨过。",
              f"- 数据源: 东财快照 + 腾讯日K（成交量单位=手，与东财一致）；"
              f"日K缓存 data/klines_{out['date']}.json。",
              "- 局限: 样本只覆盖最近约100个交易日、单一市场阶段；未考虑正股题材、"
              "强赎进度、剩余规模变化；结论只作概率参考，不构成投资建议。",
              ""]
    path = os.path.join(BASE, f"放量扫描_{date}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--min-amount", type=float, default=MIN_AMOUNT / 1e8,
                    help="成交额门槛(亿元), 默认0.3")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--show-all", action="store_true")
    ap.add_argument("--validate", action="store_true", help="用K线缓存做信号验证")
    ap.add_argument("--force", action="store_true", help="忽略日K缓存, 重新抓取")
    ap.add_argument("--report", action="store_true",
                    help="扫描+验证并生成 Markdown 日报")
    args = ap.parse_args()
    if args.validate:
        return 0 if do_validate(args) else 1
    out = do_scan(args)
    if not isinstance(out, dict):
        return out
    if args.report:
        stats = do_validate(args)
        # 把验证统计回写进 JSON, 供看板「放量榜」页签展示真实数字
        out["validation"] = stats
        out.pop("_groups", None)
        jpath = os.path.join(DATA_DIR, f"volume_scan_{out['date']}.json")
        with open(jpath, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        path = write_report(out, stats)
        print(f"日报已写入 {os.path.relpath(path, BASE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
