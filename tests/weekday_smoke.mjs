/**
 * 周几标记 —— 冒烟测试
 *
 * 需求: 日K图上要能直接看出"周一到周五"(周一效应 / 周五缩量 / 周内节奏这类问题,
 *   光看 'YYYY-MM-DD' 是看不出来的)。
 * 做法:
 *   - 横轴标签: "09-18 周五", 周一用强调色加粗(一周的起点), 塞不下的由 ECharts 隐藏重叠
 *   - 每周一画一条浅色竖虚线, 把 90 天切成"一周一格"
 *   - 悬停 tooltip 第一行: "2026-09-18 周五"
 *   - 分时卡片标题补上"日期 周几"(分时是单日, 周末回看周五数据时最容易看错)
 *
 * 覆盖:
 *   1) weekdayCn 的日期解析(含跨年/闰年/带时间后缀), 非法串返回空
 *   2) ⚠ 时区坑: 'YYYY-MM-DD' 若按 UTC 解析, +08:00 下不会错; 但若整体挪一天,
 *      真实K线日期会算成周六/周日 —— 用真实数据断言"每个交易日都落在周一~周五"来钉死
 *   3) weekAxisLabel / weekSplitMarkLine / dateWithWeek 的产物
 *   4) 集成: openBond 渲染后, 模态里三张图(转债K线/正股K线)横轴带周几、K线柱上有周一竖线、
 *      tooltip 里有周几; 分时卡片标题带"日期 周几"; 个券历史页签用的是同一套 helper
 *
 * 用法: node tests/weekday_smoke.mjs
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
  src + '\n;return {openBond, weekdayCn, dateWithWeek, isMondayCn, weekAxisLabel,'
      + ' weekSplitMarkLine, klineAmount, WEEK_CN};'
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

/* ---------------- 1. 日期 -> 周几 ---------------- */
const CASES = {
  '2026-09-21': '周一',     // 本会话当天(周一)
  '2026-09-18': '周五',
  '2026-09-19': '周六',
  '2026-09-20': '周日',
  '2026-09-14': '周一',
  '2026-09-16': '周三',
  '2026-01-01': '周四',     // 跨年
  '2024-02-29': '周四',     // 闰日
  '2026-09-21 09:30': '周一', // 带时间后缀(分时时间戳)也能吃
};
for (const [d, want] of Object.entries(CASES)) {
  const got = ctx.weekdayCn(d);
  check(`weekdayCn('${d}') = ${want}`, got === want, got === want ? '' : `得到 ${got || '空'}`);
}
check('非法日期返回空串(不会画出错标签)',
  ctx.weekdayCn('') === '' && ctx.weekdayCn('2026-09') === '' && ctx.weekdayCn(null) === '');
check('dateWithWeek 拼成「日期 周几」', ctx.dateWithWeek('2026-09-18') === '2026-09-18 周五');
check('dateWithWeek 对非法日期原样返回', ctx.dateWithWeek('xx') === 'xx');
check('isMondayCn 只认周一',
  ctx.isMondayCn('2026-09-21') && !ctx.isMondayCn('2026-09-18'));

/* ---------------- 2. 横轴标签 & 周一竖线 ---------------- */
const lab = ctx.weekAxisLabel();
const monLab = lab.formatter('2026-09-21'), friLab = lab.formatter('2026-09-18');
check('横轴标签: MM-DD + 周几(周一加粗强调)', monLab.includes('09-21') && monLab.includes('周一') &&
  monLab.includes('{mon|'), monLab);
check('横轴标签: 非周一用普通样式', friLab.includes('09-18') && friLab.includes('{w|周五}'), friLab);
check('横轴标签保留了 rich 样式定义', !!lab.rich && !!lab.rich.mon && !!lab.rich.w);
check('横轴标签开了 hideOverlap(挤不下的自动藏)',
  lab.hideOverlap === true && lab.interval === 'auto');
const dates = ['2026-09-14', '2026-09-15', '2026-09-16', '2026-09-17', '2026-09-18',
  '2026-09-21'];
const ml = ctx.weekSplitMarkLine(dates);
check('周一竖线只落在周一那几根上',
  !!ml && ml.data.length === 2 && ml.data.map(d => d.xAxis).join(',') === '2026-09-14,2026-09-21',
  ml ? ml.data.map(d => d.xAxis).join(',') : '(没有)');
check('周一竖线是浅色虚线且不显示标签、不响应鼠标',
  ml && ml.silent === true && ml.label.show === false && ml.lineStyle.type === 'dashed');
check('没有周一时不画线(不会抛错)', ctx.weekSplitMarkLine(['2026-09-15']) === undefined);

/* ---------------- 3. ⚠ 时区: 真实K线的日期必须都落在周一~周五 ---------------- */
// 日期串按 UTC 解析会在东八区把"周一"算成"周日"之类的错位; A 股周末不开市,
// 所以"所有K线日期都是工作日"就是最硬的断言(不依赖任何写死的日期)。
const files = fs.readdirSync(path.join(process.cwd(), 'data'))
  .filter(f => /^klines_\d{4}-\d{2}-\d{2}\.json$/.test(f) || /^\d{4}-\d{2}-\d{2}\.json$/.test(f));
let dayCount = 0, badDays = [];
const seen = new Set();
for (const f of files) {
  const j = JSON.parse(fs.readFileSync(path.join(process.cwd(), 'data', f), 'utf8'));
  const rowsets = j.klines ? Object.values(j.klines) : (j.bonds || []).map(b => b.kline_90d || []);
  for (const rows of rowsets) {
    for (const r of rows) {
      const d = String(r[0]).slice(0, 10);
      if (seen.has(d)) continue;
      seen.add(d);
      dayCount++;
      const w = ctx.weekdayCn(d);
      if (!w || w === '周六' || w === '周日') badDays.push(`${d}(${w || '解析失败'})`);
    }
  }
}
check(`真实数据里 ${dayCount} 个K线日期全部落在周一~周五`, dayCount > 50 && badDays.length === 0,
  badDays.length ? `异常: ${badDays.slice(0, 5).join(' ')}` : `覆盖 ${files.length} 个数据文件`);

/* ---------------- 4. 集成: 模态里的图 ---------------- */
// 取一份真实快照, 找一只有分时+正股K线的券(分时标题要用到 minute.date)
const snapFile = files.filter(f => /^\d{4}-\d{2}-\d{2}\.json$/.test(f)).sort().pop();
const snap = JSON.parse(fs.readFileSync(path.join(process.cwd(), 'data', snapFile), 'utf8'));
const bond = (snap.bonds || []).find(b =>
  (b.kline_90d || []).length > 30 && (b.stock_kline_90d || []).length > 30 &&
  b.minute && (b.minute.points || []).length);
check(`快照 ${snap.date} 里有可用样例券`, !!bond, bond ? `${bond.name} ${bond.code}` : '(没有)');

ctx.openBond(bond.code, { bond, navList: [bond.code] });

const optOf = (name) => chartOptions.filter(o => o.series &&
  o.series.some(s => s.name === name)).pop();
const kOpt = optOf('收盘价');            // 转债90日K线
const sOpt = optOf('正股收盘');          // 正股90日K线
const mOpt = chartOptions.filter(o => o.series &&
  o.series.some(s => String(s.name).includes('(转债)'))).pop();   // 分时

check('转债K线图渲染出来了', !!kOpt);
if (kOpt) {
  const ax = kOpt.xAxis[1];
  const d0 = String(kOpt.xAxis[0].data[0]).slice(0, 10);
  check('K线横轴标签带周几', ax.axisLabel.formatter(d0).includes(ctx.weekdayCn(d0)),
    `${d0} -> ${ax.axisLabel.formatter(d0)}`);
  const cs = kOpt.series.find(s => s.type === 'candlestick');
  check('K线柱上画了周一竖线', !!cs.markLine && cs.markLine.data.length > 0,
    cs.markLine ? `${cs.markLine.data.length} 条` : '(没有)');
  check('竖线全部落在周一(不是每根都画)', !!cs.markLine &&
    cs.markLine.data.every(m => ctx.isMondayCn(m.xAxis)));
  const kd = kOpt.xAxis[0].data;
  const wantMon = kd.filter(d => ctx.isMondayCn(d)).length;
  check(`竖线条数 = 区间内的周一数(${wantMon})`,
    !!cs.markLine && cs.markLine.data.length === wantMon);
  const tip = kOpt.tooltip.formatter([{ dataIndex: kd.length - 1 }]);
  const last = String(kd[kd.length - 1]).slice(0, 10);
  check('tooltip 第一行是「日期 周几」', tip.startsWith(`${last} ${ctx.weekdayCn(last)}`),
    tip.split('<br>')[0]);
  check('K线标题注明了「淡竖虚线 = 每周一」',
    el('mKlineTitle').innerHTML.includes('淡竖虚线 = 每周一'));
}
check('正股K线图也带周几+周一竖线', (() => {
  if (!sOpt) return false;
  const d = String(sOpt.xAxis[0].data[0]).slice(0, 10);
  const line = sOpt.series.find(s => s.name === '正股收盘');
  return sOpt.xAxis[1].axisLabel.formatter(d).includes(ctx.weekdayCn(d)) &&
    !!line.markLine && line.markLine.data.every(m => ctx.isMondayCn(m.xAxis)) &&
    el('mStockTitle').innerHTML.includes('淡竖虚线 = 每周一');
})(), sOpt ? `${sOpt.xAxis[1].axisLabel.formatter(sOpt.xAxis[0].data[0])}` : '(没有正股图)');
check('分时卡片标题写了「日期 周几」',
  /2026-\d\d-\d\d 周[一二三四五]/.test(el('sel#cardMinute h3').innerHTML),
  el('sel#cardMinute h3').innerHTML.replace(/<[^>]*>/g, ' ').trim().slice(0, 60));
check('分时图本身没被改坏(三条对比线还在)', !!mOpt &&
  mOpt.series.filter(s => s.type === 'line').length >= 2,
  mOpt ? mOpt.series.map(s => s.name).join(',') : '(没有分时图)');

/* ---------------- 5. 个券历史页签用的是同一套 helper(结构断言) ---------------- */
const nAxis = (src.match(/axisLabel:weekAxisLabel\(\)/g) || []).length;
const nMark = (src.match(/markLine:weekSplitMarkLine\(/g) || []).length;
check('三张日K图都用了 weekAxisLabel/weekSplitMarkLine', nAxis >= 3 && nMark >= 3,
  `axisLabel×${nAxis} markLine×${nMark}`);

console.log(fails ? `\n✗ ${fails} 项未通过` : '\n✓ 全部通过');
process.exit(fails ? 1 : 0);
