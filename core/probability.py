"""触发概率预测（纯计算，无副作用）。

权重来源与 runner._sample_minutes 完全一致，保证「图上看到的分布」就是
「真正采样出来的分布」。
"""

from __future__ import annotations

import math
import time

from .interval_sampler import (
    EVENT_BOOST_BASELINE,
    SUPPORT_MAX_MINUTES,
    band_edges,
    band_weight,
)
from .sleep_reminder import sleep_forecast

BUCKET_PRESETS = {6: 36, 12: 48, 24: 48, 48: 48}
QUANTILES = (0.10, 0.50, 0.90)


def _num(value, default: float) -> float:
    """None/非法才用默认值；0 是合法值，不能被 `or` 吞掉。"""
    if value is None:
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def build_profile(eng: dict) -> dict:
    eng = eng or {}
    return {
        "early_weight": _num(eng.get("early_weight"), 1.0),
        "early_weight_event": _num(eng.get("early_weight_event"), 3.0),
        "mid_weight": _num(eng.get("mid_weight"), 1.15),
        "tail_head_weight": _num(eng.get("tail_head_weight"), 1.15),
        "tail_floor_weight": _num(eng.get("tail_floor_weight"), 0.6),
        "tail_decay_power": _num(eng.get("tail_decay_power"), 0.3),
    }


def interval_bounds(schedule: dict) -> tuple[int, int]:
    lo = max(1, int(_num(schedule.get("min_interval_minutes"), 5)))
    hi = int(_num(schedule.get("max_interval_minutes"), 240))
    if hi < lo:
        hi = lo
    lo = min(lo, SUPPORT_MAX_MINUTES)
    hi = min(hi, SUPPORT_MAX_MINUTES)
    return lo, max(lo, hi)


def event_state(engine_cfg: dict, active_event) -> tuple[bool, float | None]:
    """返回 (加成是否生效, 合成后的早段权重)。

    `boost_budget = 0` 表示不限次数（和 runner._sample_minutes 同款语义），
    不能用 `x or 1` 把它当成 1 次，否则图上的加成会提前消失。
    """
    if not isinstance(active_event, dict):
        return False, None
    budget = int(_num(active_event.get("boost_budget"), 1))
    used = int(_num(active_event.get("boost_used"), 0))
    if not (budget <= 0 or used < budget):
        return False, None
    base = _num((engine_cfg or {}).get("early_weight_event"), 3.0)
    rule_boost = _num(active_event.get("boost"), EVENT_BOOST_BASELINE)
    # 保一个极小值：用户把事件权重拖到 0 时图不能整块塌成 0（会画不出来）
    return True, max(0.01, base * rule_boost / EVENT_BOOST_BASELINE)


def _weight(d: int, lo: int, hi: int, profile: dict, event_active: bool,
            event_weight, weighted: bool) -> float:
    if not weighted:
        return 1.0
    return band_weight(
        d, lo=lo, hi=hi, event_active=event_active,
        event_weight=event_weight, profile=profile,
    )


def minute_pmf(lo: int, hi: int, *, profile: dict, event_active: bool,
               event_weight, now_ts: float, weighted: bool = True) -> list[float]:
    """长度 hi+1 的概率数组，下标 = 「多少分钟后触发」。"""
    pmf = [0.0] * (hi + 1)
    if hi <= lo:
        pmf[lo] = 1.0
        return pmf

    total = 0.0
    for d in range(lo, hi + 1):
        w = _weight(d, lo, hi, profile, event_active, event_weight, weighted)
        pmf[d] = w
        total += w

    if total <= 1e-9:
        total = 1.0

    inv = 1.0 / total
    for d in range(lo, hi + 1):
        pmf[d] *= inv
    return pmf


def bucketize(pmf: list[float], bucket_minutes: int, count: int, start: int = 1) -> tuple[list[float], float]:
    """把逐分钟 pmf 聚合成柱状图。

    start: 采样的起始分钟（= lo）。分桶从 start 对齐，避免第一根柱子被
           lo 之前的空档（权重 0）稀释，导致「调早段权重只有第二根柱子动」。
    """
    bars = [0.0] * count
    tail = 0.0
    bucket_minutes = max(1, int(bucket_minutes))
    start = max(0, int(start))
    for d in range(start, len(pmf)):
        p = pmf[d]
        if p <= 0.0:
            continue
        idx = (d - start) // bucket_minutes
        if 0 <= idx < count:
            bars[idx] += p
        else:
            tail += p
    return bars, tail


def quantiles(pmf: list[float]) -> dict[str, int]:
    out: dict[str, int] = {}
    acc = 0.0
    targets = list(QUANTILES)
    i = 0
    for d in range(len(pmf)):
        acc += pmf[d]
        while i < len(targets) and acc >= targets[i] - 1e-12:
            out["p%d" % int(targets[i] * 100)] = d
            i += 1
        if i >= len(targets):
            break
    for t in targets[i:]:
        out["p%d" % int(t * 100)] = len(pmf) - 1
    return out


def mean_minutes(pmf: list[float]) -> float:
    return sum(d * p for d, p in enumerate(pmf))


def _quantile_clock(pmf: list[float], now_ts: float) -> dict:
    q = quantiles(pmf)
    return {
        "p10": now_ts + q["p10"] * 60,
        "p50": now_ts + q["p50"] * 60,
        "p90": now_ts + q["p90"] * 60,
    }


def dual_forecast(*, schedule: dict, engine_cfg: dict, active_event,
                  now_ts: float | None = None) -> dict:
    """返回两张图的分布。

    base  ：无事件，横轴 0..最长间隔（例如 5-240 分钟）
    event ：事件生效，横轴 0..事件剩余有效期（例如在吃饭还剩 38 分钟）
    两份数据各画各的，不再共用一条时间轴。
    """
    now_ts = now_ts or time.time()
    lo, hi = interval_bounds(schedule)
    profile = build_profile(engine_cfg)
    weighted = bool((engine_cfg or {}).get("enable_weighted", True))
    event_on, eweight = event_state(engine_cfg, active_event)

    base_pmf = minute_pmf(
        lo, hi, profile=profile, event_active=False, event_weight=None,
        now_ts=now_ts, weighted=weighted,
    )
    base_bucket = max(1, int(round(hi / 24)))
    # 横轴从 lo 开始，桶数按有效跨度 (hi - lo + 1) 算
    base_count = max(1, int(math.ceil((hi - lo + 1) / base_bucket)))
    base_bars, _ = bucketize(base_pmf, base_bucket, base_count, start=lo)

    meta = {
        "now": now_ts,
        "lo": lo,
        "hi": hi,
        "weighted": weighted,
        # 事件还在就照实报（任务页也显示它），加成用没用完单独给个标记
        "event": active_event if isinstance(active_event, dict) else None,
    }
    early_end, mid_end = band_edges(lo, hi)
    meta["bands"] = {
        "early": [lo, early_end - 1],
        "mid": [early_end, mid_end - 1],
        "tail": [mid_end, hi],
    }

    base = {
        "label": "无事件",
        "bucket_minutes": base_bucket,
        "count": base_count,
        "bars": [round(b, 6) for b in base_bars],
        "mean_minutes": round(mean_minutes(base_pmf), 1),
        "quantiles": _quantile_clock(base_pmf, now_ts),
        "total_mass": round(sum(base_bars), 6),
    }

    ev = None
    if isinstance(active_event, dict):
        expires = float(active_event.get("expires_at") or 0) or now_ts
        started = float(active_event.get("started_at") or now_ts)
        remaining = max(1.0, (expires - now_ts) / 60.0)
        ttl = max(1, int(round((expires - started) / 60.0)))

        window = int(min(hi, max(1, round(remaining))))
        bucket = max(1, int(round(window / 8)))
        count = max(1, int(math.ceil(window / bucket)))

        ev_pmf = minute_pmf(
            lo, hi, profile=profile, event_active=True, event_weight=eweight,
            now_ts=now_ts, weighted=weighted,
        )
        ev_bars, _ = bucketize(ev_pmf, bucket, count, start=lo)
        ev = {
            "label": active_event.get("label") or active_event.get("name") or "事件",
            "name": active_event.get("name") or "",
            "boost": float(active_event.get("boost") or 0) or None,
            "boost_left": bool(event_on),
            "boost_used": int(active_event.get("boost_used") or 0),
            "boost_budget": int(active_event.get("boost_budget") or 1),
            "ttl_minutes": ttl,
            "remaining_minutes": int(round(remaining)),
            "bucket_minutes": bucket,
            "count": count,
            "bars": [round(b, 6) for b in ev_bars],
            "mass_in_window": round(sum(ev_bars), 6),
            "mean_minutes": round(mean_minutes(ev_pmf), 1),
            "quantiles": _quantile_clock(ev_pmf, now_ts),
        }

    return {"meta": meta, "base": base, "event": ev, "sleep": sleep_forecast(engine_cfg, now_ts)}
