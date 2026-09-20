/**
 * 「蓄势榜」页签冒烟测试
 *
 * 覆盖: 页签渲染(选股卡片 + 点火/蓄势TOP/追高区三张表 + 分层回测表)
 *       · 行点击打开弹窗(非热度榜券走 /api/volume/kline + /api/bond_detail)
 *       · 蓄势指标进指标格 · 「点火」券的归因能算出来(coil 记录回落)
 *
 * 用法: node tests/coil_smoke.mjs [http://127.0.0.1:8734]
 */
import fs from 'node:fs';

const BASE = process.argv[2] || 'http://127.0.0.1:8734';
const src = fs.readFileSync('index.html', 'utf8').split('<script>').pop().split('</script>')[0];

/* ---------------- DOM 桩 ---------------- */
const made = {};
function el(id) {
  if (made[id]) return made[id];
  const e = {
    id, innerHTML: '', textContent: '', style: {}, dataset: {}, value: '',
    classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
    _thead: { innerHTML: '' }, _tbody: { innerHTML: '' },
    querySelector(sel) { return sel.includes('thead') ? this._thead : this._tbody; },
    querySelectorAll: () => [], addEventListener() {}, appendChild() {}, onclick: null,
  };
  made[id] = e;
  return e;
}
const document = {
  getElementById: el, querySelector: (s) => el('sel' + s), querySelectorAll: () => [],
  createElement: () => el('tmp' + Math.random()), addEventListener() {},
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
  src + '\n;return {switchTab, loadDay, renderCoilTab, openCoilBond, COIL_get:()=>COIL, DAY_get:()=>DAY};'
);
const api = { calls: [] };
const wrappedFetch = async (u) => {
  api.calls.push(u);
  const r = await fetch(BASE + u);
  return { ok: r.ok, status: r.status, json: () => r.json() };
};
const ctx = script(document, { addEventListener() {}, dispatchEvent() {} }, echarts,
  wrappedFetch, (fn, ms) => setTimeout(fn, ms), clearTimeout, { href: BASE }, () => {}, console);

let fails = 0;
const check = (name, cond, extra = '') => {
  console.log(`${cond ? '✓' : '✗'} ${name}${extra ? '  ' + extra : ''}`);
  if (!cond) fails++;
};
process.stdout.on('error', e => { if (e.code === 'EPIPE') process.exit(0); });
process.on('unhandledRejection', e => {
  console.log(`✗ 未处理的异步异常: ${(e && e.message) || e}`); fails++; process.exit(1);
});
const getJSON = async (u) => {
  try { const r = await fetch(BASE + u); return await r.json(); }
  catch (e) { console.log(`  (拉取 ${u} 失败: ${e.message})`); return { __err: String(e) }; }
};

/* ---------------- 1. 接口 ---------------- */
const html = fs.readFileSync('index.html', 'utf8');
const coil = await getJSON('/api/coil');
// 哪些券"已有可用缓存"一律问接口(预热范围会变: 放量候选 + 蓄试点火 + 蓄势TOP20)
const probeDetail = async (code) => {
  try { return await getJSON('/api/bond_detail/' + code + '?date=' + coil.date); }
  catch (e) { return {}; }
};
const cachedCodes = [], uncachedCodes = [];
for (const code of (coil.groups?.['蓄势TOP'] || []).concat(coil.groups?.['点火'] || [])) {
  if (cachedCodes.length >= 3 && uncachedCodes.length >= 6) break;
  const j = await probeDetail(code);
  if (j.cached === true) { if (!cachedCodes.includes(code)) cachedCodes.push(code); }
  else if (!uncachedCodes.includes(code)) uncachedCodes.push(code);
}
check('GET /api/coil 有数据', !coil.__err && !coil.error && coil.date,
  `${coil.date} · ${coil.counts?.scored} 只打分`);
check('接口带分层回测(validation.layers)', (coil.validation?.layers || []).length >= 6,
  `${(coil.validation?.layers || []).length} 层`);
check('点火组与 counts.ignition 一致',
  (coil.groups?.['点火'] || []).length === coil.counts?.ignition,
  `${(coil.groups?.['点火'] || []).length} vs ${coil.counts?.ignition}`);
check('每只券都带 蓄势分/量比/pos60/振幅', (coil.bonds || []).every(b =>
  Number.isFinite(b.score) && Number.isFinite(b.ratio) && Number.isFinite(b.pos60)
  && Number.isFinite(b.range20)));
check('券按蓄势分降序', (coil.bonds || []).every((b, i, a) => i === 0 || a[i - 1].score >= b.score));
check('点火券 = 低位+窄幅+温和放量收涨', (coil.bonds || []).filter(b => b.ign).every(b =>
  b.pos60 <= 0.35 && b.ratio >= 1.5 && b.ratio <= 3 && b.chg > 0 && b.range20 <= 0.12),
  (coil.bonds || []).filter(b => b.ign).map(b => `${b.name} ${b.ratio}x`).join('、') || '今天无');

/* ---------------- 2. 页签渲染 ---------------- */
const day = await getJSON('/api/dates');
await ctx.loadDay(day[0].date);
ctx.switchTab('coil');
await new Promise(r => setTimeout(r, 900));
const chips = el('coilChips').innerHTML;
check('统计条渲染出扫描日与点火数', chips.includes(coil.date) && chips.includes(String(coil.counts.ignition)),
  chips.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 80));
check('口径说明写清"蓄势不预测爆发"', el('coilHint').innerHTML.includes('不预测爆发')
  || el('coilHint').innerHTML.includes('防守'));
const ignRows = (el('sel#coilIgn')._tbody.innerHTML.match(/<tr/g) || []).length;
const topRows = (el('sel#coilTop')._tbody.innerHTML.match(/<tr/g) || []).length;
const riskRows = (el('sel#coilRisk')._tbody.innerHTML.match(/<tr/g) || []).length;
check('点火表行数 = 点火券数', ignRows === (coil.groups?.['点火'] || []).length, `${ignRows} 行`);
check('蓄势TOP 表行数 = 40', topRows === Math.min(40, coil.bonds.length), `${topRows} 行`);
check('追高区表行数 = counts.risk', riskRows === coil.counts.risk || (coil.counts.risk === 0 && el('sel#coilRisk')._tbody.innerHTML.includes('没有')),
  `${riskRows} 行 / counts ${coil.counts.risk}`);
const head = el('sel#coilTop')._thead.innerHTML;
check('表头 11 列(含蓄势分/连续缩量)', (head.match(/<th[ >]/g) || []).length === 11,
  `${(head.match(/<th[ >]/g) || []).length} 列`);
check('行内 onclick 指向 openCoilBond', el('sel#coilTop')._tbody.innerHTML.includes("openCoilBond('"));
const validBody = el('sel#coilValid')._tbody.innerHTML;
check('分层回测表渲染出点火行', validBody.includes('点火'), '');
check('回测表列出最大浮亏列', el('sel#coilValid')._thead.innerHTML.includes('最大浮亏'));
check('备注说明事后选池偏差', el('coilNote').innerHTML.includes('事后选池'));

/* ---------------- 3. 点开一只蓄势券 ---------------- */
const target = (coil.groups?.['点火'] || [])[0] || (coil.groups?.['蓄势TOP'] || [])[0];
const tm = (coil.bonds || []).find(b => b.code === target);
chartOptions.length = 0; api.calls.length = 0;
await ctx.openCoilBond(target, (coil.groups?.['点火'] || []).includes(target) ? 'ign' : 'top');
await new Promise(r => setTimeout(r, 700));
check('拉该券的K线缓存', api.calls.some(u => u.startsWith('/api/volume/kline/' + target)),
  api.calls.join(','));
check('K线图有蜡烛图', chartOptions.some(o => o.series && o.series.some(s => s.type === 'candlestick')));
const metrics = el('mMetrics').innerHTML;
check('指标格里有蓄势分', metrics.includes('蓄势分') && metrics.includes(tm.score.toFixed(1)));
check('指标格里有60日分位/连续缩量', metrics.includes('60日分位') && metrics.includes('连续缩量'));
check('头部标签写着蓄势分', el('mHead').innerHTML.includes('蓄势分'));
// 预热过的券打开就必须有分时(容器高度也该是满的) —— 这条严格断言, 免得"预热失效"被静默放过
check('已预热的点火券打开就有分时图', chartOptions.some(o => o.series
  && o.series.some(s => /\(转债\)$/.test(s.name || ''))),
  api.calls.join(','));
check('分时容器保持 320px(有数据)', el('mMinute').style.height === '320px',
  `height=${el('mMinute').style.height}`);

/* ---------------- 3b. 未缓存的蓄势券: 打开即自动补抓(不用点按钮) ---------------- */
if (uncachedCodes.length) {
  const code = uncachedCodes[0];
  chartOptions.length = 0; api.calls.length = 0;
  await ctx.openCoilBond(code, 'top');
  await new Promise(r => setTimeout(r, 400));
  check('未缓存的券: 打开就自动发起了补抓(无需点按钮)',
    api.calls.some(u => u.includes('/api/bond_detail/' + code + '?fetch=1')),
    api.calls.join(' , '));
  check('提示条写着"正在自动补抓"', el('mMinuteHint').innerHTML.includes('自动补抓'),
    el('mMinuteHint').innerHTML.replace(/<[^>]+>/g, ' ').trim().slice(0, 50));
  check('提示条在分时图上方(不用滚动就能看到)',
    html.indexOf('id="mMinuteHint"') < html.indexOf('id="mMinute"'),
    `hint@${html.indexOf('id="mMinuteHint"')} chart@${html.indexOf('id="mMinute"')}`);
  check('空图占位缩小到 118px', el('mMinute').style.height === '118px',
    `height=${el('mMinute').style.height}`);
  // 等它抓完(首次约 5~8s) -> 原地重画
  for (let i = 0; i < 40 && !api.__done; i++) {
    await new Promise(r => setTimeout(r, 500));
    if (chartOptions.some(o => o.series && o.series.some(s => /\(转债\)$/.test(s.name || '')))) break;
  }
  check('抓完自动重画分时图', chartOptions.some(o => o.series
    && o.series.some(s => /\(转债\)$/.test(s.name || ''))));
  check('重画后容器恢复 320px', el('mMinute').style.height === '320px',
    `height=${el('mMinute').style.height}`);
  check('抓完提示条清空', el('mMinuteHint').innerHTML === '',
    el('mMinuteHint').innerHTML.slice(0, 40));
  const again = await getJSON('/api/bond_detail/' + code + '?date=' + coil.date);
  check('之后这只券有缓存了(秒开)', again.cached === true && (again.minute?.points || []).length > 100,
    `cached=${again.cached}`);
} else {
  console.log('  (蓄势榜 40 只全被预热了, 跳过未缓存分支)');
}

/* ---------------- 3c. 快速翻页时自动抓有并发上限 ---------------- */
{
  const many = uncachedCodes.slice(0, 6);
  api.calls.length = 0;
  many.forEach(c => { ctx.openCoilBond(c, 'top'); });     // 故意不 await, 模拟快速连点
  await new Promise(r => setTimeout(r, 600));
  const inflight = new Set(api.calls.filter(u => u.includes('fetch=1')).map(u => u.split('/')[3].split('?')[0]));
  check('快速翻页时并发自动抓有上限(≤3)', inflight.size <= 3,
    `${inflight.size} 只在抓: ${[...inflight].join(',')}`);
}

/* ---------------- 4. 点火券能算出归因(coil 记录回落) ---------------- */
if ((coil.groups?.['点火'] || []).length) {
  const peek = await getJSON('/api/bond_detail/' + target + '?date=' + coil.date);
  check(peek.cached ? '已预热: 只读缓存直接拿到分时' : '未预热: 只读缓存不发网络请求',
    peek.cached ? (peek.minute?.points || []).length > 100 : peek.cached === false,
    `cached=${peek.cached} 分时点=${(peek.minute?.points || []).length}`);
  const det = await getJSON('/api/bond_detail/' + target + '?date=' + coil.date + '&fetch=1');
  check('现抓拿到分时(正股代码来自蓄势榜记录)', !det.__err && (det.minute?.points || []).length > 100,
    det.error || `${(det.minute?.points || []).length} 个分时点 · 正股分时 ${(det.stock_minute?.points || []).length} 点`);
  check('归因算出来了(coil 记录回落生效)', (det.drivers || []).length > 0,
    (det.drivers || []).map(d => d.tag).join('、') || '空');
} else {
  console.log('  (今天没有点火券, 跳过归因回落检查)');
}

console.log(fails ? `\n${fails} 项失败` : '\n全部通过');
process.exit(fails ? 1 : 0);
