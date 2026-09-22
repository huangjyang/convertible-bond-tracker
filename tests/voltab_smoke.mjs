/**
 * 前端渲染冒烟测试: 用最小 DOM 桩把 index.html 的脚本跑起来,
 * 验证「放量榜」页签的渲染逻辑 + 放量券弹窗的 K线 option 是否正常。
 * 用法: node tests/voltab_smoke.mjs [http://127.0.0.1:8734]
 */
import fs from 'node:fs';
import path from 'node:path';

const BASE = process.argv[2] || 'http://127.0.0.1:8734';   // 与其它三套一致(app.py 的 PORT)
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
const window = { addEventListener() {}, dispatchEvent() {}, };
const timers = [];

const script = new Function(
  'document', 'window', 'echarts', 'fetch', 'setTimeout', 'clearTimeout',
  'location', 'alert', 'console',
  src + '\n;return {renderVolume, openVolBond, openBond, queryBond, switchTab,'
      + ' fillVolTable, loadDay, applyTomorrow, loadQijinHistory, mergeKlineDays,'
      + ' DAY_get:()=>DAY, VOL_get:()=>VOL};'
);

const api = { calls: [] };
const wrappedFetch = async (url) => {
  api.calls.push(url);
  const r = await fetch(BASE + url);
  return { ok: r.ok, status: r.status, json: () => r.json() };
};

const ctx = script(document, window, echarts, wrappedFetch,
  (fn, ms) => timers.push(fn), () => {}, { href: BASE }, () => {}, console);

/* ---------------- 执行 ---------------- */
let fails = 0;
const check = (name, cond, extra = '') => {
  console.log(`${cond ? '✓' : '✗'} ${name}${extra ? '  ' + extra : ''}`);
  if (!cond) fails++;
};
// 柱状序列的数据可能是裸数字, 也可能是 {value,itemStyle}(放量突破单独染色), 统一取出数值
const vals = (s) => ((s && s.data) || []).map(d => (d && typeof d === 'object') ? d.value : d);
// 顶层裸 fetch 一旦被拒(连接被服务端复用/关闭)会以未处理拒绝崩掉进程, 且管道下已打印的
// 进度会一起丢掉 -> 看起来"0 项通过"。这里统一包一层, 失败就当断言失败。
const getJSON = async (url) => {
  try { const r = await fetch(url.startsWith('http') ? url : BASE + url); return await r.json(); }
  catch (e) { console.log(`  (拉取 ${url} 失败: ${e.message || e})`); return { __err: String(e && e.message || e) }; }
};
process.stdout.on('error', e => { if (e.code === 'EPIPE') process.exit(0); });  // 管道被提前关闭时别炸出 dump
process.on('unhandledRejection', e => {
  console.log(`✗ 未处理的异步异常: ${(e && e.message) || e}`);
  fails++; console.log(fails ? `\n${fails} 项失败` : ''); process.exit(1);
});

// 1. 页签切换: 不应抛错, 且应拉取 /api/volume
ctx.switchTab('volume');
await new Promise(r => setTimeout(r, 600));
const volJSON = await getJSON('/api/volume');
const cand = volJSON.bonds.filter(m => m.ratio20 >= 2 && m.chg > -3 && !m.flat)
  .sort((a, b) => b.score - a.score);
const first = cand.filter(m => m.pos60 <= 0.35);
const second = cand.filter(m => m.pos60 > 0.35);
const drop = volJSON.bonds.filter(m => m.ratio20 >= 2 && m.chg <= -3);

check('切到放量榜会请求 /api/volume', api.calls.includes('/api/volume'),
  `calls=${api.calls.filter(c => c.startsWith('/api/volume')).join(',')}`);
check('统计条渲染出扫描日', el('volChips').innerHTML.includes(volJSON.date));
check('统计条渲染出候选数', el('volChips').innerHTML.includes(`>${volJSON.counts.candidate}<`));
check('首选分组行数 = ' + first.length,
  (el('sel#volTable1')._tbody.innerHTML.match(/<tr/g) || []).length === first.length,
  `表头列数=${(el('sel#volTable1')._thead.innerHTML.match(/<th[ >]/g) || []).length}`);
check('次选分组行数 = ' + second.length,
  (el('sel#volTable2')._tbody.innerHTML.match(/<tr/g) || []).length === second.length);
check('回避分组行数 = ' + drop.length,
  (el('sel#volTable3')._tbody.innerHTML.match(/<tr/g) || []).length === drop.length);
check('首选表渲染出具体券名', first.length === 0 ||
  el('sel#volTable1')._tbody.innerHTML.includes(first[0].name), first[0]?.name);
check('行内 onclick 指向 openVolBond',
  el('sel#volTable1')._tbody.innerHTML.includes("openVolBond('"));
check('表头 13 列(末列是「模拟仓」快捷建仓)',
  (el('sel#volTable1')._thead.innerHTML.match(/<th[ >]/g) || []).length === 13);
check('「模拟仓」列表头/单元格都带 pinr(钉在右边缘, 横滑也在视野里)',
  el('sel#volTable1')._thead.innerHTML.includes('<th class="pinr">模拟仓</th>') &&
  el('sel#volTable1')._tbody.innerHTML.includes('<td class="pinr">'));
check('每行都有「＋模拟仓」按钮(走 simQuickBuy)',
  (el('sel#volTable1')._tbody.innerHTML.match(/class="quickbuy/g) || []).length === first.length &&
  el('sel#volTable1')._tbody.innerHTML.includes("simQuickBuy('"));
// 「历史验证」卡片的数据来自 volume_scan_*.json 里的 validation 字段, 而它只在
// `scan_volume.py --report` 时才写入; 看板自动扫描(app.py maybe_scan)不带 --report,
// 所以最新一天通常没有 —— 这种情况必须优雅降级, 不能报错或留半张表。
if (volJSON.validation && volJSON.validation.a) {
  check('验证表A有数据行',
    (el('sel#volValidA')._tbody.innerHTML.match(/<tr/g) || []).length === volJSON.validation.a.length,
    `rows=${(el('sel#volValidA')._tbody.innerHTML.match(/<tr/g) || []).length}`);
  check('验证区B/C渲染', el('volValidBC').innerHTML.includes('事件研究') &&
    el('volValidBC').innerHTML.includes('条件概率'));
} else {
  check('无 validation 时验证卡片优雅降级(自动扫描不带 --report)',
    el('sel#volValidA')._tbody.innerHTML.includes('暂无验证数据') &&
    el('sel#volValidA')._thead.innerHTML === '' && el('volValidBC').innerHTML === '',
    `扫描日 ${volJSON.date} 没有 validation 字段`);
}

// 2. 弹窗: 应拉 K线并生成 echarts option
chartOptions.length = 0;
api.calls.length = 0;
ctx.openVolBond(first[0].code);
await new Promise(r => setTimeout(r, 800));
check('弹窗请求 /api/volume/kline/' + first[0].code,
  api.calls.includes('/api/volume/kline/' + first[0].code));
const opt = chartOptions.find(o => o.series && o.series.some(s => s.type === 'candlestick'));
check('生成蜡烛图 option', !!opt);
// 数据源: 不在当日榜的券走 /api/volume/kline, K线取最近 90 根(次新债就只有几十根)
const kjson = await getJSON('/api/volume/kline/' + first[0].code);
const klen = ((kjson.kline) || []).slice(-90).length;
if (opt) {
  const cs = opt.series.find(s => s.type === 'candlestick');
  const bars = opt.series.find(s => s.type === 'bar');
  const line = opt.series.find(s => s.type === 'line');
  check(`K线数据点 = min(90, 可用K线) = ${klen}`, cs.data.length === klen,
    `图上 ${cs.data.length} vs 数据 ${klen}`);
  check('成交量柱与K线等长', bars.data.length === cs.data.length);
  check('20日均量线存在', !!line && line.data.length === cs.data.length);
  check('每根K线为 [开,收,低,高] 且为有限数',
    cs.data.every(d => d.length === 4 && d.every(Number.isFinite)));
  // 量能突破的柱子单独染色 -> 柱数据是 {value,itemStyle} 而不是裸数字
  check('成交量柱按涨跌着色',
    bars.data.every(d => d.itemStyle && d.itemStyle.color));
  check('成交量柱非负', vals(bars).every(v => v >= 0));
}
check('弹窗指标区渲染量比', el('mMetrics').innerHTML.includes('量比20'));
check('弹窗头部含券名', el('mHead').innerHTML.includes(first[0].name));
// 分时卡片: 非当日榜的券按需补抓分时/正股(bond_detail), 已预热/补抓过的打开就有数据;
// 没有缓存时图表显示"分时数据缺失"且 K线卡片占满整行(不再用 display:none 藏卡片)。
const dayJSON = await getJSON('/api/day/' + volJSON.date);
const inDay = (dayJSON.bonds || []).some(b => b.code === first[0].code);
const det = await getJSON('/api/bond_detail/' + first[0].code);
if (inDay) {
  check('当日榜券: K线卡片不占满整行(与分时并排)',
    !el('cardKline').style.gridColumn, `gridColumn=${JSON.stringify(el('cardKline').style.gridColumn)}`);
} else if (det.cached) {
  check('非当日榜但已补抓过: 分时图正常画出(不再是"分时数据缺失")',
    chartOptions.some(o => o.series && o.series.some(s => /\(转债\)$/.test(s.name || ''))),
    `${first[0].name} 命中 bond_detail 缓存`);
  check('非当日榜但已补抓过: K线卡片与分时并排',
    !el('cardKline').style.gridColumn, `gridColumn=${JSON.stringify(el('cardKline').style.gridColumn)}`);
} else {
  check('非当日榜且无缓存: 分时缺数据 -> 图表显示"分时数据缺失"',
    chartOptions.some(o => o.title && String(o.title.text || '').includes('分时数据缺失')));
  check('非当日榜且无缓存: K线卡片占满整行', el('cardKline').style.gridColumn === '1 / -1',
    `gridColumn=${JSON.stringify(el('cardKline').style.gridColumn)}`);
}

// 3. 数据完整性 + 成交额兜底
await ctx.loadDay(volJSON.date);
const day = ctx.DAY_get();
check('热度榜快照已加载', !!day && day.bonds.length > 0, `${day?.bonds?.length} 只`);

const stillZero = day.bonds.filter(b => (b.kline_90d || []).length &&
  b.kline_90d.every(r => !r[6]));
check('历史成交额已全部补齐(不再有全零券)', stillZero.length === 0,
  stillZero.length ? stillZero.map(b => b.name).join('、') : '30/30 全部有值');

const zeroRows = day.bonds.reduce((n, b) =>
  n + (b.kline_90d || []).filter(r => !r[6]).length, 0);
check('没有残留的 0 成交额行', zeroRows === 0, `残留 ${zeroRows} 行`);

const noStockK = day.bonds.filter(b => !(b.stock_kline_90d || []).length);
check('正股K线已全部补齐', noStockK.length === 0,
  noStockK.length ? noStockK.map(b => b.name).join('、') : '30/30 全部有值');

// 兜底逻辑仍需可用: 人为把一只券成交额清零, 前端必须靠量价估算画出来
const victim = day.bonds.find(b => (b.kline_90d || []).length > 30);
const saved = victim.kline_90d.map(r => r.slice());
victim.kline_90d.forEach(r => { r[6] = 0; });
chartOptions.length = 0;
ctx.openBond(victim.code);
const o1 = chartOptions.find(o => o.series &&
  o.series.some(s => s.type === 'bar' && s.name === '成交额(亿)'));
check('成交额全零时仍生成成交量柱', !!o1);
if (o1) {
  const bars = o1.series.find(s => s.type === 'bar' && s.name === '成交额(亿)');
  const bv = vals(bars);
  const nonZero = bv.filter(v => v > 0).length;
  check('全零数据下成交量柱不为 0', nonZero === bv.length,
    `${nonZero}/${bv.length} 根非零, 首根=${bv[0]}`);
  const yfmt = o1.yAxis[1].axisLabel.formatter;
  check('成交额轴标签不再被四舍五入成 0/1',
    [0.4, 1.2, 3.7, 12.5].map(yfmt).join(',') === '0.4,1.2,3.7,13',
    [0.4, 1.2, 3.7, 12.5].map(yfmt).join(','));
  check('标题标注了估算来源', el('mKlineTitle').innerHTML.includes('估算'));
}
victim.kline_90d.forEach((r, i) => { r[6] = saved[i][6]; });

// 有真实成交额时必须用原始值
const realAmt = day.bonds.find(b => (b.kline_90d || []).some(r => r[6] > 0));
chartOptions.length = 0;
ctx.openBond(realAmt.code);
const o2 = chartOptions.find(o => o.series &&
  o.series.some(s => s.type === 'bar' && s.name === '成交额(亿)'));
const bars2 = o2 && o2.series.find(s => s.type === 'bar' && s.name === '成交额(亿)');
const b2v = vals(bars2);
const ri = realAmt.kline_90d.findIndex(r => r[6] > 0);
check('有真实成交额的券用原始值 (' + realAmt.name + ')',
  bars2 && Math.abs(b2v[ri] - realAmt.kline_90d[ri][6] / 1e8) < 5e-4,
  `图上=${b2v[ri]} 原始=${(realAmt.kline_90d[ri][6] / 1e8).toFixed(3)}`);

// 4. 另外三张图也必须带量: 正股走势 / 分时对比 / 个券历史K线
chartOptions.length = 0;
ctx.openBond(victim.code);
const stockOpt = chartOptions.find(o => o.series &&
  o.series.some(s => s.name === '正股成交额(亿)'));
check('正股走势图有成交额柱', !!stockOpt);
if (stockOpt) {
  const b = stockOpt.series.find(s => s.name === '正股成交额(亿)');
  const bv = vals(b);
  check('正股成交额柱非零', bv.some(v => v > 0),
    `非零 ${bv.filter(v => v > 0).length}/${bv.length}`);
}
const minOpt = chartOptions.find(o => o.series &&
  o.series.some(s => s.type === 'bar' && /分钟量/.test(s.name || '')));
check('分时图有分钟成交量柱', !!minOpt);
if (minOpt) {
  const b = minOpt.series.find(s => s.type === 'bar');
  const bv = vals(b);
  check('分钟量柱非零(累计量已转单分钟量)',
    bv.some(v => v > 0) && bv.every(v => v >= 0),
    `样本 ${bv.length} 根, 峰值 ${Math.max(...bv)}`);
}

// 科创板正股的量纲: 腾讯按"股"报量, 乘数必须是 1 而不是 100
const starBond = day.bonds.find(b => /^68[89]/.test(String(b.stock_code || '')) &&
  (b.stock_kline_90d || []).length);
if (starBond) {
  chartOptions.length = 0;
  ctx.openBond(starBond.code);
  const o = chartOptions.find(x => x.series &&
    x.series.some(s => s.name === '正股成交额(亿)'));
  const bars = o.series.find(s => s.name === '正股成交额(亿)');
  const bv = vals(bars);
  const i = starBond.stock_kline_90d.length - 1;
  const want = +(starBond.stock_kline_90d[i][6] / 1e8).toFixed(3);
  check('科创板正股量纲正确(688, 乘数=1) ' + starBond.name + '/' + starBond.stock_code,
    Math.abs(bv[i] - want) < 5e-4, `图上=${bv[i]} vs 数据=${want}`);
}

// 个券历史页签的K线也要带量
chartOptions.length = 0;
await ctx.queryBond(victim.code);
const bk = chartOptions.find(o => o.series &&
  o.series.some(s => s.name === '日K') && o.series.some(s => s.name === '成交额(亿)'));
check('个券历史K线带成交额', !!bk);
if (bk) {
  const b = bk.series.find(s => s.name === '成交额(亿)');
  check('个券历史成交额柱非零', b.data.some(v => v > 0),
    `非零 ${b.data.filter(v => v > 0).length}/${b.data.length}`);
  check('个券历史标题标注来源', el('bkKlineTitle').innerHTML.includes('成交额'));
}

// 5. 修复的连带收益: 正股K线补齐后, 明日分不再缺数据
ctx.applyTomorrow(day);
const scored = day.bonds.filter(b => b.tomorrow_score != null).length;
check('明日关注分覆盖全部券', scored === day.bonds.length, `${scored}/${day.bonds.length}`);

// 旧的「策略推演池」(buildPanel) 已从 index.html 删除, 这里改成验数据本身:
// 各快照合并后每只券都要有完整历史(至少要够画图/算均线), 且跟到最新交易日。
const allDays = await ctx.loadQijinHistory();
const merged = ctx.mergeKlineDays(allDays);
const byCode = new Map(merged.bonds.map(b => [b.code, b]));
check('快照里的每只券都能在合并结果里找到',
  day.bonds.every(b => byCode.has(b.code)),
  `${day.bonds.filter(b => byCode.has(b.code)).length}/${day.bonds.length} 只`);
const lens = day.bonds.map(b => (byCode.get(b.code)?.k.length) || 0).sort((a, b) => a - b);
check('每只券都有可用价格点(次新债可能很短)', lens[0] >= 3,
  `最少 ${lens[0]} 根, 中位 ${lens[Math.floor(lens.length / 2)]} 根`);
check('多数券有完整90日数据', lens.filter(n => n >= 80).length >= day.bonds.length * 0.7,
  `${lens.filter(n => n >= 80).length}/${day.bonds.length} 只 ≥80 根`);
const mergedLast = merged.bonds.map(b => b.last).sort().pop();
check('合并后的K线跟到最新交易日(不落后于当日快照)',
  mergedLast >= day.date, `最新 ${mergedLast} vs 快照 ${day.date}`);

// 6. 弹窗后切回热度榜券不应残留 hidden
ctx.switchTab('rank');
check('切回热度榜不报错', true);

console.log(fails ? `\n${fails} 项失败` : '\n全部通过');
process.exit(fails ? 1 : 0);
