/* 面板主逻辑：一份界面，两种传输（AstrBot bridge / 直连 7627）。 */
(function () {
  const L = window.MiniLib;
  // AstrBot 把 bridge SDK 注入在 </body> 之前，晚于本脚本执行，
  // 所以这里不能在加载时取值，必须等真正要发请求时再探测。
  let BRIDGE = null;
  function bridge() {
    if (!BRIDGE) BRIDGE = window.AstrBotPluginView || window.AstrBotPluginPage || null;
    return BRIDGE;
  }
  async function waitBridge(ms) {
    const end = Date.now() + ms;
    while (!bridge() && Date.now() < end) {
      await new Promise((r) => setTimeout(r, 30));
    }
    return bridge();
  }
  const state = { status: null, tasks: null, config: null, prob: null, probPreview: null, tab: "overview", busy: new Set(), decisions: {}, logOpen: new Set() };
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  // ---------------------------------------------------------------- 传输
  function parseError(payload, status) {
    const msg = (payload && payload.message) || ("HTTP " + status);
    const m = /^([A-Z_]+):\s*([\s\S]*)$/.exec(msg);
    const err = new Error(m ? m[2] : msg);
    err.code = m ? m[1] : "E_INTERNAL";
    err.raw = msg;
    return err;
  }

  function unwrap(raw) {
    if (raw && typeof raw === "object" && raw.status === "error") throw parseError(raw, 0);
    if (raw && typeof raw === "object" && raw.status === "ok") return raw.data;
    return raw;
  }

  function normError(e) {
    if (e && e.code) return e;
    return parseError({ message: (e && e.message) || String(e) }, 0);
  }

  async function api(method, name, payload) {
    const B = bridge();
    if (B) {
      try {
        const raw = method === "GET"
          ? await B.apiGet("page/" + name, payload || undefined)
          : await B.apiPost("page/" + name, payload || {});
        return unwrap(raw);
      } catch (e) {
        throw normError(e);
      }
    }
    const qs = method === "GET" && payload ? "?" + new URLSearchParams(payload) : "";
    const res = await fetch("/api/" + name + qs, {
      method,
      credentials: "same-origin",
      headers: method === "GET" ? {} : { "Content-Type": "application/json" },
      body: method === "GET" ? undefined : JSON.stringify(payload || {}),
    });
    let body = null;
    try { body = await res.json(); } catch (_) { body = null; }
    if (!res.ok) throw parseError(body, res.status);
    return unwrap(body);
  }

  // ---------------------------------------------------------------- 提示
  function toast(msg, kind) {
    const box = $("#toasts");
    const el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(() => el.remove(), kind === "err" ? 8000 : 4000);
  }

  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function withBusy(btn, fn) {
    if (!btn) return fn();
    if (btn.dataset.busy === "1") return;
    btn.dataset.busy = "1";
    btn.disabled = true;
    btn.classList.add("loading");
    try { return await fn(); }
    finally {
      btn.dataset.busy = "";
      btn.disabled = false;
      btn.classList.remove("loading");
    }
  }

  // ---------------------------------------------------------------- 数据
  async function refreshAll() {
    const [status, tasks, config, schema] = await Promise.all([
      api("GET", "status"), api("GET", "tasks"), api("GET", "config"), api("GET", "schema"),
    ]);
    state.status = status; state.tasks = tasks; state.config = config; state.schema = schema;
    await loadDecisions();
    renderAll();
  }

  // 判定记录：按会话分组，只取最近 20 条（本地环形缓冲，不联网不花 token）
  async function loadDecisions() {
    try {
      const r = await api("GET", "decisions", { limit: 20 });
      const byUmo = {};
      (r.items || []).forEach((d) => {
        const k = d.umo || "";
        (byUmo[k] = byUmo[k] || []).push(d);
      });
      state.decisions = byUmo;
    } catch (_) { /* 旧版后端没有这个接口时静默跳过 */ }
  }

  async function refreshStatus() {
    try { state.status = await api("GET", "status"); renderHeader(); }
    catch (e) { if (e.code === "E_AUTH") showLogin(); }
  }

  async function loadProbability() {
    const umo = ($("#prob-umo") || {}).value || "";
    state.probPreview = null; // 拉真实数据时清掉预览
    updatePreviewUI();
    state.prob = await api("GET", "probability", umo ? { umo } : {});
    renderProbability();
    renderProbTune();
    renderProbSleep();
    renderProbAway();
    renderProbReturnSoon();
    loadRules();
  }

  // ---------------------------------------------------------------- 渲染：头部
  function renderHeader() {
    const s = state.status;
    const dot = $("#status-dot");
    const text = $("#status-text");
    if (!dot || !s) return;
    dot.className = "dot";
    if (!s.plugin.enable) { dot.classList.add("err"); text.textContent = "插件未启用"; }
    else if (!s.plugin.started) { dot.classList.add("warn"); text.textContent = "运行器未启动"; }
    else { dot.classList.add("ok"); text.textContent = "运行中 · " + s.plugin.version; }
    $("#panel-url").textContent = s.panel.running ? s.panel.url : (s.panel.error || "面板未启动");
  }

  function renderAll() {
    renderHeader();
    renderSessionSelects();
    renderOverview();
    renderTasks();
    renderConfig();
    if (state.tab === "prob") loadProbability().catch(showErr);
  }

  function showErr(err) { toast(err.message || String(err), "err"); }

  // ---------------------------------------------------------------- 渲染：概览
  function sessionList() {
    return (state.tasks && state.tasks.sessions) || [];
  }

  // 多个会话时，概览/概率页都靠这个下拉框决定看哪一个，不再猜「第一个」
  function renderSessionSelects() {
    const list = sessionList();
    [$("#ov-umo"), $("#prob-umo")].forEach((sel) => {
      if (!sel) return;
      const cur = sel.value;
      sel.innerHTML = list.length
        ? list.map((it) => {
          const ev = it.event ? ` · ${it.event.label}` : "";
          return `<option value="${esc(it.umo)}">${esc(L.shortUmo(it.umo))}${esc(ev)}</option>`;
        }).join("")
        : `<option value="">（还没有会话）</option>`;
      if (cur && list.some((it) => it.umo === cur)) sel.value = cur;
    });
    const n = list.length;
    if ($("#ov-count")) {
      $("#ov-count").textContent = n ? `${n} 个会话，下面动作作用于选中的这个` : "还没有会话";
    }
    if ($("#prob-hint")) $("#prob-hint").textContent = n ? "概率按选中会话的当前状态计算" : "还没有会话";
  }

  function renderOverview() {
    const s = state.status;
    if (!s) return;
    // 不再放「在场」：那是每个会话各自的状态，多个会话时没法用一个值表达
    const cards = [
      ["会话", s.summary.sessions, "已配置监听"],
      ["已排程", s.summary.scheduled, "有倒计时的任务"],
      ["未回复", s.summary.unanswered, "累计未回复条数"],
      ["生效事件", s.summary.events, "识别到的活动数"],
      ["规则", s.plugin.rules, "事件规则条数"],
    ];
    $("#ov-cards").innerHTML = cards.map(([k, v, sub]) => `
      <div class="stat"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div><div class="s">${esc(sub)}</div></div>
    `).join("");
  }

  // ---------------------------------------------------------------- 渲染：任务
  function renderTasks() {
    const t = state.tasks;
    if (!t) return;
    // 重渲染前把折叠状态记下来，否则 12 秒一刷新就会被合上
    $$("#task-list details[data-log-umo]").forEach((el) => {
      if (el.open) state.logOpen.add(el.dataset.logUmo);
      else state.logOpen.delete(el.dataset.logUmo);
    });
    $("#task-count").textContent = `${t.counts.total} 个会话 · ${t.counts.scheduled} 个在排程`;
    if (!t.sessions.length) {
      $("#task-list").innerHTML = `<div class="card muted">还没有配置会话。上面输入完整的会话标识后点「添加」。</div>`;
      return;
    }
    $("#task-list").innerHTML = t.sessions.map((it) => {
      const phase = { scheduled: ["已排程", "ok"], paused: ["已暂停", "warn"], due: ["到期", "warn"], unscheduled: ["未排程", ""] }[it.phase] || ["未知", ""];
      const nextClock = it.next_at ? L.fmtClock(it.next_at) : "";
      // 事件还没结束、而排程点落在事件结束之后 → 这段空档显示「等待事件结束」
      const evExpMs = it.event ? Number(it.event.expires_at || 0) * 1000 : 0;
      const evActive = evExpMs > Date.now();
      const waitingEvent = evActive && it.next_at && it.next_at * 1000 > evExpMs;
      const next = waitingEvent
        ? "等待事件结束"
        : (it.next_in_seconds !== null && it.next_in_seconds !== undefined)
          ? `${L.fmtDuration(it.next_in_seconds)}后`
          : (nextClock || "未排程");
      // 未回复显示成「已连续未回复 / 设定上限」，上限取排程里那个「最大未回复次数」
      const cfgFs = (state.config && state.config.friend_settings) || {};
      const cfgMax = (cfgFs.schedule_settings || {}).max_unanswered_times;
      const limit = (it.unanswered_limit !== undefined && it.unanswered_limit !== null)
        ? it.unanswered_limit
        : (cfgMax === undefined || cfgMax === null ? 3 : cfgMax);
      // 当前事件：还没结束就显示剩余分钟，已经过期就当作没有（后端要等下条消息才清）
      let event = "无";
      let eventTitle = "现在没有生效的事件";
      if (evActive) {
        const leftMin = Math.max(0, Math.round((evExpMs - Date.now()) / 60000));
        event = `${it.event.label} · 剩 ${leftMin} 分`;
        eventTitle = `加成 ${it.event.boost ?? "-"}×，已用 ${it.event.boost_used}/${it.event.boost_budget}；到点后失效`;
      } else if (it.event) {
        event = `无（${it.event.label} 已结束）`;
        eventTitle = "事件已过期，还没被清理；下一条消息进来会清掉";
      }
      // 在场：没记录时默认就是「在身边」（离开要靠关键词命中才会记）
      const awayKw = (((state.config || {}).custom_engine || {}).away_keywords) || [];
      const togetherOn = !!(it.together && it.together.active);
      const awayOn = !!(it.away && it.away.active);
      const away = it.away
        ? (it.away.active ? "离开：" + (it.away.reason || "—") : (togetherOn ? "在一起" : "在身边"))
        : (togetherOn ? "在一起" : "在身边");
      const awayTitle = awayKw.length
        ? "命中离开关键词会显示「离开：原因」，回来会清掉；命中「一起/牵手」这类同行词会记为「在一起」"
        : "「在场状态」里还没填离开关键词，所以永远显示在身边";
      const sleep = (() => {
        const s = it.sleep;
        if (!s || !s.state) return "—";
        if (s.state === "sleeping") return `睡眠免打扰 · 到 ${L.fmtClock(s.wake_at)}`;
        if (s.state === "waiting") {
          return s.remind_at ? `待睡 · ${L.fmtClock(s.remind_at)} 提醒` : "待睡等待";
        }
        if (s.state === "reminded") {
          const label = s.last_reminded_window === "nap" ? "午睡已提醒"
            : s.last_reminded_window === "custom" ? "等会再睡 · 已提醒"
              : "今晚已提醒";
          return `${label} · ${L.fmtClock(s.last_reminded_at)}`;
        }
        return "—";
      })();
      // 最近判定：判错了能看见原因，也能一键撤回来
      const logs = (state.decisions || {})[it.umo] || [];
      const kindName = { event: "事件", away: "离开", return: "回来", sleep: "睡觉", wait: "待睡", stop: "结束" };
      const logRows = logs.length
        ? logs.map((d) => {
            const badge = d.action === "llm_yes" ? ["模型确认", "ok"]
              : d.action === "applied" ? ["已记录", "ok"]
                : d.verdict === "reject" ? ["否决", "err"]
                  : ["拿不准", "warn"];
            const tail = d.undo_state === "available"
              ? `<button class="btn sm ghost" data-act="task" data-op="undo" data-id="${d.id}">撤销</button>`
              : `<span class="muted">${d.undo_state === "undone" ? "已撤销" : (d.undo_state === "stale" ? "状态已变" : "")}</span>`;
            return `<div class="log-row">
              <span class="muted">${L.fmtClock(d.ts)}</span>
              <span class="badge ${badge[1]}">${badge[0]}</span>
              <span class="log-kw">${esc(kindName[d.kind] || d.kind)}·${esc(d.kw)}</span>
              <span class="muted log-why" title="${esc(d.text || "")}">${esc(d.reason || "")}</span>
              ${tail}
            </div>`;
          }).join("")
        : `<div class="muted" style="font-size:12px">还没有判定记录</div>`;
      const logBlock = `
        <details class="task-log" data-log-umo="${esc(it.umo)}"${state.logOpen.has(it.umo) ? " open" : ""}>
          <summary>最近判定 ${logs.length ? `<span class="badge warn">${logs.length}</span>` : ""}</summary>
          <div class="stack gap8">${logRows}</div>
        </details>`;
      return `
      <div class="task">
        <div class="task-head">
          <div class="task-name" title="${esc(it.umo)}">${esc(it.umo)}</div>
          <span class="badge ${phase[1]}">${phase[0]}</span>
        </div>
        <div class="kv">
          <div><div class="k">下次触发</div><div class="v" title="${esc(nextClock ? "预计 " + nextClock : "还没有排程")}">${esc(next)}</div></div>
          <div><div class="k">未回复</div><div class="v" title="已连续未回复 / 设定的上限，达到上限会暂停主动消息">${esc(it.unanswered)}/${esc(limit)}</div></div>
          <div><div class="k">当前事件</div><div class="v" title="${esc(eventTitle)}">${esc(event)}</div></div>
          <div><div class="k">在场</div><div class="v" title="${esc(awayTitle)}">${esc(away)}</div></div>
          <div><div class="k">睡眠</div><div class="v">${esc(sleep)}</div></div>
        </div>
        <div class="task-actions">
          <button class="btn sm ghost" data-act="task" data-op="preview" data-umo="${esc(it.umo)}">生成预览</button>
          <button class="btn sm" data-act="task" data-op="send" data-umo="${esc(it.umo)}">立即发送</button>
          <button class="btn sm ghost" data-act="task" data-op="resched" data-umo="${esc(it.umo)}">重新排程</button>
          <button class="btn sm ghost" data-act="task" data-op="reset" data-umo="${esc(it.umo)}">重置计数</button>
          <button class="btn sm ghost" data-act="task" data-op="clear-event" data-umo="${esc(it.umo)}">清除事件</button>
          <button class="btn sm ghost" data-act="task" data-op="clear-sleep" data-umo="${esc(it.umo)}">清除睡眠</button>
          ${togetherOn
            ? `<button class="btn sm ghost" data-act="task" data-op="clear-together" data-umo="${esc(it.umo)}">取消在一起</button>
               <button class="btn sm ghost" data-act="task" data-op="away" data-umo="${esc(it.umo)}">设为不在身边</button>`
            : `<button class="btn sm ghost" data-act="task" data-op="together" data-umo="${esc(it.umo)}">设为在一起</button>`}
          ${!togetherOn && awayOn
            ? `<button class="btn sm ghost" data-act="task" data-op="clear-away" data-umo="${esc(it.umo)}">回到身边</button>`
            : ""}
        </div>
        ${logBlock}
      </div>`;
    }).join("");
  }

  // ---------------------------------------------------------------- 渲染：概率
  // 两张图各画各的：
  //   无事件：横轴 0 .. 最长间隔
  //   有事件：横轴 0 .. 事件剩余有效期（比上图短，所以事件会把触发往前压）
  // 纵轴封顶：四张柱形图共用「可调节」区里的那个百分比。
  // 以前用 Math.max(...bars) 取最高柱当满格，改权重/间隔时柱子高度几乎不变，
  // 看不出分布变化；现在按固定上限换算，超出的截顶并加一条亮边。
  const DEFAULT_CEILING_PCT = 20;
  // 记住上一次有效的纵轴上限：重渲染的瞬间输入框可能还没挂上，
  // 这时如果退回默认 20，纵轴一变柱子就会"跳一下"。
  let lastCeilingPct = null;

  function chartCeilingPct() {
    const el = document.querySelector('[data-section="web_panel"][data-path="chart_ceiling_pct"]');
    let v = el ? parseFloat(el.value) : NaN;
    if (!Number.isFinite(v) || v <= 0) {
      const raw = L.deepGet(sectionRoot("web_panel"), "chart_ceiling_pct", undefined);
      v = (typeof raw === "number" && raw > 0) ? raw : (lastCeilingPct || DEFAULT_CEILING_PCT);
    }
    const pct = Math.min(100, Math.max(1, v));
    lastCeilingPct = pct;
    return pct;
  }

  function chartCeilingValue() {
    return Math.max(0.0001, chartCeilingPct() / 100);
  }

  function barHeight(v, ceiling) {
    return Math.min(100, (v / ceiling) * 100);
  }

  function drawBars(chartEl, axisEl, spec, opts) {
    const bars = spec.bars || [];
    const count = spec.count || bars.length;
    const ceiling = chartCeilingValue();
    const now = opts.now;
    const start = (opts.start_minutes || 0) * 60; // 采样起点（lo），让柱子从 lo 对齐
    // 数据没变就整块跳过：避免保存配置后重新 innerHTML 造成柱子"跳一下"
    const sig = [count, ceiling, Math.round(now / 60), start,
      opts.kind, opts.scheduled_at || 0, (opts.quantiles || {}).p10 || 0,
      (opts.quantiles || {}).p90 || 0, bars.join(",")].join("|");
    if (chartEl.dataset.sig === sig) return;
    chartEl.dataset.sig = sig;
    chartEl.innerHTML = Array.from({ length: count }).map((_, i) => {
      const v = bars[i] || 0;
      const t0 = now + start + i * spec.bucket_minutes * 60;
      const t1 = t0 + spec.bucket_minutes * 60;
      const over = v > ceiling;
      const title = `${L.fmtClock(t0)} – ${L.fmtClock(t1)} · ${L.pct(v)}`
        + (over ? " · 超出纵轴上限，已截顶" : "");
      return `<div class="bar-wrap" data-i="${i}" title="${esc(title)}">`
        + `<div class="bar ${opts.kind}${over ? " clip" : ""}" style="height:${barHeight(v, ceiling)}%"></div></div>`;
    }).join("");

    if (opts.quantiles && !opts.noBand) {
      const span = count * spec.bucket_minutes * 60;
      const left = Math.max(0, Math.min(100, ((opts.quantiles.p10 - (now + start)) / span) * 100));
      const right = Math.max(0, Math.min(100, ((opts.quantiles.p90 - (now + start)) / span) * 100));
      const band = document.createElement("div");
      band.className = "range-band";
      band.style.left = left + "%";
      band.style.width = Math.max(1, right - left) + "%";
      chartEl.appendChild(band);
    }
    if (opts.scheduled_at) {
      const span = count * spec.bucket_minutes * 60;
      const at = ((opts.scheduled_at - (now + start)) / span) * 100;
      if (at >= 0 && at <= 100) {
        const pin = document.createElement("div");
        pin.className = "pin";
        pin.style.left = at + "%";
        pin.dataset.label = "已排程";
        chartEl.appendChild(pin);
      }
    }

    const ticks = L.axisTicks(now + start, spec.bucket_minutes, count, 4);
    axisEl.innerHTML = ticks.map((t) => `<span>+${t.minutes}m ${L.fmtClock(t.ts)}</span>`).join("");
  }

  function renderProbability() {
    // 调参时的实时预览优先；没有预览就用后端真实数据
    const p = state.probPreview || state.prob;
    if (!p) return;
    const previewing = !!state.probPreview;
    const meta = p.meta;
    const base = p.base;
    const ev = p.event;

    // 图一：无事件，覆盖 最短–最长
    drawBars($("#chart-base"), $("#axis-base"), base, {
      kind: "base",
      now: meta.now,
      start_minutes: meta.lo,
      quantiles: base.quantiles,
      scheduled_at: meta.scheduled_at,
    });
    $("#prob-base-meta").textContent = `${meta.lo}–${meta.hi} 分钟 · 每格 ${base.bucket_minutes} 分钟 · 满格 ${chartCeilingPct()}%`
      + (previewing ? " · 预览（未保存）" : "");
    $("#stats-base").innerHTML = `
      <div class="row wrap gap8">
        <span class="badge">期望间隔 ${base.mean_minutes} 分钟</span>
        <span class="muted">80% 落在 ${L.fmtClock(base.quantiles.p10)} – ${L.fmtClock(base.quantiles.p90)}</span>
        <span class="muted">中位 ${L.fmtClock(base.quantiles.p50)}</span>
      </div>
      <div class="muted" style="font-size:12px">
        区间内概率合计 ${L.pct(base.total_mass)}
        · 早段 ${meta.bands.early[0]}-${meta.bands.early[1]} · 中段 ${meta.bands.mid[0]}-${meta.bands.mid[1]} · 尾段 ${meta.bands.tail[0]}-${meta.bands.tail[1]}
      </div>`;

    // 图二：事件生效，只画到事件剩余有效期
    const chartEv = $("#chart-event");
    if (!ev) {
      chartEv.innerHTML = `<div class="muted" style="padding:20px">当前没有生效的事件。识别到「在吃饭」「在路上」这类事件后，这里会显示同一段时间的分布。</div>`;
      $("#axis-event").innerHTML = "";
      $("#prob-event-meta").textContent = "无";
      $("#stats-event").innerHTML = `<div class="muted" style="font-size:12px">没有事件时，触发概率就是上面那张图。</div>`;
    } else {
      drawBars(chartEv, $("#axis-event"), ev, {
        kind: "event",
        now: meta.now,
        start_minutes: meta.lo,
        quantiles: ev.quantiles,
        scheduled_at: meta.scheduled_at,
      });
      const used = ev.ttl_minutes - ev.remaining_minutes;
      $("#prob-event-meta").textContent = `${ev.label} · 剩余 ${ev.remaining_minutes}/${ev.ttl_minutes} 分钟 · 满格 ${chartCeilingPct()}%`;
      $("#stats-event").innerHTML = `
        <div class="row wrap gap8">
          <span class="badge">${esc(ev.label)}</span>
          ${ev.boost ? `<span class="muted">加成 ${Number(ev.boost).toFixed(2)}×</span>` : ""}
          <span class="muted">已过 ${Math.max(0, used)} 分钟</span>
          <span class="muted">期望间隔 ${ev.mean_minutes} 分钟</span>
          ${ev.boost_left ? "" : `<span class="muted">加成已用完 ${ev.boost_used}/${ev.boost_budget}，这条曲线与上图相同</span>`}
        </div>
        <div class="muted" style="font-size:12px">
          ${ev.mass_in_window >= 0.999
            ? "采样已压在这段时间内：她会在事件结束前开口"
            : `这段时间内开口的概率 ${L.pct(ev.mass_in_window)}，剩下 ${L.pct(1 - ev.mass_in_window)} 落在事件过期之后`}
          · 80% 落在 ${L.fmtClock(ev.quantiles.p10)} – ${L.fmtClock(ev.quantiles.p90)}
        </div>`;
    }

    renderSleepCharts(p);
  }

  // 睡眠两张图：晚上睡觉（提醒窗口）+ 白天补觉（静音时长）
  function renderSleepCharts(p) {
    const sl = p.sleep;
    if (!sl) return;
    const now = p.meta.now;

    // 晚睡提醒：窗口 + 三段权重决定的落点分布
    const night = sl.night;
    drawSleepBars($("#chart-night"), $("#axis-night"), night, "night", 0);
    $("#prob-night-meta").textContent = `窗口 ${night.window_start}–${night.window_end} · ${night.span_minutes} 分钟 · 夜晚起 ${night.night_start} · 满格 ${chartCeilingPct()}%`;

    // 午睡提醒：同一套算法，权重独立；关掉时把这张图变灰
    const nap = sl.nap;
    drawSleepBars($("#chart-nap"), $("#axis-nap"), nap, "nap", 0);
    const napCard = $("#nap-chart-card");
    if (napCard) napCard.classList.toggle("dim", !nap.remind_enabled);
    $("#prob-nap-meta").textContent = nap.remind_enabled
      ? `窗口 ${nap.window_start}–${nap.window_end} · ${nap.span_minutes} 分钟 · 静音 ${nap.quiet_hours} 小时 · 满格 ${chartCeilingPct()}%`
      : `午睡提醒已关 · 静音 ${nap.quiet_hours} 小时 · 起床 ${sl.wake_time}`;
  }

  // 睡眠图专用：横轴不是「未来时间」而是「时长」，刻度用分钟数
  function drawSleepBars(chartEl, axisEl, spec, kind, _now) {
    const bars = spec.bars || [];
    const count = spec.count || bars.length;
    const ceiling = chartCeilingValue();
    const sig = [kind, count, spec.bucket_minutes, ceiling, bars.join(",")].join("|");
    if (chartEl.dataset.sig === sig) return;
    chartEl.dataset.sig = sig;
    chartEl.innerHTML = Array.from({ length: count }).map((_, i) => {
      const v = bars[i] || 0;
      const t0 = i * spec.bucket_minutes;
      const t1 = t0 + spec.bucket_minutes;
      const over = v > ceiling;
      const title = `+${t0}–${t1} 分钟 · ${L.pct(v)}` + (over ? " · 超出纵轴上限，已截顶" : "");
      return `<div class="bar-wrap" data-i="${i}" title="${esc(title)}">`
        + `<div class="bar ${kind}${over ? " clip" : ""}" style="height:${barHeight(v, ceiling)}%"></div></div>`;
    }).join("");
    const ticks = L.axisTicks(0, spec.bucket_minutes, count, 4);
    axisEl.innerHTML = ticks.map((t) => `<span>+${t.minutes}m</span>`).join("");
  }

  // ---------------------------------------------------------------- 实时预览
  // 调排程/权重时，把「还没保存」的表单值发给后端算一遍分布：只画图，不写配置。
  let previewTimer = null;
  let previewSeq = 0;
  let previewLastAt = 0;
  let lastPreviewErrAt = 0;
  const PREVIEW_MIN_GAP = 100; // 拖动时的最小间隔：够跟手，又不会把后端刷爆（实测单次 ~3ms）

  function probUmo() {
    const el = $("#prob-umo");
    const v = el ? el.value : "";
    if (v) return v;
    const first = sessionList()[0];
    return first ? first.umo : "";
  }

  // 节流而不是防抖：拖动时每 130ms 就跟一次，松手后再补最后一次，
  // 这样柱子是「跟着滑杆长」的，不是松手才跳。
  function schedulePreview() {
    if (!state.config) return;
    if (previewTimer) return; // 已有排队的，合并成一次
    const gap = Date.now() - previewLastAt;
    if (gap >= PREVIEW_MIN_GAP) {
      previewLastAt = Date.now();
      runPreview();
      return;
    }
    previewTimer = setTimeout(() => {
      previewTimer = null;
      previewLastAt = Date.now();
      runPreview();
    }, PREVIEW_MIN_GAP - gap);
  }

  // 预览用的 patch：排程/加权 与 睡眠两处未保存的值都带上，
  // 这样拖完权重再拖睡眠滑块，不会把前一次的预览结果冲掉。
  function previewPatch() {
    const patch = collectTunePatch().patch || {};
    SLEEP_CONTAINERS.forEach((sel) => {
      const box = $(sel);
      if (!box) return;
      const r = collectPatch(box);
      Object.keys(r.patch).forEach((sec) => {
        patch[sec] = patch[sec] || {};
        Object.assign(patch[sec], r.patch[sec]);
      });
    });
    return patch;
  }

  async function runPreview() {
    const patch = previewPatch();
    if (!Object.keys(patch).length) return;
    const seq = ++previewSeq;
    try {
      const r = await api("POST", "probability/preview", { patch, umo: probUmo() });
      if (seq !== previewSeq) return; // 只认最后一次，避免请求乱序把图打回去
      state.probPreview = r;
      renderProbability();
      updatePreviewUI();
    } catch (err) {
      // 预览失败不打断编辑，但要让用户看见，而不是静默什么都不动
      if (seq !== previewSeq) return;
      const now = Date.now();
      if (now - lastPreviewErrAt > 15000) {
        lastPreviewErrAt = now;
        toast("预览失败：" + ((err && err.message) || "接口无响应"), "err");
      }
    }
  }

  // 睡眠区的滑块：防抖 120ms，拖的时候不用每像素都请求
  let sleepPreviewTimer = null;

  function scheduleSleepPreview() {
    if (!state.config) return;
    clearTimeout(sleepPreviewTimer);
    sleepPreviewTimer = setTimeout(() => {
      sleepPreviewTimer = null;
      runPreview();
    }, 120);
  }

  function updatePreviewUI() {
    const on = !!state.probPreview;
    ["#prob-preview-badge", "#sleep-preview-badge"].forEach((sel) => {
      const el = $(sel);
      if (el) el.classList.toggle("hidden", !on);
    });
    ["#prob-preview-reset", "#sleep-preview-reset"].forEach((sel) => {
      const el = $(sel);
      if (el) el.classList.toggle("hidden", !on);
    });
  }

  function resetPreview() {
    clearTimeout(previewTimer);
    clearTimeout(sleepPreviewTimer);
    previewTimer = null;
    sleepPreviewTimer = null;
    state.probPreview = null;
    renderProbTune();  // 表单值退回已保存的配置
    renderProbSleep();
    renderProbability();
    updatePreviewUI();
    toast("已还原到已保存的设置", "ok");
  }

  // 概率页的「调节」区：排程、加权采样分成两张卡，复用 schema 渲染，可编辑保存
  const TUNE_GROUPS = [
    { box: "#prob-sched", section: "friend_settings", paths: [
      "schedule_settings.min_interval_minutes", "schedule_settings.max_interval_minutes",
      "schedule_settings.max_unanswered_times",
    ]},
    { box: "#prob-weights", section: "custom_engine", paths: [
      "enable_weighted", "early_weight", "early_weight_event", "mid_weight",
      "tail_head_weight", "tail_floor_weight", "tail_decay_power",
    ]},
    { box: "#prob-weights", section: "web_panel", paths: ["chart_ceiling_pct"] },
  ];

  // 两个容器里同名控件的选择器（排程 / 加权分开渲染，取值要同时搜）
  function tuneSelector(section, path) {
    const part = `[data-section="${section}"][data-path="${path}"]`;
    return `#prob-sched ${part}, #prob-weights ${part}`;
  }

  // 两个容器合成一个 patch（预览和保存都用它）
  function collectTunePatch() {
    const patch = {};
    const clamped = [];
    ["#prob-sched", "#prob-weights"].forEach((sel) => {
      const box = $(sel);
      if (!box) return;
      const r = collectPatch(box);
      Object.keys(r.patch).forEach((sec) => {
        patch[sec] = patch[sec] || {};
        Object.assign(patch[sec], r.patch[sec]);
      });
      clamped.push(...r.clamped);
    });
    return { patch, clamped };
  }

  function renderProbTune() {
    if (!state.schema) return;
    const html = {};
    TUNE_GROUPS.forEach((g) => {
      html[g.box] = (html[g.box] || "") + g.paths.map((p) => renderField(g.section, p)).join("");
    });
    Object.keys(html).forEach((sel) => {
      const box = $(sel);
      if (!box) return;
      box.innerHTML = html[sel];
      // 数字框补边界
      $$("[data-section][data-path]", box).forEach((el) => {
        const r = limitFor(el.dataset.section, el.dataset.path);
        if (r && el.type === "number") { el.min = r[0]; el.max = r[1]; }
      });
    });
    renderPresets();
  }

  // 四个聊天频率档位（只调排程 + 加权采样），第 5 个「自定义」在手动改参数时自动切过去
  const PRESETS = {
    light:    { label: "偶尔聊", min: 60, max: 480, early: 0.6, mid: 1.0,  head: 1.3,  floor: 0.8,  decay: 0.4 },
    medium:   { label: "默认",   min: 5,  max: 240, early: 1.0, mid: 1.15, head: 1.15, floor: 0.6,  decay: 0.3 },
    slightly: { label: "稍微有点粘人", min: 5, max: 150, early: 1.5, mid: 1.2, head: 1.0, floor: 0.45, decay: 0.25 },
    clingy:   { label: "粘人",   min: 3,  max: 60,  early: 2.0, mid: 1.3,  head: 0.8,  floor: 0.3,  decay: 0.2 },
  };

  function presetValue() {
    // 读当前控件的值，用于判断匹配哪个档位。优先读数字框（有 data-type 的那个），
    // 因为滑杆字段是 range + number 双控件，range 框没有 data-type。
    const get = (section, path, isFloat, def) => {
      const els = $$(tuneSelector(section, path));
      let el = null;
      for (const e of els) { if (e.dataset && e.dataset.type) { el = e; break; } }
      if (!el) el = els[0];
      if (!el) return def;
      if (el.type === "checkbox") return el.checked;
      return isFloat ? parseFloat(el.value || "0") : parseInt(el.value || "0", 10);
    };
    return {
      min: get("friend_settings", "schedule_settings.min_interval_minutes", false, 5),
      max: get("friend_settings", "schedule_settings.max_interval_minutes", false, 240),
      early: get("custom_engine", "early_weight", true, 1.0),
      mid: get("custom_engine", "mid_weight", true, 1.15),
      head: get("custom_engine", "tail_head_weight", true, 1.15),
      floor: get("custom_engine", "tail_floor_weight", true, 0.6),
      decay: get("custom_engine", "tail_decay_power", true, 0.3),
    };
  }

  function matchPreset(v) {
    for (const key of ["light", "medium", "slightly", "clingy"]) {
      const p = PRESETS[key];
      if (v.min === p.min && v.max === p.max &&
          Math.abs(v.early - p.early) < 1e-6 && Math.abs(v.mid - p.mid) < 1e-6 &&
          Math.abs(v.head - p.head) < 1e-6 && Math.abs(v.floor - p.floor) < 1e-6 &&
          Math.abs(v.decay - p.decay) < 1e-6) {
        return key;
      }
    }
    return "custom";
  }

  function setField(section, path, value) {
    const el = $(tuneSelector(section, path));
    if (!el) return;
    el.value = value;
    // 滑杆联动
    const sync = $(`[data-sync="${el.id}"]`);
    if (sync) sync.value = value;
    if (el.dataset.sync) {
      const num = document.getElementById(el.dataset.sync);
      if (num) num.value = value;
    }
  }

  function applyPreset(key) {
    if (key === "custom") {
      highlightPreset("custom");
      return;
    }
    const p = PRESETS[key];
    if (!p) return;
    setField("friend_settings", "schedule_settings.min_interval_minutes", p.min);
    setField("friend_settings", "schedule_settings.max_interval_minutes", p.max);
    setField("custom_engine", "early_weight", p.early);
    setField("custom_engine", "mid_weight", p.mid);
    setField("custom_engine", "tail_head_weight", p.head);
    setField("custom_engine", "tail_floor_weight", p.floor);
    setField("custom_engine", "tail_decay_power", p.decay);
    highlightPreset(key);
    schedulePreview();
  }

  function highlightPreset(key) {
    $$("#preset-row .preset").forEach((b) => {
      b.classList.toggle("active", b.dataset.preset === key);
    });
    const hint = $("#preset-hint");
    if (hint) {
      const desc = { light: "少打扰，间隔长、拖后开口", medium: "平衡的默认节奏", slightly: "比默认黏一点，又不至于太频繁", clingy: "频繁、更快开口，更粘人", custom: "你手动调过的参数" }[key] || "";
      hint.textContent = desc;
    }
  }

  function renderPresets() {
    const v = presetValue();
    highlightPreset(matchPreset(v));
  }

  // 睡眠区：右边三块（晚睡 / 午睡 / 等会再睡），左边两张图，底部只剩作息与词表
  const SLEEP_NIGHT_PATHS = [
    "enable_sleep_reminder",
    "remind_window_start", "remind_window_end",
    "night_weight_early", "night_weight_mid", "night_weight_tail",
  ];
  const SLEEP_NAP_PATHS = [
    "nap_remind_enabled", "nap_window_start", "nap_window_end",
    "nap_weight_early", "nap_weight_mid", "nap_weight_tail",
  ];
  const SLEEP_WAIT_PATHS = [
    "wait_remind_min_minutes", "wait_remind_max_minutes",
    "wait_weight_early", "wait_weight_mid", "wait_weight_tail",
  ];
  const SLEEP_REST_PATHS = [
    "wake_time", "night_start_time", "sleep_quiet_hours",
    "sleep_keywords", "wait_keywords", "wake_keywords",
  ];
  const SLEEP_CONTAINERS = [
    "#sleep-night-controls", "#sleep-nap-controls",
    "#sleep-wait-controls", "#prob-sleep",
  ];

  function renderProbSleep() {
    if (!state.schema) return;
    [["#sleep-night-controls", SLEEP_NIGHT_PATHS],
      ["#sleep-nap-controls", SLEEP_NAP_PATHS],
      ["#sleep-wait-controls", SLEEP_WAIT_PATHS],
      ["#prob-sleep", SLEEP_REST_PATHS]].forEach(([sel, paths]) => {
      const box = $(sel);
      if (!box) return;
      box.innerHTML = paths.map((path) => renderField("custom_engine", path)).join("");
      $$("[data-section][data-path]", box).forEach((el) => {
        const r = limitFor(el.dataset.section, el.dataset.path);
        if (r && el.type === "number") { el.min = r[0]; el.max = r[1]; }
      });
    });
    applyConditions(); // 午睡关掉时，它的时间和权重直接隐藏
  }

  // 概率页的「在场状态」配置区
  function renderProbAway() {
    const box = $("#prob-away");
    if (!box || !state.schema) return;
    const paths = ["away_ttl_hours", "back_window_minutes", "away_keywords", "return_keywords"];
    box.innerHTML = paths.map((path) => renderField("custom_engine", path)).join("");
    $$("[data-section][data-path]", box).forEach((el) => {
      const r = limitFor(el.dataset.section, el.dataset.path);
      if (r && el.type === "number") { el.min = r[0]; el.max = r[1]; }
    });
  }

  // 概率页的「快回来了」配置区
  function renderProbReturnSoon() {
    const box = $("#prob-returnsoon");
    if (!box || !state.schema) return;
    const paths = ["enable_return_soon", "return_soon_keywords", "return_soon_boost", "return_soon_shrink"];
    box.innerHTML = paths.map((path) => renderField("custom_engine", path)).join("");
    $$("[data-section][data-path]", box).forEach((el) => {
      const r = limitFor(el.dataset.section, el.dataset.path);
      if (r && el.type === "number") { el.min = r[0]; el.max = r[1]; }
    });
  }

  // ---------------------------------------------------------------- 渲染：配置
  // 文案、滑杆范围、可选项全部来自 _conf_schema.json，也就是 AstrBot 插件配置页看到的那一份。
  const SECTIONS = [
    {
      section: "friend_settings", title: "基础", note: "保存后立即生效",
      paths: ["enable", "session_list", "enable_auto_start", "auto_start_delay_minutes", "proactive_prompt"],
    },
    {
      section: "friend_settings", title: "上下文注入", note: "决定她开口前能看到多少前文",
      paths: [
        "context_settings.use_conversation_history", "context_settings.use_platform_history",
        "context_settings.platform_history_count", "context_settings.include_bot_messages",
        "context_settings.platform_history_prompt",
      ],
    },
    {
      section: "friend_settings", title: "省 token",
      note: "只影响主动消息，正常回复不走这里；保存后立即生效",
      paths: [
        "context_settings.include_history_images",
        "context_settings.include_history_think",
        "context_settings.enable_context_window",
        "context_settings.context_window_count",
        "context_settings.context_window_hours",
        "context_settings.context_window_min_messages",
      ],
    },
    {
      section: "friend_settings", title: "分段发送", note: "保存后立即生效",
      paths: [
        "segmented_reply_settings.enable", "segmented_reply_settings.enable_smart_split",
        "segmented_reply_settings.split_regex", "segmented_reply_settings.no_split_around",
        "segmented_reply_settings.max_segments", "segmented_reply_settings.min_segment_length",
        "segmented_reply_settings.balanced_split",
        "segmented_reply_settings.balanced_split_ratio_min",
        "segmented_reply_settings.balanced_split_ratio_max",
        "segmented_reply_settings.trim_edge_blank",
        "segmented_reply_settings.enable_content_cleanup",
        "segmented_reply_settings.content_cleanup_rule",
        "segmented_reply_settings.delay_strategy",
        "segmented_reply_settings.random_min", "segmented_reply_settings.random_max",
        "segmented_reply_settings.linear_base", "segmented_reply_settings.linear_factor",
        "segmented_reply_settings.log_base", "segmented_reply_settings.log_factor",
        "segmented_reply_settings.fixed_delay",
      ],
    },
    {
      section: "custom_engine", title: "事件感知", note: "改动后需重载插件",
      paths: ["enable_event", "enable_llm_event", "llm_event_provider"],
    },
    {
      section: "custom_engine", title: "识别判定", note: "改完立即生效",
      paths: ["enable_utterance_judge", "judge_strict", "enable_gray_review",
              "gray_review_per_hour", "gray_review_per_day", "decision_log_size",
              "bot_nicknames"],
    },
    {
      section: "web_panel", title: "面板", note: "改动后需重载插件",
      paths: ["enable", "host", "port", "password", "allow_embed"],
    },
  ];

  // 极端情况下（接口没返回）的兜底边界
  const FALLBACK_LIMITS = {
    "friend_settings.schedule_settings.min_interval_minutes": [1, 2880],
    "friend_settings.schedule_settings.max_interval_minutes": [1, 2880],
    "web_panel.port": [1024, 65535],
  };

  function fieldMeta(section, path) {
    const f = state.schema && state.schema.fields;
    return (f && f[section + "." + path]) || null;
  }

  function limitFor(section, path) {
    const meta = fieldMeta(section, path);
    if (meta && meta.slider) return [meta.slider.min, meta.slider.max];
    return FALLBACK_LIMITS[section + "." + path] || null;
  }

  function sectionRoot(section) {
    const c = state.config || {};
    return c[section] || {};
  }

  function currentValue(section, path) {
    const v = L.deepGet(sectionRoot(section), path, undefined);
    if (v !== undefined && v !== null) return v;
    const meta = fieldMeta(section, path);
    return meta ? meta.default : "";
  }

  function fieldId(section, path) {
    return "f-" + section + "-" + String(path).replace(/\./g, "-");
  }

  function renderField(section, path) {
    const meta = fieldMeta(section, path);
    if (!meta) return "";
    const value = currentValue(section, path);
    const desc = meta.description || path;
    const common = `data-section="${section}" data-path="${path}"`;
    const hints = [];
    if (meta.hint) hints.push(meta.hint);
    let inner = "";
    let wide = false;

    if (meta.type === "bool") {
      return `<div class="field" data-field="${section}.${path}"><label class="switch">
        <input type="checkbox" ${common} ${value ? "checked" : ""}><span>${esc(desc)}</span>
      </label>${hints.length ? `<span class="hint" title="${esc(hints.join(" · "))}">${esc(hints.join(" · "))}</span>` : ""}</div>`;
    }

    if (meta.slider) {
      const step = meta.slider.step || (meta.type === "int" ? 1 : 0.05);
      const id = fieldId(section, path);
      const fmtMin = meta.slider.min;
      const fmtMax = meta.slider.max;
      // 范围直接标在滑块两端，比下面单独一行"范围 x–y"更直观
      inner = `<div class="slider">
        <span class="range-end">${esc(fmtMin)}</span>
        <input type="range" class="range" min="${meta.slider.min}" max="${meta.slider.max}" step="${step}"
               value="${esc(value)}" data-sync="${id}">
        <span class="range-end">${esc(fmtMax)}</span>
        <input class="input num" id="${id}" type="number" min="${meta.slider.min}" max="${meta.slider.max}"
               step="${step}" ${common} data-type="${meta.type}" value="${esc(value)}">
      </div>`;
    } else if (meta.type === "int" || meta.type === "float") {
      inner = `<input class="input" type="number" step="${meta.type === "int" ? 1 : 0.05}"
        ${common} data-type="${meta.type}" value="${esc(value)}">`;
    } else if (meta.type === "list") {
      wide = true;
      inner = `<textarea class="input" ${common} data-type="list" rows="3"
        placeholder="每行一个">${esc((Array.isArray(value) ? value : []).join("\n"))}</textarea>`;
    } else if (meta.type === "text") {
      wide = true;
      inner = `<textarea class="input" ${common} rows="5">${esc(value === undefined || value === null ? "" : value)}</textarea>`;
    } else if (meta.options && meta.options.length) {
      inner = `<select class="input" ${common}>${meta.options.map((o, i) =>
        `<option value="${esc(o)}" ${String(value) === String(o) ? "selected" : ""}>${esc((meta.labels || [])[i] || o)}</option>`).join("")}</select>`;
    } else if (path === "password") {
      // 接口只回 has_password，不会把明文带回来
      const has = !!(sectionRoot(section) || {}).has_password;
      inner = `<input class="input" type="password" ${common}
        placeholder="${has ? "已设置，留空表示不修改" : "留空表示不验证"}">`;
    } else {
      inner = `<input class="input" ${common} value="${esc(value === undefined || value === null ? "" : value)}">`;
    }

    return `<div class="field ${wide ? "span2" : ""}" data-field="${section}.${path}">
      <label>${esc(desc)}</label>${inner}
      ${hints.length ? `<span class="hint" title="${esc(hints.join(" · "))}">${esc(hints.join(" · "))}</span>` : ""}
    </div>`;
  }

  // 条件显示：与 AstrBot 配置页一致，开关没打开的相关项直接收起来
  function applyConditions() {
    $$("[data-field]").forEach((box) => {
      const [section, path] = box.dataset.field.split(/\.(.+)/);
      const meta = fieldMeta(section, path);
      if (!meta || !meta.condition) return;
      const prefix = path.includes(".") ? path.slice(0, path.lastIndexOf(".") + 1) : "";
      const ok = Object.keys(meta.condition).every((key) => {
        const el = $(`[data-section="${section}"][data-path="${prefix}${key}"]`);
        if (!el) return true;
        const actual = el.type === "checkbox" ? el.checked : el.value;
        return String(actual) === String(meta.condition[key]);
      });
      box.classList.toggle("hidden", !ok);
    });
  }

  function renderConfig() {
    const c = state.config;
    if (!c) return;
    const used = {};
    const cards = SECTIONS.map((g) => {
      const body = g.paths.map((path) => {
        used[g.section + "." + path] = true;
        return renderField(g.section, path);
      }).join("");
      return `<div class="card">
        <div class="card-head"><h3>${esc(g.title)}</h3><span class="muted">${esc(g.note)}</span></div>
        <div class="form-grid">${body}</div>
      </div>`;
    });

    // 没在上面的分组里出现的字段，统一放到「其他」，避免以后加配置项时漏掉
    // 但已经移到概率页的字段要跳过，不在配置页重复出现
    const MOVED_TO_PROB = new Set([
      "friend_settings.schedule_settings.min_interval_minutes",
      "friend_settings.schedule_settings.max_interval_minutes",
      "friend_settings.schedule_settings.max_unanswered_times",
      "custom_engine.enable_weighted", "custom_engine.early_weight",
      "custom_engine.early_weight_event", "custom_engine.mid_weight",
      "custom_engine.tail_head_weight", "custom_engine.tail_floor_weight",
      "custom_engine.tail_decay_power",
      "custom_engine.enable_sleep_reminder",
      "custom_engine.remind_window_start",
      "custom_engine.remind_window_end", "custom_engine.wake_time",
      "custom_engine.nap_remind_enabled", "custom_engine.nap_window_start",
      "custom_engine.nap_window_end", "custom_engine.night_start_time",
      "custom_engine.sleep_quiet_hours", "custom_engine.sleep_keywords",
      "custom_engine.wait_keywords", "custom_engine.wake_keywords",
      "custom_engine.wait_remind_min_minutes", "custom_engine.wait_remind_max_minutes",
      "custom_engine.wait_weight_early", "custom_engine.wait_weight_mid",
      "custom_engine.wait_weight_tail",
      "custom_engine.night_weight_early", "custom_engine.night_weight_mid",
      "custom_engine.night_weight_tail",
      "custom_engine.nap_weight_early", "custom_engine.nap_weight_mid",
      "custom_engine.nap_weight_tail",
      "custom_engine.away_ttl_hours", "custom_engine.back_window_minutes",
      "custom_engine.away_keywords", "custom_engine.return_keywords",
      "custom_engine.enable_return_soon", "custom_engine.return_soon_keywords",
      "custom_engine.return_soon_boost", "custom_engine.return_soon_shrink",
      // 纵轴封顶已经放在概率页的「可调节」区，配置页不再重复出现
      "web_panel.chart_ceiling_pct",
    ]);
    const rest = [];
    const fields = (state.schema && state.schema.fields) || {};
    Object.keys(fields).forEach((full) => {
      if (used[full] || MOVED_TO_PROB.has(full)) return;
      const idx = full.indexOf(".");
      rest.push(renderField(full.slice(0, idx), full.slice(idx + 1)));
    });
    if (rest.length) {
      cards.push(`<div class="card">
        <div class="card-head"><h3>其他</h3><span class="muted">配置里新增但面板还没归类的项</span></div>
        <div class="form-grid">${rest.join("")}</div>
      </div>`);
    }

    cards.push(`
      <div class="card">
        <div class="card-head"><h3>内置提示词</h3><span class="muted">只在 Web 面板可见；改动立即生效</span></div>
        <div class="form-grid">
          <div class="field span2">
            <div class="field-label-row"><label>语义识别提示词</label><button class="btn ghost sm" data-act="overrides-reset-one" data-field="classify_sys_prompt">重置</button></div>
            <textarea class="input" id="ov-classify" rows="8" placeholder="支持 {{catalog}} 占位符（事件类别表）"></textarea>
            <span class="hint">关键词没命中时，让模型判断「正在做/要去做」哪件事的提示词。支持 {{catalog}}。</span>
          </div>
          <div class="field span2">
            <div class="field-label-row"><label>提醒睡觉话术</label><button class="btn ghost sm" data-act="overrides-reset-one" data-field="reminder_hint">重置</button></div>
            <textarea class="input" id="ov-reminder" rows="4" placeholder="支持 {{time}} 占位符（当前时间）"></textarea>
            <span class="hint">触发「该睡了」提醒时给模型的场景提示。支持 {{time}}。</span>
          </div>
        </div>
        <div class="row gap8" style="margin-top:12px">
          <button class="btn sm" data-act="overrides-save">保存提示词</button>
        </div>
      </div>`);

    $("#config-root").innerHTML = cards.join("");

    $$("[data-section][data-path]").forEach((el) => {
      const r = limitFor(el.dataset.section, el.dataset.path);
      if (r && el.type === "number") { el.min = r[0]; el.max = r[1]; }
    });
    applyConditions();
    loadOverrides();
  }

  async function loadOverrides() {
    try {
      const data = await api("GET", "overrides");
      state.overrides = data;
      const ov = data.overrides || {};
      const dfl = data.defaults || {};
      // 未自定义时显示内置默认内容（可读可改），保存后即为覆盖值
      $("#ov-classify").value = ov.classify_sys_prompt || dfl.classify_sys_prompt || "";
      $("#ov-reminder").value = ov.reminder_hint || dfl.reminder_hint || "";
    } catch (e) {
      // 接口不可用时静默，不影响其它配置
    }
  }

  async function saveOverrides(btn) {
    await withBusy(btn, async () => {
      const classify = $("#ov-classify").value;
      const reminder = $("#ov-reminder").value;
      await api("POST", "overrides/save", {
        overrides: { classify_sys_prompt: classify, reminder_hint: reminder },
      });
      toast("提示词已保存，立即生效", "ok");
    }).catch(showErr);
  }

  function resetOneOverride(field) {
    const dfl = (state.overrides && state.overrides.defaults) || {};
    if (field === "classify_sys_prompt") {
      $("#ov-classify").value = dfl.classify_sys_prompt || "";
    } else if (field === "reminder_hint") {
      $("#ov-reminder").value = dfl.reminder_hint || "";
    }
  }

  async function loadRules() {
    const source = $("#rules-source");
    const list = $("#rules-list");
    if (!source || !list) return; // 事件规则已移到概率页，配置页不再渲染
    try {
      const data = await api("GET", "rules");
      state.rules = data;
      source.textContent = data.source === "config"
        ? "来源：插件配置（保存后需重载）"
        : "来源：event_rules.json（保存后热生效）";
      list.innerHTML = (data.rules || []).map((r, i) => ruleRow(r, i)).join("");
    } catch (e) {
      list.innerHTML = `<div class="muted">${esc(e.message)}</div>`;
    }
  }

  function ruleRow(r, i) {
    const kw = (v) => Array.isArray(v) ? v.join(",") : String(v || "");
    return `<div class="rule" data-i="${i}">
      <div class="field"><label>标识</label><input class="input sm" data-rule="name" value="${esc(r.name || "")}"></div>
      <div class="field"><label>显示名</label><input class="input sm" data-rule="label" value="${esc(r.label || "")}"></div>
      <div class="field"><label>有效分钟</label><input class="input sm" type="number" min="5" max="240" data-rule="ttl_minutes" value="${esc(r.ttl_minutes || 45)}"></div>
      <div class="field"><label>加成</label><input class="input sm" type="number" min="1" max="10" step="0.1" data-rule="boost" value="${esc(r.boost || 3)}"></div>
      <div class="field"><label>开始词</label><input class="input sm" data-rule="keywords" value="${esc(kw(r.keywords))}"></div>
      <div class="field"><label>结束词</label><input class="input sm" data-rule="stop_keywords" value="${esc(kw(r.stop_keywords))}"></div>
      <button class="btn sm danger icon" data-act="rule-del" data-i="${i}" title="删除这条规则" aria-label="删除">
        <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path fill="currentColor" d="M9 3h6l1 2h4a1 1 0 0 1 0 2h-1v12a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V7H4a1 1 0 0 1 0-2h4l1-2Zm-2 4v12h10V7H7Zm2 2h2v8H9V9Zm4 0h2v8h-2V9Z"/></svg>
      </button>
      <div class="field rule-hint"><label>接话话术（hint，可选）</label><textarea class="input sm" data-rule="hint" rows="2" placeholder="留空则只用事实陈述，由模型自己判断">${esc(r.hint || "")}</textarea></div>
    </div>`;
  }

  // 滑杆拖动 / 数字框输入，两边保持同步
  document.addEventListener("input", (ev) => {
    const t = ev.target;
    if (!t) return;
    if (t.dataset && t.dataset.sync) {
      const box = document.getElementById(t.dataset.sync);
      if (box) { box.value = t.value; box.dispatchEvent(new Event("change", { bubbles: true })); }
      if (box && box.dataset && box.dataset.path === "chart_ceiling_pct") {
        renderProbability();
        return;
      }
      // 拖滑杆时补发预览：滑杆本身没有 data-path，得顺着 data-sync 找回数字框
      if (box && box.closest && (box.closest("#prob-sched") || box.closest("#prob-weights"))) {
        highlightPreset("custom");
        schedulePreview();
      } else if (box && box.closest && SLEEP_CONTAINERS.some((sel) => box.closest(sel))) {
        scheduleSleepPreview();
      }
      return;
    }
    if (t.id) {
      const range = $(`[data-sync="${t.id}"]`);
      if (range) range.value = Number(t.value || 0);
    }
    // 改纵轴封顶：立刻重画四张图，不用等保存
    if (t.dataset && t.dataset.path === "chart_ceiling_pct") {
      renderProbability();
      return;
    }
    // 睡眠区：改权重/窗口/开关都实时预览（只画图，不写配置）
    if (t.closest && SLEEP_CONTAINERS.some((sel) => t.closest(sel)) && t.dataset && t.dataset.path) {
      scheduleSleepPreview();
      return;
    }
    // 手动改排程/加权采样参数 → 自动切到「自定义」档位，并实时预览曲线
    if (t.closest && (t.closest("#prob-sched") || t.closest("#prob-weights")) && t.dataset && t.dataset.path) {
      highlightPreset("custom");
      schedulePreview();
    }
  });

  // ---------------------------------------------------------------- 动作
  async function quickAction(op, umo, btn) {
    await withBusy(btn, async () => {
      if (op === "reload-plugin") return doReload();
      if (!umo) throw new Error("还没有配置会话");
      if (op === "preview") {
        const r = await api("POST", "preview", { umo });
        toast(r.text ? `预览：${r.text}` : "生成为空", r.text ? "ok" : "err");
      } else if (op === "fire") {
        const r = await api("POST", "fire", { umo });
        toast(r.sent ? `已发送，未回复计数 ${r.unanswered}` : "本次没有发出（可能被静默或上限拦下）", r.sent ? "ok" : "err");
      } else if (op === "resched") {
        const r = await api("POST", "reschedule", { umo });
        toast(`已重排：${L.fmtDuration(r.next_in_seconds)}后`, "ok");
      }
      await refreshAll();
    }).catch(showErr);
  }

  async function taskAction(op, umo, btn) {
    await withBusy(btn, async () => {
      if (op === "preview") return quickAction("preview", umo, null);
      if (op === "send") {
        const r = await api("POST", "fire", { umo });
        toast(r.sent ? `已发送，未回复 ${r.unanswered}` : "没有发出（静默时段或达到上限）", r.sent ? "ok" : "err");
      } else if (op === "resched") {
        const r = await api("POST", "reschedule", { umo });
        toast(`已重排：${L.fmtDuration(r.next_in_seconds)}后`, "ok");
      } else if (op === "reset") {
        state.tasks = await api("POST", "tasks/action", { action: "reset_unanswered", umo });
        toast("已重置未回复计数", "ok");
      } else if (op === "clear-event") {
        state.tasks = await api("POST", "tasks/action", { action: "clear_event", umo });
        toast("已清除当前事件", "ok");
      } else if (op === "clear-sleep") {
        state.tasks = await api("POST", "tasks/action", { action: "clear_sleep", umo });
        toast("已清除睡眠状态", "ok");
      } else if (op === "together") {
        state.tasks = await api("POST", "tasks/action", { action: "mark_together", umo });
        toast("已设为在一起：她知道你在旁边", "ok");
      } else if (op === "clear-together") {
        state.tasks = await api("POST", "tasks/action", { action: "clear_together", umo });
        toast("已取消在一起", "ok");
      } else if (op === "away") {
        state.tasks = await api("POST", "tasks/action", { action: "mark_away", umo });
        toast("已设为不在身边：她不会再说「就在你旁边」", "ok");
      } else if (op === "clear-away") {
        state.tasks = await api("POST", "tasks/action", { action: "clear_away", umo });
        toast("已回到身边", "ok");
      } else if (op === "undo") {
        const id = Number(btn && btn.dataset ? btn.dataset.id : 0);
        state.tasks = await api("POST", "tasks/action", { action: "undo_decision", id });
        await loadDecisions();
        toast("已撤销这条判定", "ok");
      } else if (op === "remove") {
        if (!confirm("确定移除这个会话？")) return;
        state.tasks = await api("POST", "tasks/action", { action: "remove", umo });
        toast("已移除", "ok");
      }
      renderTasks(); renderOverview();
      await refreshStatus();
    }).catch(showErr);
  }

  async function doReload() {
    await api("POST", "reload", {});
    toast("已提交重载，正在等待恢复…");
    for (let i = 0; i < 25; i++) {
      await new Promise((r) => setTimeout(r, 800));
      try {
        state.status = await api("GET", "status");
        renderHeader();
        toast("重载完成", "ok");
        await refreshAll();
        return;
      } catch (_) { /* 重载期间连接会断，继续等 */ }
    }
    toast("20 秒内未恢复，请到 AstrBot 插件列表手动重载", "err");
  }

  // ---------------------------------------------------------------- 配置保存
  function fieldLabel(el) {
    const box = el.closest(".field");
    const label = box ? box.querySelector("label") : null;
    return (label ? label.textContent : el.dataset.path || "").trim();
  }

  function collectPatch(root) {
    const patch = {};
    const clamped = [];
    $$("[data-section][data-path]", root).forEach((el) => {
      const section = el.dataset.section;
      const path = el.dataset.path;
      let value;
      if (el.type === "checkbox") value = el.checked;
      else if (el.dataset.type === "int") value = parseInt(el.value || "0", 10);
      else if (el.dataset.type === "float") value = parseFloat(el.value || "0");
      else if (el.dataset.type === "list") value = el.value.split("\n").map((s) => s.trim()).filter(Boolean);
      else value = el.value;
      if (path === "password" && !value) return; // 留空表示不修改

      const r = limitFor(section, path);
      if (r && typeof value === "number" && !isNaN(value)) {
        const fixed = Math.min(r[1], Math.max(r[0], value));
        if (fixed !== value) {
          value = fixed;
          el.value = fixed;
          clamped.push(`${fieldLabel(el)} → ${fixed}`);
        }
      }
      patch[section] = patch[section] || {};
      L.deepSet(patch[section], path, value);
    });
    return { patch, clamped };
  }

  function scheduleError(patch) {
    const cur = (state.config && state.config.friend_settings &&
      state.config.friend_settings.schedule_settings) || {};
    const p = ((patch.friend_settings || {}).schedule_settings) || {};
    const lo = p.min_interval_minutes !== undefined ? p.min_interval_minutes : cur.min_interval_minutes;
    const hi = p.max_interval_minutes !== undefined ? p.max_interval_minutes : cur.max_interval_minutes;
    if (lo === undefined || hi === undefined) return "";
    if (Number(lo) > Number(hi)) return `最短间隔（${lo}）不能大于最长间隔（${hi}）`;
    return "";
  }

  async function saveConfig(btn, thenReload) {
    await withBusy(btn, async () => {
      const { patch, clamped } = collectPatch();
      const bad = scheduleError(patch);
      if (bad) { toast(bad, "err"); return; }
      // 规则可能是在「概率」页改的，这里也一起判断，别默默丢掉
      const { rules, fixed: rulesFixed } = collectRules();
      const rulesChanged = rules.length > 0 && rulesDirty(rules);
      const r = await api("POST", "config/save", { patch });
      if (!r.saved && !rulesChanged) { toast("没有改动"); return; }
      const fixed = clamped.concat(r.clamped || []);
      if (fixed.length) toast("已按范围修正：" + fixed.join("、"), "err");
      if (r.saved) toast(`已保存 ${r.changed.length} 项`, "ok");
      let rulesNeedReload = false;
      if (rulesChanged) {
        const rr = await api("POST", "rules/save", { rules });
        rulesNeedReload = !!rr.need_reload;
        if (rulesFixed) toast(`${rulesFixed} 条规则的有效分钟/加成超出范围，已按边界保存`, "err");
        toast(rulesNeedReload ? "事件规则已保存，重载后生效" : "事件规则已保存", "ok");
        await loadRules();
      }
      if (thenReload || r.need_reload || rulesNeedReload) {
        if (thenReload) await doReload();
        else toast("有改动需要重载插件才会生效，可点「保存并重载」");
      }
      state.config = await api("GET", "config");
      renderConfig();
      await refreshStatus();
    }).catch(showErr);
  }

  // 从界面读出所有事件规则；fixed 记录有几条的有效分钟/加成被夹回范围
  function collectRules() {
    let fixed = 0;
    const rules = $$(".rule").map((row) => {
      const get = (k) => { const el = row.querySelector(`[data-rule="${k}"]`); return el ? el.value : ""; };
      const split = (v) => String(v).split(",").map((s) => s.trim()).filter(Boolean);
      const ttlRaw = parseInt(get("ttl_minutes") || "45", 10) || 45;
      const boostRaw = parseFloat(get("boost") || "3") || 3;
      const ttl = Math.min(240, Math.max(5, ttlRaw));
      const boost = Math.min(10, Math.max(1, boostRaw));
      if (ttl !== ttlRaw || boost !== boostRaw) fixed += 1;
      return {
        __template_key: "event_rule",
        name: get("name").trim(),
        label: get("label").trim(),
        ttl_minutes: ttl,
        boost: boost,
        boost_budget: 1,
        keywords: split(get("keywords")),
        stop_keywords: split(get("stop_keywords")),
        hint: get("hint").trim(),
      };
    });
    return { rules, fixed };
  }

  // 只比表单能改的那几个字段，判断规则是不是真的动过（避免每次点保存都触发重载）
  function rulesSignature(r) {
    const arr = (v) => Array.isArray(v) ? v : String(v || "").split(",");
    return JSON.stringify([
      String(r.name || "").trim(),
      String(r.label || "").trim(),
      Number(r.ttl_minutes || 45),
      Number(r.boost || 3),
      arr(r.keywords).map((s) => String(s).trim()).filter(Boolean).join("|"),
      arr(r.stop_keywords).map((s) => String(s).trim()).filter(Boolean).join("|"),
      String(r.hint || "").trim(),
    ]);
  }

  function rulesDirty(rules) {
    const base = ((state.rules && state.rules.rules) || []);
    if (base.length !== rules.length) return true;
    return rules.map(rulesSignature).join("\n") !== base.map(rulesSignature).join("\n");
  }

  async function saveRules(btn) {
    await withBusy(btn, async () => {
      const { rules, fixed } = collectRules();
      const r = await api("POST", "rules/save", { rules });
      if (fixed) toast(`${fixed} 条规则的有效分钟/加成超出范围，已按边界保存`, "err");
      toast(r.need_reload ? "规则已保存，重载后生效" : "规则已保存并热加载", "ok");
      if (r.need_reload) toast("点「保存并重载」让它生效");
      await loadRules();
    }).catch(showErr);
  }

  // ---------------------------------------------------------------- 概率页内保存
  // 悬浮按钮一次性保存概率页所有可编辑区（排程/加权采样/在场/快回来/睡觉），
  // 事件规则单独走 rules/save（因为接口不同）。
  async function saveProbAll(btn) {
    await withBusy(btn, async () => {
      // 合并所有可编辑区的字段为一个 patch（排程/加权是两张卡，先合一次）
      const tune = collectTunePatch();
      const patch = tune.patch;
      const clamped = [...tune.clamped];
      const containers = ["#prob-away", "#prob-returnsoon",
        "#sleep-night-controls", "#sleep-nap-controls", "#sleep-wait-controls", "#prob-sleep"];
      containers.forEach((sel) => {
        const box = $(sel);
        if (!box) return;
        const r = collectPatch(box);
        Object.keys(r.patch).forEach((sec) => {
          patch[sec] = patch[sec] || {};
          Object.assign(patch[sec], r.patch[sec]);
        });
        clamped.push(...r.clamped);
      });

      if (!Object.keys(patch).length) { toast("没有改动", "err"); return; }

      const bad = scheduleError(patch);
      if (bad) { toast(bad, "err"); return; }

      // 规则单独判断：只改了规则时，配置那边会返回 saved=false，不能就此打住
      const { rules, fixed: rulesFixed } = collectRules();
      const rulesChanged = rules.length > 0 && rulesDirty(rules);

      let r = { saved: false, changed: [], need_reload: false, clamped: [] };
      if (Object.keys(patch).length) {
        r = await api("POST", "config/save", { patch });
      }
      if (!r.saved && !rulesChanged) { toast("没有改动", "err"); return; }

      const fixed = clamped.concat(r.clamped || []);
      if (fixed.length) toast("已按范围修正：" + fixed.join("、"), "err");
      if (r.saved) toast(`已保存 ${r.changed.length} 项`, "ok");

      // 事件规则有改动就一并保存（不再依赖配置是否改动）
      let rulesNeedReload = false;
      if (rulesChanged) {
        const rr = await api("POST", "rules/save", { rules });
        rulesNeedReload = !!rr.need_reload;
        if (rulesFixed) toast(`${rulesFixed} 条规则的有效分钟/加成超出范围，已按边界保存`, "err");
        toast(rulesNeedReload ? "事件规则已保存，重载后生效" : "事件规则已保存", "ok");
        await loadRules();
      }

      const needReload = r.need_reload || rulesNeedReload;
      if (needReload) {
        toast("有改动需要重载插件，正在重载…", "ok");
        await doReload();
      }

      // 排程/权重改完必须重排一次：
      // 计时器里存的是上一次采样的 old 时间点，不重排的话新曲线要等这轮发完才生效；
      // 而且重载插件走的是 start(keep_if_future=True)，也会保留旧时间点。
      // 匹配用「包含」而不是前缀，兼容后端返回的两种路径格式。
      const TUNED_HINTS = [
        "schedule_settings", "min_interval_minutes", "max_interval_minutes",
        "max_unanswered_times", "enable_weighted", "weight", "decay",
      ];
      const tuned = (r.changed || []).some((c) => TUNED_HINTS.some((k) => c.includes(k)));
      if (tuned) {
        try {
          const umo = probUmo();
          if (umo) {
            const rs = await api("POST", "reschedule", { umo });
            toast(`已按新参数重新排程：${L.fmtDuration(rs.next_in_seconds)}后`, "ok");
            state.tasks = await api("GET", "tasks");
            renderTasks();
          }
        } catch (e) {
          toast("重新排程失败：" + ((e && e.message) || "接口无响应"), "err");
        }
      }

      state.probPreview = null; // 保存后以真实数据为准
      clearTimeout(previewTimer);
      clearTimeout(sleepPreviewTimer);
      previewTimer = null;
      sleepPreviewTimer = null;
      updatePreviewUI();
      state.config = await api("GET", "config");
      renderProbSleep(); // 有被夹到边界的值，重渲染一次显示真实结果
      await loadProbability();
      await refreshStatus();
    }).catch(showErr);
  }

  async function probResched() {
    const umo = ($("#prob-umo") || {}).value || (sessionList()[0] || {}).umo || "";
    if (!umo) { toast("还没有配置会话", "err"); return; }
    const r = await api("POST", "reschedule", { umo });
    toast(`已重排：${L.fmtDuration(r.next_in_seconds)}后`, "ok");
    await loadProbability();
  }

  // ---------------------------------------------------------------- 登录
  let loginShown = false;
  function showLogin() {
    if (loginShown) return;
    loginShown = true;
    $("#login-overlay").classList.remove("hidden");
    setTimeout(() => $("#login-password").focus(), 50);
  }

  $("#login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (bridge()) {
      $("#login-error").textContent = "该通道由 AstrBot 登录态保护，无需在此输入密码";
      return;
    }
    const pwd = $("#login-password").value;
    try {
      await api("POST", "login", { password: pwd });
      $("#login-overlay").classList.add("hidden");
      loginShown = false;
      await refreshAll();
      toast("已登录", "ok");
    } catch (e) {
      $("#login-error").textContent = e.message;
    }
  });

  // ---------------------------------------------------------------- 事件
  document.addEventListener("click", (ev) => {
    const tab = ev.target.closest("[data-tab]");
    if (tab) {
      state.tab = tab.dataset.tab;
      $$(".tab").forEach((t) => t.classList.toggle("active", t === tab));
      $$(".view").forEach((v) => v.classList.toggle("hidden", v.dataset.view !== state.tab));
      const fab = $("#prob-fab");
      if (fab) fab.classList.toggle("hidden", state.tab !== "prob");
      const git = $("#ov-git");
      if (git) git.classList.toggle("hidden", state.tab !== "overview");
      if (state.tab === "prob") loadProbability().catch(showErr);
      return;
    }

    const el = ev.target.closest("[data-act]");
    if (!el) return;
    const act = el.dataset.act;

    if (act === "refresh") return refreshAll().catch(showErr);
    if (act === "reload-prob") return withBusy(el, () => loadProbability()).catch(showErr);
    if (act === "prob-resched") return probResched();
    if (act === "prob-save-all") return saveProbAll(el);
    if (act === "preset-apply") return applyPreset(el.dataset.preset);
    if (act === "preview-reset") return resetPreview();
    if (act === "reload-plugin") return doReload();
    if (act === "quick") return quickAction(el.dataset.op, el.dataset.umo, el);
    if (act === "task") return taskAction(el.dataset.op, el.dataset.umo, el);
    if (act === "add-session") {
      const umo = $("#add-umo").value.trim();
      if (!umo) return toast("先填会话标识", "err");
      return withBusy(el, async () => {
        state.tasks = await api("POST", "tasks/action", { action: "add", umo });
        $("#add-umo").value = "";
        renderTasks(); renderOverview();
        toast("已添加", "ok");
      }).catch(showErr);
    }
    if (act === "config-reload") return refreshAll().catch(showErr);
    if (act === "config-save") return saveConfig(el, false);
    if (act === "config-save-reload") return saveConfig(el, true);
    if (act === "rules-save") return saveRules(el);
    if (act === "overrides-save") return saveOverrides(el);
    if (act === "overrides-reset-one") return resetOneOverride(el.dataset.field);
    if (act === "rule-add") {
      const list = $("#rules-list");
      const idx = $$(".rule").length;
      list.insertAdjacentHTML("beforeend", ruleRow({ name: "", label: "", ttl_minutes: 45, boost: 3, keywords: [], stop_keywords: [], hint: "" }, idx));
      return;
    }
    if (act === "rule-del") {
      const row = el.closest(".rule");
      if (row) row.remove();
      return;
    }
  });

  document.addEventListener("change", (ev) => {
    const t = ev.target;
    if (t && t.dataset && (t.dataset.section || t.dataset.sync)) applyConditions();
    // 睡眠区：下拉/开关/失焦也补一次预览
    if (t && t.closest && SLEEP_CONTAINERS.some((sel) => t.closest(sel))
        && t.dataset && t.dataset.path) {
      scheduleSleepPreview();
    }
    // 兜底：输入框失焦 / 下拉选择 / 松开滑杆，也补一次预览
    if (t && t.closest && (t.closest("#prob-sched") || t.closest("#prob-weights"))
        && t.dataset && t.dataset.path && t.dataset.path !== "chart_ceiling_pct") {
      schedulePreview();
    }
  });

  $("#prob-umo").addEventListener("change", () => loadProbability().catch(showErr));
  $("#ov-umo").addEventListener("change", () => renderOverview());

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshStatus().catch(() => {});
  });

  // 任务页每 12 秒自己刷新一次：事件、排程时间都是后台在变，
  // 只渲染一次的话会一直停在打开页面那一刻的快照上。
  setInterval(async () => {
    if (state.tab !== "tasks" || document.hidden || state.busy.size) return;
    // 焦点在任务列表里（正在看判定、点按钮）时跳过这一轮，别把折叠区合上
    const list = $("#task-list");
    if (list && list.contains(document.activeElement)) return;
    try {
      const [tasks] = await Promise.all([api("GET", "tasks"), loadDecisions()]);
      state.tasks = tasks;
      renderTasks();
    } catch (_) { /* 断线/未登录时忽略，下一轮再试 */ }
  }, 12000);

  // ---------------------------------------------------------------- 启动
  async function boot() {
    // 官方页面：bridge SDK 在本脚本之后才注入，先等它挂上再握手。
    if (await waitBridge(2500)) {
      try {
        await Promise.race([
          BRIDGE.ready(),
          new Promise((r) => setTimeout(r, 1500)),
        ]);
      } catch (_) { /* 拿不到 context 不影响使用 */ }
    }
    try {
      await refreshAll();
      if (state.tab === "prob") await loadProbability();
    } catch (e) {
      if (e.code === "E_AUTH") { showLogin(); return; }
      showErr(e);
    }
    setInterval(() => { if (!document.hidden) refreshStatus().catch(() => {}); }, 8000);
  }

  boot();
})();
