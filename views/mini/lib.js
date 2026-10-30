/* 纯函数集中在这里，方便 node --check 与单测。浏览器挂在 window.MiniLib。 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.MiniLib = api;
})(typeof self !== "undefined" ? self : this, function () {
  const pad = (n) => String(n).padStart(2, "0");

  function fmtClock(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function fmtDuration(sec) {
    if (sec === null || sec === undefined || isNaN(sec)) return "—";
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return `${sec} 秒`;
    const m = Math.floor(sec / 60);
    if (m < 60) return `${m} 分钟`;
    const h = Math.floor(m / 60);
    const rest = m % 60;
    return rest ? `${h} 小时 ${rest} 分` : `${h} 小时`;
  }

  function pct(v, digits) {
    const n = Number(v || 0) * 100;
    return `${n.toFixed(digits === undefined ? 2 : digits)}%`;
  }

  function shortUmo(umo) {
    const parts = String(umo || "").split(":");
    return parts.length >= 3 ? `${parts[0]}·${parts[2]}` : String(umo || "—");
  }

  function sum(arr) {
    return (arr || []).reduce((a, b) => a + (Number(b) || 0), 0);
  }

  function clamp(v, lo, hi) {
    return Math.min(hi, Math.max(lo, Number(v) || 0));
  }

  function deepSet(obj, path, value) {
    const keys = String(path).split(".").filter(Boolean);
    let cur = obj;
    for (let i = 0; i < keys.length - 1; i++) {
      const k = keys[i];
      if (!cur[k] || typeof cur[k] !== "object") cur[k] = {};
      cur = cur[k];
    }
    cur[keys[keys.length - 1]] = value;
    return obj;
  }

  function bucketLabels(now, bucketMinutes, count) {
    const out = [];
    for (let i = 0; i < count; i++) {
      out.push(now + i * bucketMinutes * 60);
    }
    return out;
  }

  function deepGet(obj, path, fallback) {
    let cur = obj;
    for (const k of String(path).split(".").filter(Boolean)) {
      if (cur === null || cur === undefined || typeof cur !== "object" || !(k in cur)) return fallback;
      cur = cur[k];
    }
    return cur === undefined ? fallback : cur;
  }

  // 一组数字 -> 好看的刻度（用于图表纵轴/轴标签）
  function axisTicks(startTs, stepMinutes, count, maxTicks) {
    const want = Math.min(maxTicks || 4, Math.max(2, count));
    const idx = [0];
    for (let i = 1; i < want - 1; i++) idx.push(Math.round((count * i) / (want - 1)));
    idx.push(count);
    return Array.from(new Set(idx)).map((i) => ({
      i,
      ts: startTs + i * stepMinutes * 60,
      minutes: i * stepMinutes,
    }));
  }

  return { fmtClock, fmtDuration, pct, shortUmo, sum, clamp, deepSet, deepGet, bucketLabels, axisTicks };
});
