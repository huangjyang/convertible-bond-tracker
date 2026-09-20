/**
 * 「量价齐升 · 信号明细」待定行冒烟测试
 *
 * 背景: 原口径要求 i+3<kl.length, 导致表里最新的信号日固定是 3 个交易日之前,
 * 今天/昨天的信号永远不出现。现在放宽到「只要次日开盘价存在」就能进表:
 *   - 已完成  : T+1/T+3/T+5 齐全, 计入均值/胜率/对照基准
 *   - 待定    : 全市场都还没走完 3 个交易日(T+3 显示"-"), 只有 T+1 确定, 不进统计
 *   - 数据不足: 该券K线没跟到信号日之后(那天不在快照 Top30), 补不出 T+3 -> 不进表, 只在 chip 里计数
 * 同时修掉一个副作用: 每只券取「K线跟得最晚」的那份快照, 否则早期快照刚好停在信号日会被误判。
 *
 * 用法: node tests/qijin_pending_smoke.mjs [http://127.0.0.1:8734]
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
    id, innerHTML: '', textContent: '', style: {}, dataset: {}, value: '',
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
const echarts = {
  init: () => ({ setOption() {}, dispose() {}, resize() {} }),
  getInstanceByDom: () => null,
};
const window = { addEventListener() {}, dispatchEvent() {} };
globalThis.localStorage = {
  _d: {}, getItem(k) { return this._d[k] ?? null; },
  setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; },
};

const script = new Function(
  'document', 'window', 'echarts', 'fetch', 'setTimeout', 'clearTimeout',
  'location', 'alert', 'console',
  src + '\n;return {scanQijin, mergeKlineDays, loadQijinHistory, renderQijinBacktest,'
      + ' renderQijinTable, simEnsureData, vqMult, vqNHOnly,'
      + ' SIMK_get:()=>SIMK, SIMCAL_get:()=>SIMCAL, VQ_get:()=>VQ};'
);

const api = { calls: [] };
const wrappedFetch = async (url) => {
  api.calls.push(url);
  const r = await fetch(BASE + url);
  return { ok: r.ok, status: r.status, json: () => r.json() };
};

const ctx = script(document, window, echarts, wrappedFetch,
  (fn, ms) => setTimeout(fn, ms), clearTimeout, { href: BASE }, () => {}, console);

/* ---------------- 断言 ---------------- */
let fails = 0;
const check = (name, cond, extra = '') => {
  console.log(`${cond ? '✓' : '✗'} ${name}${extra ? '  ' + extra : ''}`);
  if (!cond) fails++;
};

process.stdout.on('error', e => { if (e.code === 'EPIPE') process.exit(0); });  // 管道被提前关闭时别炸出 dump
process.on('unhandledRejection', e => {
  console.log(`✗ 未处理的异步异常: ${(e && e.message) || e}`);
  fails++; process.exit(1);
});

const days = await ctx.loadQijinHistory();
check('拉到全部快照的K线', days.length > 0, `${days.length} 个快照`);

// 交易日历(所有快照K线里出现过的日期, 升序)
const allDates = [...new Set(days.flatMap(d => d.bonds.flatMap(b => b.k.map(r => r[0]))))].sort();
const lastDate = allDates[allDates.length - 1];       // 今天
const prevDate = allDates[allDates.length - 2];       // 昨天: 表里最新的信号日
console.log(`  交易日历最新三天: ${allDates[allDates.length - 3]} / ${prevDate} / ${lastDate}`);

// 结构: 按券+日期合并成每只券的完整历史(而不是只取某一份快照的滚动窗口)
const merged = ctx.mergeKlineDays(days);
check('每只券只剩一份K线', merged.bonds.length === new Set(merged.bonds.map(b => b.code)).size,
  `${merged.bonds.length} 只`);
check('合并后每只券的日期严格递增(无重复日)',
  merged.bonds.every(b => b.k.every((r, i) => i === 0 || b.k[i - 1][0] < r[0])));
check('合并后K线根数 ≥ 任一单份快照里该券的根数(历史没被滚掉)',
  merged.bonds.every(b => days.flatMap(d => d.bonds).filter(x => x.code === b.code)
    .every(x => b.k.length >= x.k.length)),
  `样例 ${merged.bonds[0].code}: ${merged.bonds[0].k.length} 根 → ${merged.bonds[0].last}`);

const R = ctx.scanQijin(days, ctx.vqMult(), ctx.vqNHOnly());
const sigDates = [...new Set(R.sigs.map(s => s.date))].sort();
const maxSig = sigDates[sigDates.length - 1];
const after = d => allDates.filter(x => x > d).length;

check('有信号', R.sigs.length > 0, `${R.sigs.length} 个`);
check('对照基准里没有 NaN(放宽后仍只放已走完的券日)',
  R.base.length > 0 && R.base.every(Number.isFinite), `样本 ${R.base.length} 个`);
check('baseR1 全部有限', R.baseR1.every(Number.isFinite), `${R.baseR1.length} 个`);

// 核心: 最新信号日 = 倒数第二个交易日(今天那根还没有次日开盘价, 买不进)
check(`最新信号日 = 倒数第二个交易日 ${prevDate}`, maxSig === prevDate, `实际 ${maxSig}`);
check('没有信号日超出倒数第二个交易日', sigDates.every(d => d <= prevDate),
  sigDates.slice(-3).join(','));
check('最新的今日那根K线不进表', !sigDates.includes(lastDate));

const done = R.sigs.filter(s => s.state === 'done');
const pend = R.sigs.filter(s => s.state === 'pend');
const nodata = R.sigs.filter(s => s.state === 'nodata');
check('三种状态都识别出来了',
  done.length > 0 && pend.length === R.pendN && nodata.length === R.nodataN,
  `已完成 ${done.length} / 待定 ${pend.length} / 数据不足 ${nodata.length}`);
// 回归锚点: 旧口径(要求 T+3 可算)在这批快照上正好 346 个可验证信号, 且都在 09-14 及之前。
// 改口径只应该"往后加"新行, 不该动已有回测样本。
check('已完成数 = 346(旧口径的样本一个不少)', done.length === 346, `${done.length} 个`);
check('已完成的最新日期是 09-14(旧口径的上限)',
  [...new Set(done.map(s => s.date))].sort().pop() === '2026-09-14',
  [...new Set(done.map(s => s.date))].sort().pop());
check('待定行 = 全市场日历上信号日之后不足 3 个交易日',
  pend.every(s => after(s.date) < 3), [...new Set(pend.map(s => s.date))].join(','));
check('待定行落在最新两个信号日',
  pend.every(s => s.date === prevDate || s.date === allDates[allDates.length - 3]),
  [...new Set(pend.map(s => s.date))].join(','));
check('数据不足行 = 日历够了但该券K线没跟到',
  nodata.every(s => after(s.date) >= 3 && s.lastBar < allDates[allDates.length - 1]),
  `${nodata.length} 个`);
check('待定/数据不足行的 T+3 都为空, T+1 都有值',
  [...pend, ...nodata].every(s => s.r3 == null && Number.isFinite(s.r1)));
check('待定行的窗口天数 1~2 天', pend.every(s => s.winN >= 1 && s.winN < 3),
  [...new Set(pend.map(s => s.winN))].join('/'));
check('已完成行 T+3 齐全', done.every(s => Number.isFinite(s.r3)));
check('所有行的 涨幅/量比/买入价 都是有限数',
  R.sigs.every(s => Number.isFinite(s.chg) && Number.isFinite(s.ratio) && s.entry > 0));

// 回归: 截图里 2026-09-14 的 6 行必须原样保留且可验证
const d914 = R.sigs.filter(s => s.date === '2026-09-14');
const want914 = ['123268', '118044', '111012', '123091', '123245', '111024'];
const show914 = d914.filter(s => s.state !== 'nodata');
check('2026-09-14 进表的仍是 6 行(与截图一致)', show914.length === 6, `${show914.length} 行`);
check('2026-09-14 的 6 只券未变',
  want914.every(c => show914.some(s => s.code === c)), show914.map(s => s.name).join('、'));
check('2026-09-14 的 6 行全部已完成(T+3 可算)',
  show914.every(s => s.state === 'done' && Number.isFinite(s.r3)));
check('2026-09-14 另有 3 只券因K线没跟到被标"数据不足"',
  d914.filter(s => s.state === 'nodata').length === 3,
  d914.filter(s => s.state === 'nodata').map(s => `${s.name}(${s.lastBar})`).join('、'));
const bc = d914.find(s => s.code === '123268');
check('本川转债 123268 的量比仍为 1.86x',
  bc && Math.abs(bc.ratio - 1.86) < 0.005, `实际 ${bc?.ratio?.toFixed(2)}x`);

/* ---------------- 渲染层 ---------------- */
ctx.renderQijinBacktest(true);
await new Promise(r => setTimeout(r, 1500));

const tbody = el('sel#vqTable tbody').innerHTML;
const rows = tbody.split('<tr').slice(1);
const shown = R.sigs.length - nodata.length;
check('表格行数 = 可验证信号数(数据不足的不进表)', rows.length === shown,
  `${rows.length} 行 vs ${shown}`);
check('没有"数据不足"的行混进表', !tbody.includes('数据不足'));
check('每行 13 列(新增「状态」列)',
  rows.every(r => (r.match(/<td/g) || []).length === 13),
  `列数样例 ${(rows[0]?.match(/<td/g) || []).length}`);
check('渲染出「待定」标记', (tbody.match(/待定/g) || []).length === pend.length,
  `${(tbody.match(/待定/g) || []).length} 个 vs 待定 ${pend.length} 个`);
check('渲染出「已完成」标记', (tbody.match(/已完成/g) || []).length === done.length,
  `${(tbody.match(/已完成/g) || []).length} 个 vs 已完成 ${done.length} 个`);
check('表里出现最新信号日 ' + prevDate, tbody.includes(prevDate));
// 表头/表体列数必须一致(否则列会错位)
const thead = html.split('id="vqTable"')[1].split('</thead>')[0];
const nth = (thead.match(/<th[ >]/g) || []).length;   // <th 或 <th> (跳过 <thead>)
check('表头 13 列', nth === 13, `${nth} 列`);
check('待定行的 T+3 显示为 "-"',
  (tbody.match(/class="flat">-<\/td>/g) || []).length >= pend.length,
  `${(tbody.match(/class="flat">-<\/td>/g) || []).length} 个 "-"`);
check('待定行的窗口值带"窗口还没走完"提示', tbody.includes('T+3 持有窗口还没走完'));

const kpis = el('vqKpis').innerHTML;
check('KPI「信号样本」标出未完成数', kpis.includes(`${R.pendN + R.nodataN} 未完成`),
  `未完成 ${R.pendN + R.nodataN}`);
check('KPI「覆盖」不再写死 9 个快照池',
  kpis.includes(`${days.length} 个快照池`) && !kpis.includes('9 个快照池'));
const base = el('vqBase').innerHTML;
check('chip 报出待定数', base.includes(`${pend.length}</b> 个刚出现`), `待定 ${pend.length}`);
check('chip 报出数据不足数', base.includes(`${nodata.length}</b> 个该券K线没跟到`),
  `数据不足 ${nodata.length}`);
check('chip 说明这些行不计入统计', base.includes('都不计入上面的均值/胜率/对照'));
check('说明文案解释两种未完成', el('vqNote').innerHTML.includes('「待定」')
  && el('vqNote').innerHTML.includes('「数据不足」'));
check('说明文案指出最新信号日只能是倒数第二个交易日',
  el('vqNote').innerHTML.includes('倒数第二个交易日'));

/* ---------------- 模拟仓复用同一套合并口径 ---------------- */
const ok = await ctx.simEnsureData();
const simK = ctx.SIMK_get(), simCal = ctx.SIMCAL_get();
check('模拟仓能拿到K线', ok === true && Object.keys(simK).length > 0,
  `${Object.keys(simK).length} 只`);
check('模拟仓日历 = 合并后的全部交易日',
  simCal.length === new Set(merged.bonds.flatMap(b => b.k.map(r => r[0]))).size,
  `${simCal.length} 天`);
check('模拟仓同一只券的K线与「量价验证」完全一致',
  merged.bonds.every(b => simK[b.code] && simK[b.code].length === b.k.length),
  `样例 ${merged.bonds[0].code}: ${simK[merged.bonds[0].code]?.length} vs ${merged.bonds[0].k.length}`);

console.log(fails ? `\n${fails} 项失败` : '\n全部通过');
process.exit(fails ? 1 : 0);
