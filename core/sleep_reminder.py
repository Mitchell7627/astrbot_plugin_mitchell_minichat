"""「提醒睡觉」状态机（纯逻辑，不读写 session_data、不持锁，方便离线单测）。

场景：
- 用户说「待会睡 / 再看会手机」→ 进入 waiting，等待「提醒窗口」到来；
- 当前时间落在提醒窗口（如 00:00–00:20）内时，按概率触发一次「该睡了」提醒；
- 用户又说「待会睡」→ 重新等待（重新抽提醒时机）；
- 用户说「睡了 / 晚安」→ 进入 sleeping，开启动态免打扰，直到 wake_time；
- 到 wake_time（或用户主动说话）→ 退出 sleeping。

窗口支持跨天（如 23:50–00:20）。
"""

from __future__ import annotations

import math
import random
import time
from datetime import datetime

from . import text_rules as rules

# 睡眠状态键（session_data 里）
SLEEP_KEY = "sleep_state"

STATE_WAITING = "waiting"
STATE_SLEEPING = "sleeping"
# 本轮窗口已经提醒过（不代表用户答应睡，只是打了标记，面板上能看出「今晚已提醒」）
STATE_REMINDED = "reminded"

# 默认词表
DEFAULT_SLEEP_KEYWORDS = ["睡了", "睡觉", "晚安", "要睡了", "去睡了", "睡觉了", "睡了哦"]
DEFAULT_WAIT_KEYWORDS = [
    "待会睡", "稍后睡", "再看会手机", "再看会儿手机", "再躺会", "一会睡", "等会睡", "再看一会",
    "再玩会儿就睡", "再玩一会就睡", "玩会就睡", "打完这把就睡",
]

# 「确定要睡」的否定前缀：出现这些就别当成要睡
SLEEP_NEGATIONS = ["不", "没", "别", "还没", "不想", "不要", "不困", "睡不着"]

# 「待会再睡」的否定：出现这些其实是不想睡/还醒着，别当成等待
WAIT_NEGATIONS = ["不", "没", "别", "还不", "不想", "不要"]

# 午睡/小睡：交给事件识别（nap），不要开整夜免打扰
_NAP_WORDS = ("午睡", "午觉", "补觉", "眯一会", "眯一会儿", "眯一下", "小睡", "打盹")

# 第三人称主语：说的是别人睡了，不该静音自己
# 「宝宝/宝贝」按本插件的语境多半指机器人自己，也不能把用户静音。
_THIRD_PERSON = (
    "你", "他", "她", "孩子", "我妈", "我爸", "室友", "同事", "同学",
    "朋友", "家里", "猫", "狗", "宝宝", "宝贝",
)


def _iter_hits(text: str, keywords):
    """逐个产出 (关键词, 出现位置)：同一个词出现多次也要全部看到。"""
    for kw in _split_kw(keywords):
        if not kw:
            continue
        idx = text.find(kw)
        while idx != -1:
            yield kw, idx
            idx = text.find(kw, idx + 1)


def _is_negated_at(text: str, idx: int, negations) -> bool:
    """否定词紧贴关键词前 4 个字才视为否定（如「还不困」「不想睡」），且不跨分句。

    「不困，再看会手机」：`再看会手机` 的分句头在逗号之后，不受前面的「不」影响。
    """
    return rules.negated_by_clause(text, idx, negations, window=4)


def _is_past_at(text: str, kw: str, idx: int) -> bool:
    """说的是已经睡过/吃过的事（「昨天睡了8小时」），别当成现在要睡。"""
    return rules.is_past_clause(text, idx, kw_len=len(kw))


def _is_question_at(text: str, kw: str, idx: int) -> bool:
    """关键词所在的分句是不是疑问（「睡了吗」「你睡了没」）。

    只看同一个分句：`我睡了，你呢` 里的「呢」属于下一句，不能把前半句也判成疑问。
    """
    tail = text[idx + len(kw):]
    cut = len(tail)
    for pos, char in enumerate(tail):
        if char in "，。！？,.!?；;、\n":
            if char in "？?":
                return True
            cut = pos
            break
    clause = tail[:cut]
    if any(mark in clause for mark in ("吗", "呢", "么")):
        return True
    return clause[:1] == "没"


def _has_third_person(text: str, idx: int) -> bool:
    """关键词前 3 个字里出现第三人称主语（孩子/我妈/他/她…）。"""
    head = text[max(0, idx - 3):idx]
    return any(who in head for who in _THIRD_PERSON)


def _split_kw(raw) -> list[str]:
    """把逗号/顿号分隔的词表拆成列表，去空、去重。"""
    if isinstance(raw, list):
        parts = [str(x).strip() for x in raw if str(x).strip()]
    else:
        parts = [p.strip() for p in str(raw or "").replace("，", ",").replace("、", ",").split(",")]
    out: list[str] = []
    for p in parts:
        if p and p not in out:
            out.append(p)
    return out


def _parse_hhmm(spec: str):
    """把 'HH:MM' 解析成 (hour, minute)，非法返回 None。"""
    try:
        h_s, m_s = str(spec or "").strip().split(":", 1)
        h, m = int(h_s), int(m_s)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        return h, m
    except (ValueError, AttributeError):
        return None


def _minutes_of_day(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def in_remind_window(start_spec: str, end_spec: str, now: datetime | None = None) -> bool:
    """判断当前是否落在提醒窗口内（支持跨天）。"""
    now = now or datetime.now()
    start = _parse_hhmm(start_spec)
    end = _parse_hhmm(end_spec)
    if start is None or end is None:
        return False
    cur = _minutes_of_day(now)
    s = start[0] * 60 + start[1]
    e = end[0] * 60 + end[1]
    if s == e:
        return False
    if s < e:
        return s <= cur < e
    # 跨天：如 23:50 – 00:20
    return cur >= s or cur < e


def wake_at_timestamp(wake_spec: str, now: datetime | None = None) -> float | None:
    """把「起床时间」转成下一个时间戳。若当前已过 wake_time，取明天的同一时刻。"""
    now = now or datetime.now()
    parsed = _parse_hhmm(wake_spec)
    if parsed is None:
        return None
    h, m = parsed
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target.timestamp() <= now.timestamp():
        # 已过今天的起床点，取明天
        from datetime import timedelta
        target = target + timedelta(days=1)
    return target.timestamp()


def window_position(start_spec: str, end_spec: str, now: datetime | None = None) -> dict | None:
    """当前时刻相对提醒窗口的位置（支持跨天）。

    返回 {"inside", "minutes_to_start", "minutes_left", "span"}：
    - minutes_to_start：距离下一次窗口开始还有多少分钟（已在窗口内则为 0）
    - minutes_left：窗口内还剩多少分钟（窗口外则为 0）
    - span：整个窗口的分钟数
    窗口配置非法（解析失败或首尾相同）时返回 None。
    """
    now = now or datetime.now()
    start = _parse_hhmm(start_spec)
    end = _parse_hhmm(end_spec)
    if start is None or end is None:
        return None
    s = start[0] * 60 + start[1]
    e = end[0] * 60 + end[1]
    if s == e:
        return None
    cur = _minutes_of_day(now)
    span = (e - s) % (24 * 60)
    inside = in_remind_window(start_spec, end_spec, now)
    if inside:
        left = (e - cur) % (24 * 60)
        return {"inside": True, "minutes_to_start": 0, "minutes_left": left or span, "span": span}
    to_start = (s - cur) % (24 * 60)
    return {"inside": False, "minutes_to_start": to_start, "minutes_left": 0, "span": span}


def current_window_start_ts(start_spec: str, end_spec: str, now: datetime | None = None) -> float | None:
    """「当前所处（或刚刚过去的）」那个窗口的开始时间戳。

    用来判断某次提醒是不是发生在同一个窗口里，从而实现「一个窗口只催一次」。
    """
    now = now or datetime.now()
    pos = window_position(start_spec, end_spec, now)
    if not pos:
        return None
    from datetime import timedelta
    if pos["inside"]:
        elapsed = max(0, int(pos["span"] - pos["minutes_left"]))
        return (now - timedelta(minutes=elapsed)).timestamp()
    # 窗口外：下一次窗口开始减去一天 = 刚刚过去的那个窗口开始
    return (now + timedelta(minutes=int(pos["minutes_to_start"])) - timedelta(days=1)).timestamp()


def pick_weighted_offset(
    lo_minutes: int,
    hi_minutes: int,
    w_early: float = 1.0,
    w_mid: float = 1.0,
    w_tail: float = 1.0,
    rng=None,
) -> int:
    """在 [lo, hi] 分钟里按「早/中/尾」三段权重抽一个延时（分钟）。

    - 三段等分，先按权重选段，再在段内均匀取整；
    - 权重全 <= 0 时退化成整段均匀分布；
    - 返回值保证落在 [lo, hi] 内。
    """
    source = rng or random
    lo = max(1, int(lo_minutes))
    hi = max(lo, int(hi_minutes))
    if hi == lo:
        return lo
    weights = [max(0.0, float(w or 0)) for w in (w_early, w_mid, w_tail)]
    if sum(weights) <= 0:
        return source.randint(lo, hi)

    span = hi - lo
    cut1 = lo + span / 3.0
    cut2 = lo + span * 2 / 3.0
    total = sum(weights)
    r = source.random() * total
    acc = 0.0
    idx = 2
    for i, w in enumerate(weights):
        acc += w
        if r <= acc:
            idx = i
            break
    if idx == 0:
        a, b = lo, cut1
    elif idx == 1:
        a, b = cut1, cut2
    else:
        a, b = cut2, hi
    return max(lo, min(hi, int(round(source.uniform(a, b)))))


def match_sleep(text: str, keywords) -> str | None:
    """命中「确定要睡」返回命中的词，否则 None。

    过滤五类误判：
    - 否定（「不睡了」）；
    - 疑问（「睡了吗」「你睡了没」）；
    - 第三人称（「孩子睡了」「我妈睡了」）；
    - 过去时（「昨天睡了8小时」「昨晚睡了一会儿」——说已经睡过，人其实醒着）；
    - 午睡/小睡（「我午睡了」）——这类交给 nap 事件，别开整夜免打扰。
    同一个词出现多次时逐个检查，前面那次被否定不影响后面那次。
    """
    text = str(text or "")
    if not text:
        return None
    if any(word in text for word in _NAP_WORDS):
        return None
    for kw, idx in _iter_hits(text, keywords):
        if _is_negated_at(text, idx, SLEEP_NEGATIONS):
            continue
        if _is_question_at(text, kw, idx):
            continue
        if _has_third_person(text, idx):
            continue
        if _is_past_at(text, kw, idx):
            continue
        return kw
    return None


def match_wait(text: str, keywords) -> str | None:
    """命中「待会再睡」返回命中的词，否则 None。带否定/疑问/第三人称/过去时过滤。"""
    text = str(text or "")
    if not text:
        return None
    for kw, idx in _iter_hits(text, keywords):
        if _is_negated_at(text, idx, WAIT_NEGATIONS):
            continue
        if _is_question_at(text, kw, idx):
            continue
        if _has_third_person(text, idx):
            continue
        if _is_past_at(text, kw, idx):
            continue
        return kw
    return None


def build_reminder_hint(state: dict, now: datetime | None = None) -> str:
    """生成「该睡了」提醒时的 event_hint，让她的语气贴合场景。"""
    now = now or datetime.now()
    hhmm = now.strftime("%H:%M")
    kw = str((state or {}).get("keyword") or "").strip()
    if kw and kw != "自动提醒":
        lead = f"现在 {hhmm}。他之前说过「{kw}」，现在是说好该提醒他的时间。"
    else:
        lead = f"现在 {hhmm}，到了该提醒他睡觉的时间点。"
    return (
        lead
        + "催他一句该睡了，语气要像在意的黏人，别冷冰冰地下命令；"
        "可以提一下时间晚了、明天还要早起之类的由头。"
    )


def next_remind_delay(lo_minutes: int, hi_minutes: int, rng=None) -> int:
    """在 [lo, hi] 分钟内均匀抽一个「下次提醒」延时（分钟）。窗口驱动下，实际只在窗口内生效。"""
    source = rng or random
    lo = max(1, int(lo_minutes))
    hi = max(lo, int(hi_minutes))
    return source.randint(lo, hi)


def _window_span_minutes(start_spec: str, end_spec: str) -> int:
    """提醒窗口的时长（分钟），支持跨天。非法返回 0。"""
    start = _parse_hhmm(start_spec)
    end = _parse_hhmm(end_spec)
    if start is None or end is None:
        return 0
    s = start[0] * 60 + start[1]
    e = end[0] * 60 + end[1]
    if s == e:
        return 0
    if s < e:
        return e - s
    return (24 * 60 - s) + e


def _band_shape(count: int, w_early: float, w_mid: float, w_tail: float) -> list[float]:
    """把「前/中/后」三个权重插值成 count 根柱子的概率（归一化后）。

    三个控制点分别在 1/6、1/2、5/6 处，中间线性过渡，两端保持端点值，
    这样拖权重时柱子是平滑起伏，而不是硬邦邦的三级台阶。
    """
    pts = (
        (1 / 6, max(0.0, float(w_early or 0))),
        (0.5, max(0.0, float(w_mid or 0))),
        (5 / 6, max(0.0, float(w_tail or 0))),
    )
    n = max(1, int(count))
    weights = []
    for i in range(n):
        r = (i + 0.5) / n
        if r <= pts[0][0]:
            v = pts[0][1]
        elif r <= pts[1][0]:
            t = (r - pts[0][0]) / max(1e-6, pts[1][0] - pts[0][0])
            v = pts[0][1] + (pts[1][1] - pts[0][1]) * t
        elif r <= pts[2][0]:
            t = (r - pts[1][0]) / max(1e-6, pts[2][0] - pts[1][0])
            v = pts[1][1] + (pts[2][1] - pts[1][1]) * t
        else:
            v = pts[2][1]
        weights.append(v)
    total = sum(weights)
    if total <= 0:
        return [round(1.0 / n, 6)] * n
    return [round(w / total, 6) for w in weights]


def pick_window_offset(span_minutes: float, elapsed_minutes: float,
                       w_early: float, w_mid: float, w_tail: float, rng=None) -> int:
    """在窗口内按三段权重抽一个「相对窗口开始的偏移分钟」。

    elapsed_minutes 是窗口里已经过去的部分（窗口中途重排时用），
    抽到的点不会早于它，保证提醒一定落在「现在之后」。
    """
    source = rng or random
    span = max(1, int(round(float(span_minutes or 1))))
    elapsed = max(0, min(span, int(round(float(elapsed_minutes or 0)))))
    weights = [max(0.0, float(w or 0)) for w in (w_early, w_mid, w_tail)]
    if sum(weights) <= 0:
        weights = [1.0, 1.0, 1.0]
    total = sum(weights)
    r = source.random() * total
    acc = 0.0
    idx = 2
    for i, w in enumerate(weights):
        acc += w
        if r <= acc:
            idx = i
            break
    a = idx * span / 3.0
    b = (idx + 1) * span / 3.0
    off = int(round(source.uniform(a, b)))
    off = max(1, min(span, off))
    return max(off, min(span, elapsed + 1))


def sleep_forecast(engine_cfg: dict, now_ts: float | None = None) -> dict:
    """给面板画的两张「提醒落点」分布图。

    两张图用同一套算法：横轴 = 各自的提醒窗口，柱形 = 按「前/中/后」三段权重
    算出的落点概率（归一化）。和真实采样用的 `pick_window_offset` 是同一组权重，
    所以图不是装饰，改了权重真会照着抽。
    """
    cfg = engine_cfg or {}
    # 兜底值与 schema / README 保持一致（08:00 起床、23:00 开夜窗），
    # 否则配置缺字段时图上的窗口和真机催睡窗口对不上。
    wake_time = str(cfg.get("wake_time") or "08:00")

    def _window(start_key: str, end_key: str, w_early, w_mid, w_tail, fallback):
        start = str(cfg.get(start_key) or fallback[0])
        end = str(cfg.get(end_key) or fallback[1])
        span = max(10, _window_span_minutes(start, end))  # 至少 10 分钟，避免空图
        bucket = max(1, int(round(span / 10)))
        count = max(1, int(math.ceil(span / bucket)))
        return start, end, span, bucket, count, _band_shape(count, w_early, w_mid, w_tail)

    win_start, win_end, span, bucket, night_count, night_bars = _window(
        "remind_window_start", "remind_window_end",
        cfg.get("night_weight_early", 1.5),
        cfg.get("night_weight_mid", 1.0),
        cfg.get("night_weight_tail", 0.6),
        ("23:00", "00:20"),
    )
    nap_start, nap_end, nap_span, nap_bucket, nap_count, nap_bars = _window(
        "nap_window_start", "nap_window_end",
        cfg.get("nap_weight_early", 1.4),
        cfg.get("nap_weight_mid", 1.0),
        cfg.get("nap_weight_tail", 0.6),
        ("13:00", "13:30"),
    )

    try:
        nap_hours = max(1.0, float(cfg.get("sleep_quiet_hours") or 8.0))
    except (TypeError, ValueError):
        nap_hours = 8.0

    return {
        "night": {
            "label": "晚睡提醒",
            "window_start": win_start,
            "window_end": win_end,
            "night_start": str(cfg.get("night_start_time") or "18:00"),
            "span_minutes": span,
            "bucket_minutes": bucket,
            "count": night_count,
            "bars": night_bars,
        },
        "nap": {
            "label": "午睡提醒",
            "window_start": nap_start,
            "window_end": nap_end,
            "span_minutes": nap_span,
            "bucket_minutes": nap_bucket,
            "count": nap_count,
            "bars": nap_bars,
            "quiet_hours": nap_hours,
            "remind_enabled": bool(cfg.get("nap_remind_enabled", True)),
            "remind_window_start": nap_start,
            "remind_window_end": nap_end,
        },
        "wake_time": wake_time,
    }
