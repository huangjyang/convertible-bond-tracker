#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板块资金流采集 + 转债映射 —— 盘中的「让信息主动找我」数据源。

背景: 看板现有数据都是日频(收盘后 15:30 抓一次, 且靠打开页面触发)。
要让系统主动推信息, 盘中必须回答两个问题: 哪个行业正在被资金追? 那个行业里有哪些转债?

数据源(均为实测结论, 不是文档推测):
  - 东财 push2delay.eastmoney.com/api/qt/clist/get, fs="m:90 t:2" = 申万行业板块,
    共 496 个, 单页上限 100 需翻 5 页。字段 f62 = 主力净流入(单位: 元)。
  - 坑1(必踩): push2.eastmoney.com 对本机 IP 直接断连(RemoteDisconnected, 0.2s 就断),
    必须走 push2delay。代价是行情有延迟, 但资金流本身就是当日累计值, 用于分钟级简报无妨。
  - 坑2(会影响正确性): 这 496 个板块把申万一级(电子 BK1201)/二级(半导体 BK1036)/
    三级(数字芯片设计 BK1331)混在同一个列表里。做榜或求和时**绝对不要跨层相加**,
    否则重复计数 —— "电子 +198亿" 里已经包含了 "半导体 +182亿"。
    本脚本的做法: 每只转债只归属**唯一一个**板块(按名称精确匹配), 不做任何加总。
  - 坑3: 转债列表里的 f127 返回的是数字(涨跌幅)而不是行业, 批量拿不到行业归属,
    只能逐只正股查(见 fetch_daily.fetch_stock_industry)。约 160ms/只, 全市场 ~50s,
    所以结果缓存起来, 之后只补新券。

映射关系(关键结论): 转债记录里的 industry 字段(东财 f127)与板块名**属于同一套申万分类**,
  实测 30 个 distinct 值 30/30 精确匹配且板块名无重名 → 运行时按名称精确匹配即可,
  **不需要手写映射表**。(板块名带 Ⅱ 后缀的如 "特钢Ⅱ/环保设备Ⅱ" 在两边都带, 一致。)

产出:
  data/bond_industry.json        转债→行业→板块 映射缓存(增量刷新, 只补新券)
  data/sector_boards.json        板块名录快照(名称/代码/资金流, 也作为离线测试的基准)
  data/sector_flow_<日期>.json   当天多次采集的时序(snapshots), 用来判断资金是加速还是减速

用法:
  python3 sector_flow.py                 # 采集一次(首次会先建映射缓存, 约 50s)
  python3 sector_flow.py --map-only      # 只刷新映射缓存
  python3 sector_flow.py --rebuild-map   # 全量重建映射缓存(行业变了/怀疑漂移时)
  python3 sector_flow.py --quiet
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
DATA_DIR = os.path.join(BASE, "data")

import fetch_daily as fd       # noqa: E402  复用 get_json(带退避重试) / 全市场列表 / f127 行业

# ---------------------------------------------------------------- 常量
BOARD_FS = "m:90 t:2"                       # 申万行业板块(一/二/三级混排, 见模块 docstring 坑2)
BOARD_FIELDS = "f12,f14,f3,f62,f184,f66,f72,f78,f84"
PAGE = 100                                  # clist 单页硬上限, 传更大也无效
PAGE_SLEEP = 1.5                            # 页间节流: 上游对突发会断连(实测)
PAGE_MAX = 8                                # 496/100=5 页, 留余量防死循环
EM_DELAY = "https://push2delay.eastmoney.com"
EM_REF = "https://data.eastmoney.com/bkzj/hy.html"

MAP_FILE = os.path.join(DATA_DIR, "bond_industry.json")
BOARDS_FILE = os.path.join(DATA_DIR, "sector_boards.json")
MAP_VERSION = 1

SNAP_KEEP = 60          # 每天最多保留的时点数(约 5 分钟一次 × 4 小时)
SNAP_MIN_GAP = 60       # 与上一时点间隔小于该秒数 -> 覆盖而不追加(重复跑不产生垃圾时点)
HOT_IN, HOT_OUT = 15, 10
BOND_CAP = 25           # 单个板块最多列出的转债数(超出记 bond_total)
# 时点里每只券保留的字段。刻意不含 stock_code/stock_name/redeem_trigger 等:
# 那些是日内不变或变化很慢的属性, 放在映射缓存与既有日频快照里就够;
# 若每个时点都重复存一遍, 48 个时点/天会把文件撑到 3.7MB。
BOND_FIELDS = ("code", "name", "price", "chg", "turnover_yi", "premium", "stock_chg")


# ---------------------------------------------------------------- 小工具
def _yi(v):
    """元 -> 亿元(保留 2 位); None 透传。"""
    n = fd.num(v)
    return None if n is None else round(n / 1e8, 2)


def _atomic_write(path, obj, indent=None):
    """原子写: 前端/别的进程不会读到写了一半的文件。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)


def session_of(now=None):
    """当前处于哪个交易时段 -> (session, live)。

    live=False 表示数据不是当日的实时进度, **推送前必须检查这个标志**,
    否则会在周末/盘前把上一交易日的收盘资金流当成"刚刚流入 XX 亿"推出去。

    局限: 不认法定节假日(只看周末), 节假日会被判成"盘前" —— 此时 live 仍为 False,
    方向是安全的(宁可不推), 但不要据此判断"今天是交易日"。
    """
    now = now or datetime.now()
    if now.weekday() >= 5:
        return "休市", False
    hm = now.hour * 100 + now.minute
    if hm < 915:
        return "盘前", False
    if hm < 930:
        return "集合竞价", False
    if hm <= 1130:
        return "盘中(上午)", True
    if hm < 1300:
        return "午间休市", True          # 数据冻结在 11:30, 但仍是当日进度
    if hm <= 1500:
        return "盘中(下午)", True
    return "盘后", False


# ---------------------------------------------------------------- 板块名录
def clean_board(r):
    """clist 行 -> 精简板块记录(资金单位一律换算成亿元)。"""
    return {
        "code": r.get("f12"),
        "name": r.get("f14"),
        "pct_chg": fd.num(r.get("f3")),
        "main_net_yi": _yi(r.get("f62")),        # 主力净流入 = 超大单 + 大单
        "main_pct": fd.num(r.get("f184")),       # 主力净占比 %
        "super_yi": _yi(r.get("f66")),
        "big_yi": _yi(r.get("f72")),
        "mid_yi": _yi(r.get("f78")),
        "small_yi": _yi(r.get("f84")),
    }


def fetch_boards(quiet=False):
    """拉全部行业板块 -> (boards, complete)。complete=False 表示有页失败(部分结果)。"""
    out, seen, total, pn = [], set(), None, 1
    while pn <= PAGE_MAX:
        url = (f"{EM_DELAY}/api/qt/clist/get?" +
               urllib.parse.urlencode({
                   "pn": pn, "pz": PAGE, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                   "fid": "f62", "fs": BOARD_FS, "fields": BOARD_FIELDS,
               }))
        try:
            data = (fd.get_json(url, referer=EM_REF).get("data") or {})
        except Exception as e:
            print(f"⚠ 板块第 {pn} 页失败({e}) —— 本次结果可能不完整", file=sys.stderr)
            return out, False
        if total is None:
            total = data.get("total") or 0
        diff = data.get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        if not diff:
            break
        for r in diff:
            c = r.get("f12")
            if c and c not in seen:
                seen.add(c)
                out.append(clean_board(r))
        if total and len(out) >= total:
            break
        pn += 1
        time.sleep(PAGE_SLEEP)
    return out, bool(out)


def board_index(boards):
    """名称 -> 板块。实测 496 个板块名无重名, 所以这个索引是无歧义的。

    注意: 不要跨层比较。索引里同时有 "电子"(一级) 和 "半导体"(二级),
    各自独立 —— 转债的 industry 只会命中其中一个, 这正是我们要的。
    """
    return {b["name"]: b for b in boards if b.get("name")}


# ---------------------------------------------------------------- 映射缓存
def load_map():
    try:
        with open(MAP_FILE, encoding="utf-8") as f:
            m = json.load(f)
        return m if m.get("v") == MAP_VERSION else {"v": MAP_VERSION, "items": {}}
    except Exception:
        return {"v": MAP_VERSION, "items": {}}


def _industry_of(stock_code):
    """取正股行业; 失败返回 None。复用 fetch_daily(它内部已做单次快速尝试)。"""
    try:
        return fd.fetch_stock_industry(stock_code)
    except Exception:
        return None


def refresh_map(universe, by_name, quiet=False, rebuild=False):
    """增量刷新 转债→行业→板块 映射。只有缺的/失败的才发网络请求。

    universe: [{code, name, stock_code, stock_name}, ...]
    返回 (map_dict, stats)
    """
    m = {"v": MAP_VERSION, "items": {}} if rebuild else load_map()
    items = m["items"]
    todo = []
    for b in universe:
        code, sc = b.get("code"), b.get("stock_code")
        if not code or not sc or sc == "-":
            continue                      # 无正股的券(极少)明确不在映射里, 不算失败
        if rebuild or not items.get(code, {}).get("industry"):
            todo.append((code, sc, b.get("stock_name")))

    built, failed = 0, []
    for i, (code, sc, sname) in enumerate(todo, 1):
        ind = _industry_of(sc)
        if not ind:
            failed.append((code, sc, sname))
            continue
        bd = by_name.get(ind)
        items[code] = {
            "stock_code": sc, "stock_name": sname, "industry": ind,
            # 板块代码自证: 下次运行若与实时名录不符 -> 说明东财改了代码/改名, 会打警告
            "board_code": bd["code"] if bd else None,
            "board_name": bd["name"] if bd else None,
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        built += 1
        if not quiet and i % 50 == 0:
            print(f"   映射进度 {i}/{len(todo)} ...", file=sys.stderr)

    # 失败的重试一轮(首轮偶发超时居多)
    for code, sc, sname in list(failed):
        ind = _industry_of(sc)
        if ind:
            bd = by_name.get(ind)
            items[code] = {
                "stock_code": sc, "stock_name": sname, "industry": ind,
                "board_code": bd["code"] if bd else None,
                "board_name": bd["name"] if bd else None,
                "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            built += 1
            failed.remove((code, sc, sname))

    m["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m["size"] = len(items)
    return m, {"todo": len(todo), "built": built, "failed": [f[0] for f in failed]}


def check_map_drift(m, by_name):
    """缓存里的板块代码/名称与实时名录对不上 -> 报告(东财改代码是真实存在的风险)。"""
    drift = []
    for code, it in m.get("items", {}).items():
        ind = it.get("industry")
        bd = by_name.get(ind) if ind else None
        if bd is None:
            drift.append({"code": code, "industry": ind, "issue": "行业在板块名录里找不到"})
        elif it.get("board_code") and it["board_code"] != bd["code"]:
            drift.append({"code": code, "industry": ind,
                          "issue": f"板块代码变了 {it['board_code']} -> {bd['code']}"})
    return drift


# ---------------------------------------------------------------- join(纯函数, 可离线测试)
def join(bonds, boards):
    """把带 industry 的转债归属到板块。

    bonds: [{code, name, industry, ...}]  (industry 为空/未匹配 -> 进 unmapped, 绝不静默丢弃)
    boards: 板块列表
    -> (hits, unmapped)  hits 按主力净流入降序
    """
    by_name = board_index(boards)
    hits, unmapped = {}, {}
    for b in bonds:
        ind = b.get("industry")
        bd = by_name.get(ind) if ind else None
        if bd is None:
            unmapped.setdefault(ind or "(无行业)", []).append(b["code"])
            continue
        h = hits.setdefault(bd["code"], {
            "board_code": bd["code"], "board_name": bd["name"],
            "main_net_yi": bd.get("main_net_yi"), "main_pct": bd.get("main_pct"),
            "pct_chg": bd.get("pct_chg"), "bonds": [],
        })
        h["bonds"].append({k: b.get(k) for k in BOND_FIELDS})
    for h in hits.values():
        h["bonds"].sort(key=lambda x: -(x.get("turnover_yi") or 0))
        h["bond_total"] = len(h["bonds"])
        h["bonds"] = h["bonds"][:BOND_CAP]
    out = sorted(hits.values(), key=lambda h: -(h.get("main_net_yi") or 0))
    return out, unmapped


def top_flows(boards, n_in=HOT_IN, n_out=HOT_OUT):
    """净流入 Top / 净流出 Top。各自只看一个层级内的绝对值, 不做跨层加总。"""
    ok = [b for b in boards if b.get("main_net_yi") is not None]
    si = sorted(ok, key=lambda b: -b["main_net_yi"])[:n_in]
    so = sorted(ok, key=lambda b: b["main_net_yi"])[:n_out]
    return si, so


def _slim(b):
    """写进时序文件的板块字段(省空间, 不存超大/大/中小单明细)。"""
    return {k: b.get(k) for k in ("code", "name", "pct_chg", "main_net_yi", "main_pct")}


# ---------------------------------------------------------------- 采集主流程
def collect(quiet=False, rebuild_map=False, map_only=False):
    t0 = time.time()
    sess, live = session_of()

    boards, complete = fetch_boards(quiet=quiet)
    if not boards:
        print("✗ 板块资金流拿不到(检查网络/push2delay 是否可达)", file=sys.stderr)
        return 1
    by_name = board_index(boards)

    # 全市场转债(顺带拿到正股代码/名字, 用于映射)
    universe = []
    try:
        for r in fd.fetch_cb_universe():
            p = fd.parse_cb_row(r)
            if not p:
                continue
            p["turnover_yi"] = round((p.get("turnover") or 0) / 1e8, 2)
            universe.append(p)
    except Exception as e:
        print(f"✗ 转债全市场列表失败: {e}", file=sys.stderr)
        return 1

    m, st = refresh_map(universe, by_name, quiet=quiet, rebuild=rebuild_map)
    _atomic_write(MAP_FILE, m)
    drift = check_map_drift(m, by_name)

    bonds = []
    for u in universe:
        it = m["items"].get(u["code"]) or {}
        b = dict(u)
        b["industry"] = it.get("industry")
        bonds.append(b)
    hits, unmapped = join(bonds, boards)
    hot_in, hot_out = top_flows(boards)

    snap = {
        "t": datetime.now().strftime("%H:%M:%S"),
        "session": sess,
        "live": live,
        "boards_total": len(boards),
        "boards_complete": complete,
        "hot_in": [_slim(b) for b in hot_in],
        "hot_out": [_slim(b) for b in hot_out],
        "hits": hits,
        "unmapped": {k: v for k, v in unmapped.items()},
        "map_stats": st,
        "drift": drift,
    }

    date = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(DATA_DIR, f"sector_flow_{date}.json")
    doc = {"date": date, "updated_at": snap["t"], "source": f"{EM_DELAY} fs={BOARD_FS}",
           "unit": "亿元", "notes": [
               "板块列表把申万一级/二级/三级混排, 不要跨层相加(电子已含半导体)",
               "push2.eastmoney.com 对本机 IP 断连, 只能走 push2delay(行情有延迟)",
               "live=false 时数据不是当日实时进度, 推送前必须检查",
           ], "snapshots": []}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
            if old.get("date") == date and isinstance(old.get("snapshots"), list):
                doc["snapshots"] = old["snapshots"]
        except Exception:
            pass
    # 间隔太近 -> 覆盖上一个时点, 避免重复跑堆垃圾
    if doc["snapshots"]:
        last = doc["snapshots"][-1]
        try:
            lt = datetime.strptime(f"{date} {last['t']}", "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - lt).total_seconds() < SNAP_MIN_GAP:
                doc["snapshots"][-1] = snap
            else:
                doc["snapshots"].append(snap)
        except Exception:
            doc["snapshots"].append(snap)
    else:
        doc["snapshots"].append(snap)
    doc["snapshots"] = doc["snapshots"][-SNAP_KEEP:]
    _atomic_write(path, doc)
    _atomic_write(BOARDS_FILE, {"updated_at": snap["t"], "count": len(boards),
                                "boards": boards})

    if not quiet:
        print(f"板块 {len(boards)} 个{' (不完整!)' if not complete else ''} · "
              f"转债 {len(universe)} 只 · 映射 {m['size']} 条 "
              f"(本次新增 {st['built']}/{st['todo']}"
              + (f", 失败 {len(st['failed'])}" if st["failed"] else "") + ")")
        print(f"时段: {sess}" + ("" if live else "  ⚠ 非交易时段, 数据非当日实时进度"))
        if drift:
            print(f"⚠ 映射漂移 {len(drift)} 条: {drift[:3]}")
        if unmapped:
            tot = sum(len(v) for v in unmapped.values())
            print(f"⚠ 未映射 {tot} 只: " + ", ".join(f"{k}({len(v)})" for k, v in unmapped.items()))
        print("── 主力净流入 Top ──")
        for b in hot_in[:8]:
            h = next((x for x in hits if x["board_code"] == b["code"]), None)
            tag = f"  ← {h['bond_total']} 只转债" if h else ""
            print(f"  {b['name']:<12} {b['main_net_yi']:+8.2f}亿  涨{b['pct_chg']}%{tag}")
        print("── 有转债的板块(按净流入) ──")
        for h in hits[:8]:
            names = "、".join(f"{x['name']}({x['chg']:+.1f}%)" for x in h["bonds"][:5]
                             if x.get("chg") is not None)
            print(f"  {h['board_name']:<12} {h['main_net_yi']:+8.2f}亿  {h['bond_total']}只: {names}")
        print(f"  → {os.path.relpath(path, BASE)}  时点 {len(doc['snapshots'])} 个 "
              f"(用時 {time.time()-t0:.1f}s)")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--rebuild-map", action="store_true", help="全量重建映射缓存")
    ap.add_argument("--map-only", action="store_true", help="只刷新映射缓存")
    args = ap.parse_args()
    if args.map_only:
        boards, _ = fetch_boards(quiet=args.quiet)
        if not boards:
            return 1
        universe = []
        for r in fd.fetch_cb_universe():
            p = fd.parse_cb_row(r)
            if p:
                universe.append(p)
        m, st = refresh_map(universe, board_index(boards), quiet=args.quiet,
                            rebuild=args.rebuild_map)
        _atomic_write(MAP_FILE, m)
        print(f"映射缓存: {m['size']} 条 (本次新增 {st['built']}/{st['todo']}, "
              f"失败 {len(st['failed'])})")
        return 0
    return collect(quiet=args.quiet, rebuild_map=args.rebuild_map)


if __name__ == "__main__":
    sys.exit(main())
