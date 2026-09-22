/**
 * 模拟仓冒烟测试: 榜单里的「＋模拟仓」按钮 + 持仓张数 ±1 手 + 每次加仓固定张数
 *
 * 背景: 原来只能去模拟仓页签的「今日可买」表里点「买入」, 手动买入表单只能填一次张数。
 *   现在 ①热度榜/放量榜/蓄势榜每行行尾都有「＋模拟仓」按钮;
 *        ②点一次 = 「每次加仓」张数(simLot, 默认 1 手 = 10 张), 不再按「单笔金额」换算;
 *        ③已在模拟仓的券按钮变「✓ 已加入」, 再点只回执当前持仓、不重复买入
 *          (要加量用持仓表的 −/+; 手动买入表单是显式操作, 仍可加仓摊薄成本);
 *        ④持仓表的张数可 −/+ 按 1 手(10 张)现价加减, 减仓的部分计入成交记录。
 *
 * 覆盖:
 *   1) 榜单按钮渲染: 行尾有按钮, 未持仓「＋模拟仓」, 已持仓「✓ 已加入」, 且列钉在右边缘
 *   2) 建仓: 张数 = 每次加仓张数(10), 现金扣「成交额+手续费」, 应卖日 = 买入日+2个交易日
 *   3) 已在仓再点: 不再买入(张数/现金/均价/费用都不动), 提示里有「已加入模拟仓」和当前持仓
 *   4) 「今日可买」表同一套: 未持仓写「买入 10张」, 已持仓「✓ 已加入」+ 显示已持张数
 *   5) 手动买入留空张数 = 每次张数; 显式加仓仍可摊薄成本
 *   6) 「每次加仓」设置: 改成 20 张后点一下就是 20 张
 *   7) 张数 +: 按现价买 10 张, 权益只少了手续费(同价买入不产生盈亏)
 *   8) 张数 −: 按现价卖 10 张, 成交记录里出现这一笔, 盈亏按分摊成本算
 *   9) 减到 0 即清仓(剩不到 1 手时一次清掉); 资金不足时只提示、不改任何数字
 *  10) 买入日不在交易日历时拒绝下单(否则应卖日会算错)
 *  11) 扫描榜(放量/蓄势)里的券: 按需补日K后能估值
 *
 * 用法: node tests/sim_smoke.mjs [http://127.0.0.1:8734]   (需要 app.py 在跑)
 */
import fs from 'node:fs';
import path from 'node:path';

const BASE = process.argv[2] || 'http://127.0.0.1:8734';
const html = fs.readFileSync(path.join(process.cwd(), 'index.html'), 'utf8');
const src = html.split('<script>').pop().split('</script>')[0];

/* ---------------- 最小 DOM 桩 ---------------- */
const made = {};
function el(id) {
  if (made[id]) return made[id];
  const e = {
    id, innerHTML: '', textContent: '', style: {}, dataset: {}, value: '', className: '',
    classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
    _thead: { innerHTML: '' }, _tbody: { innerHTML: '' },
    querySelector(sel) { return sel.includes('thead') ? this._thead : this._tbody; },
    querySelectorAll: () => [],
    addEventListener() {}, appendChild() {}, onclick: null,
  };
  made[id] = e;
  return e;
}
const document = {
  getElementById: el,
  querySelector: (sel) => el('sel' + sel),
  querySelectorAll: () => [],
  createElement: () => el('tmp' + Math.random()),
  addEventListener() {},
};
const chartOptions = [];
const echarts = {
  init: () => ({ setOption: (o) => chartOptions.push(o), dispose() {}, resize() {} }),
  getInstanceByDom: () => null,
};
const window = { addEventListener() {}, dispatchEvent() {} };
const timers = [];
globalThis.localStorage = {
  _d: {}, getItem(k) { return this._d[k] ?? null; },
  setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; },
};

const script = new Function(
  'document', 'window', 'echarts', 'fetch', 'setTimeout', 'clearTimeout',
  'location', 'alert', 'console',
  src + '\n;return {loadDay, renderTable, renderSim, simState, simBuy, simQtyStep,'
      + ' simQuickBuy, simQuickBtn, simHeld, simHeldQty, simEnsureData, simQuote, simAddDays,'
      + ' simCalIdx, simBuyManual, simLot, simSaveCfg, qijinOf, simMult, SIM_LOT,'
      + ' DAY_get:()=>DAY, SIMCAL_get:()=>SIMCAL, SIMK_get:()=>SIMK};'
);

const api = { calls: [] };
const wrappedFetch = async (url) => {
  api.calls.push(url);
  // 别让测试真的去触发一次抓取/扫描(那是 app.py 后台跑子进程干的事)
  if (url.startsWith('/api/refresh')) {
    return { ok: true, status: 200, json: async () => ({ status: 'skipped', scan: 'skipped' }) };
  }
  const r = await fetch(BASE + url);
  return { ok: r.ok, status: r.status, json: () => r.json() };
};

const ctx = script(document, window, echarts, wrappedFetch,
  (fn, ms) => timers.push(fn), () => {}, { href: BASE }, () => {}, console);

let fails = 0;
const check = (name, cond, extra = '') => {
  console.log(`${cond ? '✓' : '✗'} ${name}${extra ? '  ' + extra : ''}`);
  if (!cond) fails++;
};
const near = (a, b, eps = 0.01) => Number.isFinite(a) && Number.isFinite(b) && Math.abs(a - b) < eps;
const rate = (s) => (s.feeBp || 0) / 1000;
const equity = (s) => s.cash + s.positions.reduce((a, p) => {
  const q = ctx.simQuote(p.code); return a + (q ? q.price : p.buyPrice) * p.qty;
}, 0);
process.on('unhandledRejection', e => {
  console.log(`✗ 未处理的异步异常: ${(e && e.message) || e}`);
  process.exit(1);
});

/* ---------------- 0. 服务在跑 + 拿到当日数据 ---------------- */
let dates;
try {
  const r = await fetch(BASE + '/api/dates');
  dates = await r.json();
} catch (e) {
  console.log(`✗ 连不上 ${BASE}（先运行 python3 app.py）: ${e.message || e}`);
  process.exit(1);
}
const DATE = dates[0].date;
await ctx.loadDay(DATE);
const DAY = ctx.DAY_get();
check('加载当日快照 ' + DATE, !!DAY && DAY.date === DATE && (DAY.bonds || []).length > 0,
  `${(DAY.bonds || []).length} 只`);
// 页面右上角显示版本号: 报问题时一眼看出跑的是哪一版(之前吃过"改了但标签页是旧的"的亏)
check('页面带版本号', /const PAGE_VER='\d{4}-\d{2}-\d{2}(\.\d+)?'/.test(html) &&
  html.includes('id="pageVer"') && /^版本 \d{4}-\d{2}-\d{2}/.test(el('pageVer').textContent),
  el('pageVer').textContent);

const ok = await ctx.simEnsureData();
check('模拟仓交易日历/K线加载成功', ok === true);
const CAL = ctx.SIMCAL_get();
check('交易日历覆盖到当日', !!CAL && CAL.includes(DATE), `${CAL && CAL[0]} ~ ${CAL && CAL[CAL.length - 1]}`);

/* ---------------- 1. 榜单按钮 ---------------- */
ctx.renderTable();
const rankHtml = el('sel#rankTable tbody').innerHTML;
check('热度榜每行都有「＋模拟仓」按钮',
  (rankHtml.match(/class="quickbuy/g) || []).length === DAY.bonds.length,
  `${(rankHtml.match(/class="quickbuy/g) || []).length} 个按钮 / ${DAY.bonds.length} 行`);
check('按钮走 simQuickBuy 且带 stopPropagation(不误开详情弹窗)',
  rankHtml.includes("event.stopPropagation();simQuickBuy('"));
check('热度榜表头 14 列(静态表头也加了「模拟仓」)', (() => {
  const seg = html.split('id="rankTable"')[1].split('</thead>')[0];
  return (seg.match(/<th[ >]/g) || []).length === 14;
})(), `${(html.split('id="rankTable"')[1].split('</thead>')[0].match(/<th[ >]/g) || []).length} 列`);
// 这一列在表格最右边, 表比窗口宽时会被挤出可视区(用户就是这么找不到按钮的) ->
// 必须钉在右边缘: 表头/单元格带 pinr 类, 并且 CSS 里真有 sticky right:0 的规则。
check('「模拟仓」列钉在右边缘(横滑也在视野里)', (() => {
  const css = html.split('</style>')[0];
  const seg = html.split('id="rankTable"')[1].split('</thead>')[0];
  return /th\.pinr,td\.pinr\{position:sticky;right:0/.test(css) &&
    seg.includes('<th class="pinr"') &&
    el('sel#rankTable tbody').innerHTML.includes('<td class="pinr">');
})(), html.match(/th\.pinr,td\.pinr\{[^}]*\}/) ? 'CSS 规则在' : '缺 CSS 规则');

/* ---------------- 2. 建仓(每次加 = 10 张) ---------------- */
const B = DAY.bonds.find(b => b.price > 0 && ctx.simCalIdx(DATE) >= 0);
const P = +B.price, s0 = ctx.simState();
const qty1 = ctx.simLot();
const cash0 = s0.cash;
check('SIM_LOT = 10 张(1 手)', ctx.SIM_LOT === 10);
check(`每次加仓 = 10 张(不再是"按金额算"的 ${Math.floor(10000 / P / 10) * 10} 张)`,
  qty1 === 10 && !('per' in s0), `simLot()=${qty1}, per=${s0.per}`);
const bought = await ctx.simQuickBuy(B.code, P, DATE, B.name);
check('点「＋模拟仓」建仓成功', bought === true);
check('持仓 1 行', s0.positions.length === 1);
const p1 = s0.positions[0];
check(`张数 = 每次加仓张数 = ${qty1} 张`, p1.qty === qty1, `实际 ${p1.qty}`);
check('提示语写「已加入模拟仓」', /已加入模拟仓/.test(el('toast').textContent),
  el('toast').textContent);
check('买入价 = 现价', near(p1.buyPrice, P));
check('现金扣掉「成交额 + 手续费」',
  near(cash0 - s0.cash, P * qty1 * (1 + rate(s0))), `扣了 ${(cash0 - s0.cash).toFixed(4)}`);
check('买入费 = 成交额 × 费率', near(p1.fee, P * qty1 * rate(s0), 0.005), `fee=${p1.fee}`);
const dueExp = ctx.simAddDays(DATE, 2) || '';
check(`应卖日 = 买入日 + 2 个交易日${dueExp ? ` (${dueExp})` : '（当日就是日历最后一天 → 还没到 T+3, 先留空）'}`,
  p1.dueDate === dueExp, `实际 "${p1.dueDate}"`);
check('状态已写进 localStorage', !!localStorage.getItem('cbf_sim_v1'));

/* 已持仓 -> 按钮变「✓ 已加入」, 再点只提示(不重复买入) */
ctx.renderTable();
const rankHtml2 = el('sel#rankTable tbody').innerHTML;
const heldBtn = (rankHtml2.match(/<button[^>]*class="quickbuy held"[^>]*>[^<]*<\/button>/g) || []);
check('已持仓的券按钮变「✓ 已加入」', heldBtn.length === 1 && /已加入/.test(heldBtn[0]),
  heldBtn.length ? heldBtn[0].replace(/title="[^"]*"/, '') : '(没找到)');
check('未持仓的券仍是「＋模拟仓」',
  (rankHtml2.match(/class="quickbuy"/g) || []).length === DAY.bonds.length - 1);

/* ---------------- 3. 已在模拟仓 -> 只提示, 不再加仓 ---------------- */
const qHeld = p1.qty, cashHeld = s0.cash, feeHeld = p1.fee, avgHeld = p1.buyPrice;
el('toast').textContent = '';
const again = await ctx.simQuickBuy(B.code, +(P * 0.9).toFixed(3), DATE, B.name);
check('再点不再买入(返回 false)', again === false);
check('张数/现金/均价/费用都没变',
  p1.qty === qHeld && near(s0.cash, cashHeld, 1e-9) && near(p1.fee, feeHeld, 1e-9) &&
  p1.buyPrice === avgHeld, `张数 ${qHeld} -> ${p1.qty}`);
check('提示写「已加入模拟仓」并给出当前持仓', /已加入模拟仓/.test(el('toast').textContent) &&
  el('toast').textContent.includes(`${qHeld}张`), el('toast').textContent);
check('仍然只有 1 行', s0.positions.length === 1);

/* ---------------- 4. 「今日可买」表也用同一套: 已持仓显示「✓ 已加入」 ---------------- */
// 这张表只列当日「量价齐升」的券, 所以要挑一只真有信号的券来验证(澳弘转债那种没信号的进不了表)
const sigBond = (DAY.bonds || []).find(b => b.price > 0 && !ctx.simHeldQty(b.code) &&
  ctx.qijinOf(b, ctx.simMult()).ok);
ctx.renderSim();
let sigHtml = el('sel#simSignal').innerHTML;
if (sigBond) {
  check(`今日可买: 未持仓的行按钮写明张数(买入 ${ctx.simLot()}张)`,
    new RegExp(`买入 ${ctx.simLot()}张`).test(sigHtml));
  await ctx.simQuickBuy(sigBond.code, +sigBond.price, DATE, sigBond.name);
  ctx.renderSim();
  sigHtml = el('sel#simSignal').innerHTML;
  const rowHeld2 = sigHtml.split('<tr').find(r => r.includes(`simQuickBuy('${sigBond.code}'`)) || '';
  check('今日可买: 已持仓的行按钮变「✓ 已加入」', /✓ 已加入/.test(rowHeld2),
    (rowHeld2.match(/>[^<>]*已加入[^<>]*</) || [''])[0] || '(没找到那一行)');
  check('今日可买: 已持仓的行改显示当前持仓、不算"本次成本"', /已持 \d+张/.test(rowHeld2));
  // 收尾: 把这笔清掉, 后面的用例只围绕 B 这一笔
  const pSig = s0.positions.find(p => p.code === sigBond.code);
  ctx.simQtyStep(pSig.id, -1);
  check('清掉这笔后不残留', !s0.positions.some(p => p.code === sigBond.code));
} else {
  check('今日没有量价齐升信号(表格为空, 跳过按钮张数断言)',
    sigHtml.includes('今日没有量价齐升信号'), '');
}
check('今日可买: 表头改成「本次张数/本次成本」',
  sigHtml.includes('本次张数') && sigHtml.includes('本次成本'));

/* ---------------- 5. 手动买入表单: 已在仓也拒绝(与榜单按钮同一条规矩) ---------------- */
const qManual = p1.qty, cashManual = s0.cash, feeManual = p1.fee;
el('simCode').value = B.code; el('simPrice').value = String(+(P * 0.9).toFixed(3));
el('simQty').value = '30'; el('simDate').value = DATE;
el('toast').textContent = '';
ctx.simBuyManual();
check('手动买入遇到已持仓的券: 不买、不动账',
  p1.qty === qManual && near(s0.cash, cashManual, 1e-9) && near(p1.fee, feeManual, 1e-9),
  `张数 ${qManual} -> ${p1.qty}`);
check('手动买入被拦时也提示「已加入模拟仓」', /已加入模拟仓/.test(el('toast').textContent),
  el('toast').textContent);

/* 底层 simBuy 仍保留"合并加仓"分支(± 和未来调用方要用), 单独验一次账目 */
const P2 = +(P * 0.9).toFixed(3), qty2 = 10;
const feeBefore = p1.fee, cashBefore2 = s0.cash, dateBefore = p1.buyDate, dueBefore = p1.dueDate;
const okAdd = ctx.simBuy(B.code, P2, DATE, qty2, B.name);
check('底层 simBuy 合并加仓(仍只有一行)', okAdd === true && s0.positions.length === 1);
check('张数累加', p1.qty === qManual + qty2, `${qManual}+${qty2} = ${p1.qty}`);
const avgExp = +(((avgHeld * qManual + P2 * qty2) / (qManual + qty2)).toFixed(3));
check('均价加权摊薄', near(p1.buyPrice, avgExp, 0.002), `${p1.buyPrice} vs ${avgExp}`);
check('买入费累加', near(p1.fee, feeBefore + P2 * qty2 * rate(s0), 0.01));
check('首次买入日/应卖日不变', p1.buyDate === dateBefore && p1.dueDate === dueBefore);
check('现金再扣一笔', near(cashBefore2 - s0.cash, P2 * qty2 * (1 + rate(s0))));

/* ---------------- 6. 「每次加仓」设置: 改了张数, 点一下按新张数买 ---------------- */
el('simLot').value = '20';
ctx.simSaveCfg();
check('设置保存后 simLot() = 20', ctx.simLot() === 20, `simLot()=${ctx.simLot()}`);
const C = DAY.bonds.find(b => b.price > 0 && !ctx.simHeldQty(b.code));
const okLot = await ctx.simQuickBuy(C.code, +C.price, DATE, C.name);
const pC = s0.positions.find(p => p.code === C.code);
check('点一下买 20 张(按设置, 不是写死 10)', okLot === true && !!pC && pC.qty === 20,
  pC ? `${pC.qty} 张` : '(没买进)');
el('simLot').value = '10'; ctx.simSaveCfg();     // 还原成默认, 后面的用例都按 10 张
check('改回 10 张后 simLot() = 10', ctx.simLot() === 10);

/* ---------------- 7. 手动买入表单: 张数留空也按「每次加仓」张数 ---------------- */
const D = DAY.bonds.find(b => b.price > 0 && !ctx.simHeldQty(b.code));
el('simCode').value = D.code; el('simPrice').value = String(D.price);
el('simQty').value = ''; el('simDate').value = DATE;
ctx.simBuyManual();
const pD = s0.positions.find(p => p.code === D.code);
check('手动买入留空张数 = 每次张数(10 张)', !!pD && pD.qty === ctx.simLot(),
  pD ? `${pD.name} ${pD.qty}张` : '(没买进)');
ctx.simQtyStep(pD.id, -1);                       // 收尾清掉
check('手动买入那笔也能按 10 张清掉', !s0.positions.some(p => p.code === D.code));

/* ---------------- 8. 持仓张数 ±1 手 ---------------- */
const lot = ctx.SIM_LOT;
const qBefore = p1.qty, cashBefore = s0.cash, avgBefore = p1.buyPrice;
const eqBefore = equity(s0);
ctx.simQtyStep(p1.id, 1);
check(`张数 +${lot}`, p1.qty === qBefore + lot, `${qBefore} -> ${p1.qty}`);
check('加仓按现价成交(扣现金 + 手续费)',
  near(cashBefore - s0.cash, P * lot * (1 + rate(s0))));
check('同价加仓: 权益只少了手续费', near(eqBefore - equity(s0), P * lot * rate(s0), 0.02),
  `少了 ${(eqBefore - equity(s0)).toFixed(4)}`);
const avgPlus = +(((avgBefore * qBefore + P * lot) / (qBefore + lot)).toFixed(3));
check('加仓后均价按加权重算', near(p1.buyPrice, avgPlus, 0.002), `${p1.buyPrice} vs ${avgPlus}`);
const qMid = p1.qty, cashMid = s0.cash, feeMid = p1.fee, avgMid = p1.buyPrice;
ctx.simQtyStep(p1.id, -1);
check(`张数 -${lot}`, p1.qty === qMid - lot, `${qMid} -> ${p1.qty}`);
check('减仓 = 按现价卖出(现金回款 = 成交额 - 手续费)',
  near(s0.cash - cashMid, P * lot * (1 - rate(s0))));
check('减仓不改变剩余持仓的均价', p1.buyPrice === avgMid, `${avgMid} -> ${p1.buyPrice}`);
const closed = s0.closed[0];
check('减仓的那部分进了成交记录', !!closed && closed.qty === lot && closed.code === B.code,
  closed ? `${closed.name} ${closed.qty}张 @${closed.sellPrice}` : '(没有记录)');
const buyFeePart = +(feeMid * (lot / qMid)).toFixed(2);
const costPart = avgMid * lot + buyFeePart;
check('这笔的盈亏 = 卖出净额 - 分摊成本',
  closed && near(closed.pnl, P * lot * (1 - rate(s0)) - costPart, 0.03),
  closed ? `pnl=${closed.pnl} 期望≈${(P * lot * (1 - rate(s0)) - costPart).toFixed(2)}` : '');
check('剩余持仓的买入费也按比例减掉了',
  near(p1.fee, feeMid - buyFeePart, 0.011), `fee=${p1.fee} 期望≈${(feeMid - buyFeePart).toFixed(2)}`);

/* ---------------- 6. 剩不到 1 手 -> 一次清仓; 资金不足 -> 只提示 ---------------- */
const id = p1.id;
for (let i = 0; i < 40 && p1.qty > lot; i++) ctx.simQtyStep(id, -1);
check('连续减仓后剩 1 手', p1.qty === lot, `${p1.qty} 张`);
const closedN = s0.closed.length, cashLast = s0.cash;
ctx.simQtyStep(id, -1);
check('最后 1 手减掉 = 清仓(这只券的持仓行消失)', !s0.positions.some(p => p.id === id));
check('清仓也留成交记录', s0.closed.length === closedN + 1,
  `${s0.closed.length - closedN} 笔`);
check('清仓后现金 = 之前 + 卖出净额', near(s0.cash - cashLast, P * lot * (1 - rate(s0))));

// 重新建一笔, 把现金压到买不起 1 手, 再点头部的 ＋
await ctx.simQuickBuy(B.code, P, DATE, B.name);
const p2 = s0.positions[0];
const qNow = p2.qty;
s0.cash = 1;
el('toast').textContent = '';
ctx.simQtyStep(p2.id, 1);
check('资金不足时加仓被拒绝(张数不变)', p2.qty === qNow, `${qNow} -> ${p2.qty}`);
check('资金不足有提示(浮层看得见)', /不足/.test(el('toast').textContent), el('toast').textContent);
check('被拒绝时现金没被扣', s0.cash === 1);
s0.cash = 100000;

/* ---------------- 7. 买入日必须在交易日历里 ---------------- */
const nPos = s0.positions.length;
const bad = ctx.simBuy('123456', P, '1999-01-01', 10, '乱填的券');
check('非交易日买入被拒绝', bad === false && s0.positions.length === nPos,
  el('toast').textContent);

/* ---------------- 8. 持仓表渲染出 ± 按钮 ---------------- */
ctx.renderSim();
const posHtml = el('sel#simPos').innerHTML;
check('持仓表头写了「张数(±10)」', posHtml.includes('张数(±10)'));
check('张数格渲染出 −/+ 两个步进按钮', (() => {
  const row = posHtml.split('<tr').find(r => r.includes(`simQtyStep(${p2.id},-1)`)) || '';
  return (row.match(/class="step"/g) || []).length === 2 && row.includes(`simQtyStep(${p2.id},1)`);
})());
check('步进按钮也 stopPropagation(不误开该券详情)', posHtml.includes('event.stopPropagation();simQtyStep('));

/* ---------------- 9. 在历史交易日建仓: 应卖日算得出来 ---------------- */
const earlier = CAL[CAL.length - 6];
const okHist = ctx.simBuy('123999', 100, earlier, 10, '历史日建仓');
const pHist = s0.positions.find(p => p.code === '123999');
check(`历史交易日 ${earlier} 建仓 -> 应卖日 = 买入日 + 2 (${ctx.simAddDays(earlier, 2)})`,
  okHist === true && !!pHist && pHist.dueDate === ctx.simAddDays(earlier, 2),
  pHist ? `${pHist.buyDate} -> ${pHist.dueDate}` : '(没建成)');

/* ---------------- 10. 扫描榜(放量/蓄势)里的券: 按需补日K后能估值 ---------------- */
// 放量榜/蓄势榜是全市场扫描(310 只), 而模拟仓的K线只来自每日 Top30 快照 ——
// 这类券建仓后必须能从 /api/volume/kline/<code> 补到日K, 否则市值永远停在买入价。
const vol = await (await fetch(BASE + '/api/volume')).json();
const SIMK = ctx.SIMK_get();
const scan = (vol.bonds || []).find(b => b.price > 0 && !SIMK[b.code] && b.ratio20 >= 2 && b.chg > -3);
check('放量榜里找到一只「不在快照K线里」的券', !!scan, scan ? `${scan.name} ${scan.code}` : '(没有)');
if (scan) {
  const okScan = await ctx.simQuickBuy(scan.code, scan.price, vol.date, scan.name);
  const pScan = s0.positions.find(p => p.code === scan.code);
  check('扫描榜的券也能建仓', okScan === true && !!pScan,
    pScan ? `${pScan.name} ${pScan.qty}张 @${pScan.buyPrice} 应卖日${pScan.dueDate}` : '(没建成)');
  check('建仓后补到了它的日K(不用手点)',
    await (async () => {
      for (let i = 0; i < 30 && !SIMK[scan.code]; i++) await new Promise(r => setTimeout(r, 100));
      return !!SIMK[scan.code];
    })(), SIMK[scan.code] ? `${SIMK[scan.code].length} 根` : '(没补到)');
  ctx.renderSim();
  const scanQuote = ctx.simQuote(scan.code);
  check('估值改用它的日K收盘价', !!scanQuote && near(scanQuote.price, scan.price, 0.02) &&
    scanQuote.date === SIMK[scan.code][SIMK[scan.code].length - 1][0],
    scanQuote ? `${scanQuote.price} @ ${scanQuote.date}` : '(还是没行情)');
  check('该券的持仓行显示了行情日期(不再「无行情」)', (() => {
    const row = el('sel#simPos').innerHTML.split('<tr')
      .find(r => r.includes(`simOpen('${scan.code}')`)) || '';
    return row.includes(SIMK[scan.code][SIMK[scan.code].length - 1][0]) && !row.includes('无行情');
  })(), `日K末行 ${SIMK[scan.code][SIMK[scan.code].length - 1][0]}`);
}

console.log(fails ? `\n✗ ${fails} 项未通过` : '\n✓ 全部通过');
process.exit(fails ? 1 : 0);
