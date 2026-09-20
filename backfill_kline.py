#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把所有历史快照里缺失的K线数据一次性补齐。

补两件事
--------
A. 缺失的正股日K (stock_kline_90d 为空)
   原因: 腾讯股票 qfq 行的第7个元素是 dict(分红信息), 早期 tx_kline 直接
   float(r[6]) 抛 TypeError 被上层静默吞掉 -> 正股K线整块为空。
   (fetch_daily.py 已修; 本脚本把历史快照补回来)

B. 缺失的成交额 (kline 末列为 0)
   原因: 抓日K两级源 东财 push2his(带成交额) -> 腾讯 ifzq(无成交额, 记0),
   东财限流熔断后整批走腾讯源。
   补法: 成交额 ≈ 成交量 × 典型价(高+低+收)/3 × 乘数
         转债 ×10 (1手=10张)
         正股 主板/创业板 ×100 (1手=100股)
         正股 科创板(688/689) ×1  <- 腾讯K线对科创板按"股"报量, 实测=东财手数×100

校准依据
--------
腾讯K线成交量与东财 f47 完全一致(比值 1.000);
转债 ×10: 30只样本对东财真实成交额 平均绝对误差 0.36%, 最大 1.01%;
正股 ×100: 实测 成交额/(量×价) = 98.7 ~ 103.3。

原则
----
· 已有的真实数据(非空K线、非0成交额)一律不动;
· 幂等, 可重复运行;
· 每个快照写入 kline_backfill 标记, 记录补了什么、什么时候补的。
· 正股K线为前复权, 用当前复权因子回填历史快照, 与当日当时抓取的可能有细微差异。

用法
----
  python3 backfill_kline.py --dry-run
  python3 backfill_kline.py
  python3 backfill_kline.py --only 2026-09-15
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

import fetch_daily as F

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")

# 乘数统一由 fetch_daily.amount_multiplier 给出(已按板块区分)


def est_amount(row, mult):
    """末列成交额缺失时按 量×典型价×每手张数 估算(元)"""
    c, h, l, v = row[2], row[3], row[4], row[5]
    if not v or not c:
        return None
    return round(v * (h + l + c) / 3.0 * mult, 2)


def fill_series(rows, mult):
    """只把成交额为 0 的行补上; 返回补了几行"""
    n = 0
    for r in rows:
        if len(r) > 6 and not r[6]:
            a = est_amount(r, mult)
            if a:
                r[6] = a
                n += 1
    return n


def stock_mult(code):
    return F.amount_multiplier(code, False)


def bond_mult(code):
    return F.amount_multiplier(code, True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计, 不写盘")
    ap.add_argument("--only", help="只处理某个日期 (YYYY-MM-DD)")
    ap.add_argument("--no-fetch", action="store_true",
                    help="不联网补正股K线, 只补已有K线的成交额")
    args = ap.parse_args()

    files = sorted(f for f in os.listdir(DATA_DIR)
                   if len(f) == 15 and f.endswith(".json") and f[0].isdigit())
    if args.only:
        files = [f for f in files if f.startswith(args.only)]
    if not files:
        print("data/ 下没有找到快照文件", file=sys.stderr)
        return 1

    snaps = {}
    for f in files:
        with open(os.path.join(DATA_DIR, f), encoding="utf-8") as fp:
            snaps[f] = json.load(fp)

    # ---------- A. 找出缺失的正股 ----------
    missing = {}          # stock_code -> [快照文件名...]
    for f, d in snaps.items():
        for b in d.get("bonds", []):
            sc = b.get("stock_code")
            if sc and not (b.get("stock_kline_90d") or []):
                missing.setdefault(sc, []).append(f)

    print(f"缺失正股日K: {len(missing)} 只正股, 涉及 "
          f"{sum(len(v) for v in missing.values())} 条转债记录")

    fetched, failed = {}, []
    if missing and not args.no_fetch:
        for i, sc in enumerate(sorted(missing), 1):
            sym = F.tx_stock_symbol(sc)
            try:
                k = F.tx_kline(sym, 90)
            except Exception as e:
                failed.append((sc, str(e)[:60]))
                continue
            if k:
                fill_series(k, stock_mult(sc))
                fetched[sc] = k
            else:
                failed.append((sc, "空数据"))
            if i % 20 == 0:
                print(f"  抓取正股K线 {i}/{len(missing)} ...", file=sys.stderr)
            time.sleep(0.15)
        print(f"  成功 {len(fetched)} 只" + (f", 失败 {len(failed)} 只" if failed else ""))
        for sc, why in failed[:10]:
            print(f"    ✗ {sc}: {why}")

    # ---------- 写回 ----------
    print(f"\n{'快照':<13}{'补正股K线':>10}{'转债补额':>9}{'正股补额':>9}"
          f"{'已有真实额':>11}{'状态':>8}")
    print("-" * 62)
    t_stock = t_bond = t_stockamt = t_real = 0
    for f, d in snaps.items():
        n_stock = 0
        for b in d.get("bonds", []):
            sc = b.get("stock_code")
            if sc and not (b.get("stock_kline_90d") or []) and sc in fetched:
                b["stock_kline_90d"] = [list(r) for r in fetched[sc]]
                n_stock += 1
        nb = ns = nreal = 0
        for b in d.get("bonds", []):
            # 先统计本次补之前就已经有成交额(真实值)的行数, 再补
            nreal += sum(1 for r in (b.get("kline_90d") or [])
                         if len(r) > 6 and r[6])
            nb += fill_series(b.get("kline_90d") or [], bond_mult(b["code"]))
            ns += fill_series(b.get("stock_kline_90d") or [],
                              stock_mult(b.get("stock_code") or ""))
        changed = (nb + ns + n_stock) > 0
        t_stock += n_stock
        t_bond += nb
        t_stockamt += ns
        t_real += nreal
        print(f"{f[:-5]:<13}{n_stock:>10}{nb:>9}{ns:>9}{nreal:>11}"
              f"{('待写入' if not args.dry_run else '需补') if changed else '完整':>8}")
        if changed and not args.dry_run:
            d["kline_backfill"] = {
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "stock_kline_filled": n_stock,
                "bond_amount_rows": nb, "stock_amount_rows": ns,
                "amount_method": "量(手)×典型价(高+低+收)/3×每手张数 (转债×10, 正股×100)",
                "note": "仅补空K线与为0的成交额; 已有真实数据未改动",
            }
            with open(os.path.join(DATA_DIR, f), "w", encoding="utf-8") as fp:
                json.dump(d, fp, ensure_ascii=False)

    print("-" * 62)
    print(f"合计: 补正股K线 {t_stock} 条, 转债成交额 {t_bond} 行, "
          f"正股成交额 {t_stockamt} 行; 已有真实成交额 {t_real} 行未改动")
    if args.dry_run:
        print("(dry-run: 没有写入任何文件)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
