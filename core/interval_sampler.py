"""主动消息间隔的加权随机采样（区间比例自适应版）。

分段不再写死绝对分钟，而是按 [lo, hi] 的跨度取固定比例切分：
- 早段：区间前 15/235（默认 5-240 下即 5-19 分钟），事件加成只作用在这里；
- 中段：紧接着 20/235（默认 20-39 分钟），权重略高于基线；
- 尾段：其余部分（默认 40-240 分钟），权重按幂次衰减到最低点。

比例常量由默认区间 5-240 反推，所以默认配置下行为与旧实现逐分钟一致；
把区间改成 50-180 时，分段点自动变成 50-57 / 58-68 / 69-180，事件加成照样生效。

本模块只做纯计算：不读写 session_data，不持锁，方便离线单测。
"""

from __future__ import annotations

import math
import random

MIN_MINUTES = 5
SUPPORT_MAX_MINUTES = 2880
"""支持的最长间隔（分钟）= 两天。仅作硬顶防误配，实际上限跟随配置值。"""

EVENT_BOOST_BASELINE = 3.0
"""单条事件 boost 的基准值。等于 3.0 表示不额外调整。"""

EARLY_SPAN_RATIO = 15.0 / 235.0
EARLY_END_DEFAULT = 20
MID_SPAN_RATIO = 20.0 / 235.0
MID_END_DEFAULT = 40

MODE_BASE = "weighted_base"
MODE_EVENT = "weighted_event"

DEFAULT_PROFILE = {
    "early_weight": 1.00,
    "early_weight_event": 3.00,
    "mid_weight": 1.15,
    "tail_head_weight": 1.15,
    "tail_floor_weight": 0.60,
    "tail_decay_power": 0.30,
}


def band_edges(lo, hi):
    """返回 (early_end, mid_end)。

    早段为 [lo, early_end)，中段为 [early_end, mid_end)，尾段为 [mid_end, hi]。
    退化区间（hi <= lo）返回 (lo, lo)，调用方不应再按分段取值。
    """
    try:
        lo, hi = int(lo), int(hi)
    except (TypeError, ValueError):
        return EARLY_END_DEFAULT, MID_END_DEFAULT

    if hi <= lo:
        return lo, lo

    span = hi - lo
    d_early = max(1, int(round(span * EARLY_SPAN_RATIO)))
    d_mid = max(1, int(round(span * MID_SPAN_RATIO)))
    early_end = lo + d_early
    mid_end = early_end + d_mid

    if mid_end > hi:
        mid_end = hi
    if early_end > mid_end:
        early_end = mid_end
    return early_end, mid_end


def band_weight(minute, *, lo=MIN_MINUTES, hi=240, event_active=False,
                event_weight=None, profile=None):
    """返回某一分钟的未归一化权重。

    event_weight: 事件生效时早段的权重（已由调用方把全局倍数与单条规则 boost 合成）。
                  为 None 时退回 profile 的 early_weight_event。
    """
    p = DEFAULT_PROFILE if not profile else {**DEFAULT_PROFILE, **profile}

    early_end, mid_end = band_edges(lo, hi)

    if mid_end <= lo:
        return float(p["mid_weight"])

    if minute < early_end:
        if not event_active:
            return float(p["early_weight"])
        return float(p["early_weight_event"] if event_weight is None else event_weight)

    if minute < mid_end:
        return float(p["mid_weight"])

    den = max(1, hi - mid_end)
    ratio = (hi - minute) / float(den)
    if ratio < 0.0:
        ratio = 0.0
    elif ratio > 1.0:
        ratio = 1.0

    head = float(p["tail_head_weight"])
    floor = float(p["tail_floor_weight"])
    return floor + (head - floor) * (ratio ** float(p["tail_decay_power"]))


def sample_interval_seconds(
    min_seconds,
    max_seconds,
    *,
    event_active=False,
    event_weight=None,
    profile=None,
    rng=None,
):
    """在配置区间内按加权分布采样一个触发间隔。

    Returns:
        (间隔秒数, 模式)，模式为 MODE_BASE 或 MODE_EVENT。
    """
    source = rng or random

    try:
        lo = max(1, int(min_seconds) // 60)
        hi = int(math.ceil(float(max_seconds) / 60.0))
    except (TypeError, ValueError):
        lo, hi = MIN_MINUTES, 240

    # 上下限都夹到硬顶，避免手填超大值绕过限制（例如 3000-4000）
    lo = min(lo, SUPPORT_MAX_MINUTES)
    hi = min(hi, SUPPORT_MAX_MINUTES)
    if hi < lo:
        # 配置非法时收敛到单点，不再静默塌回 5-240，避免用户设置被悄悄丢掉。
        hi = lo
    if hi <= 0:
        lo, hi = MIN_MINUTES, 240

    mode = MODE_EVENT if event_active else MODE_BASE
    if hi == lo:
        return lo * 60, mode

    minutes = list(range(lo, hi + 1))
    weights = [
        band_weight(
            m,
            lo=lo,
            hi=hi,
            event_active=event_active,
            event_weight=event_weight,
            profile=profile,
        )
        for m in minutes
    ]
    total = sum(weights)

    if total <= 0:
        return source.randint(lo, hi) * 60, MODE_BASE

    point = source.random() * total
    acc = 0.0
    for minute, weight in zip(minutes, weights):
        acc += weight
        if point <= acc:
            return minute * 60, mode
    return hi * 60, mode


def sample_uniform_seconds(min_seconds, max_seconds, *, rng=None):
    """原版行为：区间内均匀随机（用作关闭加权后的回退）。"""
    source = rng or random
    lo = int(min_seconds)
    hi = max(lo, int(max_seconds))
    return source.randint(lo, hi), "uniform_legacy"
