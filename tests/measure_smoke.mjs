/**
 * K线「区间收益」(拖选测收益) —— 冒烟测试
 *
 * 背景: 详情页的转债/正股90日K线卡片里各加了一个「📏 区间收益」开关, 点开后在图上是按住拖一段,
 *   算出这一段的收益(像 TradingView 的测量工具)。口径: 起止都取收盘价, 起点收盘买入 → 终点收盘卖出,
 *   不含手续费; 另外给最大回撤(收盘口径)/振幅/日均/累计成交额 —— 只看一个收益率最容易误判。
 *
 * 覆盖:
 *   1) measureRange 纯计算: 收益/每手金额/回撤/振幅/日均/累计成交额, 以及反向拖、越界、单日、坏数据
 *   2) 与 Python 口径一致: 用真实快照 data/klines_2026-09-18.json 复算, 对 hardcode 的期望值
 *   3) brush 配置里那三个"实证坑": xAxisIndex 只绑价格格子(0, 绑量能格子会把柱子刷灰)、
 *      toolbox 必须显式关(否则右上角多一排图标)、开关关着时不发 takeGlobalCursor(手机上才滑得动页面)
 *   4) 集成: openBond 渲染后拖选 -> 结果块出现且数字正确; 空选/关闭 -> 选区与结果一起清干净
 *   5) 换券后开关保持、选区清空; 没K线数据时点开关不炸
 *   6) 每手口径: 转债 10张 / 正股 100股 / 科创板正股 200股
 *
 * 用法: node tests/measure_smoke.mjs        (纯本地, 不需要起服务)
 */
import fs from 'node:fs';
import path from 'node:path';

const html = fs.readFileSync(path.join(process.cwd(), 'index.html'), 'utf8');
const src = html.split('<script>').pop().split('</script>')[0];
const snapshot = JSON.parse(fs.readFileSync(
  path.join(process.cwd(), 'data', 'klines_2026-09-18.json'), 'utf8'));

/* ---------------- 最小 DOM 桩 ---------------- */
const made = {};
function el(id) {
  if (made[id]) return made[id];
  const cls = new Set();
  const e = {
    id, innerHTML: '', textContent: '', title: '', value: '', disabled: false,
    style: {}, dataset: {},
    classList: {
      toggle(c, on) { const v = on === undefined ? !cls.has(c) : !!on; v ? cls.add(c) : cls.delete(c); return v; },
      add(c) { cls.add(c); }, remove(c) { cls.delete(c); }, contains: (c) => cls.has(c),
    },
    _cls: cls,
    _thead: { innerHTML: '' }, _tbody: { innerHTML: '' },
    querySelector(sel) { return sel.includes('thead') ? this._thead : this._tbody; },
    querySelectorAll: () => [],
    addEventListener() {}, appendChild() {}, onclick: null,
  };
  made[id] = e;
  return e;
}
const documentStub = {
  getElementById: el,
  querySelector: (sel) => el('sel' + sel),
  querySelectorAll: () => [],
  createElement: () => el('tmp' + Math.random()),
  addEventListener() {},
  body: el('body'),
};
/* 图表桩: 记录每个实例的 setOption / dispatchAction / 事件回调, 让测试能"手动触发拖选" */
function makeEcharts() {
  const byDom = new Map(), insts = [];
  const echarts = {
    init(dom) {
      const inst = {
        dom, options: [], actions: [], handlers: {}, disposed: false,
        setOption(o) { this.options.push(o); },
        on(t, fn) { (this.handlers[t] = this.handlers[t] || []).push(fn); },
        dispatchAction(a) { this.actions.push(a); },
        dispose() { this.disposed = true; }, isDisposed() { return this.disposed; },
        resize() {}, getZr: () => ({ on() {} }),
      };
      byDom.set(dom, inst); insts.push(inst);
      return inst;
    },
    getInstanceByDom(dom) { const i = byDom.get(dom); return (i && !i.disposed) ? i : null; },
  };
  return { echarts, insts };
}
function loadPage(width = 1400) {
  const { echarts, insts } = makeEcharts();
  const script = new Function(
    'document', 'window', 'echarts', 'fetch', 'setTimeout', 'clearTimeout',
    'location', 'alert', 'console',
    src + '\n;return {measureRange, measureHtml, measureBrushOption, MEASURE, MEASURE_DATA,'
        + ' toggleMeasure, resetMeasure, openBond, klineAmount, baseLine, fmt, pct, VOL_BREAK};'
  );
  const ctx = script(documentStub,
    { innerWidth: width, addEventListener() {}, dispatchEvent() {} },
    echarts, async () => { throw new Error('本测试不该发网络请求'); },
    (fn, ms) => setTimeout(fn, ms), clearTimeout,
    { href: 'http://127.0.0.1:0' }, () => {}, console);
  return { ctx, insts };
}
const instOf = (insts, id) => insts.filter(i => i.dom && i.dom.id === id && !i.disposed).pop();

let fails = 0;
const check = (name, cond, extra = '') => {
  console.log(`${cond ? '✓' : '✗'} ${name}${extra ? '  ' + extra : ''}`);
  if (!cond) fails++;
};
const near = (a, b, eps = 1e-6) => a != null && b != null && Math.abs(a - b) < eps;

const { ctx, insts } = loadPage();

/* ---------------- 1. measureRange 纯计算 ---------------- */
// 造一段10天的"每天涨1元"的K线(收盘 100..109, 高=收+1, 低=收-1, 每天成交额 2亿)
const ramp = Array.from({ length: 10 }, (_, i) => {
  const c = 100 + i;
  return [`2026-06-${String(1 + i).padStart(2, '0')}`, c - 0.5, c, c + 1, c - 1, 1000, 0];
});
const rampAmt = Array(10).fill(2);
const r1 = ctx.measureRange(ramp, rampAmt, 0, 9, 10);
check('区间收益 = 收盘价口径 (100 -> 109 = +9%)', near(r1.ret, 9), `得到 ${r1.ret}`);
check('交易日数 = 10, 自然日 = 9', r1.days === 10 && r1.calDays === 9,
  `days=${r1.days} calDays=${r1.calDays}`);
check('价差与每手金额: 单调上涨 -> 回撤 0, 每手(+90元)',
  near(r1.diff, 9) && near(r1.dd, 0) && near(r1.diff * r1.lot, 90), `dd=${r1.dd}`);
check('振幅 = (最高价-最低价)/起点收盘 (110/99 -> 11%)', near(r1.amp, 11), `得到 ${r1.amp}`);
check('日均 = 区间收益/交易日数 (9/10 = 0.9%/日)', near(r1.perDay, 0.9), `得到 ${r1.perDay}`);
check('累计成交额 = 20亿', near(r1.amtSum, 20), `得到 ${r1.amtSum}`);
check('最高/最低收盘 取首尾相同的极值(单调序列)',
  near(r1.hc, 109) && near(r1.lc, 100) && r1.hcD === '2026-06-10' && r1.lcD === '2026-06-01',
  `hc=${r1.hc}@${r1.hcD} lc=${r1.lc}@${r1.lcD}`);

check('从右往左拖 = 同一次拖选(自动交换起止)',
  JSON.stringify(ctx.measureRange(ramp, rampAmt, 9, 0, 10)) === JSON.stringify(r1));
check('越界下标被钳到两端 (i0=-5, i1=99 = 全区间)',
  near(ctx.measureRange(ramp, rampAmt, -5, 99, 10).ret, 9));
check('小数下标按四舍五入取整 (2.6 -> 3)', ctx.measureRange(ramp, rampAmt, 2.6, 9, 10).i0 === 3);
const one = ctx.measureRange(ramp, rampAmt, 4, 4, 10);
check('只选一根K线: 收益 0%、1 个交易日(不炸也不瞎报)',
  near(one.ret, 0) && one.days === 1 && near(one.dd, 0));
check('空K线 -> null', ctx.measureRange([], [], 0, 5, 10) === null);
check('起点收盘为 0 -> 收益 null(而不是 Infinity)',
  ctx.measureRange([[`2026-06-01`, 0, 0, 0, 0, 0, 0], ramp[1]], rampAmt, 0, 1, 10).ret === null);
check('成交额缺失也不会算出 NaN', (() => {
  const m = ctx.measureRange(ramp, null, 0, 9, 10);
  return m && m.amtSum === 0 && isFinite(m.ret);
})());

// 回撤: 100 -> 120 -> 90, 最大回撤 = 90/120-1 = -25%(收盘口径)
const zig = [['2026-06-01', 100, 100, 101, 99, 0, 0],
             ['2026-06-02', 100, 120, 121, 99, 0, 0],
             ['2026-06-03', 120, 90, 121, 89, 0, 0]];
const mz = ctx.measureRange(zig, [1, 1, 1], 0, 2, 10);
check('最大回撤 = 阶段高点后的最低收盘 (120 -> 90 = -25%)', near(mz.dd, -25), `得到 ${mz.dd}`);
check('振幅用盘中最高/最低价 (121/89 -> 32%)', near(mz.amp, 32), `得到 ${mz.amp}`);

/* ---------------- 2. 与 Python 口径一致 (真实快照) ---------------- */
// 期望值由 python3 独立复算同一份快照得出。注意成交额这条: 该券是腾讯源(K线里没有真实成交额字段),
// 每日成交额由 量×典型价 估算, 而且 klineAmount 逐日 toFixed(3) —— Python 那边照抄同一口径
// (逐日四舍五入到 3 位再求和)才比对得上, 直接求和会差 0.001。
const CODE = '123160', I0 = 10, I1 = 40;
const EXPECT = {
  p0: 185.3, p1: 163.815, ret: -11.594711, days: 31, calDays: 45,
  hc: 198.8, hcD: '2026-04-22', lc: 158.201, lcD: '2026-05-14',
  dd: -20.422032, amp: 25.402051, amtSum: 76.753, perDay: -0.374023, money: -215,
};
const rows = snapshot.klines[CODE];
const { amt } = ctx.klineAmount(rows, 10);            // 转债 1手=10张
const m = ctx.measureRange(rows, amt, I0, I1, 10);
check(`快照里有 ${CODE} 且区间够长`, !!rows && rows.length > I1);
check('收益与 Python 一致', near(m.ret, EXPECT.ret, 1e-3), `js ${m.ret?.toFixed(6)} vs py ${EXPECT.ret}`);
check('起止价与 Python 一致', near(m.p0, EXPECT.p0) && near(m.p1, EXPECT.p1));
check('交易日/自然日与 Python 一致', m.days === EXPECT.days && m.calDays === EXPECT.calDays,
  `${m.days}/${m.calDays}`);
check('最高/最低收盘(含日期)与 Python 一致',
  near(m.hc, EXPECT.hc) && m.hcD === EXPECT.hcD && near(m.lc, EXPECT.lc) && m.lcD === EXPECT.lcD,
  `${m.hc}@${m.hcD} / ${m.lc}@${m.lcD}`);
check('最大回撤与 Python 一致', near(m.dd, EXPECT.dd, 1e-3), `js ${m.dd?.toFixed(6)} vs py ${EXPECT.dd}`);
check('振幅与 Python 一致', near(m.amp, EXPECT.amp, 1e-3), `js ${m.amp?.toFixed(6)} vs py ${EXPECT.amp}`);
check('累计成交额与 Python 一致', near(m.amtSum, EXPECT.amtSum, 1e-3),
  `js ${m.amtSum} vs py ${EXPECT.amtSum}`);
check('日均与 Python 一致', near(m.perDay, EXPECT.perDay, 1e-3));

/* ---------------- 3. brush 配置: 三个实证坑 ---------------- */
const bo = ctx.measureBrushOption();
check('只绑价格格子 xAxisIndex=0 (绑上量能格子会把柱子刷成灰)',
  bo.xAxisIndex === 0, `xAxisIndex=${JSON.stringify(bo.xAxisIndex)}`);
check('工具项 toolbox=false (选项里还要 toolbox:{show:false} 才不会出现右上角图标)', bo.toolbox === false);
check('拖选类型 lineX + 单选 + 不可二次拖拽', bo.brushType === 'lineX' && bo.brushMode === 'single'
  && bo.transformable === false);
check('点空白处取消选区(removeOnClick)', bo.removeOnClick === true);

/* ---------------- 4. 集成: openBond 里拖选算收益 ---------------- */
const mkBond = (code, extra = {}) => Object.assign({
  code, name: '测试券' + code, market: 'SH', chg: 1.2, price: 120,
  stock_name: '测试正股', stock_code: '600000', stock_chg: 0.55,   // pct() 对 undefined 会炸, 桩数据得给全
  rank: 7, status: 'new', days_on_list: 3, rating: 'AA', scale: 12.5,
  listing_date: '2020-01-01', turnover_yi: 1.2,
}, extra);
ctx.openBond(CODE, { bond: mkBond(CODE, { kline_90d: rows }), navList: [CODE] });

const kc = instOf(insts, 'mKline');
const kOpt = kc && kc.options.filter(o => o.series).pop();
check('K线图带上了 brush 配置(纯拖选, 不依赖 toolbox)',
  !!kOpt && !!kOpt.brush && kOpt.brush.brushType === 'lineX' && kOpt.brush.xAxisIndex === 0);
check('toolbox 被显式关掉(否则右上角会多一排 brush 图标)',
  !!kOpt && !!kOpt.toolbox && kOpt.toolbox.show === false);
check('K线图本身没被改坏(蜡烛/成交额柱/基准虚线都在)',
  !!kOpt && kOpt.series.map(s => s.name).join(',') === '收盘价,成交额(亿),20日均额(亿)',
  kOpt ? kOpt.series.map(s => s.name).join(',') : '(没有图)');
check('渲染完就登记了区间收益用的数据(转债 1手=10张)',
  ctx.MEASURE_DATA.bond && ctx.MEASURE_DATA.bond.lot === 10
  && ctx.MEASURE_DATA.bond.volIdx === 1 && ctx.MEASURE_DATA.bond.chart === kc);

const box = el('mzBoxBond'), btn = el('mzBtnBond'), hint = el('mzHintBond');
const brushEv = kc.handlers.brushSelected && kc.handlers.brushSelected[0];
check('图表订阅了 brushSelected 事件', typeof brushEv === 'function');
check('图表订阅了 brushEnd(松手时收掉悬停提示, 手机上那提示块会盖住选区)',
  typeof (kc.handlers.brushEnd || [])[0] === 'function');
if ((kc.handlers.brushEnd || [])[0]) {
  kc.handlers.brushEnd[0]();
  check('开关关着时 brushEnd 不发 hideTip(不干扰普通悬停看盘)',
    !kc.actions.some(a => a.type === 'hideTip'));
}
check('开关默认是关的: 不发 takeGlobalCursor(手机上照常滑动页面)',
  ctx.MEASURE.bond === false && !kc.actions.some(a => a.type === 'takeGlobalCursor')
  && btn._cls.has('on') === false);

ctx.toggleMeasure('bond');                    // 打开
const cursorOn = kc.actions.filter(a => a.type === 'takeGlobalCursor').pop();
check('点开后用 takeGlobalCursor 激活 lineX 拖选',
  !!cursorOn && cursorOn.key === 'brush' && cursorOn.brushOption.brushType === 'lineX',
  JSON.stringify(cursorOn && cursorOn.brushOption));
check('按钮进入选中态 + 提示"按住拖选"',
  btn._cls.has('on') && /拖选/.test(hint.textContent), hint.textContent);
if ((kc.handlers.brushEnd || [])[0]) {
  kc.handlers.brushEnd[0]();                  // 开关开着时, 松手要收掉悬停提示
  check('开关开着时 brushEnd 发 hideTip(免得提示盖住选区)',
    kc.actions.some(a => a.type === 'hideTip'));
}

// 手动喂一个拖选事件(等价于真在图上从左拖到右): 选中下标 10..40
const idx = Array.from({ length: I1 - I0 + 1 }, (_, i) => I0 + i);
brushEv({ batch: [{ areas: [{ coordRange: [I0, I1] }], selected: [{ dataIndex: idx }] }] });
const boxHtml = box.innerHTML;
check('拖选后结果块显示出来', box.style.display !== 'none' && boxHtml.length > 0);
check('结果块里的区间收益与 Python 一致 (-11.59%)', boxHtml.includes(ctx.pct(EXPECT.ret)),
  (boxHtml.match(/区间收益<\/div><div class="v[^"]*">([^<]+)/) || [])[1] || '(没找到)');
check('用跌色标出负收益', /class="v down">-11\.59%/.test(boxHtml));
check('结果块写清起止日期/收盘价与交易日数',
  boxHtml.includes('2026-04-14') && boxHtml.includes('2026-05-29')
  && boxHtml.includes(ctx.fmt(EXPECT.p0, 3)) && boxHtml.includes('31 个交易日')
  && boxHtml.includes('45 个自然日'), boxHtml.slice(0, 120));
check('每手(10张)金额 = 价差 × 10 (-215元)', boxHtml.includes('每手(10张)')
  && boxHtml.includes(EXPECT.money + ' 元'));
check('结果块带最大回撤/振幅/日均/累计成交额',
  boxHtml.includes('-20.42%') && boxHtml.includes('25.40%') && boxHtml.includes('-0.37%')
  && boxHtml.includes('76.8亿'));
check('最高/最低收盘带日期(标明是「收盘」口径, 免得跟盘中最高价混淆)',
  boxHtml.includes('最高收 04-22') && boxHtml.includes('最低收 05-14'));
check('量能格子上画了同区间的 markArea(价格格子由 brush 自己画)',
  (() => { const last = kc.options[kc.options.length - 1];
    const s1 = last.series && last.series[1];
    const d = s1 && s1.markArea && s1.markArea.data && s1.markArea.data[0];
    return !!d && d[0].xAxis === '2026-04-14' && d[1].xAxis === '2026-05-29'; })());

// 真实浏览器里「折线」序列(正股收盘那条线)拖出来的事件长这样: areas 有 coordRange, 但
// selected[].dataIndex 是空的 —— 只认 dataIndex 的话正股图会永远"拖了没反应"(这是实测踩到的坑)
brushEv({ batch: [{ areas: [{ coordRange: [I0, I1] }], selected: [{ dataIndex: [] }] }] });
check('折线序列只给 coordRange、不给 dataIndex 时照样能算(正股图那个坑)',
  box.innerHTML.includes(ctx.pct(EXPECT.ret))
  && box.innerHTML.includes('2026-04-14') && box.innerHTML.includes('2026-05-29'),
  box.innerHTML ? (box.innerHTML.match(/区间收益<\/div><div class="v[^"]*">([^<]+)/) || [])[1] : '(空)');
check('折线序列的 coordRange 是小数(如 30.6)也按四舍五入取整、不越界',
  (() => { brushEv({ batch: [{ areas: [{ coordRange: [10.4, 40.6] }], selected: [{ dataIndex: [] }] }] });
    const dd = box.innerHTML.match(/(\d{4}-\d{2}-\d{2}) 周./g) || [];
    return dd.length === 2 && dd[0].includes(snapshot.klines[CODE][10][0])
      && dd[1].includes(snapshot.klines[CODE][41][0]); })());

// 点空白处(空选) -> 选区与结果一起清掉
brushEv({ batch: [{ areas: [], selected: [{ dataIndex: [] }] }] });
check('空选/点空白: 结果块收起且 markArea 清空', box.style.display === 'none' && box.innerHTML === ''
  && (() => { const last = kc.options[kc.options.length - 1];
    return last.series && last.series[1] && last.series[1].markArea.data.length === 0; })());

// 结果块在图「下面」: 拖选时不会把画布往下挤(挤了选区就会飞)
const cardHtml = html.slice(html.indexOf('id="cardKline"'), html.indexOf('id="cardDrivers"'));
check('结果块排在图表之后(避免拖选时画布移位)',
  cardHtml.indexOf('id="mKline"') < cardHtml.indexOf('id="mzBoxBond"'));

ctx.toggleMeasure('bond');                    // 关闭
const cursorOff = kc.actions.filter(a => a.type === 'takeGlobalCursor').pop();
check('关掉后发 takeGlobalCursor(brushType=false) + brush clear, 结果块收起',
  cursorOff && cursorOff.brushOption.brushType === false
  && kc.actions.some(a => a.type === 'brush' && a.command === 'clear')
  && box.style.display === 'none' && !btn._cls.has('on') && hint.textContent === '');

/* ---------------- 5. 关闭后再拖不出数 / 换券 / 无数据 ---------------- */
brushEv({ batch: [{ areas: [{ coordRange: [I0, I1] }], selected: [{ dataIndex: idx }] }] });
check('开关关着时再拖不会算出结果(事件被忽略)', box.style.display === 'none');

ctx.toggleMeasure('bond');
brushEv({ batch: [{ areas: [{ coordRange: [I0, I1] }], selected: [{ dataIndex: idx }] }] });
const other = '113634';
ctx.openBond(other, { bond: mkBond(other, { kline_90d: snapshot.klines[other] }), navList: [other] });
const kc2 = instOf(insts, 'mKline');
check('换券后开关保持, 并在新图表上重新激活 brush(手机/桌面都能接着拖)',
  ctx.MEASURE.bond === true && ctx.MEASURE_DATA.bond.chart === kc2
  && kc2.actions.some(a => a.type === 'takeGlobalCursor' && a.brushOption.brushType === 'lineX'));
check('换券时清掉上一只券留下的结果块', box.style.display === 'none' && box.innerHTML === '');
const zc = kc2.handlers.brushSelected[0];
zc({ batch: [{ areas: [{ coordRange: [0, 0] }], selected: [{ dataIndex: [0] }] }] });
check('只点中一根K线也能出结果(days=1, 收益 0.00%, 不炸)',
  box.innerHTML.includes('1 个交易日') && box.innerHTML.includes('0.00%'));

ctx.resetMeasure();
check('关弹窗复位: 开关归位、结果块收起',
  ctx.MEASURE.bond === false && ctx.MEASURE_DATA.bond === null
  && !btn._cls.has('on') && box.style.display === 'none');

const noK = '113999';
ctx.openBond(noK, { bond: mkBond(noK), navList: [noK] });      // 没有 kline_90d
const kc3 = instOf(insts, 'mKline');
ctx.toggleMeasure('bond');
check('没有K线数据时点开关不炸、也不激活拖选',
  ctx.MEASURE_DATA.bond === null
  && !kc3.actions.some(a => a.type === 'takeGlobalCursor' && a.brushOption.brushType === 'lineX')
  && box.style.display === 'none');
ctx.resetMeasure();

/* ---------------- 6. 正股图: 每手口径随板块 ---------------- */
const srows = rows.slice(0, 40).map(r => [r[0], r[1], r[2], r[3], r[4], r[5], r[6]]);
ctx.openBond('113634', { bond: mkBond('113634', { kline_90d: snapshot.klines['113634'],
  stock_kline_90d: srows, stock_code: '600000' }), navList: ['113634'] });
check('正股图也接上了区间收益(1手=100股)',
  ctx.MEASURE_DATA.stock && ctx.MEASURE_DATA.stock.lot === 100
  && ctx.MEASURE_DATA.stock.volIdx === 1);
ctx.toggleMeasure('stock');
const sc = instOf(insts, 'mStockKline');
check('正股图的 brush 同样只绑价格格子', (() => {
  const o = sc.options.filter(x => x.series).pop();
  return !!o && o.brush.xAxisIndex === 0 && o.toolbox.show === false; })());
sc.handlers.brushSelected[0]({ batch: [{ areas: [{ coordRange: [2, 20] }], selected: [{ dataIndex: [] }] }] });
const sbox = el('mzBoxStock');
const sm = ctx.measureRange(srows, ctx.klineAmount(srows, 100).amt, 2, 20, 100);
check('正股结果块按"每手(100股)"写金额', sbox.innerHTML.includes('每手(100股)')
  && sbox.innerHTML.includes(Math.abs(Math.round(sm.diff * 100)) + ' 元'));
check('正股区间收益数字正确', sbox.innerHTML.includes(ctx.pct(sm.ret)),
  (sbox.innerHTML.match(/区间收益<\/div><div class="v[^"]*">([^<]+)/) || [])[1] || '(没找到)');
check('退回 dataIndex 的兜底路径也能用(选区没给 coordRange 时)',
  (() => { sc.handlers.brushSelected[0]({ batch: [{ areas: [{}],
      selected: [{ dataIndex: Array.from({ length: 5 }, (_, i) => i + 3) }] }] });
    const d = (sbox.innerHTML.match(/(\d{4}-\d{2}-\d{2}) 周./g) || []);
    return d.length === 2 && d[0].includes(srows[3][0]) && d[1].includes(srows[7][0]); })());

const kcb = mkBond('118888', { kline_90d: snapshot.klines['113634'], stock_kline_90d: srows,
  stock_code: '688111' });
ctx.openBond('118888', { bond: kcb, navList: ['118888'] });
check('科创板正股按 1手=200股(不是100)', ctx.MEASURE_DATA.stock.lot === 200);
ctx.resetMeasure();

/* ---------------- 7. 手机端(轴字号包装过的那条路)也没把 brush 弄丢 ---------------- */
const mob = loadPage(390);
const mcode = '123160';
mob.ctx.openBond(mcode, { bond: mkBond(mcode, { kline_90d: rows }), navList: [mcode] });
const mopt = instOf(mob.insts, 'mKline').options.filter(o => o.series).pop();
check('手机宽度下 K线图仍带 brush/toolbox 配置(坐标轴包装没吃掉选项)',
  !!mopt && !!mopt.brush && mopt.brush.xAxisIndex === 0 && mopt.toolbox.show === false
  && mopt.series.length === 3);

console.log(fails ? `\n✗ ${fails} 项未通过` : '\n✓ 全部通过');
process.exit(fails ? 1 : 0);
