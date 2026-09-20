#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可转债热度 Top20 跟踪看板 - 本地服务 (仅标准库)"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
PORT = 8734
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import bond_detail  # noqa: E402  按需补齐非热榜个券的分时/正股(见 bond_detail.py)

_lock = threading.Lock()
_cache = {"sig": None, "dates": [], "by_date": {}, "bond_hist": {}, "bond_meta": {}}

# ---------------- 按需抓取 (页面刷新触发, 带节流) ----------------
# 不做定时任务了; 页面加载时调 /api/refresh, 缺当天数据才后台抓一次。
_fetch = {"running": False, "last_ok_date": None, "last_seen": None, "done": False,
          "msg": "", "started_at": None}
_fetch_lock = threading.Lock()


def _run_fetch(force):
    """后台线程: 跑 fetch_daily.py (脚本自己会判断交易日/周末补抓), 成功后接着跑放量扫描"""
    try:
        cmd = [sys.executable, os.path.join(BASE, "fetch_daily.py")]
        if force:
            cmd.append("--force")
        r = subprocess.run(cmd, cwd=BASE, capture_output=True,
                           text=True, timeout=900)
        with _fetch_lock:
            _fetch["running"] = False
            _fetch["done"] = True
            _fetch["msg"] = ((r.stdout or "") + (r.stderr or ""))[-1500:]
            if r.returncode == 0:
                _fetch["last_ok_date"] = datetime.now().strftime("%Y-%m-%d")
                _fetch["last_seen"] = _latest_snapshot_date()   # 实际生成到哪一天
        if r.returncode == 0:
            maybe_scan(force)
    except Exception as e:
        with _fetch_lock:
            _fetch["running"] = False
            _fetch["done"] = True
            _fetch["msg"] = f"抓取失败: {e}"
        with _scan_lock:
            _scan["running"] = False
            _scan["done"] = True
            _scan["msg"] = f"抓取失败: {e}"


# ---------------- 放量扫描 (scan_volume.py) ----------------
# 与 fetch_daily 同一套按需触发逻辑: 当天扫描结果不存在才后台跑一次。
_scan = {"running": False, "done": False, "msg": "", "started_at": None,
         "last_ok_date": None}
_scan_lock = threading.Lock()


def _latest_snapshot_date():
    """data/ 里最近一个交易快照日期(YYYY-MM-DD)"""
    best = None
    try:
        for f in os.listdir(DATA_DIR):
            m = re.match(r"(\d{4}-\d{2}-\d{2})\.json$", f)
            if m and (best is None or m.group(1) > best):
                best = m.group(1)
    except Exception:
        pass
    return best


def _scan_path(date):
    return os.path.join(DATA_DIR, f"volume_scan_{date}.json")


def _coil_path(date):
    return os.path.join(DATA_DIR, f"coil_scan_{date}.json")


def _newest_file_date(prefix):
    """data/<prefix>_<日期>.json 里最新的那个日期"""
    pat = re.compile(rf"^{prefix}_(\d{{4}}-\d{{2}}-\d{{2}})\.json$")
    ds = sorted(m.group(1) for m in (pat.match(f) for f in os.listdir(DATA_DIR)) if m)
    return ds[-1] if ds else None


def _newest_coil():
    """最新的 coil_scan_*.json 内容(蓄势榜)"""
    date = _newest_file_date("coil_scan")
    return _load_json(_coil_path(date)) if date else None


def _newest_scan():
    """最新的 volume_scan_*.json 内容(读盘一次就够, 预热/查名字都用它)"""
    dates = sorted(
        m.group(1) for m in
        (re.match(r"^volume_scan_(\d{4}-\d{2}-\d{2})\.json$", f)
         for f in os.listdir(DATA_DIR)) if m)
    return _load_json(_scan_path(dates[-1])) if dates else None


_scan_idx = {"mtime": None, "by_code": {}}


def _scan_index():
    """最新 volume_scan 的 {code: 记录}(按文件 mtime 记忆, 不必每次请求都读 190KB)"""
    dates = sorted(
        m.group(1) for m in
        (re.match(r"^volume_scan_(\d{4}-\d{2}-\d{2})\.json$", f)
         for f in os.listdir(DATA_DIR)) if m)
    if not dates:
        return {}
    p = _scan_path(dates[-1])
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return {}
    if _scan_idx["mtime"] == mt:
        return _scan_idx["by_code"]
    d = _load_json(p) or {}
    by = {b["code"]: b for b in (d.get("bonds") or []) if b.get("code")}
    _scan_idx["mtime"], _scan_idx["by_code"] = mt, by
    return by


_coil_idx = {"mtime": None, "by_code": {}}


def _coil_index():
    """最新 coil_scan 的 {code: 记录}(按 mtime 记忆)"""
    date = _newest_file_date("coil_scan")
    if not date:
        return {}
    p = _coil_path(date)
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return {}
    if _coil_idx["mtime"] == mt:
        return _coil_idx["by_code"]
    d = _load_json(p) or {}
    by = {b["code"]: b for b in (d.get("bonds") or []) if b.get("code")}
    _coil_idx["mtime"], _coil_idx["by_code"] = mt, by
    return by


def _bond_record(code):
    """补抓详情/算归因要用的一整条券记录: 放量扫描记录优先(当天最新最全), 快照索引兜底"""
    rec = dict(_scan_index().get(code) or {})
    for k, v in (_coil_index().get(code) or {}).items():     # 蓄势榜里的券(可能不在放量榜)
        if v is not None and rec.get(k) is None:
            rec[k] = v
    for k, v in (_cache["bond_meta"].get(code) or {}).items():
        if v is not None and rec.get(k) is None:
            rec[k] = v
    return rec


def _prewarm_candidates():
    """扫描后预热「放量温和上涨/拉升/滞涨」候选券的分时/正股到缓存。
    这样从放量榜点开时弹窗直接有分时, 不必等用户点「补抓」。已缓存的会跳过。"""
    d = _newest_scan()
    if not d:
        return None
    groups = d.get("groups") or {}
    want = set()
    for g in bond_detail.PREWARM_GROUPS:
        want.update(groups.get(g) or [])
    recs = {b["code"]: b for b in (d.get("bonds") or []) if b.get("code") in want}
    coil = _coil_index()
    for b in coil.values():                       # 蓄势榜: 「点火」券
        if b.get("ign") and b.get("code"):
            recs[b["code"]] = b
    cj = _newest_coil() or {}
    for code in (cj.get("groups", {}).get("蓄势TOP") or [])[:20]:   # 蓄势分前 20 也预热
        if code in coil:
            recs[code] = coil[code]
    if not recs:
        return None
    return bond_detail.prewarm(d.get("date"), list(recs.values()))


def _run_scan(force):
    """后台线程: 跑 scan_volume.py --report (扫描 + 历史验证 + 日报), 完了预热候选券分时"""
    try:
        cmd = [sys.executable, os.path.join(BASE, "scan_volume.py"), "--report"]
        if force:
            cmd.append("--force")
        r = subprocess.run(cmd, cwd=BASE, capture_output=True,
                           text=True, timeout=1200)
        msg = ((r.stdout or "") + (r.stderr or ""))[-1500:]
        if r.returncode == 0:
            try:                                    # 顺带跑蓄势扫描(产出 coil_scan_<日期>.json)
                rc = subprocess.run([sys.executable, os.path.join(BASE, "scan_coil.py")],
                                    cwd=BASE, capture_output=True, text=True, timeout=600)
                msg += "\n[蓄势扫描] " + (
                    (rc.stdout or rc.stderr or "").strip().splitlines()[-1]
                    if (rc.stdout or rc.stderr) else f"exit={rc.returncode}")
            except Exception as e:
                msg += f"\n[蓄势扫描] 失败: {e}"
            try:
                st = _prewarm_candidates()
                if st:
                    msg += (f"\n[预热个券分时] 候选 {st['total']} 只: "
                            f"新抓 {st['fetched']}, 失败 {st['failed']}, "
                            f"已有缓存 {st['skipped']}")
            except Exception as e:
                msg += f"\n[预热个券分时] 失败: {e}"
        with _scan_lock:
            _scan["running"] = False
            _scan["done"] = True
            _scan["msg"] = msg
            if r.returncode == 0:
                _scan["last_ok_date"] = datetime.now().strftime("%Y-%m-%d")
    except Exception as e:
        with _scan_lock:
            _scan["running"] = False
            _scan["done"] = True
            _scan["msg"] = f"放量扫描失败: {e}"


def maybe_scan(force=False):
    """需要才跑放量扫描, 返回 skipped / started / running"""
    date = _latest_snapshot_date()
    with _scan_lock:
        if _scan["running"]:
            return "running"
        if not force:
            if date and os.path.exists(_scan_path(date)):
                return "skipped"
            if datetime.now().weekday() >= 5:
                return "skipped"
        _scan["running"] = True
        _scan["done"] = False
        _scan["msg"] = ""
        _scan["started_at"] = datetime.now().strftime("%H:%M:%S")
    threading.Thread(target=_run_scan, args=(force,), daemon=True).start()
    return "started"


def _after_close(now):
    """A股已收盘且数据稳定(15:30 之后, 仅工作日)"""
    return now.weekday() < 5 and (now.hour > 15 or (now.hour == 15 and now.minute >= 30))


def maybe_fetch(force=False):
    """需要才抓, 返回 skipped / started / running

    注意: 凌晨抓取时数据源的最新交易日还是"昨天", 那一次只能得到昨天的快照 ——
    所以"今天抓过"≠"今天的数据已经有了"。收盘后若今天的数据仍未生成, 必须再抓一次,
    否则当天就再也不抓了(会把 09-17 这种正常交易日的快照漏掉)。"""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    with _fetch_lock:
        if _fetch["running"]:
            return "running"
        if not force:
            # data/ 里已有今天快照 -> 跳过
            if os.path.exists(os.path.join(DATA_DIR, f"{today}.json")):
                _fetch["last_ok_date"] = today
                return "skipped"
            # 周末不会产生新交易日数据; 已有最近交易日的快照就够了(脚本也要先打网络才判断)
            if now.weekday() >= 5 and _fetch["last_seen"]:
                return "skipped"
            # 今天已经抓过: 只有"收盘后 + 今天的数据还没生成 + 上次只抓到了更早的日期"才再抓
            if _fetch["last_ok_date"] == today:
                if not (_after_close(now) and (_fetch["last_seen"] or "") < today):
                    return "skipped"
        _fetch["running"] = True
        _fetch["done"] = False
        _fetch["msg"] = ""
        _fetch["started_at"] = now.strftime("%H:%M:%S")
    threading.Thread(target=_run_fetch, args=(force,), daemon=True).start()
    return "started"


def _start_scheduler():
    """每天收盘后(15:30 起)自动抓一次, 无需依赖系统定时任务/手动刷新。
    maybe_fetch 已处理周末/节假日/已存在等跳过; 这里只加时间窗, 避免盘中抓到不完整数据。
    抓完快照后接着补跑放量扫描(maybe_scan 自己有存在性判断, 重复调用无副作用)。"""
    def loop():
        while True:
            try:
                now = datetime.now()
                # 工作日, 且已过 15:30 (A股收盘数据稳定) 才尝试
                if now.weekday() < 5 and (now.hour > 15 or
                                          (now.hour == 15 and now.minute >= 30)):
                    maybe_fetch(force=False)
                    maybe_scan(force=False)
            except Exception:
                pass
            time.sleep(600)  # 每 10 分钟检查一次
    threading.Thread(target=loop, daemon=True).start()


def _load_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _load_day(date):
    return _load_json(os.path.join(DATA_DIR, f"{date}.json"))


def _rebuild_cache():
    """扫描 data/*.json, 构建日期索引与个券历史(带 mtime 缓存)"""
    sig = []
    for f in sorted(os.listdir(DATA_DIR)):
        if re.match(r"\d{4}-\d{2}-\d{2}\.json$", f):
            sig.append((f, int(os.path.getmtime(os.path.join(DATA_DIR, f)))))
    sig = tuple(sig)
    if sig == _cache["sig"]:
        return
    dates, by_date, bond_hist, bond_meta = [], {}, {}, {}
    for f, _ in sig:
        date = f[:10]
        d = _load_day(date)
        if not d:
            continue
        dates.append(date)
        by_date[date] = d
        for b in d.get("bonds", []):
            bond_hist.setdefault(b["code"], []).append({
                "date": date, "rank": b["rank"], "price": b["price"],
                "chg": b["chg"], "turnover_yi": b["turnover_yi"],
                "premium": b.get("premium"), "status": b.get("status"),
                "drivers": [x["tag"] for x in b.get("drivers", [])],
            })
            bond_meta[b["code"]] = {
                "name": b["name"], "code": b["code"],
                "stock_code": b.get("stock_code"),
                "stock_name": b.get("stock_name"),
                "rating": b.get("rating"), "scale": b.get("scale"),
                "listing_date": b.get("listing_date"),
                "last_seen": date,
            }
        for x in d.get("changes", {}).get("dropped_out", []):
            if x["code"] not in bond_meta:
                bond_meta[x["code"]] = {
                    "name": x["name"], "code": x["code"], "last_seen": date}
    dates.sort(reverse=True)
    # 每个券取最近一次快照里的 kline, 供历史页使用
    # 注意: bond_hist[code] 是按日期升序追加的, hist[-1] 才是最近在榜日
    for code, hist in bond_hist.items():
        last = hist[-1]["date"]
        for b in by_date[last].get("bonds", []):
            if b["code"] == code:
                bond_meta[code]["kline"] = b.get("kline_90d", [])
                bond_meta[code]["stock_kline"] = b.get("stock_kline_90d", [])
                break
    # 首次进入 / 移除日期
    dates_asc = sorted(dates)
    latest = dates_asc[-1] if dates_asc else None
    for code, hist in bond_hist.items():
        meta = bond_meta[code]
        meta["first_seen"] = hist[0]["date"]
        meta["last_seen"] = hist[-1]["date"]
        meta["removed_on"] = None
        if latest and meta["last_seen"] != latest:
            # 移除日期 = 最后一次在榜后的下一个交易日
            for dt in dates_asc:
                if dt > meta["last_seen"]:
                    meta["removed_on"] = dt
                    break
    with _lock:
        _cache.update({"sig": sig, "dates": dates, "by_date": by_date,
                       "bond_hist": bond_hist, "bond_meta": bond_meta})


def _consecutive_days(code, date):
    """截止 date 当天, 该券连续在榜的交易日数 (按已有数据文件统计; 中断一天重新计)"""
    day_dates = {h["date"] for h in _cache["bond_hist"].get(code, [])}
    if date not in day_dates:
        return 0
    dates_asc = sorted(_cache["dates"])
    try:
        i = dates_asc.index(date)
    except ValueError:
        return 1
    n = 0
    while i >= 0 and dates_asc[i] in day_dates:
        n += 1
        i -= 1
    return n


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json; charset=utf-8", code=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        _rebuild_cache()
        raw_path = self.path
        path = raw_path.split("?")[0]
        if path in ("/", "/index.html"):
            p = os.path.join(BASE, "index.html")
            if os.path.exists(p):
                self._send(open(p, "rb").read(), "text/html; charset=utf-8")
            else:
                self._send("index.html missing", "text/plain", 404)
            return
        # PWA: 添加到主屏幕用的 manifest / 图标 / favicon
        if path == "/manifest.webmanifest":
            p = os.path.join(BASE, "manifest.webmanifest")
            if os.path.exists(p):
                self._send(open(p, "rb").read(), "application/manifest+json; charset=utf-8")
            else:
                self._send("{}", "application/manifest+json; charset=utf-8", 404)
            return
        if path == "/favicon.ico":
            p = os.path.join(BASE, "assets", "icon-192.png")
            if os.path.exists(p):
                self._send(open(p, "rb").read(), "image/png")
            else:
                self._send("not found", "text/plain", 404)
            return
        if path.startswith("/assets/"):
            name = os.path.basename(path)
            p = os.path.join(BASE, "assets", name)
            if os.path.exists(p):
                ctype = ("application/javascript" if name.endswith(".js")
                         else "image/png" if name.endswith(".png")
                         else "text/css" if name.endswith(".css")
                         else "application/json" if name.endswith(".json")
                         else "application/octet-stream")
                self._send(open(p, "rb").read(), ctype)
            else:
                self._send("not found", "text/plain", 404)
            return
        # 按需抓取: 页面刷新时触发; 已有当天数据则跳过 (force=1 强制重抓)
        if path == "/api/refresh":
            force = "force" in raw_path
            st = maybe_fetch(force=force)
            sc = maybe_scan(force=force)
            self._send(json.dumps({"status": st, "scan": sc}, ensure_ascii=False))
            return
        if path == "/api/refresh/status":
            with _fetch_lock:
                out = {"running": _fetch["running"], "done": _fetch["done"],
                       "msg": _fetch["msg"], "started_at": _fetch["started_at"]}
            with _scan_lock:
                out["scan_running"] = _scan["running"]
                out["scan_done"] = _scan["done"]
                out["scan_msg"] = _scan["msg"]
            out["running"] = out["running"] or out["scan_running"]
            self._send(json.dumps(out, ensure_ascii=False))
            return
        # ---- 放量榜 ----
        if path == "/api/volume/dates":
            out = []
            for f in sorted(os.listdir(DATA_DIR), reverse=True):
                m = re.match(r"^volume_scan_(\d{4}-\d{2}-\d{2})\.json$", f)
                if not m:
                    continue
                d = _load_json(os.path.join(DATA_DIR, f))
                if d:
                    out.append({"date": m.group(1), "counts": d.get("counts", {}),
                                "generated_at": d.get("generated_at")})
            self._send(json.dumps(out, ensure_ascii=False))
            return
        if path in ("/api/volume", "/api/volume/latest"):
            dates = sorted(
                m.group(1) for m in
                (re.match(r"^volume_scan_(\d{4}-\d{2}-\d{2})\.json$", f)
                 for f in os.listdir(DATA_DIR)) if m)
            if not dates:
                self._send('{"error":"no scan data"}', code=404)
                return
            d = _load_json(_scan_path(dates[-1]))
            self._send(json.dumps(d or {"error": "no scan data"},
                                  ensure_ascii=False),
                       code=200 if d else 404)
            return
        m = re.match(r"^/api/volume/kline/(\d{6})$", path)
        if m:
            code = m.group(1)
            files = sorted(f for f in os.listdir(DATA_DIR)
                           if f.startswith("klines_") and f.endswith(".json"))
            if not files:
                self._send('{"error":"no kline cache"}', code=404)
                return
            d = _load_json(os.path.join(DATA_DIR, files[-1])) or {}
            k = (d.get("klines") or {}).get(code)
            if not k:
                self._send('{"error":"no kline"}', code=404)
                return
            self._send(json.dumps({"date": d.get("date"), "code": code,
                                   "kline": k}, ensure_ascii=False))
            return
        m = re.match(r"^/api/volume/(\d{4}-\d{2}-\d{2})$", path)
        if m:
            d = _load_json(_scan_path(m.group(1)))
            if d:
                self._send(json.dumps(d, ensure_ascii=False))
            else:
                self._send('{"error":"no scan data"}', code=404)
            return
        # ---- 蓄势榜 ----
        if path in ("/api/coil", "/api/coil/latest"):
            date = _newest_file_date("coil_scan")
            d = _load_json(_coil_path(date)) if date else None
            self._send(json.dumps(d or {"error": "no coil scan data"},
                                  ensure_ascii=False), code=200 if d else 404)
            return
        m = re.match(r"^/api/coil/(\d{4}-\d{2}-\d{2})$", path)
        if m:
            d = _load_json(_coil_path(m.group(1)))
            if d:
                self._send(json.dumps(d, ensure_ascii=False))
            else:
                self._send('{"error":"no coil scan data"}', code=404)
            return
        # 非当日热度榜的券: 快照里没有它的分时/正股 -> 按需补抓 (默认只读缓存, fetch=1 才发网络请求)
        m = re.match(r"^/api/bond_detail/(\d{6})$", path)
        if m:
            code = m.group(1)
            qs = urllib.parse.parse_qs(raw_path.split("?", 1)[1]) \
                if "?" in raw_path else {}
            date = (qs.get("date") or [None])[0] or _latest_snapshot_date()
            want = str((qs.get("fetch") or ["0"])[0]).lower() in ("1", "true", "yes")
            payload, _from_cache = bond_detail.get(code, date, _bond_record(code),
                                                   fetch=want)
            self._send(json.dumps(payload, ensure_ascii=False))
            return
        if path == "/api/dates":
            out = []
            for dt in _cache["dates"]:
                d = _cache["by_date"][dt]
                out.append({
                    "date": dt,
                    "summary": d.get("summary", {}),
                    "changes": {
                        "new_in": len(d.get("changes", {}).get("new_in", [])),
                        "dropped": len(d.get("changes", {}).get("dropped_out", [])),
                    },
                })
            self._send(json.dumps(out, ensure_ascii=False))
            return
        m = re.match(r"^/api/day/(\d{4}-\d{2}-\d{2})$", path)
        if m:
            d = _cache["by_date"].get(m.group(1))
            if d:
                out = dict(d)
                bonds_out = []
                for b in d.get("bonds", []):
                    bb = dict(b)
                    bb["days_on_list"] = _consecutive_days(bb["code"], d["date"])
                    bonds_out.append(bb)
                out["bonds"] = bonds_out
                self._send(json.dumps(out, ensure_ascii=False))
            else:
                self._send('{"error":"no data"}', code=404)
            return
        if path == "/api/kline_history":
            # 所有快照的日K(只留必要字段), 供前端做「量价齐升」信号前瞻收益验证。
            # 口径与前端图表一致: [日期, 开, 收, 高, 低, 量(手), 成交额(元)]
            days = []
            for dt in _cache["dates"]:
                d = _cache["by_date"][dt]
                bs = []
                for b in d.get("bonds", []):
                    kl = b.get("kline_90d") or []
                    if not kl:
                        continue
                    bs.append({
                        "code": b.get("code"), "name": b.get("name", ""),
                        "rank": b.get("rank"), "chg": b.get("chg"),
                        "k": [[r[0]] + [round(float(x), 4) for x in r[1:]]
                              for r in kl],
                    })
                days.append({"date": dt, "bonds": bs})
            self._send(json.dumps({"days": days}, ensure_ascii=False))
            return
        m = re.match(r"^/api/bond/(\d{6})$", path)
        if m:
            code = m.group(1)
            meta = _cache["bond_meta"].get(code)
            hist = _cache["bond_hist"].get(code, [])
            if not meta:
                self._send('{"error":"unknown bond"}', code=404)
                return
            self._send(json.dumps({"meta": meta, "history": hist},
                                  ensure_ascii=False))
            return
        if path == "/api/bonds":
            out = sorted(_cache["bond_meta"].values(),
                         key=lambda x: x.get("last_seen", ""), reverse=True)
            self._send(json.dumps(out, ensure_ascii=False))
            return
        self._send("not found", "text/plain", 404)


def _lan_ips():
    """本机在局域网里的 IPv4 地址 (手机访问用)"""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("223.5.5.5", 80))    # 不真发包, 只让内核挑出默认出口网卡
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            ips.append(ip)
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    return ips


def main():
    _rebuild_cache()
    _start_scheduler()
    # 绑 0.0.0.0 而不是 127.0.0.1: 手机要能用局域网 IP 打开并"添加到主屏幕"
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"可转债热度看板  本机: http://127.0.0.1:{PORT}")
    ips = _lan_ips()
    if ips:
        for ip in ips:
            print(f"                手机(需同一 Wi-Fi): http://{ip}:{PORT}")
    else:
        print("                手机访问: 没取到局域网 IP, 见 系统设置-网络- Wi-Fi 详情")
    print("  Ctrl+C 退出 · 手机打不开先检查 macOS 防火墙是否放行 python")
    srv.serve_forever()


if __name__ == "__main__":
    main()
