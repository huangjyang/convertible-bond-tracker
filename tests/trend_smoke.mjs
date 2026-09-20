/**
 * 量能基准线趋势标签(斜率) —— 冒烟测试
 *
 * 背景: 详情页K线标题里加一个「量能基准 ↑x.xx%/日」, 让人一眼看出量能基准在抬升还是衰减。
 *   实现: baseSlope = 基准线最近 TREND_WIN(10) 日的相对斜率(%/交易日); baseTrend = 滞回判定的方向。
 *   标定: slope_probe.py 在 310只 × 120交易日 上跑出来的窗口/阈值, 见 index.html 里 baseSlope 上方注释。
 *
 * 覆盖:
 *   1) baseSlope 数值正确(线性序列有解析解) / 数据不足与含 null 时返回 null
 *   2) 滞回: 斜率已转负但没越过对侧阈值时**不翻方向**; 越过阈值才翻
 *   3) 与 Python 口径一致: 用真实快照 data/klines_2026-09-18.json 复算, 对 hardcode 的期望值
 *   4) 集成: openBond 渲染后标题里确实出现带 ↑/↓ 和颜色的趋势标签
 *
 * 用法: node tests/trend_smoke.mjs        (纯本地, 不需要起服务)
 */
import fs from 'node:fs';
import path from 'node:path';

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
const chartOptions = [];
const echarts = {
  init: () => ({ setOption: (o) => chartOptions.push(o), dispose() {}, resize() {} }),
  getInstanceByDom: () => null,
};
globalThis.localStorage = {
  _d: {}, getItem(k) { return this._d[k] ?? null; },
  setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; },
};

const script = new Function(
  'document', 'window', 'echarts', 'fetch', 'setTimeout', 'clearTimeout',
  'location', 'alert', 'console',
  src + '\n;return {baseSlope, baseTrend, baseLine, klineAmount, openBond,'
      + ' TREND_WIN, TREND_TH, TREND_BASE, UP, DOWN};'
);
const noFetch = async () => { throw new Error('本测试不该发网络请求'); };
const ctx = script(document,
  { innerWidth: 1400, addEventListener() {}, dispatchEvent() {} },
  echarts, noFetch, (fn, ms) => setTimeout(fn, ms), clearTimeout,
  { href: 'http://127.0.0.1:0' }, () => {}, console);

let fails = 0;
const check = (name, cond, extra = '') => {
  console.log(`${cond ? '✓' : '✗'} ${name}${extra ? '  ' + extra : ''}`);
  if (!cond) fails++;
};
const near = (a, b, eps = 1e-4) => a != null && b != null && Math.abs(a - b) < eps;

/* ---------------- 1. baseSlope 数值 ---------------- */
const ramp = Array.from({ length: 10 }, (_, i) => i + 1);          // 1..10: 斜率1/均值5.5
check('线性序列斜率有解析解 (1..10 -> +18.1818%/日)',
  near(ctx.baseSlope(ramp), 1 / 5.5 * 100), `得到 ${ctx.baseSlope(ramp)}`);
check('只取最近 n 个点 (前面塞噪声不影响)', (() => {
  const arr = [999, -500, 0].concat(ramp);
  return near(ctx.baseSlope(arr), 1 / 5.5 * 100);
})());
check('下降序列为负', near(ctx.baseSlope(ramp.slice().reverse()), -1 / 5.5 * 100));
check('平坦序列 = 0', ctx.baseSlope(Array(10).fill(3)) === 0);
check('点数不足 -> null', ctx.baseSlope([1, 2, 3]) === null);
check('含 null -> null', ctx.baseSlope(ramp.map((v, i) => (i === 4 ? null : v))) === null);
check('基准为 0 -> null', ctx.baseSlope(Array(10).fill(0)) === null);
check('窗口/阈值常量 = 标定值', ctx.TREND_WIN === 10 && ctx.TREND_TH === 0.5 && ctx.TREND_BASE === 20,
  `win=${ctx.TREND_WIN} th=${ctx.TREND_TH} base=${ctx.TREND_BASE}`);

/* ---------------- 2. 滞回: 别在小幅波动上来回翻 ---------------- */
// 前 29 天稳稳上行(把状态定成"抬升"), 随后 10 天小幅阴跌: 斜率转负但只有 -0.23%/日, 没越过 -0.5%
const rise = Array.from({ length: 29 }, (_, i) => 100 + i);
const mild = rise.concat(Array.from({ length: 10 }, (_, i) => 130 - 0.3 * i));
const hard = rise.concat(Array.from({ length: 10 }, (_, i) => 130 - 6 * i));
const sMild = ctx.baseSlope(mild), sHard = ctx.baseSlope(hard);
check('小幅阴跌时斜率确实已转负', sMild < 0 && Math.abs(sMild) < 0.5, `斜率 ${sMild.toFixed(3)}%/日`);
check('滞回: 斜率转负但未越阈值 -> 仍报「抬升」(不抖)', ctx.baseTrend(mild) === 1,
  `方向 ${ctx.baseTrend(mild)}`);
check('滞回: 斜率越过 -0.5% -> 翻成「衰减」', ctx.baseTrend(hard) === -1,
  `斜率 ${sHard.toFixed(2)}%/日, 方向 ${ctx.baseTrend(hard)}`);
check('数据不足(基准线只有10天) -> null', ctx.baseTrend(Array(10).fill(5)) === null);

/* ---------------- 3. 与 Python 口径一致 (真实快照) ---------------- */
const snap = JSON.parse(fs.readFileSync(
  path.join(process.cwd(), 'data', 'klines_2026-09-18.json'), 'utf8'));
// 期望值由 slope_probe.py 的同口径复算得出(生产规则: 从基准满20日样本处起步)
const EXPECT = {
  123261: { slope: -14.177242, dir: -1 },
  123160: { slope: 16.680259, dir: 1 },
  113634: { slope: -0.836064, dir: -1 },
};
for (const [code, exp] of Object.entries(EXPECT)) {
  const rows = snap.klines[code];
  if (!rows) { check(`快照里有 ${code}`, false); continue; }
  const { amt } = ctx.klineAmount(rows, 10);
  const bl = ctx.baseLine(amt, 20);
  const sl = ctx.baseSlope(bl), dir = ctx.baseTrend(bl);
  check(`${code} 斜率与 Python 一致`, near(sl, exp.slope, 1e-3),
    `js ${sl?.toFixed(4)} vs py ${exp.slope}`);
  check(`${code} 方向与 Python 一致`, dir === exp.dir, `dir=${dir}`);
}

/* ---------------- 4. 集成: 标题里出现趋势标签 ---------------- */
const mkBond = (code) => ({
  code, name: '测试券' + code, market: 'SH', chg: 1.2, price: 120,
  stock_name: '测试正股', stock_code: '600000', stock_chg: 0.55,      // pct() 对 undefined 会炸, 桩数据得给全
  rank: 7, status: 'new', days_on_list: 3, rating: 'AA', scale: 12.5,
  listing_date: '2020-01-01', turnover_yi: 1.2, kline_90d: snap.klines[code],
});
ctx.openBond('123160', { bond: mkBond('123160'), navList: ['123160'] });
const upTitle = el('mKlineTitle').innerHTML;
check('标题含「量能基准 ↑16.68%/日」且用涨色',
  /量能基准 <b style="color:#d92b2b">↑16\.68%\/日<\/b>/.test(upTitle), upTitle.slice(0, 160));

ctx.openBond('123261', { bond: mkBond('123261'), navList: ['123261'] });
const downTitle = el('mKlineTitle').innerHTML;
check('标题含「量能基准 ↓14.18%/日」且用跌色',
  /量能基准 <b style="color:#0a8f4e">↓14\.18%\/日<\/b>/.test(downTitle), downTitle.slice(0, 160));

check('标题仍然保留原有的「今日 x.xx x 倍数」',
  /今日 \d+\.\d+x/.test(upTitle) || /今日 \d+\.\d+x/.test(downTitle));

/* ---------------- 5. K线图本身没被改坏 ---------------- */
const klineOpt = chartOptions.filter(o => o.series &&
  o.series.some(s => s.name === '20日均额(亿)')).pop();
check('K线图三件套(蜡烛/成交额柱/基准虚线)都在',
  !!klineOpt && klineOpt.series.map(s => s.name).join(',') === '收盘价,成交额(亿),20日均额(亿)',
  klineOpt ? klineOpt.series.map(s => s.name).join(',') : '(没有图)');

console.log(fails ? `\n✗ ${fails} 项未通过` : '\n✓ 全部通过');
process.exit(fails ? 1 : 0);
