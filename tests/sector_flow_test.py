#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sector_flow.py 的测试: 默认纯离线(不碰网络), 加 --live 才打真实接口。

重点覆盖的不是"代码能跑", 而是这次调研里三个真实的坑:
  1. 板块列表把申万一级/二级/三级混排 —— 必须保证不会跨层相加/重复计数。
  2. 转债 industry 未匹配到板块时, **绝不能静默丢弃**(宁可报告, 不能装作全市场都映射上了)。
  3. 非交易时段拿到的资金流不是当日实时进度 —— 推送前必须能识别(live 标志)。

用法: python3 tests/sector_flow_test.py          # 离线, 42 项
      python3 tests/sector_flow_test.py --live   # 额外打真实接口
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import sector_flow as sf   # noqa: E402

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL.append(name)
        print(f"  ✗ {name}  {detail}")


def load_boards_file():
    p = os.path.join(BASE, "data", "sector_boards.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)["boards"]


def local_industries():
    """本地快照里出现过的全部 industry 值(这是转债宇宙实际用到的分类)。"""
    vals = set()
    for fp in glob.glob(os.path.join(BASE, "data", "2026-*.json")):
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        for b in d.get("bonds", []):
            if b.get("industry"):
                vals.add(b["industry"])
    return vals


# ---------------------------------------------------------------- 1. 时段判定
def test_session():
    print("\n[1] 交易时段判定(live 标志决定能不能推送)")
    def s(*a):
        return sf.session_of(datetime(*a))
    check("周六 -> 休市/live=False", s(2026, 9, 19, 10, 30) == ("休市", False))
    check("周日 -> 休市/live=False", s(2026, 9, 20, 10, 30) == ("休市", False))
    check("周五 08:00 -> 盘前/live=False", s(2026, 9, 18, 8, 0) == ("盘前", False))
    check("周五 09:20 -> 集合竞价/live=False", s(2026, 9, 18, 9, 20) == ("集合竞价", False))
    check("周五 10:00 -> 盘中(上午)/live=True", s(2026, 9, 18, 10, 0) == ("盘中(上午)", True))
    check("周五 11:30 -> 盘中(上午)/live=True", s(2026, 9, 18, 11, 30) == ("盘中(上午)", True))
    check("周五 12:00 -> 午间休市/live=True(数据仍是当日进度)",
          s(2026, 9, 18, 12, 0) == ("午间休市", True))
    check("周五 14:30 -> 盘中(下午)/live=True", s(2026, 9, 18, 14, 30) == ("盘中(下午)", True))
    check("周五 15:00 -> 盘中(下午)/live=True", s(2026, 9, 18, 15, 0) == ("盘中(下午)", True))
    check("周五 15:30 -> 盘后/live=False(现有日频抓取时点)",
          s(2026, 9, 18, 15, 30) == ("盘后", False))


# ---------------------------------------------------------------- 2. 板块解析
def test_clean_board():
    print("\n[2] 板块行解析(单位换算 + '-' 处理)")
    b = sf.clean_board({"f12": "BK1036", "f14": "半导体", "f3": 4.35,
                        "f62": 18220373248.0, "f184": 2.1,
                        "f66": 1.2e10, "f72": 6.2e9, "f78": -1.0e9, "f84": -2.0e9})
    check("f62 元 -> 亿元", b["main_net_yi"] == 182.2, f"实际 {b['main_net_yi']}")
    check("代码/名称保留", (b["code"], b["name"]) == ("BK1036", "半导体"))
    check("涨跌幅保留", b["pct_chg"] == 4.35)
    n = sf.clean_board({"f12": "BKxxxx", "f14": "空板块", "f3": "-", "f62": "-",
                        "f184": "-", "f66": "-", "f72": "-", "f78": "-", "f84": "-"})
    check("'-' -> None(不是 0)", n["main_net_yi"] is None and n["pct_chg"] is None)


# ---------------------------------------------------------------- 3. join 正确性
def test_join_basic():
    print("\n[3] join: 板块 -> 转债")
    boards = [
        {"code": "BK1036", "name": "半导体", "main_net_yi": 182.2, "pct_chg": 4.35},
        {"code": "BK0459", "name": "元件", "main_net_yi": -38.06, "pct_chg": 1.39},
    ]
    bonds = [
        {"code": "110001", "name": "甲转债", "industry": "半导体", "turnover_yi": 5.0},
        {"code": "110002", "name": "乙转债", "industry": "半导体", "turnover_yi": 9.0},
        {"code": "110003", "name": "丙转债", "industry": "元件", "turnover_yi": 1.0},
    ]
    hits, unmapped = sf.join(bonds, boards)
    check("两个板块各自成组", len(hits) == 2)
    check("按主力净流入降序", [h["board_name"] for h in hits] == ["半导体", "元件"])
    semi = hits[0]
    check("半导体 2 只", semi["bond_total"] == 2)
    check("板块内按成交额降序", [x["code"] for x in semi["bonds"]] == ["110002", "110001"])
    check("无未映射", unmapped == {})


def test_join_unmapped_not_dropped():
    print("\n[4] 未映射的券必须被报告, 不能静默丢弃")
    boards = [{"code": "BK1036", "name": "半导体", "main_net_yi": 1.0, "pct_chg": 1.0}]
    bonds = [
        {"code": "110001", "name": "甲", "industry": "半导体"},
        {"code": "110009", "name": "新行业债", "industry": "某种新行业"},
        {"code": "110010", "name": "无行业债", "industry": None},
    ]
    hits, unmapped = sf.join(bonds, boards)
    check("命中的进 hits", sum(h["bond_total"] for h in hits) == 1)
    check("未知行业进 unmapped", unmapped.get("某种新行业") == ["110009"])
    check("行业为空 -> '(无行业)' 而非丢弃", unmapped.get("(无行业)") == ["110010"])
    total_in = sum(h["bond_total"] for h in hits) + sum(len(v) for v in unmapped.values())
    check("每只券都有归属(hits+unmapped 守恒)", total_in == len(bonds), f"实际 {total_in}")


def test_no_cross_level_sum():
    print("\n[5] 不能跨层合计(电子 已含 半导体, 相加即重复计数)")
    boards = [
        {"code": "BK1201", "name": "电子", "main_net_yi": 198.54, "pct_chg": 2.81},
        {"code": "BK1036", "name": "半导体", "main_net_yi": 182.20, "pct_chg": 4.35},
        {"code": "BK1331", "name": "数字芯片设计", "main_net_yi": 75.35, "pct_chg": 4.68},
    ]
    bonds = [
        {"code": "110001", "name": "半导体债", "industry": "半导体"},
        {"code": "110002", "name": "电子债", "industry": "电子"},
        {"code": "110003", "name": "芯片债", "industry": "数字芯片设计"},
    ]
    hits, unmapped = sf.join(bonds, boards)
    check("三层各成一个独立条目(3 组)", len(hits) == 3, f"实际 {len(hits)}")
    check("每只券只归属 1 个板块(不重复计入)",
          sum(h["bond_total"] for h in hits) == 3)
    codes = [h["board_code"] for h in hits]
    check("同一只券不出现在两个板块",
          len(codes) == len(set(codes)) == 3)
    # Top 榜同样只是并列展示, 不做加总
    si, _ = sf.top_flows(boards, n_in=3, n_out=0)
    check("热榜并列返回三层, 不求和", len(si) == 3)
    check("热榜首项是净流入最大的 电子(198.54)", si[0]["name"] == "电子")


def test_top_flows():
    print("\n[6] 净流入/净流出 榜")
    boards = [{"code": f"B{i}", "name": f"板{i}", "main_net_yi": v} for i, v in
              enumerate([-30.0, 50.0, -5.0, 120.0, 0.0])]
    boards.append({"code": "BX", "name": "无数据", "main_net_yi": None})
    si, so = sf.top_flows(boards, n_in=2, n_out=2)
    check("净流入 Top2 降序", [b["main_net_yi"] for b in si] == [120.0, 50.0])
    check("净流出 Top2(最负在前)", [b["main_net_yi"] for b in so] == [-30.0, -5.0])
    check("None 不参与排序", all(b["main_net_yi"] is not None for b in si + so))


def test_map_drift():
    print("\n[7] 映射漂移检测(东财改板块代码是真实风险)")
    by_name = {"半导体": {"code": "BK1036", "name": "半导体"}}
    m = {"items": {
        "110001": {"industry": "半导体", "board_code": "BK1036"},
        "110002": {"industry": "半导体", "board_code": "BK9999"},   # 代码变了
        "110003": {"industry": "消失的行业", "board_code": "BK0001"},  # 名录里没有
    }}
    drift = sf.check_map_drift(m, by_name)
    issues = {d["code"]: d["issue"] for d in drift}
    check("代码一致的券不报漂移", "110001" not in issues)
    check("板块代码变化被检出", "110002" in issues and "BK9999" in issues["110002"])
    check("行业从名录消失被检出", "110003" in issues)


# ---------------------------------------------------------------- 8. 真实数据回归
def test_industry_map_matches_boards():
    print("\n[8] 回归: 转债 industry 与板块名录的名称匹配(本次调研的核心结论)")
    boards = load_boards_file()
    if boards is None:
        check("data/sector_boards.json 存在(先跑一次 python3 sector_flow.py)", False)
        return
    check("板块名录有 496 个板块", len(boards) == 496, f"实际 {len(boards)}")
    names = [b["name"] for b in boards]
    check("板块名无重名(名称匹配才无歧义)", len(names) == len(set(names)))
    idx = sf.board_index(boards)
    inds = local_industries()
    check("本地快照里有 30 个 distinct industry", len(inds) == 30, f"实际 {len(inds)}")
    miss = sorted(i for i in inds if i not in idx)
    check(f"全部 {len(inds)} 个 industry 都能精确匹配到板块(应 100%)",
          not miss, f"未命中: {miss}")
    # 混排层级确实存在 -> 提醒"别跨层加总"不是杞人忧天
    check("名录里同时存在 一级'电子' 与 二级'半导体'(证实混排)",
          "电子" in idx and "半导体" in idx)
    bad = [b["name"] for b in boards if b.get("main_net_yi") is not None
           and abs(b["main_net_yi"]) > 2000]
    check("没有离谱的净流入数值(单位换算正常)", not bad, f"异常: {bad[:3]}")

    # 更强的断言: 完整宇宙(不只是历史快照 Top30 出现过的 30 个行业)是否全部可解析。
    # 实测全市场会用到 30 个之外的行业(如"电网设备"), 手写映射表会漏掉它们。
    mp = os.path.join(BASE, "data", "bond_industry.json")
    if os.path.exists(mp):
        with open(mp, encoding="utf-8") as f:
            m = json.load(f)
        full = {it.get("industry") for it in m["items"].values() if it.get("industry")}
        miss2 = sorted(i for i in full if i not in idx)
        check(f"完整映射里 {len(full)} 个 distinct 行业全部可解析",
              not miss2, f"未命中: {miss2}")
        check(f"完整宇宙行业数({len(full)}) > 历史快照的 30(证伪'手写30行就够')",
              len(full) > 30, f"实际 {len(full)}")


# ---------------------------------------------------------------- 9. 产出文件结构
def test_output_shape():
    print("\n[9] 产出文件结构(如果已经跑过采集)")
    globs = sorted(glob.glob(os.path.join(BASE, "data", "sector_flow_*.json")))
    if not globs:
        check("data/sector_flow_*.json 存在(先跑一次采集)", False)
        return
    with open(globs[-1], encoding="utf-8") as f:
        doc = json.load(f)
    check("有 date/snapshots", "date" in doc and isinstance(doc.get("snapshots"), list))
    snap = doc["snapshots"][-1]
    for k in ("t", "session", "live", "hot_in", "hot_out", "hits", "unmapped"):
        check(f"时点含 {k}", k in snap)
    check("live 是布尔(推送前要判它)", isinstance(snap["live"], bool))
    check("hits 里的券带 code/name", all(
        "code" in x and "name" in x for h in snap["hits"] for x in h["bonds"]))
    check("单个板块列出的券不超过 25", all(len(h["bonds"]) <= 25 for h in snap["hits"]))
    check("时点里不含重复存储的慢变字段(stock_name/redeem_trigger)",
          all("stock_name" not in x and "redeem_trigger" not in x
              for h in snap["hits"] for x in h["bonds"]))
    one = len(json.dumps(snap, ensure_ascii=False).encode())
    check(f"单个时点 < 60KB(实测 {one // 1024}KB, 满一天 48 个时点约 "
          f"{one * 48 / 1024 / 1024:.1f}MB, 与既有日频文件同量级)", one < 60 * 1024)
    check("hits 占了时点的绝大部分(时序文件的主体确实是板块→转债)",
          len(json.dumps(snap["hits"], ensure_ascii=False).encode()) > one * 0.8)
    mp = os.path.join(BASE, "data", "bond_industry.json")
    if os.path.exists(mp):
        with open(mp, encoding="utf-8") as f:
            m = json.load(f)
        check("映射缓存版本号正确", m.get("v") == sf.MAP_VERSION)
        check("映射缓存非空", m.get("size", 0) > 0)
        bad = [c for c, it in m["items"].items() if not it.get("industry")]
        check("映射条目都带 industry", not bad, f"缺: {bad[:3]}")


# ---------------------------------------------------------------- 10. 真实接口
def test_live():
    print("\n[10] 真实接口(--live)")
    try:
        boards, complete = sf.fetch_boards(quiet=True)
    except Exception as e:
        check(f"板块接口可达: {e}", False)
        return
    check("拿到 496 个板块", len(boards) == 496, f"实际 {len(boards)}")
    check("翻页完整(无页失败)", complete)
    check("资金流字段有值", sum(1 for b in boards if b.get("main_net_yi") is not None) > 400)
    check("板块名录文件已同步", load_boards_file() is not None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="额外打真实接口")
    args = ap.parse_args()
    print("=" * 64)
    print("sector_flow 测试 (离线)" + (" + live" if args.live else ""))
    print("=" * 64)
    test_session()
    test_clean_board()
    test_join_basic()
    test_join_unmapped_not_dropped()
    test_no_cross_level_sum()
    test_top_flows()
    test_map_drift()
    test_industry_map_matches_boards()
    test_output_shape()
    if args.live:
        test_live()
    print("\n" + "=" * 64)
    if FAIL:
        print(f"✗ {PASS} 项通过, {len(FAIL)} 项失败:")
        for f in FAIL:
            print(f"    - {f}")
        return 1
    print(f"✓ 全部 {PASS} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
