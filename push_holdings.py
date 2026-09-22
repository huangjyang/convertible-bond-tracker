#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「模拟仓持仓」按固定间隔推送到手机 (ntfy)。

数据流:
    index.html (模拟仓, localStorage 是真源)
        │  每次 simPersist() POST
        ▼
    app.py  POST /api/sim  ->  data/portfolio.json   (只读镜像)
        │
        ▼
    push_holdings.py (本脚本, 每 30 分钟跑一次)
        │  ① 读镜像  ② 批量取行情  ③ 算盈亏/风险  ④ POST ntfy
        ▼
    手机 ntfy App

设计要点 (都是踩过的坑, 改动前先读):
  1. **本脚本不发买卖指令、不写回持仓** —— 它只读镜像 + 只推消息。
     持仓真源永远是浏览器 localStorage, 这里绝不"代替"用户记账。
  2. **时段门**: 只在 盘中(上午)/盘中(下午) 推。非交易时段的数据冻结在上一时点,
     推了就是噪音; 周末/节假日由 session_of() 兜住(live=False 一律不推)。
  3. **去重**: data/push_state.json 记最后一次发送时间。launchd 与 app.py 内置调度
     可能同时触发, 靠这里的 min_gap_minutes 保证你半小时只收到一条。
  4. **空仓不推**: 没有持仓就不发, 否则每半小时一条"空仓"等于骚扰。
  5. 行情走 push2delay.eastmoney.com —— push2 对本机 IP 直接断连(见 memory 2026-09-20)。
  6. 数字全部由本脚本算, 不含任何"解读"文字。要加解读, 让 LLM 只写理由、不碰数字。

用法:
    python3 push_holdings.py                 # 正常推送(带时段门 + 去重)
    python3 push_holdings.py --dry-run       # 只打印, 不发送(不需要配 topic)
    python3 push_holdings.py --force         # 忽略时段门与去重(调试用)
    python3 push_holdings.py --test          # 发一条固定测试消息, 验证通道
    python3 push_holdings.py --config        # 打印当前配置(隐藏 topic 中间部分)
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import fetch_daily as fd          # noqa: E402  复用 get_json(带退避重试) / bond_market / num
import sector_flow as sf          # noqa: E402  复用 session_of(时段判定, 单一真源)

DATA_DIR = os.path.join(BASE, "data")
MIRROR_FILE = os.path.join(DATA_DIR, "portfolio.json")
STATE_FILE = os.path.join(DATA_DIR, "push_state.json")
CONFIG_FILE = os.path.join(BASE, "push_config.json")

EM_DELAY = "https://push2delay.eastmoney.com"
TX_QUOTE = "https://qt.gtimg.cn/q="
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 只要这些字段: 价格/涨跌/溢价/强赎触发价/到期赎回价/正股/行情时点(f124)
QUOTE_FIELDS = "f2,f3,f12,f14,f124,f229,f230,f232,f234,f237,f240,f241"
# 一次请求最多塞多少只券(实测东财对超长 secids 会失败, 分批更稳)
QUOTE_BATCH = 40
# 东财慢变字段的本地缓存: 溢价率/强赎触发价/正股 —— 东财断连走腾讯兜底时,
# 拿这份"上次成功抓到的值"仍能判强赎风险, 但必须在文案里标明是旧值。
Q_CACHE_FILE = os.path.join(DATA_DIR, "push_quotes_cache.json")

DEFAULT_CONFIG = {
    "enabled": True,
    "channel": "ntfy",
    "ntfy": {"server": "https://ntfy.sh", "topic": "", "token": ""},
    "window": {
        "only_trading_session": True,   # 只在盘中推(上午/下午), 午间休市与盘后都不推
        "min_gap_minutes": 25,          # 距上次推送不足这个分钟数就跳过(去重)
    },
    "content": {
        "max_chars": 1800,              # ntfy 单条上限 4096 字节, 留足余量
        "max_positions": 12,            # 超过则按 |浮动盈亏| 取前 N 笔, 其余汇总一行
        "redeem_near_pct": 5.0,         # 正股价距强赎触发价 <= 该百分比 -> 提醒
        "high_premium": 50.0,           # 溢价率 >= 该值 -> 提醒"正股涨它不跟"
        "priority_normal": 2,           # 2=low: 不响不震, 拉下通知栏才看到(半小时一条别吵)
        "priority_alert": 4,            # 4=high: 有告警(强赎/到应卖日)才响
        "click_dashboard": True,        # 点通知打开看板
    },
}


# ---------------------------------------------------------------- 配置
def _deep_merge(base, over):
    """用 over 覆盖 base(只覆盖 base 里已有的键, 未知键也带上以便用户自己加)。"""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=CONFIG_FILE):
    """读配置; 不存在就用默认值建一个, 并返回 (cfg, created)。"""
    if not os.path.exists(path):
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        _atomic_write(path, cfg, indent=2)
        return cfg, True
    try:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
    except Exception as e:
        raise SystemExit(f"✗ 配置文件读不了({path}): {e}")
    return _deep_merge(DEFAULT_CONFIG, user), False


def _mask(s):
    if not s:
        return "(未设置)"
    return s if len(s) <= 6 else f"{s[:3]}…{s[-3:]}(共{len(s)}位)"


def _atomic_write(path, obj, indent=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 状态(去重)
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_state(st):
    try:
        _atomic_write(STATE_FILE, st, indent=1)
    except Exception as e:
        print(f"  (状态写盘失败, 不影响本次推送: {e})", file=sys.stderr)


# ---------------------------------------------------------------- 持仓镜像
def load_portfolio():
    """读服务端镜像。返回 (portfolio|None, 说明文字)。"""
    if not os.path.exists(MIRROR_FILE):
        return None, ("服务端还没有持仓镜像 data/portfolio.json —— "
                      "打开看板网页并切到「模拟仓」页签, 会自动同步一次")
    try:
        with open(MIRROR_FILE, encoding="utf-8") as f:
            mir = json.load(f)
    except Exception as e:
        return None, f"镜像文件读不了: {e}"
    pf = (mir or {}).get("portfolio")
    if not isinstance(pf, dict):
        return None, "镜像里没有 portfolio 字段(格式不对)"
    return {"portfolio": pf, "saved_at": mir.get("saved_at"),
            "revision": mir.get("revision"), "source": mir.get("source")}, "ok"


# ---------------------------------------------------------------- 行情
def fetch_quotes_em(codes):
    """东财批量行情(带溢价率/强赎触发价)。一只都没拿到返回 {}。"""
    out = {}
    codes = [c for c in dict.fromkeys(codes) if c]
    for i in range(0, len(codes), QUOTE_BATCH):
        chunk = codes[i:i + QUOTE_BATCH]
        secids = ",".join(f"{fd.bond_market(c)}.{c}" for c in chunk)
        url = (EM_DELAY + "/api/qt/ulist.np/get?" + urllib.parse.urlencode({
            "fltt": 2, "invt": 2, "secids": secids, "fields": QUOTE_FIELDS}))
        try:
            d = fd.get_json(url, referer="https://quote.eastmoney.com/")
        except Exception as e:
            print(f"  ✗ 东财行情失败: {str(e)[-90:]}", file=sys.stderr)
            continue
        for r in ((d.get("data") or {}).get("diff") or []):
            code = str(r.get("f12") or "")
            if code:
                out[code] = {**parse_quote(r), "_src": "em"}
    return out


def parse_tx_lines(txt):
    """腾讯 `qt.gtimg.cn/q=` 返回的 `v_sz127080="a~b~c";` 多行文本 -> 行情 dict。

    字段位置(实测): [1]=名称 [2]=代码 [3]=现价 [30]=行情时间 [32]=涨跌幅%
    字段数不足 33 的行一律跳过 —— 宁可少一只, 也不要拿错位的字段当价格用。
    """
    out = {}
    for line in (txt or "").splitlines():
        if '="' not in line:
            continue
        f = line.split('="', 1)[1].rstrip('";').split("~")
        if len(f) < 33:
            continue
        out[f[2]] = {"name": f[1], "price": fd.num(f[3]), "chg": fd.num(f[32]),
                     "qdate": _fmt_qdate(f[30]), "premium": None, "stock_code": None,
                     "stock_name": None, "stock_price": None, "stock_chg": None,
                     "redeem_trigger": None, "maturity_redeem": None,
                     "quote_time": f[30], "_src": "tx"}
    return out


def fetch_quotes_tx(codes):
    """腾讯批量行情(只有价格/涨跌%, 一个请求可带多只) —— 东财断连时的兜底。

    实测腾讯与东财的价/涨跌幅完全一致(191.414 / -1.11% 两边相同),
    所以拿它算市值与盈亏是可靠的; 缺的只有溢价率与强赎触发价。
    """
    out = {}
    codes = [c for c in dict.fromkeys(codes) if c]
    for i in range(0, len(codes), QUOTE_BATCH):
        chunk = codes[i:i + QUOTE_BATCH]
        url = TX_QUOTE + ",".join(fd.tx_bond_symbol(c) for c in chunk)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Referer": "https://gu.qq.com/"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                txt = resp.read().decode("gbk", "ignore")
        except Exception as e:
            print(f"  ✗ 腾讯行情失败: {e}", file=sys.stderr)
            continue
        out.update(parse_tx_lines(txt))
    return out


def load_q_cache():
    try:
        with open(Q_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_q_cache(em_quotes, now):
    """把本次东财拿到的慢变字段存下来, 供下次兜底时判强赎。"""
    cache = load_q_cache()
    for code, q in em_quotes.items():
        cache[code] = {
            "premium": q.get("premium"), "stock_code": q.get("stock_code"),
            "stock_name": q.get("stock_name"), "stock_price": q.get("stock_price"),
            "stock_chg": q.get("stock_chg"),
            "redeem_trigger": q.get("redeem_trigger"),
            "maturity_redeem": q.get("maturity_redeem"),
            "at": f"{now:%Y-%m-%d %H:%M}",
        }
    try:
        _atomic_write(Q_CACHE_FILE, cache, indent=1)
    except Exception as e:
        print(f"  (行情缓存写盘失败, 不影响本次推送: {e})", file=sys.stderr)


def fetch_quotes(codes):
    """东财优先(带溢价率/强赎价), 腾讯兜底(只有价格/涨跌)。

    返回 (quotes, source)。source ∈ {em, tx, em+tx, none}。
    刻意**不**做"东财失败就整体放弃": 价格与盈亏才是推送的主体,
    强赎/溢价是加分项, 不该因为它拿不到就什么都不推。
    """
    codes = [c for c in dict.fromkeys(codes) if c]
    em = fetch_quotes_em(codes)
    if len(em) >= len(codes) and em:
        return em, "em"
    need = [c for c in codes if c not in em]
    tx = fetch_quotes_tx(need) if need else {}
    if not em and not tx:
        return {}, "none"
    merged = dict(tx)
    merged.update(em)
    src = "em+tx" if (em and tx) else ("em" if em else "tx")
    return merged, src


def _fmt_qdate(v):
    """行情时点 -> 'MM-DD'; 取不到返回 ''(不猜)。

    两个源的时点格式不同, 别用同一个分支硬套:
      腾讯 [30] = '20260921161434'(14 位紧凑) 或 '20260921'(8 位)
      东财 f124 = unix 秒(10 位, 2026 年约 1.79e9) 或毫秒(13 位)
    """
    if v in (None, "", "-"):
        return ""
    s = str(v).strip()
    if s.isdigit() and len(s) in (8, 12, 14) and s.startswith("20"):
        return f"{s[4:6]}-{s[6:8]}"            # 腾讯紧凑格式
    try:
        n = float(s)
    except (TypeError, ValueError):
        return ""
    if n > 1e11:                                # 毫秒 -> 秒
        n /= 1000.0
    if not (1e9 <= n <= 4e9):                    # 合理区间约 2001~2096, 之外不认
        return ""
    try:
        return datetime.fromtimestamp(n).strftime("%m-%d")
    except (OSError, OverflowError, ValueError):
        return ""


def parse_quote(r):
    """东财原始行 -> 精简行情。'-' 一律变 None(停牌/无数据), 不猜。"""
    return {
        "name": r.get("f14"),
        "price": fd.num(r.get("f2")),
        "chg": fd.num(r.get("f3")),
        "qdate": _fmt_qdate(r.get("f124")),        # 行情时点 -> 'MM-DD'
        "premium": fd.num(r.get("f237")),
        "stock_code": r.get("f232"), "stock_name": r.get("f234"),
        "stock_price": fd.num(r.get("f229")), "stock_chg": fd.num(r.get("f230")),
        "redeem_trigger": fd.num(r.get("f240")),
        "maturity_redeem": fd.num(r.get("f241")),
    }


# ---------------------------------------------------------------- 计算
def compute(pf, quotes, cfg, source="em", qcache=None):
    """算账 + 标风险。所有数字都出自这里, 不含任何文字解读。

    quotes 里的每一条都已经是 parse_quote() 过的精简行情(带 _src)。
    腾讯兜底时溢价率/强赎触发价取自 qcache(上次东财成功抓到的值), 并在文案里标注时点 ——
    宁可明说"这是 09:30 的缓存值", 也不要静默地把旧值当实时值用。
    """
    c = cfg["content"]
    qcache = qcache or {}
    positions, warnings, stale_at = [], [], ""
    for p in pf.get("positions") or []:
        code = str(p.get("code") or "")
        q = quotes.get(code) or {}
        qty = float(p.get("qty") or 0)
        buy_price = float(p.get("buyPrice") or 0)
        fee = float(p.get("fee") or 0)
        cost = buy_price * qty + fee                     # 与前端一致: 成本含费
        price = q.get("price")
        name = p.get("name") or q.get("name") or code
        due = p.get("dueDate") or ""
        # 没有行情(停牌/新股未上市)时**按买入价计** —— 与前端 renderSim() 完全一致
        # (那边是 `const price=q?q.price:p.buyPrice`)。这样推送的总资产/浮动盈亏与看板逐字相同;
        # 若这里改成"剔除不计", 两边数字就会不一样, 反而更危险。同时在文案里标出来。
        no_quote = price is None
        if no_quote:
            price = buy_price
        mv = price * qty
        pnl = mv - cost
        prem = q.get("premium")
        trig = q.get("redeem_trigger")
        sp = q.get("stock_price")
        note = ""
        if q.get("_src") != "em":                        # 腾讯兜底: 慢变字段用缓存
            cc = qcache.get(code) or {}
            prem = cc.get("premium") if prem is None else prem
            trig = cc.get("redeem_trigger") if trig is None else trig
            sp = cc.get("stock_price") if sp is None else sp
            if cc.get("at"):
                note = f"（{cc['at']} 的缓存值）"
                stale_at = cc["at"]
        flags = []
        if no_quote:
            flags.append("无行情(停牌?) 按成本价计, 盈亏记 0")
        if sp is not None and trig:
            if sp >= trig:
                flags.append(f"已满足强赎价格条件 正股{sp} ≥ 触发{trig}{note}")
            else:
                near = (trig - sp) / sp * 100
                if near <= float(c["redeem_near_pct"]):
                    flags.append(f"逼近强赎 正股{sp} 距触发{trig} 还差{near:.2f}%{note}")
        if prem is not None and prem >= float(c["high_premium"]):
            flags.append(f"高溢价 {prem:.1f}% 正股涨它不跟{note}")
        if due and due <= datetime.now().strftime("%Y-%m-%d"):
            flags.append(f"已到应卖日(T+3) {due}")
        positions.append({
            "code": code, "name": name,
            "qty": qty, "buy_price": buy_price, "cost": cost, "price": price,
            "no_quote": no_quote, "chg": q.get("chg"), "premium": prem,
            "qdate": q.get("qdate") or "",
            "mv": mv, "pnl": pnl, "pnl_pct": (pnl / cost * 100 if cost else 0.0),
            "due_date": due, "flags": flags,
        })
        for f in flags:                       # 告警汇总(格式化的正文里再列一次)
            warnings.append((code, name, f))
    live = [p for p in positions if not p.get("no_quote")]
    mv = sum(p["mv"] for p in positions)
    cost = sum(p["cost"] for p in positions)
    cash = float(pf.get("cash") or 0)
    init = float(pf.get("init") or 0)
    realized = sum(float(x.get("pnl") or 0) for x in (pf.get("closed") or []))
    float_pnl = mv - cost
    return {
        "positions": positions, "warnings": warnings,
        "mv": mv, "cost": cost, "cash": cash, "init": init,
        "total": cash + mv, "float_pnl": float_pnl,
        "float_pnl_pct": (float_pnl / cost * 100 if cost else 0.0),
        "realized": realized, "n": len(positions),
        "no_quote": len(positions) - len(live),
        "source": source, "stale_at": stale_at,
        "total_pnl": (cash + mv) - init if init else None,
        "total_pnl_pct": (((cash + mv) - init) / init * 100 if init else None),
    }


# ---------------------------------------------------------------- 文案
def _sgn(v, digits=2, plus=True):
    if v is None:
        return "-"
    s = f"{v:+,.{digits}f}" if plus else f"{v:,.{digits}f}"
    return s


MIRROR_SOURCE_WEB = "web(localStorage)"   # app.py 写镜像时用的 source 值


def mirror_note(mirror):
    """镜像不是浏览器同步来的 -> 返回提示文字, 否则 ''。

    为什么需要这个: 镜像正常只由 app.py(POST /api/sim)写入, source 是 "web(localStorage)"。
    若它是别的来源(例如人工按截图重建), 那"成本"这类用户自己录入的字段就不是真源,
    推送必须**自己说出来**, 不能让它看起来像浏览器同步的持仓。
    """
    src = (mirror or {}).get("source")
    if src == MIRROR_SOURCE_WEB:
        return ""
    return (f"[⚠ 持仓镜像来源: {src or '未知'}(不是浏览器同步) —— "
            f"重启 app.py 并打开看板「模拟仓」页签后会变成你的真实持仓]")


def mirror_age_days(mirror, now=None):
    """镜像距上次同步多少天; 取不到时间戳返回 None。

    注意语义: 镜像是**每次打开看板都会刷新**的, 所以这个"年龄"反映的是
    "多久没开看板", 而不是"多久没交易"。超过阈值时在消息里点一句,
    免得拿一份很旧的持仓当今天的。阈值 3 天: 正常天天看盘的人不会看到它。
    """
    ts = (mirror or {}).get("saved_ts")
    if not ts:
        return None
    try:
        return ((now or datetime.now()).timestamp() - float(ts)) / 86400.0
    except (TypeError, ValueError):
        return None


def format_report(rep, cfg, now=None):
    """生成 (title, body, priority, tags)。纯文本排版 —— ntfy 的 Markdown 在
    Android 端不保证渲染, 用 ** 反而会显示成一堆星号, 所以不赌它。

    字段口径由用户 2026-09-22 指定: 只要**转债 / 成本(含费) / 最新价 / 浮动盈亏** 四项
    (与看板持仓表里打了红框的四列一一对应)。市值、张数、买入价、涨跌幅、总资产、
    现金、已实现盈亏都**不进正文** —— 想加回来改这里就行, 数字在 rep 里都现成。
    行情日期带上是为了能一眼看出价格是不是今天的(比如盘前拿到的是昨收)。
    """
    now = now or datetime.now()
    c = cfg["content"]
    hm = now.strftime("%H:%M")
    alert = bool(rep["warnings"])
    pnl = rep["float_pnl"]
    arrow = "▲" if pnl >= 0 else "▼"
    title = f"持仓 {hm} {arrow} {_sgn(pnl)}元 ({_sgn(rep['float_pnl_pct'])}%)"

    L = [f"持仓 {rep['n']} 笔 · 浮动 {_sgn(pnl)} 元 ({_sgn(rep['float_pnl_pct'])}%)"]
    src = rep.get("source") or "em"
    if src != "em":                       # 东财断连: 说清数据来源, 别让人以为是全字段实时
        why = "腾讯行情" if src == "tx" else "东财+腾讯混合"
        extra = f" · 风险字段用 {rep['stale_at']} 的缓存" if rep.get("stale_at") else ""
        L.append(f"[{why}, 东财接口不通{extra}]")
    if rep.get("no_quote"):
        L.append(f"[{rep['no_quote']} 只无行情, 按成本价计入(与看板口径一致)]")
    mn = rep.get("mirror_note")
    if mn:
        L.append(mn)
    age = rep.get("mirror_age_days")
    if age is not None and age >= 3:
        L.append(f"[⚠ 持仓镜像 {age:.0f} 天前同步 —— 打开看板会自动刷新]")
    L.append("")

    # 顺序保持镜像里的原序(= 看板持仓表的建仓顺序), **不按盈亏排序** ——
    # 推送是为了和看板对着看, 两边顺序不一致会很别扭; 按盈亏排还会让列表随价格跳动。
    rows = list(rep["positions"])
    show, rest = rows[:int(c["max_positions"])], rows[int(c["max_positions"]):]
    for p in show:
        L.append(f"{p['name']} {p['code']}")
        qd = f" ({p['qdate']})" if p.get("qdate") else ""
        if p.get("no_quote"):
            L.append(f"  成本 {p['cost']:,.2f} · 最新 无行情 · "
                     f"盈亏 {_sgn(p['pnl'])} ({_sgn(p['pnl_pct'])}%)")
        else:
            L.append(f"  成本 {p['cost']:,.2f} · 最新 {p['price']:.3f}{qd} · "
                     f"盈亏 {_sgn(p['pnl'])} ({_sgn(p['pnl_pct'])}%)")
        for f in p["flags"]:
            L.append(f"  ⚠ {f}")
        L.append("")
    if rest:
        rp = sum(p["pnl"] for p in rest if p["pnl"] is not None)
        L.append(f"…另有 {len(rest)} 笔(合计 {_sgn(rp)} 元), 详见看板")
        L.append("")

    if rep["warnings"]:
        L.append("—— 告警 ——")
        for _code, name, f in rep["warnings"][:8]:
            L.append(f"· {name}: {f}")
        if len(rep["warnings"]) > 8:
            L.append(f"· …另有 {len(rep['warnings']) - 8} 条")

    body = "\n".join(L).strip()
    limit = int(c["max_chars"])
    if len(body) > limit:                      # 极端情况(持仓特别多)兜底截断
        body = body[:limit - 20].rstrip() + "\n…(已截断, 详见看板)"

    prio = int(c["priority_alert"] if alert else c["priority_normal"])
    tags = ["rotating_light"] if alert else (
        ["chart_with_upwards_trend"] if pnl >= 0 else ["chart_with_downwards_trend"])
    return title, body, prio, tags


def _dashboard_url():
    """看板局域网地址(点通知直接打开)。取不到就返回空。"""
    try:
        import app as appmod                  # noqa: PLC0415  只为复用 _lan_ips
        ips = appmod._lan_ips()
        return f"http://{ips[0]}:{appmod.PORT}" if ips else ""
    except Exception:
        return ""


# ---------------------------------------------------------------- 发送
def send(cfg, title, body, priority=3, tags=None, timeout=20):
    """按配置的通道发送。目前只实现 ntfy。返回 (ok, msg)。"""
    ch = cfg.get("channel") or "ntfy"
    if ch != "ntfy":
        return False, f"不支持的 channel: {ch}"
    n = cfg["ntfy"] or {}
    server = (n.get("server") or "https://ntfy.sh").rstrip("/")
    topic = (n.get("topic") or "").strip()
    if not topic:
        return False, ("还没配 ntfy topic —— 编辑 push_config.json 填 \"topic\", "
                       "手机上装 ntfy App 后订阅同一个话题名")
    url = f"{server}/{urllib.parse.quote(topic)}"
    headers = {
        "Title": title.encode("utf-8").decode("latin-1", "ignore") or "持仓",
        "Priority": str(priority),
        "Markdown": "no",
        "User-Agent": UA,
    }
    if tags:
        headers["Tags"] = ",".join(tags)
    if cfg["content"].get("click_dashboard"):
        d = _dashboard_url()
        if d:
            headers["Click"] = d
    if n.get("token"):
        headers["Authorization"] = f"Bearer {n['token']}"
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers,
                                method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "ignore")
        j = json.loads(raw) if raw.strip().startswith("{") else {}
        if resp.status == 200 and j.get("id"):
            return True, f"已发送 (id={j.get('id')})"
        return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:200]
        return False, f"HTTP {e.code}: {detail}"
    except Exception as e:
        return False, f"发送失败: {e}"


# ---------------------------------------------------------------- 主流程
def should_skip(cfg, state, now, force):
    """返回 (skip, 原因)。force=True 时只跳过 disabled。"""
    if not cfg.get("enabled", True) and not force:
        return True, "配置里 enabled=false"
    if force:
        return False, ""
    if cfg["window"].get("only_trading_session", True):
        sess, live = sf.session_of(now)
        if sess not in ("盘中(上午)", "盘中(下午)"):
            return True, f"非盘中时段({sess}, live={live})"
    gap = float(cfg["window"].get("min_gap_minutes") or 0)
    last = state.get("last_sent_ts") or 0
    if gap > 0 and last and (now.timestamp() - last) < gap * 60:
        mins = (now.timestamp() - last) / 60
        return True, f"距上次推送 {mins:.1f} 分钟 < {gap:g} 分钟(去重)"
    return False, ""


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="把模拟仓持仓推送到手机 (ntfy)",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="只打印消息, 不发送")
    ap.add_argument("--force", action="store_true", help="忽略时段门与去重(调试用)")
    ap.add_argument("--test", action="store_true", help="发一条固定测试消息, 验证通道")
    ap.add_argument("--config", action="store_true", help="打印当前配置")
    ap.add_argument("--no-state", action="store_true",
                    help="不读写去重状态(配合 --dry-run 做纯预览)")
    a = ap.parse_args(argv)

    cfg, created = load_config()
    if a.config:
        n = cfg["ntfy"]
        print(f"配置文件: {CONFIG_FILE}{'  (本次新建)' if created else ''}")
        print(f"  enabled={cfg['enabled']}  channel={cfg['channel']}")
        print(f"  server={n.get('server')}  topic={_mask(n.get('topic'))}"
              f"  token={'有' if n.get('token') else '无'}")
        w = cfg["window"]
        print(f"  只在盘中推={w.get('only_trading_session')}  "
              f"去重间隔={w.get('min_gap_minutes')} 分钟")
        c = cfg["content"]
        print(f"  最多列 {c.get('max_positions')} 笔  单条上限 {c.get('max_chars')} 字  "
              f"优先级 {c.get('priority_normal')}/{c.get('priority_alert')}  "
              f"强赎提醒≤{c.get('redeem_near_pct')}%  高溢价≥{c.get('high_premium')}%")
        return 0
    if created:
        print(f"已生成配置文件 {CONFIG_FILE} —— 把 ntfy.topic 填成你自己起的话题名")

    now = datetime.now()
    if created:
        return 0

    if a.test:
        ok, msg = send(cfg, "可转债看板 · 推送测试",
                       f"通道正常。\n时间 {now:%Y-%m-%d %H:%M:%S}\n"
                       f"这条是 --test 手工发的, 不受时段与去重限制。",
                       priority=int(cfg["content"]["priority_alert"]),
                       tags=["heavy_check_mark"])
        print(("✓ " if ok else "✗ ") + msg)
        return 0 if ok else 1

    state = {} if a.no_state else load_state()
    skip, why = should_skip(cfg, state, now, a.force)
    if skip:
        print(f"跳过推送: {why}")
        return 0

    mirror, note = load_portfolio()
    if not mirror:
        print(f"跳过推送: {note}")
        return 0
    pf = mirror["portfolio"]
    if not (pf.get("positions") or []):
        print("跳过推送: 模拟仓空仓(空仓不推, 免得每半小时一条废话)")
        return 0

    codes = [str(p.get("code")) for p in pf["positions"]]
    quotes, source = fetch_quotes(codes)
    if not quotes:
        print("跳过推送: 行情一只都没拿到(东财与腾讯都不通) —— 宁可不推也不推错数")
        return 1
    if source == "em":
        save_q_cache({k: v for k, v in quotes.items() if v.get("_src") == "em"}, now)
    missing = [c for c in codes if c not in quotes]
    if missing:
        print(f"  注意: {len(missing)} 只没行情 {','.join(missing[:5])}"
              f"{'…' if len(missing) > 5 else ''}")

    rep = compute(pf, quotes, cfg, source=source, qcache=load_q_cache())
    rep["mirror_age_days"] = mirror_age_days(mirror, now)
    rep["mirror_note"] = mirror_note(mirror)
    title, body, prio, tags = format_report(rep, cfg, now)
    head = (f"持仓 {rep['n']} 笔 · 浮动 {_sgn(rep['float_pnl'])} 元 "
            f"({_sgn(rep['float_pnl_pct'])}%) · 行情 {len(quotes)}/{len(codes)} "
            f"[{source}]")
    print(head)
    print(f"镜像 revision={mirror.get('revision')} saved_at={mirror.get('saved_at')}")
    print("-" * 58)
    print(title)
    print(body)
    print("-" * 58)

    if a.dry_run:
        print("[dry-run] 未发送")
        return 0

    ok, msg = send(cfg, title, body, priority=prio, tags=tags)
    print(("✓ " if ok else "✗ ") + msg)
    if ok and not a.no_state:
        today = now.strftime("%Y-%m-%d")
        st = state if isinstance(state, dict) else {}
        n_today = int(st.get("sent_today") or 0) + 1 if st.get("date") == today else 1
        st.update({"last_sent_ts": now.timestamp(), "last_sent_at": f"{now:%Y-%m-%d %H:%M:%S}",
                   "date": today, "sent_today": n_today,
                   "last_revision": mirror.get("revision"),
                   "last_float_pnl": round(rep["float_pnl"], 2),
                   "last_title": title})
        save_state(st)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
