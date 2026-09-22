#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""push_holdings.py 的测试: 默认纯离线(不碰网络), 加 --live 才打真实接口。

重点覆盖的不是"代码能跑", 而是这类推送最容易出错、且出错最贵的几件事:

  1. **盈亏口径必须与看板逐字一致**。推送和网页给出两个不一样的"浮动盈亏",
     比不推送更糟 —— 用户会开始怀疑所有数字。前端 renderSim() 的公式是
     `val = price*qty; c = buyPrice*qty + fee`, 这里逐项对齐。
  2. **停牌时按买入价计**(与前端 `q?q.price:p.buyPrice` 一致), 不是把该笔剔除 ——
     剔除会让总资产对不上, 而且持仓"凭空少一笔"。
  3. **时段门**: 非盘中时段数据冻结在上一时点, 推了就是噪音; 必须能识别并跳过。
  4. **去重**: launchd 与 app.py 内置调度可能同时触发, 不能让你半小时收到两条。
  5. **东财断连时不许静默降级**: 缺溢价率/强赎价要说清是"腾讯价 + 旧缓存",
     而不是把 None 当"没有风险"。
  6. **数字不许来自文案层**: 所有金额都必须在 compute() 里算出来。

用法: python3 tests/push_test.py          # 离线
      python3 tests/push_test.py --live   # 额外验证真实行情接口(东财+腾讯)
"""
import argparse
import copy
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import push_holdings as ph   # noqa: E402

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL.append(name)
        print(f"  ✗ {name}  {detail}")


def close(a, b, tol=0.01):
    return a is not None and b is not None and abs(a - b) <= tol


# ---------------------------------------------------------------- 桩
def mk_quote(price, chg=0.0, premium=10.0, stock_price=10.0, trig=13.0,
             src="em", name="测试转债"):
    """造一条**已 parse 过**的行情(与 fetch_quotes_* 的输出同构)。"""
    return {"name": name, "price": price, "chg": chg, "premium": premium,
            "stock_code": "600000", "stock_name": "测试正股",
            "stock_price": stock_price, "stock_chg": 0.0,
            "redeem_trigger": trig, "maturity_redeem": 108.0, "_src": src}


def mk_pos(code="113701", qty=10, buy_price=100.0, fee=1.0, due="2099-01-01",
           name="测试转债"):
    return {"id": 1, "code": code, "name": name, "buyDate": "2026-09-01",
            "buyPrice": buy_price, "qty": qty, "fee": fee, "dueDate": due}


def mk_pf(positions, cash=1000.0, init=10000.0, closed=None):
    return {"init": init, "cash": cash, "lot": 10, "feeBp": 0.1, "seq": 1,
            "positions": positions, "closed": closed or []}


BASE_CFG = copy.deepcopy(ph.DEFAULT_CONFIG)


# ---------------------------------------------------------------- 1. 账目口径
def t_accounting():
    print("\n[1] 盈亏口径(与前端 renderSim() 逐字对齐)")
    # 前端: val = price*qty ; c = buyPrice*qty + fee ; pnl = val - c
    pos = mk_pos(qty=10, buy_price=100.0, fee=1.0)
    rep = ph.compute(mk_pf([pos], cash=1000.0, init=10000.0),
                     {"113701": mk_quote(110.0)}, BASE_CFG)
    p = rep["positions"][0]
    check("成本含费 = 买入价×张数 + 手续费", close(p["cost"], 1001.0), p["cost"])
    check("市值 = 现价×张数", close(p["mv"], 1100.0), p["mv"])
    check("浮动盈亏 = 市值 - 成本", close(p["pnl"], 99.0), p["pnl"])
    check("盈亏% 以含费成本为分母", close(p["pnl_pct"], 99.0 / 1001.0 * 100), p["pnl_pct"])
    check("总资产 = 现金 + 市值", close(rep["total"], 2100.0), rep["total"])
    check("汇总浮动盈亏 = 逐笔之和", close(rep["float_pnl"], 99.0), rep["float_pnl"])
    check("总收益 = 总资产 - 初始资金", close(rep["total_pnl"], 2100.0 - 10000.0),
          rep["total_pnl"])

    # 多笔 + 已实现
    pf = mk_pf([mk_pos("113701", 10, 100.0, 1.0), mk_pos("111024", 10, 200.0, 2.0)],
               cash=500.0, init=10000.0,
               closed=[{"qty": 10, "pnl": 123.45}, {"qty": 10, "pnl": -23.45}])
    rep = ph.compute(pf, {"113701": mk_quote(110.0), "111024": mk_quote(190.0)},
                     BASE_CFG)
    check("已实现盈亏 = closed 的 pnl 之和", close(rep["realized"], 100.0), rep["realized"])
    check("成本合计 = 1001 + 2002", close(rep["cost"], 3003.0), rep["cost"])
    check("市值合计 = 1100 + 1900", close(rep["mv"], 3000.0), rep["mv"])
    check("浮动盈亏合计 = -3", close(rep["float_pnl"], -3.0), rep["float_pnl"])
    check("总资产 = 现金500 + 市值3000", close(rep["total"], 3500.0), rep["total"])
    check("持仓笔数", rep["n"] == 2, rep["n"])

    # 手续费口径: fee=0 时成本 == 买入价×张数
    rep0 = ph.compute(mk_pf([mk_pos(fee=0.0)]), {"113701": mk_quote(100.0)}, BASE_CFG)
    check("手续费为 0 时成本 = 买入价×张数", close(rep0["positions"][0]["cost"], 1000.0))

    # 加仓摊薄后的均价由前端算好写进 buyPrice; 这里只验证按它算
    rep1 = ph.compute(mk_pf([mk_pos(qty=30, buy_price=123.456, fee=3.7)]),
                      {"113701": mk_quote(130.0)}, BASE_CFG)
    check("加仓后的均价×张数+费 直接采信", close(rep1["positions"][0]["cost"],
                                                123.456 * 30 + 3.7))


# ---------------------------------------------------------------- 2. 停牌/无行情
def t_no_quote():
    print("\n[2] 无行情(停牌) —— 不许凭空少一笔, 口径要对齐看板")
    pf = mk_pf([mk_pos("113701", 10, 100.0, 1.0), mk_pos("111024", 10, 200.0, 2.0)])
    rep = ph.compute(pf, {"113701": mk_quote(110.0)}, BASE_CFG)   # 111024 无行情
    by = {p["code"]: p for p in rep["positions"]}
    check("无行情的那笔仍在列表里(不静默丢弃)", "111024" in by)
    check("无行情时按买入价计市值(对齐前端)", close(by["111024"]["price"], 200.0),
          by["111024"]["price"])
    check("无行情时该笔盈亏 = -手续费(与前端一致, 不是 0)",
          close(by["111024"]["pnl"], -2.0), by["111024"]["pnl"])
    check("成本合计仍含无行情那笔(2002+1001)", close(rep["cost"], 3003.0), rep["cost"])
    check("市值合计含无行情那笔(1100+2000)", close(rep["mv"], 3100.0), rep["mv"])
    check("no_quote 计数 = 1", rep["no_quote"] == 1, rep["no_quote"])
    check("无行情会被标出来", any("无行情" in f for f in by["111024"]["flags"]),
          by["111024"]["flags"])
    check("无行情进告警汇总", any("无行情" in w[2] for w in rep["warnings"]), rep["warnings"])


# ---------------------------------------------------------------- 3. 风险标记
def t_flags():
    print("\n[3] 风险标记: 强赎 / 高溢价 / 应卖日")
    today = datetime.now().strftime("%Y-%m-%d")

    # 强赎三态
    rep = ph.compute(mk_pf([mk_pos()]),
                     {"113701": mk_quote(100.0, stock_price=14.0, trig=13.0)}, BASE_CFG)
    check("正股价 ≥ 触发价 -> 已满足强赎",
          any("已满足强赎" in f for f in rep["positions"][0]["flags"]),
          rep["positions"][0]["flags"])

    rep = ph.compute(mk_pf([mk_pos()]),
                     {"113701": mk_quote(100.0, stock_price=13.0, trig=13.0)}, BASE_CFG)
    check("正股价 == 触发价 也算已满足(边界)",
          any("已满足强赎" in f for f in rep["positions"][0]["flags"]),
          rep["positions"][0]["flags"])

    # 距触发 4.0% < 5% 阈值 -> 逼近
    rep = ph.compute(mk_pf([mk_pos()]),
                     {"113701": mk_quote(100.0, stock_price=12.5, trig=13.0)}, BASE_CFG)
    check("距触发 4.0% (≤5%) -> 逼近强赎",
          any("逼近强赎" in f and "4.00%" in f for f in rep["positions"][0]["flags"]),
          rep["positions"][0]["flags"])

    # 距触发 30% -> 不提醒
    rep = ph.compute(mk_pf([mk_pos()]),
                     {"113701": mk_quote(100.0, stock_price=10.0, trig=13.0)}, BASE_CFG)
    check("距触发 30% -> 不提醒强赎",
          not any("强赎" in f for f in rep["positions"][0]["flags"]),
          rep["positions"][0]["flags"])

    # 阈值可配: 把阈值放宽到 2%, 4% 就不该报了
    cfg = copy.deepcopy(BASE_CFG)
    cfg["content"]["redeem_near_pct"] = 2.0
    rep = ph.compute(mk_pf([mk_pos()]),
                     {"113701": mk_quote(100.0, stock_price=12.5, trig=13.0)}, cfg)
    check("redeem_near_pct 阈值生效(2% 时 4% 不报)",
          not any("强赎" in f for f in rep["positions"][0]["flags"]),
          rep["positions"][0]["flags"])

    # 高溢价
    rep = ph.compute(mk_pf([mk_pos()]),
                     {"113701": mk_quote(100.0, premium=55.0)}, BASE_CFG)
    check("溢价 55% ≥ 50% -> 高溢价提醒",
          any("高溢价" in f for f in rep["positions"][0]["flags"]),
          rep["positions"][0]["flags"])
    rep = ph.compute(mk_pf([mk_pos()]),
                     {"113701": mk_quote(100.0, premium=49.9)}, BASE_CFG)
    check("溢价 49.9% < 50% -> 不提醒",
          not any("高溢价" in f for f in rep["positions"][0]["flags"]))
    rep = ph.compute(mk_pf([mk_pos()]),
                     {"113701": mk_quote(100.0, premium=None)}, BASE_CFG)
    check("溢价取不到(None) 不报也不崩",
          not any("高溢价" in f for f in rep["positions"][0]["flags"]))

    # 应卖日
    rep = ph.compute(mk_pf([mk_pos(due="2020-01-01")]),
                     {"113701": mk_quote(100.0)}, BASE_CFG)
    check("应卖日已过 -> 提醒",
          any("已到应卖日" in f for f in rep["positions"][0]["flags"]),
          rep["positions"][0]["flags"])
    rep = ph.compute(mk_pf([mk_pos(due=today)]),
                     {"113701": mk_quote(100.0)}, BASE_CFG)
    check("应卖日就是今天 -> 提醒",
          any("已到应卖日" in f for f in rep["positions"][0]["flags"]))
    rep = ph.compute(mk_pf([mk_pos(due="2099-01-01")]),
                     {"113701": mk_quote(100.0)}, BASE_CFG)
    check("应卖日在未来 -> 不提醒",
          not any("应卖日" in f for f in rep["positions"][0]["flags"]))
    rep = ph.compute(mk_pf([mk_pos(due="")]), {"113701": mk_quote(100.0)}, BASE_CFG)
    check("无应卖日字段 -> 不崩不报", not rep["positions"][0]["flags"])


# ---------------------------------------------------------------- 4. 腾讯兜底
def t_fallback():
    print("\n[4] 东财断连走腾讯兜底: 说明来源, 不假装有风险数据")
    txq = mk_quote(100.0, src="tx", premium=None, stock_price=None, trig=None)
    rep = ph.compute(mk_pf([mk_pos()]), {"113701": txq}, BASE_CFG)
    check("无缓存时: 不报强赎也不报高溢价(不猜)",
          not any(("强赎" in f or "高溢价" in f) for f in rep["positions"][0]["flags"]),
          rep["positions"][0]["flags"])
    check("无缓存时 stale_at 为空", rep["stale_at"] == "", rep["stale_at"])

    # 有缓存 -> 用缓存值判, 且必须标注时点
    cache = {"113701": {"premium": 60.0, "redeem_trigger": 13.0, "stock_price": 14.0,
                        "stock_name": "测试正股", "at": "2026-09-22 09:30"}}
    rep = ph.compute(mk_pf([mk_pos()]), {"113701": txq}, BASE_CFG, source="tx",
                     qcache=cache)
    flags = rep["positions"][0]["flags"]
    check("有缓存时能判出强赎", any("已满足强赎" in f for f in flags), flags)
    check("有缓存时能判出高溢价", any("高溢价" in f for f in flags), flags)
    check("缓存值必须标注时点", all("2026-09-22 09:30 的缓存值" in f
                                  for f in flags if "强赎" in f or "高溢价" in f), flags)
    check("stale_at 透出给文案层", rep["stale_at"] == "2026-09-22 09:30", rep["stale_at"])

    # 东财成功时不该用缓存
    rep = ph.compute(mk_pf([mk_pos()]),
                     {"113701": mk_quote(100.0, premium=10.0, stock_price=10.0,
                                         trig=13.0)},
                     BASE_CFG, source="em", qcache=cache)
    check("东财数据可用时不套用缓存",
          not any("缓存值" in f for f in rep["positions"][0]["flags"]))
    check("东财源时 stale_at 为空", rep["stale_at"] == "")


# ---------------------------------------------------------------- 5. 时段门
def t_window():
    print("\n[5] 时段门: 只在盘中推")
    cfg = copy.deepcopy(BASE_CFG)
    D = datetime(2026, 9, 22)          # 周二
    cases = [("盘前 09:00", D.replace(hour=9, minute=0), True),
             ("集合竞价 09:20", D.replace(hour=9, minute=20), True),
             ("盘中上午 10:00", D.replace(hour=10, minute=0), False),
             ("盘中上午边界 11:30", D.replace(hour=11, minute=30), False),
             ("午间休市 12:00", D.replace(hour=12, minute=0), True),
             ("盘中下午 14:00", D.replace(hour=14, minute=0), False),
             ("盘中下午边界 15:00", D.replace(hour=15, minute=0), False),
             ("盘后 15:30", D.replace(hour=15, minute=30), True),
             ("周末 周六 10:00", datetime(2026, 9, 26, 10, 0), True),
             ("周末 周日 14:00", datetime(2026, 9, 27, 14, 0), True)]
    for label, when, want_skip in cases:
        skip, why = ph.should_skip(cfg, {}, when, force=False)
        check(f"{label} -> {'跳过' if want_skip else '推送'}",
              skip == want_skip, f"got skip={skip} ({why})")

    # 关掉时段门 -> 盘前也推
    cfg2 = copy.deepcopy(BASE_CFG)
    cfg2["window"]["only_trading_session"] = False
    skip, _ = ph.should_skip(cfg2, {}, D.replace(hour=3, minute=0), force=False)
    check("only_trading_session=false 时盘前也推", not skip)

    # force 绕过时段门
    skip, _ = ph.should_skip(cfg, {}, D.replace(hour=3, minute=0), force=True)
    check("--force 绕过时段门", not skip)

    # enabled=false 时 force 也拦不住? 不, force 应能绕过(手工测试用)
    cfg3 = copy.deepcopy(BASE_CFG)
    cfg3["enabled"] = False
    skip, why = ph.should_skip(cfg3, {}, D.replace(hour=10), force=False)
    check("enabled=false 时不推", skip and "enabled" in why, why)
    skip, _ = ph.should_skip(cfg3, {}, D.replace(hour=10), force=True)
    check("enabled=false 时 --force 仍可手工推", not skip)


# ---------------------------------------------------------------- 6. 去重
def t_dedupe():
    print("\n[6] 去重: 半小时只能收到一条")
    cfg = copy.deepcopy(BASE_CFG)
    now = datetime(2026, 9, 22, 10, 0, 0)
    gap = cfg["window"]["min_gap_minutes"]

    skip, why = ph.should_skip(cfg, {"last_sent_ts": now.timestamp() - 10 * 60}, now, False)
    check(f"距上次 10 分钟 (<{gap}) -> 跳过", skip and "去重" in why, why)
    skip, _ = ph.should_skip(cfg, {"last_sent_ts": now.timestamp() - 30 * 60}, now, False)
    check("距上次 30 分钟 -> 推", not skip)
    skip, _ = ph.should_skip(cfg, {"last_sent_ts": now.timestamp() - 10 * 60}, now, True)
    check("--force 绕过去重", not skip)
    skip, _ = ph.should_skip(cfg, {}, now, False)
    check("没有历史记录 -> 推", not skip)
    # 双调度场景: launchd 与 app.py 同时触发, 第二次必须被拦
    st = {"last_sent_ts": now.timestamp()}
    skip2, why2 = ph.should_skip(cfg, st, now.replace(second=30), False)
    check("同一次触发内的第二次(30秒后)被拦", skip2 and "去重" in why2, why2)
    cfg0 = copy.deepcopy(BASE_CFG)
    cfg0["window"]["min_gap_minutes"] = 0
    skip3, _ = ph.should_skip(cfg0, {"last_sent_ts": now.timestamp() - 1}, now, False)
    check("min_gap_minutes=0 -> 不去重", not skip3)


# ---------------------------------------------------------------- 7. 文案
def t_format():
    print("\n[7] 文案: 纯文本、有上限、箭头与优先级正确")
    rep = ph.compute(mk_pf([mk_pos()], cash=1000.0, init=10000.0),
                     {"113701": mk_quote(110.0)}, BASE_CFG)
    title, body, prio, tags = ph.format_report(rep, BASE_CFG,
                                               datetime(2026, 9, 22, 10, 30))
    check("标题含时间", "10:30" in title, title)
    check("盈利时标题带 ▲", "▲" in title, title)
    check("标题含浮动盈亏金额", "+99.00" in title, title)

    rep2 = ph.compute(mk_pf([mk_pos()], cash=1000.0, init=10000.0),
                      {"113701": mk_quote(90.0)}, BASE_CFG)
    title2, _, _, _ = ph.format_report(rep2, BASE_CFG, datetime(2026, 9, 22, 10, 30))
    check("亏损时标题带 ▼", "▼" in title2, title2)

    check("正文不含 markdown 星号(Android 端不保证渲染)", "**" not in body)
    check("正文不含 markdown 标题井号", not any(l.startswith("#") for l in body.split("\n")))
    check("正文含逐笔明细", "113701" in body and "成本" in body)

    # 字段口径由用户 2026-09-22 指定: 只要 转债/成本(含费)/最新价/浮动盈亏 四项。
    # 这四项一个都不能少 —— 少了就是功能退化。
    check("逐笔含: 成本(含费)", "成本 1,001.00" in body, body)
    check("逐笔含: 最新价", "最新 110.000" in body, body)
    check("逐笔含: 浮动盈亏(额+%)", "盈亏 +99.00 (+9.89%)" in body, body)
    check("逐笔含: 转债名+代码", "测试转债 113701" in body, body)
    # 用户没要的字段不该出现(出现就是没按口径改)
    for gone in ("市值", "张数", "总资产", "现金", "已实现", "总收益", "10张"):
        check(f"正文不含用户没要的「{gone}」", gone not in body,
              [l for l in body.split("\n") if gone in l])
    check("成本显示两位小数(含费口径)", "1,001.00" in body)

    # 告警 -> 高优先级 + 警示标签
    rep3 = ph.compute(mk_pf([mk_pos(due="2020-01-01")]),
                      {"113701": mk_quote(110.0)}, BASE_CFG)
    _, body3, prio3, tags3 = ph.format_report(rep3, BASE_CFG)
    check("有告警时优先级用 priority_alert", prio3 == BASE_CFG["content"]["priority_alert"],
          prio3)
    check("有告警时用警示标签", tags3 == ["rotating_light"], tags3)
    check("有告警时正文有告警段", "—— 告警 ——" in body3)
    check("无告警时优先级用 priority_normal", prio == BASE_CFG["content"]["priority_normal"],
          prio)
    check("无告警时不用警示标签", tags != ["rotating_light"], tags)

    # 涨幅方向标签
    _, _, _, tags_up = ph.format_report(rep, BASE_CFG)
    _, _, _, tags_dn = ph.format_report(rep2, BASE_CFG)
    check("盈利标签 = 上涨图", tags_up == ["chart_with_upwards_trend"], tags_up)
    check("亏损标签 = 下跌图", tags_dn == ["chart_with_downwards_trend"], tags_dn)

    # 混合来源要说明
    rep4 = ph.compute(mk_pf([mk_pos()]),
                      {"113701": mk_quote(100.0, src="tx", premium=None,
                                          stock_price=None, trig=None)},
                      BASE_CFG, source="tx",
                      qcache={"113701": {"at": "2026-09-22 09:30", "premium": 10.0,
                                         "redeem_trigger": None, "stock_price": None}})
    _, body4, _, _ = ph.format_report(rep4, BASE_CFG)
    check("腾讯兜底时正文声明来源", "腾讯行情" in body4, body4[:200])
    check("腾讯兜底时声明缓存时点", "2026-09-22 09:30" in body4, body4[:200])

    # 顺序: 必须与看板持仓表一致(建仓顺序), 不按盈亏排
    p3 = [mk_pos("111111", name="首"), mk_pos("222222", name="次"), mk_pos("333333", name="末")]
    rep3 = ph.compute(mk_pf(p3),
                      {"111111": mk_quote(101.0), "222222": mk_quote(140.0),
                       "333333": mk_quote(100.5)}, BASE_CFG)
    b3 = ph.format_report(rep3, BASE_CFG, datetime(2026, 9, 22, 10, 30))[1]
    check("顺序保持镜像原序(与看板一致, 不按盈亏排)",
          b3.index("首 111111") < b3.index("次 222222") < b3.index("末 333333"),
          [l for l in b3.split("\n") if " 1" in l or " 2" in l or " 3" in l])

    # 数量上限 + 截断
    many = [mk_pos(code=f"11370{i}", name=f"券{i}") for i in range(20)]
    quotes = {f"11370{i}": mk_quote(100.0 + i) for i in range(20)}
    cfg_small = copy.deepcopy(BASE_CFG)
    cfg_small["content"]["max_positions"] = 3
    rep5 = ph.compute(mk_pf(many), quotes, cfg_small)
    _, body5, _, _ = ph.format_report(rep5, cfg_small)
    check("超过 max_positions 时汇总其余", "另有 17 笔" in body5, body5[-400:])

    cfg_tiny = copy.deepcopy(BASE_CFG)
    cfg_tiny["content"]["max_chars"] = 200
    _, body6, _, _ = ph.format_report(rep5, cfg_tiny)
    check("超过 max_chars 时截断", len(body6) <= 200, len(body6))
    check("截断时有提示", "已截断" in body6, body6[-60:])

    # 空仓也不该崩(正常路径会在 main 里提前跳过)
    rep7 = ph.compute(mk_pf([]), {}, BASE_CFG)
    t7, b7, _, _ = ph.format_report(rep7, BASE_CFG)
    check("空仓格式化不崩", isinstance(b7, str) and len(b7) > 0)
    check("空仓浮动盈亏为 0", close(rep7["float_pnl"], 0.0))
    check("空仓时盈亏百分比不除零", rep7["float_pnl_pct"] == 0.0, rep7["float_pnl_pct"])

    # 镜像太旧要提示(不然会拿几天前的持仓当今天的)
    now = datetime(2026, 9, 22, 10, 30)
    fresh = {"saved_ts": now.timestamp() - 3600}          # 1 小时前
    old = {"saved_ts": now.timestamp() - 5 * 86400}       # 5 天前
    check("1 小时前的镜像天数 ≈ 0.04", close(ph.mirror_age_days(fresh, now), 1 / 24, 0.01),
          ph.mirror_age_days(fresh, now))
    check("5 天前的镜像天数 ≈ 5", close(ph.mirror_age_days(old, now), 5.0, 0.01),
          ph.mirror_age_days(old, now))
    check("无时间戳时返回 None(不瞎猜)", ph.mirror_age_days({}, now) is None)
    check("时间戳是垃圾时返回 None 不崩", ph.mirror_age_days({"saved_ts": "x"}, now) is None)
    rep_f = ph.compute(mk_pf([mk_pos()]), {"113701": mk_quote(110.0)}, BASE_CFG)
    rep_f["mirror_age_days"] = ph.mirror_age_days(fresh, now)
    check("镜像新鲜时不提示", "天前同步" not in ph.format_report(rep_f, BASE_CFG, now)[1])
    rep_o = dict(rep_f, mirror_age_days=ph.mirror_age_days(old, now))
    check("镜像超过 3 天时提示", "天前同步" in ph.format_report(rep_o, BASE_CFG, now)[1])

    # 镜像来源: 非浏览器同步的镜像必须自曝来源(否则会把重建/样例数据当成用户的真实持仓)
    check("浏览器同步的镜像不提示来源",
          ph.mirror_note({"source": ph.MIRROR_SOURCE_WEB}) == "",
          ph.mirror_note({"source": ph.MIRROR_SOURCE_WEB}))
    check("非浏览器来源会被点名",
          "不是浏览器同步" in ph.mirror_note({"source": "from-screenshot"}),
          ph.mirror_note({"source": "from-screenshot"}))
    check("来源缺失也算异常(不装作正常)",
          "未知" in ph.mirror_note({}), ph.mirror_note({}))
    rep_src = dict(rep_f, mirror_note=ph.mirror_note({"source": "from-screenshot"}))
    check("来源提示会进正文", "不是浏览器同步" in ph.format_report(rep_src, BASE_CFG, now)[1])
    check("来源正常时正文不提这事",
          "不是浏览器同步" not in ph.format_report(
              dict(rep_f, mirror_note=ph.mirror_note({"source": ph.MIRROR_SOURCE_WEB})),
              BASE_CFG, now)[1])

    # 行情日期: 让手机上一眼看出价格是不是今天的(盘前拿到的是昨收)
    check("有行情日期时逐笔带上 (MM-DD)", "(09-21)" in ph.format_report(
        dict(rep_f, positions=[dict(rep_f["positions"][0], qdate="09-21")]),
        BASE_CFG, now)[1])
    check("没有行情日期时不显示空括号", "()" not in ph.format_report(
        dict(rep_f, positions=[dict(rep_f["positions"][0], qdate="")]),
        BASE_CFG, now)[1])

    # _fmt_qdate: 两个源的时点格式完全不同, 别用同一分支硬套
    QD = [("20260921161434", "09-21"),   # 腾讯紧凑 14 位
          ("20260921", "09-21"),         # 腾讯 8 位
          ("1790033787", "09-22"),       # 东财 unix 秒(= 2026-09-22 07:36 CST)
          (1790033787000, "09-22"),      # 东财毫秒
          (None, ""), ("", ""), ("-", ""), ("0", ""), ("abc", ""), ("2026", "")]
    for v, want in QD:
        got = ph._fmt_qdate(v)
        check(f"_fmt_qdate({v!r}) = {want!r}", got == want, f"得到 {got!r}")


# ---------------------------------------------------------------- 8. 配置
def t_config():
    print("\n[8] 配置合并: 用户只写一部分也要拿到默认值")
    merged = ph._deep_merge(ph.DEFAULT_CONFIG,
                            {"ntfy": {"topic": "mytopic"},
                             "content": {"max_positions": 5}})
    check("用户覆盖生效(topic)", merged["ntfy"]["topic"] == "mytopic")
    check("未覆盖的兄弟键保留(server)", merged["ntfy"]["server"] == "https://ntfy.sh")
    check("用户覆盖生效(max_positions)", merged["content"]["max_positions"] == 5)
    check("未覆盖的默认值保留(max_chars)",
          merged["content"]["max_chars"] == ph.DEFAULT_CONFIG["content"]["max_chars"])
    check("未覆盖的顶层键保留(window)", "min_gap_minutes" in merged["window"])
    check("覆盖不污染默认值字典",
          ph.DEFAULT_CONFIG["ntfy"]["topic"] == "", ph.DEFAULT_CONFIG["ntfy"]["topic"])

    # topic 掩码(不该把完整 topic 打进日志/终端)
    check("topic 掩码不留全文", ph._mask("abcdefghijklmn") == "abc…lmn(共14位)",
          ph._mask("abcdefghijklmn"))
    check("空 topic 显示未设置", ph._mask("") == "(未设置)", ph._mask(""))
    check("短 topic 原样显示", ph._mask("abc") == "abc", ph._mask("abc"))

    # 发不出去时不许静默成功
    cfg0 = copy.deepcopy(ph.DEFAULT_CONFIG)
    ok, msg = ph.send(cfg0, "t", "b")
    check("未配 topic 时 send 返回失败并说明", (not ok) and ("topic" in msg), msg)
    cfg1 = copy.deepcopy(ph.DEFAULT_CONFIG)
    cfg1["channel"] = "telegram"
    ok1, msg1 = ph.send(cfg1, "t", "b")
    check("未知 channel 明确失败", (not ok1) and ("不支持" in msg1), msg1)


# ---------------------------------------------------------------- 9. 镜像校验
def t_mirror_validation():
    print("\n[9] POST /api/sim 的入参校验(这是唯一从局域网写盘的接口)")
    import app as appmod
    good = mk_pf([mk_pos()])
    ok, msg = appmod._validate_sim(good)
    check("合法持仓通过", ok, msg)

    bad_cases = [
        ("非对象", ["not", "a", "dict"]),
        ("缺 positions", {"init": 1}),
        ("positions 不是数组", {"positions": {}}),
        ("closed 不是数组", {"positions": [], "closed": {}}),
        ("code 非 6 位数字", {"positions": [{"code": "abc", "qty": 1}]}),
        ("code 带路径穿越", {"positions": [{"code": "../../etc/passwd", "qty": 1}]}),
        ("qty 为 0", {"positions": [{"code": "113701", "qty": 0}]}),
        ("qty 为负", {"positions": [{"code": "113701", "qty": -10}]}),
        ("qty 非数字", {"positions": [{"code": "113701", "qty": "10"}]}),
        ("qty 是 bool", {"positions": [{"code": "113701", "qty": True}]}),
        ("持仓数超上限", {"positions": [{"code": "113701", "qty": 1}] * 501}),
    ]
    for label, payload in bad_cases:
        ok, msg = appmod._validate_sim(payload)
        check(f"拒绝: {label}", not ok, f"竟然通过了: {msg}")

    check("空仓是合法的(清空操作)", appmod._validate_sim({"positions": [],
                                                    "closed": []})[0])
    check("镜像大小上限存在且合理",
          256 * 1024 <= appmod.SIM_MAX_BYTES <= 8 * 1024 * 1024, appmod.SIM_MAX_BYTES)
    check("镜像路径在 data/ 下", os.path.dirname(appmod.SIM_MIRROR) ==
          os.path.join(BASE, "data"), appmod.SIM_MIRROR)


# ---------------------------------------------------------------- 10. 行情层兜底逻辑
def t_quote_layer():
    print("\n[10] 行情层: 东财失败自动退腾讯(用桩, 不碰网络)")
    orig_em, orig_tx = ph.fetch_quotes_em, ph.fetch_quotes_tx
    codes = ["113701", "111024"]
    try:
        # 东财全挂 -> 走腾讯
        ph.fetch_quotes_em = lambda cs: {}
        ph.fetch_quotes_tx = lambda cs: {c: mk_quote(100.0, src="tx") for c in cs}
        q, src = ph.fetch_quotes(codes)
        check("东财全挂时 source=tx", src == "tx", src)
        check("腾讯兜底覆盖全部代码", set(q) == set(codes), list(q))

        # 东财全给 -> 不叫腾讯
        called = {"tx": False}
        ph.fetch_quotes_em = lambda cs: {c: mk_quote(100.0) for c in cs}

        def _tx_spy(cs):
            called["tx"] = True
            return {}
        ph.fetch_quotes_tx = _tx_spy
        q, src = ph.fetch_quotes(codes)
        check("东财齐全时 source=em", src == "em", src)
        check("东财齐全时不打腾讯(省一次请求)", not called["tx"])

        # 东财只给一半 -> 剩下用腾讯
        ph.fetch_quotes_em = lambda cs: {cs[0]: mk_quote(100.0)}
        ph.fetch_quotes_tx = lambda cs: {c: mk_quote(100.0, src="tx") for c in cs}
        q, src = ph.fetch_quotes(codes)
        check("部分缺失时 source=em+tx", src == "em+tx", src)
        check("混合结果两只都在", set(q) == set(codes), list(q))
        check("东财那条标 em", q[codes[0]]["_src"] == "em", q[codes[0]]["_src"])
        check("兜底那条标 tx", q[codes[1]]["_src"] == "tx", q[codes[1]]["_src"])

        # 两边都挂 -> 明确返回 none, 让 main 跳过而不是推错数
        ph.fetch_quotes_em = lambda cs: {}
        ph.fetch_quotes_tx = lambda cs: {}
        q, src = ph.fetch_quotes(codes)
        check("两边都挂时返回 none", src == "none" and q == {}, (src, q))

        # 去重代码 + 空输入
        ph.fetch_quotes_em = lambda cs: {c: mk_quote(100.0) for c in cs}
        q, _ = ph.fetch_quotes(["113701", "113701", ""])
        check("重复代码只请求一次", list(q) == ["113701"], list(q))
    finally:
        ph.fetch_quotes_em, ph.fetch_quotes_tx = orig_em, orig_tx


# ---------------------------------------------------------------- 11. 腾讯解析
def t_tx_parse():
    print("\n[11] 腾讯行情解析(用真实报文片段)")
    REAL = ('v_sz127080="51~声迅转债~127080~242.800~236.809~236.990~'
            '82345~41234~41234~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~'
            '20260921161457~5.991~2.53~242.800~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0";')
    SHORT = 'v_sz127080="51~声迅转债~127080~242.800~236.809";'
    JUNK = "this is not a quote line at all"

    parsed = ph.parse_tx_lines(REAL)
    check("解析出 1 只", len(parsed) == 1, list(parsed))
    q = parsed.get("127080")
    check("名称解析正确", bool(q) and q["name"] == "声迅转债", q)
    check("现价解析正确", bool(q) and close(q["price"], 242.8, 0.001), q)
    check("涨跌幅解析正确", bool(q) and close(q["chg"], 2.53, 0.001), q)
    check("时点解析正确", bool(q) and q["quote_time"].startswith("2026"), q)
    check("来源标记为 tx", bool(q) and q["_src"] == "tx", q)
    check("溢价/强赎价显式为 None(不猜)",
          bool(q) and q["premium"] is None and q["redeem_trigger"] is None, q)

    check("字段不足 33 的行被跳过(不许错位取价)",
          ph.parse_tx_lines(SHORT) == {}, ph.parse_tx_lines(SHORT))
    check("非行情行被跳过", ph.parse_tx_lines(JUNK) == {})
    check("空输入返回空(不崩)",
          ph.parse_tx_lines("") == {} and ph.parse_tx_lines(None) == {})
    check("多行混合时只留合法行",
          set(ph.parse_tx_lines("\n".join([JUNK, REAL, SHORT]))) == {"127080"})
    dash = 'v_sz127080="' + "~".join(["51", "声迅转债", "127080", "-", "-", "-"]
                                    + ["0"] * 27) + '";'
    check("'-' 价格变 None 而不是 0",
          (ph.parse_tx_lines(dash).get("127080") or {}).get("price") is None)


# ---------------------------------------------------------------- 12. live
def t_live():
    print("\n[12] --live: 真实行情接口")
    codes = ["113701", "111024", "127080"]
    em = ph.fetch_quotes_em(codes)
    print(f"    东财: 拿到 {len(em)}/{len(codes)} 只")
    tx = ph.fetch_quotes_tx(codes)
    print(f"    腾讯: 拿到 {len(tx)}/{len(codes)} 只")
    check("至少一个行情源可用", bool(em or tx), "东财与腾讯都拿不到")

    if tx:
        for c, q in tx.items():
            check(f"腾讯 {c} 有价格且为正", (q["price"] or 0) > 0, q["price"])
        if em:
            # 两个源的价/涨跌幅应一致(这是敢拿腾讯兜底的依据)
            common = [c for c in codes if c in em and c in tx]
            if common:
                c0 = common[0]
                check(f"{c0} 两个源价格一致", close(em[c0]["price"], tx[c0]["price"], 0.02),
                      f"东财={em[c0]['price']} 腾讯={tx[c0]['price']}")
                check(f"{c0} 两个源涨跌幅一致", close(em[c0]["chg"], tx[c0]["chg"], 0.05),
                      f"东财={em[c0]['chg']} 腾讯={tx[c0]['chg']}")
    q, src = ph.fetch_quotes(codes)
    check(f"fetch_quotes 端到端可用 (source={src})", src != "none" and len(q) > 0, src)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="额外打真实行情接口")
    a = ap.parse_args()

    print("=" * 62)
    print("push_holdings.py 测试 (离线)" + ("  + live" if a.live else ""))
    print("=" * 62)
    t_accounting()
    t_no_quote()
    t_flags()
    t_fallback()
    t_window()
    t_dedupe()
    t_format()
    t_config()
    t_mirror_validation()
    t_quote_layer()
    t_tx_parse()
    if a.live:
        t_live()

    print("\n" + "=" * 62)
    total = PASS + len(FAIL)
    if FAIL:
        print(f"✗ {len(FAIL)}/{total} 项失败:")
        for n in FAIL:
            print(f"    - {n}")
        return 1
    print(f"✓ 全部通过: {total} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
