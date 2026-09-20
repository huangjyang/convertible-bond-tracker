#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按需补齐「非当日热度榜」个券的详情: 分时(转债/正股) + 正股日K + 新闻/行业。

为什么需要:
    每日快照 data/YYYY-MM-DD.json 只对**当日 Top30** 抓分时/正股/归因/新闻
    (fetch_daily.py 第 5 步)。从「放量扫描」页签点进来的券通常不在榜里, 本地
    就没有分时, 弹窗只能显示"分时数据缺失"。这里复用 fetch_daily 现成的抓取
    函数按需补抓, 抓完缓存到 data/bond_detail/<日期>/<代码>.json。

口径:
    - 只读外部数据源, 不修改任何快照文件;
    - 缓存按「日期 + 代码」存, 同一天重复打开不再发请求;
    - 抓取失败只返回 {"error": ...}, 不抛异常(接口要能优雅降级)。
"""
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))

import fetch_daily as fd  # noqa: E402  (脚本同目录, 依赖 BASE 在 sys.path 里)

CACHE_DIR = os.path.join(BASE, "data", "bond_detail")
# 放量扫描里"值得点开看"的三组 -> 每天扫描后顺手预热
PREWARM_GROUPS = ("放量温和上涨", "放量拉升", "放量滞涨")
# 缓存结构版本: 加了新字段就 +1, 老缓存自动失效重抓(否则新字段永远补不上)
CACHE_VERSION = 3   # 2->3: 归因要 price 字段, 老缓存重抓

_locks = {}
_locks_guard = threading.Lock()


def _code_lock(code):
    """每只券一把锁: 并发点开/预热同一只券时只发一次网络请求"""
    with _locks_guard:
        return _locks.setdefault(code, threading.Lock())


def cache_path(date, code):
    return os.path.join(CACHE_DIR, str(date), f"{code}.json")


def read_cache(date, code):
    try:
        with open(cache_path(date, code), encoding="utf-8") as f:
            obj = json.load(f)
    except Exception:
        return None
    return obj if obj.get("v") == CACHE_VERSION else None   # 老版本缓存当作没有


def _write_cache(date, code, obj):
    p = cache_path(date, code)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, p)          # 原子替换: 前端不会读到写了一半的文件


def fetch_detail(code, record=None, date=None):
    """现抓一只券的详情(走网络)。返回 dict; 失败带 error 字段。

    record: 放量扫描里的那只券记录(字段最全: price/chg/premium/scale/... ), 用来算归因。
    """
    record = dict(record or {})
    if record.get("price") is None and record.get("close") is not None:
        record["price"] = record["close"]      # 蓄势榜记录用 close 命名, analyze_drivers 要 price
    stock_code = record.get("stock_code")
    stock_name = record.get("stock_name")
    bond_name = record.get("name")
    out = {"v": CACHE_VERSION, "code": code, "date": date,
           "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    try:
        minute = fd.fetch_trends(fd.tx_bond_symbol(code),
                                 f"{fd.bond_market(code)}.{code}")
    except Exception as e:
        return {**out, "error": f"分时抓取异常: {e}"}
    if not minute or not (minute.get("points") or []):
        return {**out, "error": "分时抓取失败（腾讯/东财都没有返回）"}
    out["minute"] = minute
    out["minute_date"] = minute.get("date")
    if stock_code:
        tx_s, em_s = fd.tx_stock_symbol(stock_code), \
            f"{fd.stock_market(stock_code)}.{stock_code}"
        try:
            out["stock_minute"] = fd.fetch_trends(tx_s, em_s)
        except Exception:
            out["stock_minute"] = None
        try:
            out["stock_kline_90d"] = fd.est_amount(
                fd.fetch_kline(tx_s, 90, em_s),
                fd.amount_multiplier(stock_code, False))
        except Exception:
            out["stock_kline_90d"] = []
        try:
            out["industry"] = fd.fetch_stock_industry(stock_code)
        except Exception:
            out["industry"] = None
        try:
            out["news"] = fd.fetch_news(stock_name or code, bond_name or code, date)
        except Exception:
            out["news"] = []
    # 归因: 与当日榜完全同一套算法(analyze_drivers 只看券本身字段 + 分时), 所以非榜内券也算得出来
    try:
        out["drivers"] = fd.analyze_drivers(record, None, out.get("minute"),
                                            out.get("stock_minute")) if record else []
    except Exception:
        out["drivers"] = []
    return out


def get(code, date, meta=None, fetch=False):
    """取一只券的详情。默认只看缓存; fetch=True 时才发网络请求。

    meta: 放量扫描的券记录(或快照索引), 用于算归因/拿正股代码。
    返回 (payload, from_cache)。payload 一定带 code/date; 命中缓存带 cached=True。
    """
    meta = meta or {}
    hit = read_cache(date, code)
    if hit:
        return {**hit, "cached": True}, True
    if not fetch:
        return {"cached": False, "code": code, "date": date}, False
    with _code_lock(code):
        hit = read_cache(date, code)          # 双检: 可能刚被别的线程抓完
        if hit:
            return {**hit, "cached": True}, True
        d = fetch_detail(code, meta, date)
        if "error" not in d:
            _write_cache(date, code, d)
        d["date"] = date
        return {**d, "cached": False}, False


def prewarm(date, records, workers=6, only_groups=True):
    """把放量扫描的候选券分时补进缓存(扫描后自动跑, 已缓存的跳过)。

    records: [{"code","name","stock_code","stock_name", ...}, ...]
    返回统计 dict。
    """
    stats = {"total": len(records), "fetched": 0, "failed": 0, "skipped": 0}
    todo = []
    for r in records:
        if read_cache(date, r["code"]):
            stats["skipped"] += 1
        else:
            todo.append(r)
    if not todo:
        return stats

    def one(r):
        d, _ = get(r["code"], date, r, fetch=True)
        return "error" not in d

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for ok in ex.map(one, todo):
            stats["fetched" if ok else "failed"] += 1
    return stats
