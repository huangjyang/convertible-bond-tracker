/**
 * 模拟仓 -> 服务端镜像同步 —— 冒烟测试 (纯本地, 不打网络)
 *
 * 背景: 定时推送(push_holdings.py)读的是服务端那份 data/portfolio.json,
 *   而模拟仓的真源始终是浏览器 localStorage。所以前端每次 simPersist() 都要把
 *   一份副本 POST 给 app.py。这段同步代码有个**会毁数据**的失败模式, 是本测试的重点:
 *
 *   ⚠ 换一个浏览器打开看板时, localStorage 是空的。如果启动时无脑把"空仓"推上去,
 *     就会把服务端已有的持仓冲成空仓, 之后每半小时的推送都变成"空仓" —— 而且是静默的。
 *     所以: 本地空仓 + 服务端有记录 => 启动时**必须一个字节都不推**。
 *
 * 覆盖:
 *   1) 启动同步: 本地有仓 -> 推一次; 本地空仓但服务端有仓 -> 不推(clobber 防护)
 *   2) 本地空仓且服务端也空 -> 不推(推空仓没意义)
 *   3) simPersist() 触发防抖 POST, 连续多次改动只发一次, body 是完整模拟仓
 *   4) 服务端返回 404/非 JSON(典型: app.py 是没重启的旧代码) -> 明确失败, 不谎报成功
 *   5) 失败不影响本地账目(localStorage 照常写入)
 *
 * 用法: node tests/push_sync_smoke.mjs
 */
import fs from 'node:fs';
import path from 'node:path';

const html = fs.readFileSync(path.join(process.cwd(), 'index.html'), 'utf8');
const src = html.split('<script>').pop().split('</script>')[0];

const REAL_SET_TIMEOUT = globalThis.setTimeout;
const tick = () => new Promise((r) => REAL_SET_TIMEOUT(r, 0));
const settle = async (n = 8) => { for (let i = 0; i < n; i++) await tick(); };

let fails = 0;
const check = (name, cond, extra = '') => {
  console.log(`${cond ? '✓' : '✗'} ${name}${extra ? '  ' + extra : ''}`);
  if (!cond) fails++;
};

/* ---------------- 最小 DOM 桩 ---------------- */
function makeDocument() {
  const made = {};
  const el = (id) => {
    if (made[id]) return made[id];
    const e = {
      id, innerHTML: '', textContent: '', title: '', style: {}, dataset: {}, value: '',
      classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
      _thead: { innerHTML: '' }, _tbody: { innerHTML: '' },
      querySelector(sel) { return String(sel).includes('thead') ? this._thead : this._tbody; },
      querySelectorAll: () => [],
      addEventListener() {}, appendChild() {}, onclick: null,
    };
    made[id] = e;
    return e;
  };
  return {
    document: {
      getElementById: el, querySelector: (s) => el('sel' + s), querySelectorAll: () => [],
      createElement: () => el('tmp' + Math.random()), addEventListener() {},
    },
    el, made,
  };
}

const POS = (code, qty, buyPrice) => ({
  id: 1, code, name: '券' + code, buyDate: '2026-09-15', buyPrice,
  qty, fee: 1.0, dueDate: '2099-01-01',
});

function localSim(positions = [], closed = []) {
  return JSON.stringify({ init: 100000, cash: 100000, lot: 10, feeBp: 0.1, seq: 9,
                          positions, closed });
}

/**
 * 造一个跑起来的看板上下文。
 *  serverSim: GET /api/sim 要返回的镜像(null = 服务端还没有镜像)
 *  postReply: POST /api/sim 的返回(可设为 404/非 JSON 来模拟旧版 app.py)
 */
function boot({ local = null, serverSim = null, postReply = { ok: true, msg: '已同步' } } = {}) {
  const { document, el, made } = makeDocument();
  const calls = [];          // 所有 fetch 调用
  const store = {};
  if (local !== null) store['cbf_sim_v1'] = local;

  globalThis.localStorage = {
    _d: store,
    getItem(k) { return this._d[k] ?? null; },
    setItem(k, v) { this._d[k] = String(v); },
    removeItem(k) { delete this._d[k]; },
  };

  const localSetTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
  const timers = [];
  const clearTimeoutStub = (id) => { if (id && timers[id - 1]) timers[id - 1] = null; };
  const flush = () => {
    const pending = timers.splice(0, timers.length);
    for (const t of pending) if (t && t.fn) t.fn();
  };

  const fetchStub = async (url, init = {}) => {
    const method = (init.method || 'GET').toUpperCase();
    calls.push({ url: String(url), method, body: init.body });
    if (String(url).includes('/api/sim')) {
      if (method === 'POST') {
        if (postReply === '404') {
          return { ok: false, status: 404, json: async () => { throw new Error('not json'); },
                   text: async () => 'not found' };
        }
        return { ok: true, status: 200, json: async () => postReply };
      }
      return {
        ok: true, status: 200,
        json: async () => (serverSim
          ? { ok: true, exists: true, revision: 3, saved_at: '2026-09-22T07:40:00',
              portfolio: JSON.parse(serverSim) }
          : { ok: true, exists: false }),
      };
    }
    // 其它接口(app.py 的看板数据)一律给个够用的空壳, 让启动流程走得下去
    const u = String(url);
    if (u.includes('/api/dates')) return { ok: true, status: 200, json: async () => [] };
    if (u.includes('/api/bonds')) return { ok: true, status: 200, json: async () => [] };
    if (u.includes('/api/refresh')) {
      return { ok: true, status: 200, json: async () => ({ status: 'skipped', scan: 'skipped' }) };
    }
    return { ok: true, status: 200, json: async () => ({}), text: async () => '{}' };
  };

  const script = new Function(
    'document', 'window', 'echarts', 'fetch', 'setTimeout', 'clearTimeout',
    'location', 'alert', 'console',
    // 注意: SIM_SYNC_LAST 在实现里是**整体重新赋值**的, 直接把它 return 出来会拿到
    // 导出那一刻的过期引用 —— 所以这里用 getter 取当前值。
    src + '\n;return {simState, simPersist, simBootSync, simServerSyncNow, simSyncText,'
        + ' simServerSync, getSync: () => SIM_SYNC_LAST};'
  );
  const ctx = script(
    document,
    { innerWidth: 1400, addEventListener() {}, dispatchEvent() {} },
    { init: () => ({ setOption() {}, dispose() {}, resize() {} }), getInstanceByUrl: () => null },
    fetchStub, localSetTimeout, clearTimeoutStub,
    { href: 'http://127.0.0.1:0' }, () => {}, console);

  const simCalls = () => calls.filter((c) => c.url.includes('/api/sim'));
  return { ctx, calls, simCalls, el, made, flush, store };
}

/* ================= 1. 启动同步 ================= */
console.log('\n[1] 启动同步 simBootSync()');
{
  // 本地有仓 + 服务端有仓 -> 应该推(让服务端跟上本机)
  const b = boot({
    local: localSim([POS('113701', 10, 100)]),
    serverSim: localSim([POS('113701', 10, 100)]),
  });
  await settle();
  const posts = b.simCalls().filter((c) => c.method === 'POST');
  check('本地有仓 -> 启动时推一次', posts.length === 1, `POST 次数=${posts.length}`);
  if (posts.length === 1) {
    const sent = JSON.parse(posts[0].body);
    check('推送的 body 是完整模拟仓对象', Array.isArray(sent.positions) &&
      sent.positions.length === 1 && sent.positions[0].code === '113701', posts[0].body);
    check('body 含现金/初始资金(推送算总资产要用)',
      sent.cash === 100000 && sent.init === 100000, JSON.stringify(sent).slice(0, 120));
  }
}

/* ================= 2. clobber 防护(最关键) ================= */
console.log('\n[2] ⚠ 空仓不许覆盖服务端持仓');
{
  const b = boot({
    local: null,                                   // 换了个浏览器: localStorage 是空的
    serverSim: localSim([POS('113701', 10, 100), POS('111024', 10, 200)]),
  });
  await settle();
  const posts = b.simCalls().filter((c) => c.method === 'POST');
  check('本地空仓 + 服务端有 2 笔 -> 一个字节都不 POST', posts.length === 0,
    `竟然 POST 了 ${posts.length} 次: ${JSON.stringify(posts[0]?.body || '').slice(0, 80)}`);
  check('给出了"没动它(避免冲掉)"的提示', /避免冲掉|没动它/.test(b.ctx.getSync().msg || ''),
    b.ctx.getSync().msg);
  check('提示里说清了服务端有几笔',
    /2 笔/.test(b.ctx.getSync().msg || ''), b.ctx.getSync().msg);

  // 服务端也空 -> 同样不推(推空仓毫无意义)
  const b2 = boot({ local: null, serverSim: null });
  await settle();
  check('本地与服务端都空 -> 不 POST',
    b2.simCalls().filter((c) => c.method === 'POST').length === 0);

  // 本地显式清仓(有 closed 记录) -> 应视为"有内容", 允许推
  const b3 = boot({
    local: localSim([], [{ id: 5, code: '113686', qty: 10, pnl: 10 }]),
    serverSim: localSim([POS('113701', 10, 100)]),
  });
  await settle();
  check('本地无持仓但有已平仓记录 -> 仍算有内容, 允许推',
    b3.simCalls().filter((c) => c.method === 'POST').length === 1);
}

/* ================= 3. simPersist 防抖 ================= */
console.log('\n[3] simPersist() 触发防抖同步');
{
  const b = boot({ local: localSim([POS('113701', 10, 100)]), serverSim: null });
  await settle();
  const before = b.simCalls().filter((c) => c.method === 'POST').length;

  // 连续三次改动(模拟 +/+/+ 三下) -> 只发一次
  b.ctx.simPersist();
  b.ctx.simPersist();
  b.ctx.simPersist();
  await settle(3);
  const mid = b.simCalls().filter((c) => c.method === 'POST').length;
  check('防抖: 改动后不会立刻发(等 1.5s)', mid === before, `POST=${mid} before=${before}`);

  b.flush();                     // 触发那个被 set 的定时器
  await settle();
  const after = b.simCalls().filter((c) => c.method === 'POST').length;
  check('防抖: 三次改动只发一次', after === before + 1, `POST=${after} before=${before}`);

  // 本地账目照常写进 localStorage(同步失败也不该丢账)
  const saved = JSON.parse(b.store['cbf_sim_v1']);
  check('本地 localStorage 已写入', Array.isArray(saved.positions) && saved.positions.length === 1,
    JSON.stringify(saved).slice(0, 80));
}

/* ================= 4. 旧版 app.py(404) ================= */
console.log('\n[4] 服务端是没有 /api/sim 的旧代码时');
{
  const b = boot({ local: localSim([POS('113701', 10, 100)]), postReply: '404' });
  await settle();
  check('404 时标记为失败, 不谎报成功', b.ctx.getSync().ok === false,
    JSON.stringify(b.ctx.getSync()));
  check('状态文案是"同步失败"', /同步失败/.test(b.ctx.simSyncText()), b.ctx.simSyncText());
  check('404 的提示点明"旧进程要重启 app.py"(本项目高频坑)',
    /重启 app\.py/.test(b.ctx.getSync().msg || ''), b.ctx.getSync().msg);
  check('本地账目仍然完好',
    JSON.parse(b.store['cbf_sim_v1']).positions.length === 1);

  // 手工同步返回 false, 供按钮上的提示用
  const ok = await b.ctx.simServerSyncNow();
  check('手工同步失败时返回 false', ok === false, ok);
}

/* ================= 5. 明确的手工同步 ================= */
console.log('\n[5] 手工「立即同步」');
{
  const b = boot({ local: localSim([POS('113701', 10, 100)]), serverSim: null });
  await settle();
  const before = b.simCalls().filter((c) => c.method === 'POST').length;
  const ok = await b.ctx.simServerSyncNow();
  check('手工同步成功返回 true', ok === true, ok);
  check('手工同步真的发了 POST',
    b.simCalls().filter((c) => c.method === 'POST').length === before + 1);
  check('成功文案是"已同步"', /已同步/.test(b.ctx.simSyncText()), b.ctx.simSyncText());
}

/* ================= 6. 空仓时手工同步也要推(用户明确要求) ================= */
console.log('\n[6] 空仓时手工同步(用户主动点按钮 = 明确意图)');
{
  const b = boot({ local: localSim([]), serverSim: localSim([POS('113701', 10, 100)]) });
  await settle();
  // 启动时被拦住了
  check('启动时被 clobber 防护拦住',
    b.simCalls().filter((c) => c.method === 'POST').length === 0);
  // 但用户主动点「立即同步」应该照做(配合"清空"这类操作)
  const ok = await b.ctx.simServerSyncNow();
  check('手工点按钮时允许推空仓', ok === true &&
    b.simCalls().filter((c) => c.method === 'POST').length === 1);
}

console.log('');
if (fails) {
  console.log(`✗ ${fails} 项失败`);
  process.exit(1);
}
console.log('✓ 全部通过');
