/**
 * 「非当日热度榜」个券按需补抓分时/正股 —— 前后端联调冒烟测试
 *
 * 背景: 每日快照只对当日 Top30 抓分时/正股/新闻, 从放量榜点开的券本地没有分时,
 * 弹窗显示"分时数据缺失"。新增:
 *   - 后端 /api/bond_detail/<code>      只读缓存(扫描后预热过的直接命中, 不发网络请求)
 *   - 后端 /api/bond_detail/<code>?fetch=1  现抓(转债分时 + 正股分时 + 正股日K + 新闻)并缓存
 *   - 前端 分时卡片空态给「补抓分时/正股」按钮, 抓完原地重画; 命中缓存则打开就有数据
 *
 * 用法: node tests/bond_detail_smoke.mjs [http://127.0.0.1:8734]
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
  src + '\n;return {renderVolume, openVolBond, fetchBondDetail, loadDay, applyDetail,'
      + ' DETAIL_get:()=>DETAIL, DETAIL_CTX_get:()=>DETAIL_CTX, VOL_get:()=>VOL, DAY_get:()=>DAY};'
);
const api = { calls: [], failNext: false };   // failNext: 注入一次失败, 用来测重试按钮
const wrappedFetch = async (url, opt) => {
  api.calls.push(url);
  if (api.failNext && url.includes('fetch=1')) {
    api.failNext = false;
    return { ok: false, status: 500, json: async () => ({ error: '测试注入的失败' }) };
  }
  const r = await fetch(BASE + url, opt);
  return { ok: r.ok, status: r.status, json: () => r.json() };
};
const ctx = script(document, { addEventListener() {}, dispatchEvent() {} }, echarts,
  wrappedFetch, (fn, ms) => setTimeout(fn, ms), clearTimeout, { href: BASE }, () => {}, console);

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
const minuteOpt = () => chartOptions.filter(o => o.series &&
  o.series.some(s => /\(转债\)$/.test(s.name || ''))).pop();
const stockOpt = () => chartOptions.filter(o => o.series &&
  o.series.some(s => s.name === '正股成交额(亿)')).pop();
const cacheFile = (date, code) =>
  path.join(process.cwd(), 'data', 'bond_detail', date, `${code}.json`);

/* ---------------- 准备: 拿扫描数据 ---------------- */
ctx.renderVolume();
await new Promise(r => setTimeout(r, 800));
const vol = ctx.VOL_get();
check('放量榜数据已加载', !!(vol && vol.bonds.length), `${vol?.bonds?.length} 只, 扫描日 ${vol?.date}`);
await ctx.loadDay(vol.date);

// 选券: "已有可用缓存" vs "没缓存" —— 一律问接口
// (缓存带版本号, 老版本文件后端不认; 而且之前的测试跑动过会把券写进缓存, 不能靠"预热名单"猜)
const inDay = new Set((ctx.DAY_get()?.bonds || []).map(b => b.code));
const outside = vol.bonds.filter(b => !inDay.has(b.code));
const probe = async (code) => {
  try { return (await (await fetch(BASE + '/api/bond_detail/' + code + '?date=' + vol.date)).json()); }
  catch (e) { return { __err: String(e) }; }
};
let cachedOne = null, coldOne = null, probes = 0;
for (const b of outside) {
  if ((cachedOne && coldOne) || probes >= 25) break;
  probes++;
  const j = await probe(b.code);
  if (j.cached === true && !cachedOne) cachedOne = b;
  if (j.cached !== true && !coldOne) coldOne = b;
}
check('找到一只已有缓存的非榜内券', !!cachedOne,
  cachedOne ? `${cachedOne.name}(${cachedOne.code})` : '没有 -> 预热还没跑, 先跑一次扫描');
check('找到一只无缓存的非榜内券', !!coldOne, coldOne ? `${coldOne.name}(${coldOne.code})` : '');
if (cachedOne) {
  const j = await probe(cachedOne.code);
  check('有缓存的券: 接口直接给分时(cached=true, 点数齐全)', j.cached === true
    && (j.minute?.points || []).length > 100,
    `cached=${j.cached} 分时点=${(j.minute?.points || []).length}`);
}
if (coldOne) {
  const j = await probe(coldOne.code);
  check('无缓存的券: 只读接口不发网络请求(cached=false)', j.cached !== true,
    `cached=${j.cached}`);
}

/* ---------------- 1. 缓存命中: 打开就有分时 ---------------- */
if (cachedOne) {
  chartOptions.length = 0; api.calls.length = 0;
  await ctx.openVolBond(cachedOne.code, 'volTable1');
  await new Promise(r => setTimeout(r, 400));
  check('打开时只读缓存接口', api.calls.some(u => u.startsWith('/api/bond_detail/' + cachedOne.code)
    && !u.includes('fetch=1')), api.calls.filter(u => u.includes('bond_detail')).join(','));
  check('没有触发现抓请求', !api.calls.some(u => u.includes('fetch=1')));
  const mo = minuteOpt();
  check('分时对比图直接画出来了', !!mo, mo ? `${mo.series.length} 条线` : '没画');
  if (mo) {
    check('分时图含转债/正股/中证转债三条线',
      ['(转债)', '(正股)', '中证转债'].every(k => mo.series.some(s => (s.name || '').includes(k))),
      mo.series.map(s => s.name).join(' | '));
    const bondLine = mo.series.find(s => /\(转债\)$/.test(s.name || ''));
    check('转债分时点数 > 100', bondLine.data.filter(v => v != null).length > 100,
      `${bondLine.data.filter(v => v != null).length} 点`);
  }
  check('K线卡片不再占满整行(与分时并排)', el('cardKline').style.gridColumn === '');
  check('正股走势图也画出来了', !!stockOpt());
  check('头部标签显示"分时/正股/归因/新闻已补齐"',
    el('mHead').innerHTML.includes('分时/正股/归因/新闻已补齐'));
  check('归因卡片渲染出具体因子(不再是空提示)',
    el('mDrivers').innerHTML.includes('drv') && !el('mDrivers').innerHTML.includes('快照没算它的归因'),
    (el('mDrivers').innerHTML.match(/class="tag (primary|warn|info)">([^<]+)</g) || []).join(' '));
  check('缺数据提示条已清空', el('mMinuteHint').innerHTML === '', el('mMinuteHint').innerHTML.slice(0, 40));
}

/* ---------------- 2. 缓存未命中: 打开即自动补抓(不用点按钮) ---------------- */
if (coldOne) {
  chartOptions.length = 0; api.calls.length = 0;
  await ctx.openVolBond(coldOne.code, 'volTable1');
  await new Promise(r => setTimeout(r, 400));
  check('未缓存时仍能打开弹窗(有K线)', !!chartOptions.find(o => o.series &&
    o.series.some(s => s.type === 'candlestick')));
  check('缺数据的券打开就自动补抓(无需点按钮)',
    api.calls.some(u => u.includes('/api/bond_detail/' + coldOne.code + '?fetch=1')),
    api.calls.filter(u => u.includes('bond_detail')).join(','));
  check('提示条显示"正在自动补抓"', el('mMinuteHint').innerHTML.includes('自动补抓'),
    el('mMinuteHint').innerHTML.slice(0, 50));
  check('分时图显示"分时数据缺失"',
    chartOptions.some(o => o.title && String(o.title.text || '').includes('分时数据缺失')));
  check('K线卡片占满整行', el('cardKline').style.gridColumn === '1 / -1');
  check('空图占位缩小到 118px(提示条不用滚动就看得到)', el('mMinute').style.height === '118px',
    `height=${el('mMinute').style.height}`);
  check('头部标签仍提示无分时/归因/新闻', el('mHead').innerHTML.includes('无分时/归因/新闻'));

  // 等自动补抓完成(首次约 5~8 秒) -> 原地重画
  const drawn = () => chartOptions.some(o => o.series
    && o.series.some(s => /\(转债\)$/.test(s.name || '')));
  for (let i = 0; i < 40 && !drawn(); i++) await new Promise(r => setTimeout(r, 500));
  check('抓完原地重画分时图', drawn());
  check('重画后容器恢复 320px', el('mMinute').style.height === '320px',
    `height=${el('mMinute').style.height}`);
  check('重画后K线卡片恢复并排', el('cardKline').style.gridColumn === '');
  check('标签变成"分时/正股/归因/新闻已补齐"',
    el('mHead').innerHTML.includes('分时/正股/归因/新闻已补齐'));
  check('补抓后归因也补上了', el('mDrivers').innerHTML.includes('drv'));
  check('归因/新闻不再显示"没有数据"的旧提示',
    !el('mDrivers').innerHTML.includes('没有归因/新闻数据'));
  check('内存里记住了这只券的详情', !!ctx.DETAIL_get()[coldOne.code]);
  const again = await probe(coldOne.code);
  check('之后再打开走缓存(cached=true)', again.cached === true);
  check('缓存里带分时日期', again.minute_date === vol.date, `${again.minute_date} vs ${vol.date}`);
  check('提示条已清空', el('mMinuteHint').innerHTML === '', el('mMinuteHint').innerHTML.slice(0, 40));

  // 手动「补抓」入口仍然可用(按钮走同一个函数)
  chartOptions.length = 0; api.calls.length = 0;
  await ctx.fetchBondDetail(coldOne.code);
  check('手动补抓入口仍能用', api.calls.some(u => u.includes('fetch=1')) && drawn(),
    api.calls.filter(u => u.includes('bond_detail')).join(','));

  // 抓失败时要给出可点的重试按钮
  api.failNext = true;
  chartOptions.length = 0;
  await ctx.fetchBondDetail(coldOne.code);
  check('抓失败时提示条给出重试按钮',
    el('mMinuteHint').innerHTML.includes('补抓失败') &&
    el('mMinuteHint').innerHTML.includes(`fetchBondDetail('${coldOne.code}')`),
    el('mMinuteHint').innerHTML.replace(/<[^>]+>/g, ' ').trim().slice(0, 60));
  api.failNext = false;
}

console.log(fails ? `\n${fails} 项失败` : '\n全部通过');
process.exit(fails ? 1 : 0);
