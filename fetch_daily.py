#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
可转债热度榜 每日跟踪 - 数据抓取脚本
数据源: 东方财富(延迟行情, 盘后即收盘数据)
筛选(固定口径, 见下方 MAX_PRICE/MIN_TURNOVER/MAX_SCALE/TOP_N):
  价格<=400 且 成交额>2亿 且 发行规模<10亿, 按成交额取前30
产出: data/YYYY-MM-DD.json
注意: 修改下面筛选常量后, 需重新抓取各交易日快照, 否则历史口径不一致。
"""
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# ===== 固定筛选口径 (单一来源) =====
MAX_PRICE = 400.0   # 转债价格上限
MIN_TURNOVER = 2e8  # 当日成交额下限(元)
MAX_SCALE = 10.0    # 发行规模上限(亿)
TOP_N = 30          # 按成交额取前 N

INDEXES = [
    ("sh", "上证指数", "sh000001", "1.000001"),
    ("sz", "深证成指", "sz399001", "0.399001"),
    ("cb_idx", "中证转债", "sh000832", "1.000832"),
]


def get_json(url, referer=None, retries=3):
    last_err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Referer": referer or "https://quote.eastmoney.com/",
            })
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            if not raw.strip():
                raise RuntimeError("empty response")
            # jsonp strip
            m = re.match(r"^[a-zA-Z_$][\w$]*\((.*)\)[;]?$", raw.strip(), re.S)
            if m:
                raw = m.group(1)
            return json.loads(raw)
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url[:120]} :: {last_err}")


def bond_market(code):
    return 1 if code[:2] in ("10", "11") else 0


def stock_market(code):
    return 1 if code.startswith("6") else 0


def tx_bond_symbol(code):
    return ("sh" if bond_market(code) == 1 else "sz") + code


def tx_stock_symbol(code):
    if code.startswith("6"):
        return "sh" + code
    if code[:1] in ("4", "8"):
        return "bj" + code
    return "sz" + code


def num(v):
    """ '-' 或 None -> None """
    if v in ("-", None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 可转债列表

def fetch_cb_universe():
    """沪深全部可转债(clist 单页上限100, 需翻页), 按成交额降序"""
    out = []
    for mkt in (0, 1):
        for pn in (1, 2, 3, 4):
            url = ("https://push2delay.eastmoney.com/api/qt/clist/get?"
                   + urllib.parse.urlencode({
                       "pn": pn, "pz": 100, "po": 1, "np": 1,
                       "fltt": 2, "invt": 2, "fid": "f6",
                       "fs": f"m:{mkt}+b:MK0354",
                       "fields": ("f2,f3,f5,f6,f8,f12,f14,f15,f16,f17,f18,"
                                  "f229,f230,f232,f234,f235,f236,f237,f238,"
                                  "f239,f240,f241"),
                   }))
            data = (get_json(url).get("data") or {})
            diff = data.get("diff") or []
            out.extend(diff)
            if len(diff) < 100 or pn * 100 >= (data.get("total") or 0):
                break
            time.sleep(0.2)
    return out


def parse_cb_row(r):
    price = num(r.get("f2"))
    turnover = num(r.get("f6"))
    if price is None or turnover is None or price <= 0:
        return None
    return {
        "code": r["f12"], "name": r["f14"],
        "price": price, "chg": num(r.get("f3")) or 0.0,
        "turnover": turnover,
        "volume": num(r.get("f5")), "turnover_rate": num(r.get("f8")),
        "open": num(r.get("f17")), "high": num(r.get("f15")),
        "low": num(r.get("f16")), "pre_close": num(r.get("f18")),
        "stock_code": r.get("f232"), "stock_name": r.get("f234"),
        "stock_price": num(r.get("f229")), "stock_chg": num(r.get("f230")) or 0.0,
        "convert_price": num(r.get("f235")), "convert_value": num(r.get("f236")),
        "premium": num(r.get("f237")), "pure_bond_premium": num(r.get("f238")),
        "put_trigger": num(r.get("f239")), "redeem_trigger": num(r.get("f240")),
        "maturity_redeem": num(r.get("f241")),
    }


# ---------------------------------------------------------------- 分时 & K线
# 主源: 腾讯 ifzq; 备源: 东财 push2his(可能限流)

def tx_minute(symbol):
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={symbol}"
    d = get_json(url, referer="https://gu.qq.com/")
    node = (d.get("data") or {}).get(symbol) or {}
    inner = (node.get("data") or {})
    rows = inner.get("data") or []
    if not rows:
        return None
    points = []
    for line in rows:
        p = line.split(" ")
        points.append({
            "t": f"{p[0][:2]}:{p[0][2:]}",
            "p": float(p[1]),
            "v": float(p[2]),
            "avg": None,
        })
    pre = None
    try:
        pre = float(node["qt"][symbol][4])
    except Exception:
        pass
    dstr = inner.get("date") or ""
    date = f"{dstr[:4]}-{dstr[4:6]}-{dstr[6:8]}" if len(dstr) == 8 else None
    return {"preClose": pre, "points": points, "date": date}


def tx_kline(symbol, lmt=90):
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={symbol},day,,,{lmt},qfq")
    d = get_json(url, referer="https://gu.qq.com/")
    node = (d.get("data") or {}).get(symbol) or {}
    # 债券行情放在 day 键下; 股票 qfq 复权行情放在 qfqday 键下(修复正股K线为空)
    rows = node.get("day") or node.get("qfqday") or node.get("hfqday") or []
    out = []
    for r in rows:
        # [date, open, close, high, low, volume(, amount...)]; 腾讯一般无成交额 -> 记0
        # 注意: 股票的 qfq 行可能带第7个元素且是 dict(分红信息等), 不能直接 float()
        #       —— 早期这里会抛 TypeError 被上层吞掉, 导致正股K线整块为空。
        amt = 0.0
        if len(r) > 6:
            try:
                amt = float(r[6])
            except (TypeError, ValueError):
                amt = 0.0
        out.append([r[0], float(r[1]), float(r[2]), float(r[3]),
                    float(r[4]), float(r[5]), amt])
    return out


def em_trends(secid):
    url = (f"https://push2his.eastmoney.com/api/qt/stock/trends2/get?secid={secid}"
           "&fields1=f1,f2,f3,f7,f8&fields2=f51,f53,f56,f58&iscr=0&ndays=1")
    data = get_json(url, retries=1).get("data")
    if not data or not data.get("trends"):
        return None
    points = []
    for line in data["trends"]:
        parts = line.split(",")
        points.append({
            "t": parts[0][11:16],
            "p": float(parts[1]),
            "v": float(parts[2]) if len(parts) > 2 else 0,
            "avg": float(parts[3]) if len(parts) > 3 else None,
        })
    return {"preClose": data.get("preClose"), "points": points,
            "date": data["trends"][0][:10]}


def em_kline(secid, lmt=90):
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
           f"&klt=101&fqt=0&lmt={lmt}&end=20500101"
           "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57")
    data = get_json(url, retries=2).get("data")
    if not data or not data.get("klines"):
        return []
    rows = []
    for line in data["klines"]:
        p = line.split(",")
        rows.append([p[0], float(p[1]), float(p[2]), float(p[3]),
                     float(p[4]), float(p[5]), float(p[6])])
    return rows  # [date, open, close, high, low, volume, amount]


# 东财 push2his 连续失败熔断
_em_down = {"count": 0}


def fetch_trends(tx_symbol, em_secid=None):
    """分时: 腾讯主源, 东财备源"""
    try:
        m = tx_minute(tx_symbol)
        if m:
            return m
    except Exception:
        pass
    if em_secid and _em_down["count"] < 3:
        try:
            m = em_trends(em_secid)
            if m:
                _em_down["count"] = 0
                return m
        except Exception:
            _em_down["count"] += 1
    return None


def fetch_kline(tx_symbol, lmt=90, em_secid=None):
    """日K: 东财主源(含成交额), 腾讯备源(仅OHLC, 成交额记0)"""
    if em_secid and _em_down["count"] < 3:
        try:
            k = em_kline(em_secid, lmt)
            if k:
                _em_down["count"] = 0
                return k
        except Exception:
            _em_down["count"] += 1
    try:
        k = tx_kline(tx_symbol, lmt)
        if k:
            return k
    except Exception:
        pass
    return []


# 成交额估算的乘数(量 -> 张/股)。已用东财 f47(成交量)/f48(成交额) 逐只校准:
#   转债:   腾讯K线量 = 手, 1手 = 10张                     -> ×10
#   股票:   主板/创业板 腾讯K线量 = 手, 1手 = 100股        -> ×100
#           科创板(688/689) 腾讯K线量 = **股**(实测正好是东财手数的100倍) -> ×1
# 注意: 东财源的行带真实成交额, 不参与估算; 需要估算的都是腾讯源的行。
LOT_SIZE = {"bond": 10, "stock": 100, "stock_star": 1}


def amount_multiplier(code, is_bond=True):
    """按标的与板块给出成交额估算乘数"""
    if is_bond:
        return LOT_SIZE["bond"]
    return LOT_SIZE["stock_star"] if str(code).startswith(("688", "689")) \
        else LOT_SIZE["stock"]


def est_amount(rows, mult):
    """日K成交额补全(转债 mult=10, 正股 mult=100)。

    腾讯备源没有成交额(字段记0), 而东财 push2his 会限流/熔断, 导致快照里
    大部分券的成交额是 0(前端 K线下方成交量柱画不出来)。这里按
        成交额 ≈ 成交量(手) × 典型价(高+低+收)/3 × 每手张数
    估算补上; 东财源的原始成交额不动。
    校准: 腾讯K线成交量与东财 f47 完全一致(比值1.000); 转债×10 对东财真实
    成交额平均绝对误差 0.36%(最大1.01%); 正股×100 实测 额/(量×价)=98.7~103.3。
    用收盘价会系统性偏低 1.2%, 典型价偏差仅 -0.3%。
    """
    for r in rows:
        if len(r) > 6 and not r[6] and r[5] and r[2]:
            r[6] = round(r[5] * (r[3] + r[4] + r[2]) / 3.0 * mult, 2)
    return rows


def est_bond_amount(rows):
    """转债日K成交额补全(保留旧名, 等价于 est_amount(rows, 10))"""
    return est_amount(rows, LOT_SIZE["bond"])


# ---------------------------------------------------------------- 静态信息

def fetch_stock_industry(code):
    """正股所属行业(东财 f127), 失败返回 None。单次快速尝试, 不重试(避免拖慢)。"""
    m = stock_market(code)
    url = (f"https://push2delay.eastmoney.com/api/qt/stock/get?fltt=2"
           f"&secid={m}.{code}&fields=f57,f58,f127")
    try:
        d = get_json(url, referer="https://quote.eastmoney.com/", retries=1)
        return (d.get("data") or {}).get("f127")
    except Exception:
        return None


def fetch_bond_basic():
    """评级/规模/到期日, 本地缓存一天"""
    cache = os.path.join(DATA_DIR, "bond_basic_cache.json")
    today = datetime.now().strftime("%Y-%m-%d")
    if os.path.exists(cache):
        try:
            c = json.load(open(cache, encoding="utf-8"))
            if c.get("date") == today:
                return c["map"]
        except Exception:
            pass
    mapping = {}
    for page in (1, 2, 3):
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
               + urllib.parse.urlencode({
                   "reportName": "RPT_BOND_CB_LIST",
                   "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,RATING,"
                              "ACTUAL_ISSUE_SCALE,LISTING_DATE,EXPIRE_DATE",
                   "pageSize": 500, "pageNumber": page,
                   "sortColumns": "SECURITY_CODE", "sortTypes": "1",
                   "source": "WEB", "client": "WEB"}))
        data = get_json(url, referer="https://data.eastmoney.com/")
        result = data.get("result") or {}
        rows = result.get("data") or []
        if not rows:
            break
        for r in rows:
            mapping[r["SECURITY_CODE"]] = {
                "rating": r.get("RATING"),
                "scale": r.get("ACTUAL_ISSUE_SCALE"),
                "listing_date": (r.get("LISTING_DATE") or "")[:10],
                "expire_date": (r.get("EXPIRE_DATE") or "")[:10],
            }
        if page * 500 >= (result.get("count") or 0):
            break
    try:
        json.dump({"date": today, "map": mapping},
                  open(cache, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    return mapping


# ---------------------------------------------------------------- 新闻

def fetch_news(keyword, bond_name, today_str, limit=6):
    param = {
        "uid": "", "keyword": keyword,
        "type": ["cmsArticleWebOld"], "client": "web",
        "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "default", "sort": "time",
            "pageIndex": 1, "pageSize": 12, "preTag": "", "postTag": ""}},
    }
    url = ("https://search-api-web.eastmoney.com/search/jsonp?cb=cb&param="
           + urllib.parse.quote(json.dumps(param, ensure_ascii=False)))
    try:
        data = get_json(url, referer="https://so.eastmoney.com/")
    except Exception:
        return []
    arts = (data.get("result") or {}).get("cmsArticleWebOld") or []
    cutoff = (datetime.strptime(today_str, "%Y-%m-%d")
              - timedelta(days=2)).strftime("%Y-%m-%d")
    seen, out = set(), []
    for a in arts:
        d = (a.get("date") or "")[:16]
        title = re.sub(r"<[^>]+>", "", a.get("title") or "")
        if not title or d[:10] < cutoff or title in seen:
            continue
        seen.add(title)
        strong = (keyword in title) or (bond_name in title)
        out.append({"date": d, "title": title, "media": a.get("mediaName"),
                    "url": a.get("url"), "_s": strong})
    out.sort(key=lambda x: (not x["_s"], x["date"]), reverse=False)
    strong_first = sorted(out, key=lambda x: (not x["_s"],), reverse=False)
    strong_first.sort(key=lambda x: (not x["_s"]))
    strong_first = sorted(strong_first, key=lambda x: (not x["_s"], x["date"]))
    res = strong_first[:limit]
    for r in res:
        r.pop("_s", None)
    return res


# ---------------------------------------------------------------- 归因分析

def corr(xs, ys):
    n = min(len(xs), len(ys))
    if n < 10:
        return None
    xs, ys = xs[:n], ys[:n]
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs) ** 0.5
    vy = sum((y - my) ** 2 for y in ys) ** 0.5
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy)


def minute_returns(minute):
    if not minute or not minute["points"]:
        return []
    base = minute["preClose"] or minute["points"][0]["p"]
    return [(p["p"] / base - 1) * 100 for p in minute["points"]]


def analyze_drivers(b, prev, bond_minute, stock_minute):
    """b: 今日数据, prev: 昨日快照(可无)"""
    drivers = []
    sch, bch = b["stock_chg"], b["chg"]
    diff = bch - sch
    corr_v = corr(minute_returns(bond_minute), minute_returns(stock_minute)) \
        if bond_minute and stock_minute else None

    if abs(sch) >= 2 and (bch * sch > 0) and abs(diff) < abs(sch) + 2:
        drivers.append({
            "tag": "正股驱动" if sch > 0 else "正股拖累",
            "level": "primary",
            "text": (f"正股{b['stock_name']}今日{'上涨' if sch > 0 else '下跌'}{abs(sch):.2f}%，"
                     f"转债{'跟涨' if bch > 0 else '跟随调整'}{abs(bch):.2f}%，股债联动紧密"
                     + (f"（分时相关性 {corr_v:.2f}）" if corr_v is not None else ""))})
    if diff >= 3:
        drivers.append({
            "tag": "溢价率抬升", "level": "primary",
            "text": (f"转债跑赢正股 {diff:.1f} 个百分点，转股溢价率 {b['premium']:.1f}%，"
                     "转债端获得资金主动加价，估值扩张（常见于游资炒作/题材情绪）")})
    if diff <= -3 and sch > 0:
        drivers.append({
            "tag": "溢价率压缩", "level": "primary",
            "text": (f"正股上涨 {sch:.2f}% 但转债仅 {bch:.2f}%，溢价率被压缩至 "
                     f"{b['premium']:.1f}%，转债持有人获利回吐或存在赎回预期压制")})
    if b["turnover_rate"] and b["turnover_rate"] >= 100:
        drivers.append({
            "tag": "高换手博弈", "level": "info",
            "text": f"换手率高达 {b['turnover_rate']:.0f}%，筹码剧烈换手，短线投机资金主导"})
    if b["price"] >= 180:
        drivers.append({
            "tag": "高价位高波动", "level": "info",
            "text": (f"价格已达 {b['price']:.1f} 元，债底保护基本失效，"
                     "涨跌完全由正股与情绪主导，谨防强赎公告杀溢价")})
    if b["stock_price"] and b["redeem_trigger"] and b["stock_price"] >= b["redeem_trigger"]:
        drivers.append({
            "tag": "强赎触发区", "level": "warn",
            "text": (f"正股价 {b['stock_price']:.2f} 已达到强赎触发价 "
                     f"{b['redeem_trigger']:.2f}，若维持可能公告有条件赎回，"
                     "高溢价转债将面临'杀溢价'风险")})
    if b["stock_price"] and b["put_trigger"] and b["stock_price"] <= b["put_trigger"]:
        drivers.append({
            "tag": "回售触发区", "level": "warn",
            "text": (f"正股价 {b['stock_price']:.2f} 已跌破回售触发价 "
                     f"{b['put_trigger']:.2f}，回售条款生效将形成债底支撑")})
    if b.get("listing_date"):
        days = (datetime.now() - datetime.strptime(
            b["listing_date"], "%Y-%m-%d")).days
        if 0 <= days <= 60:
            drivers.append({
                "tag": "次新债", "level": "info",
                "text": f"上市仅 {days} 天，流通盘小、无历史套牢盘，容易被资金炒作"})
    if b.get("scale") and b["scale"] <= 5:
        drivers.append({
            "tag": "小盘债", "level": "info",
            "text": f"发行规模仅 {b['scale']:.2f} 亿，盘子小、弹性大，资金推动成本低"})
    if bch >= 15:
        drivers.append({
            "tag": "异动/临停", "level": "warn",
            "text": f"单日暴涨 {bch:.1f}%，盘中大概率触发临停机制，情绪极端，追高风险极大"})
    if bch <= -12:
        drivers.append({
            "tag": "暴跌出逃", "level": "warn",
            "text": f"单日大跌 {abs(bch):.1f}%，疑似前期炒作资金撤退，谨防连续跌停式杀跌"})
    if not drivers:
        drivers.append({
            "tag": "温和波动", "level": "info",
            "text": (f"正股{b['stock_name']} {sch:+.2f}%，转债 {bch:+.2f}%，"
                     "价量表现平稳，无突出异动因子")})
    order = {"primary": 0, "warn": 1, "info": 2}
    drivers.sort(key=lambda d: order.get(d["level"], 3))
    return drivers


# ---------------------------------------------------------------- 主流程

def resolve_target_date(trade_date, today_str, snap_exists, force=False):
    """决定本次应生成哪一天(目标日)的快照。

    规则: 周一~周五收盘后运行 -> 今天; 周六/周日/节假日运行 -> 最近一个交易日,
    该交易日快照缺失则自动补抓, 已存在则跳过(--force 可强制重抓)。
    返回 (target_date, msg); target_date 为 None 表示本次无需生成, 直接退出。
    """
    if trade_date == today_str:
        return today_str, f"正常交易日 {trade_date}，抓取当日快照。"
    if trade_date > today_str:
        return None, (f"数据源返回的日期 {trade_date} 晚于今天({today_str})，"
                      "异常，跳过。")
    # trade_date < today_str: 周末/节假日, 应定位到最近一个交易日
    if snap_exists(trade_date):
        if force:
            return (trade_date, f"今天({today_str})非交易日，--force "
                    f"强制重新抓取最近交易日 {trade_date} 的数据。")
        return None, (f"最新分时日期为 {trade_date}（今天 {today_str} "
                      f"不是交易日），该日快照已存在，无需重复抓取，跳过。")
    return (trade_date, f"今天({today_str})不是交易日，"
            f"自动补抓最近交易日 {trade_date} 的数据。")


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    now = datetime.now()
    print(f"[1/6] 抓取沪深可转债列表 ...")
    raw = fetch_cb_universe()
    universe = [r for r in (parse_cb_row(x) for x in raw) if r]
    print(f"      全市场可转债 {len(universe)} 只")

    # 交易日判断: 以上证指数分时首条日期为准
    print("[2/6] 抓取指数分时 ...")
    mkt = {}
    for key, name, tx_sym, em_sym in INDEXES:
        t = fetch_trends(tx_sym, em_sym)
        if t and t["points"]:
            mkt[key] = {"name": name, "preClose": t["preClose"],
                        "points": [{"t": p["t"], "p": p["p"]} for p in t["points"]]}
    sh_min = fetch_trends("sh000001")
    trade_date = (sh_min or {}).get("date") or now.strftime("%Y-%m-%d")
    today_str = now.strftime("%Y-%m-%d")
    # 目标快照日期: 周末/节假日自动落到最近交易日(如周六 -> 周五)
    target_date, msg = resolve_target_date(
        trade_date, today_str,
        lambda d: os.path.exists(os.path.join(DATA_DIR, f"{d}.json")),
        "--force" in sys.argv)
    print(msg)
    if target_date is None:
        return 0
    for k, v in mkt.items():
        if v["preClose"]:
            last = v["points"][-1]["p"]
            v["chg"] = round((last / v["preClose"] - 1) * 100, 2)

    print("[3/6] 抓取静态信息(评级/规模) ...")
    basic = fetch_bond_basic()

    eligible = [r for r in universe
                if r["price"] <= MAX_PRICE and r["turnover"] > MIN_TURNOVER
                and (basic.get(r["code"], {}).get("scale") or 0) < MAX_SCALE]
    eligible.sort(key=lambda r: r["turnover"], reverse=True)
    top = eligible[:TOP_N]
    print(f"[4/6] 符合条件(价格<=400 且 成交额>2亿 且 规模<{MAX_SCALE:.0f}亿) {len(eligible)} 只，取前 {len(top)} 只")

    # 昨日快照
    prev_snap = None
    for f in sorted(os.listdir(DATA_DIR), reverse=True):
        if re.match(r"\d{4}-\d{2}-\d{2}\.json$", f) and f[:10] < target_date:
            prev_snap = json.load(open(os.path.join(DATA_DIR, f), encoding="utf-8"))
            break
    prev_map = {b["code"]: b for b in (prev_snap or {}).get("bonds", [])} \
        if prev_snap else {}

    print("[5/6] 抓取个券分时/K线/新闻并归因 ...")
    bonds = []
    for i, b in enumerate(top):
        code = b["code"]
        tx_b = tx_bond_symbol(code)
        em_b = f"{bond_market(code)}.{code}"
        bond_minute = fetch_trends(tx_b, em_b)
        stock_minute = None
        stock_kline = []
        if b["stock_code"]:
            tx_s = tx_stock_symbol(b["stock_code"])
            em_s = f"{stock_market(b['stock_code'])}.{b['stock_code']}"
            stock_minute = fetch_trends(tx_s, em_s)
            stock_kline = est_amount(fetch_kline(tx_s, 90, em_s),
                                     amount_multiplier(b["stock_code"], False))
        kline = est_amount(fetch_kline(tx_b, 90, em_b),
                           amount_multiplier(code, True))
        b["industry"] = fetch_stock_industry(b["stock_code"]) if b["stock_code"] else None
        news = fetch_news(b["stock_name"] or code, b["name"], target_date) \
            if b["stock_name"] else []

        b.update(basic.get(code, {}))
        b["market"] = "SH" if bond_market(code) == 1 else "SZ"
        b["rank"] = i + 1
        b["turnover_yi"] = round(b["turnover"] / 1e8, 2)
        b["double_low"] = round(b["price"] + (b["premium"] or 0), 1)

        p = prev_map.get(code)
        if p:
            b["prev_rank"] = p["rank"]
            b["status"] = "stay"
            b["rank_change"] = p["rank"] - (i + 1)
        else:
            b["prev_rank"] = None
            b["status"] = "new"
            b["rank_change"] = None

        b["minute"] = bond_minute
        b["stock_minute"] = stock_minute
        b["kline_90d"] = kline
        b["stock_kline_90d"] = stock_kline
        b["drivers"] = analyze_drivers(b, p, bond_minute, stock_minute)
        b["news"] = news
        bonds.append(b)
        print(f"      [{i+1}/{len(top)}] {b['name']}({code}) 完成")
        time.sleep(0.25)

    # 跌出榜单分析
    top_codes = {b["code"] for b in bonds}
    today_map = {r["code"]: (i + 1, r) for i, r in enumerate(eligible)}
    universe_map = {r["code"]: r for r in universe}
    dropped = []
    for pb in (prev_snap or {}).get("bonds", []):
        if pb["code"] in top_codes:
            continue
        r = universe_map.get(pb["code"])
        if r is None:
            reason = "已摘牌/停牌"
        elif r["price"] > MAX_PRICE:
            reason = f"价格超{MAX_PRICE:.0f}元"
        elif r["turnover"] <= MIN_TURNOVER:
            reason = "成交额跌破2亿"
        elif today_map.get(pb["code"]):
            reason = f"热度下滑(今日成交额{r['turnover']/1e8:.2f}亿, 排名今日第{today_map[pb['code']][0]})"
        elif (basic.get(pb["code"], {}).get("scale") or 0) >= MAX_SCALE:
            reason = f"规模超{MAX_SCALE:.0f}亿"
        else:
            reason = "未入前N(热度不足)"
        dropped.append({
            "code": pb["code"], "name": pb["name"],
            "prev_rank": pb["rank"], "reason": reason,
            "today_turnover_yi": round(r["turnover"] / 1e8, 2) if r else None,
            "today_price": r["price"] if r else None,
            "today_chg": r["chg"] if r else None,
        })

    ups = len([b for b in bonds if b["chg"] > 0])
    downs = len([b for b in bonds if b["chg"] < 0])
    snapshot = {
        "date": target_date,
        "trade_date": trade_date,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "filters": {"max_price": MAX_PRICE, "min_turnover": MIN_TURNOVER,
                    "max_scale": MAX_SCALE, "top_n": TOP_N, "universe": len(universe),
                    "eligible": len(eligible)},
        "market": mkt,
        "summary": {
            "total_turnover_yi": round(sum(b["turnover"] for b in bonds) / 1e8, 1),
            "avg_chg": round(sum(b["chg"] for b in bonds) / len(bonds), 2),
            "up": ups, "down": downs, "flat": len(bonds) - ups - downs,
            "new_in": len([b for b in bonds if b["status"] == "new"]),
        },
        "changes": {"new_in": [
            {"code": b["code"], "name": b["name"], "rank": b["rank"],
             "chg": b["chg"], "turnover_yi": b["turnover_yi"]}
            for b in bonds if b["status"] == "new"], "dropped_out": dropped},
        "bonds": bonds,
    }
    out_path = os.path.join(DATA_DIR, f"{target_date}.json")
    json.dump(snapshot, open(out_path, "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))

    # 维护个券索引
    idx_path = os.path.join(DATA_DIR, "bond_map.json")
    idx = {}
    if os.path.exists(idx_path):
        try:
            idx = json.load(open(idx_path, encoding="utf-8"))
        except Exception:
            idx = {}
    for b in bonds:
        idx[b["code"]] = {"name": b["name"], "stock_code": b["stock_code"],
                          "stock_name": b["stock_name"]}
    for d in dropped:
        if d["code"] not in idx:
            idx[d["code"]] = {"name": d["name"], "stock_code": None,
                              "stock_name": None}
    json.dump(idx, open(idx_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    print(f"[6/6] 已保存 {out_path}")
    print(f"      总成交额 {snapshot['summary']['total_turnover_yi']}亿, "
          f"涨{ups}/跌{downs}, 新进 {snapshot['summary']['new_in']} 只")
    return 0


if __name__ == "__main__":
    sys.exit(main())
