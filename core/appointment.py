# -*- coding: utf-8 -*-
"""约定时间点：从一句话里认出「他几点会做什么」。

和事件系统互补——事件管「正在做什么」（吃饭、洗澡，按 TTL 过期），
本模块管「几点会到一个时刻」（11:50 下课、上到十二点）。

纯函数、零 IO、不读配置（配置由 runner 传入），便于离线单测。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from . import text_rules as rules

TAG = "[Minichat]"

# ---------------------------------------------------------------- 中文数字
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_CN_CHARS = "零〇一二三四五六七八九十两"


def cn_to_int(raw: str):
    """「十二」「二十」「两」「11」→ int；认不出返回 None。"""
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    if "十" in raw:
        left, _, right = raw.partition("十")
        if left and left not in _CN_DIGITS:
            return None
        if right and right not in _CN_DIGITS:
            return None
        tens = _CN_DIGITS[left] if left else 1
        ones = _CN_DIGITS[right] if right else 0
        return tens * 10 + ones
    return _CN_DIGITS.get(raw)


# ---------------------------------------------------------------- 时间表达式
_COLON_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{2})(?!\d)")
_POINT_RE = re.compile(
    rf"(?<!\d)(\d{{1,2}}|[{_CN_CHARS}]{{1,3}})\s*点\s*"
    rf"(?:(半|一刻|三刻|整)|(\d{{1,2}}|[{_CN_CHARS}]{{1,3}})\s*分?)?"
)
_MINUTE_WORDS = {"半": 30, "一刻": 15, "三刻": 45, "整": 0}

# 时段词按「离时间越近越优先」找，最多回看 4 个字
_PERIOD_WORDS = (
    (("凌晨",), "am"),
    (("早上", "早晨", "上午", "今早"), "am"),
    (("中午", "正午"), "noon"),
    (("下午", "午后"), "pm"),
    (("傍晚", "晚上", "夜里", "今晚", "晚间"), "pm"),
)

# 「还有十分钟下课」这类剩余时长，只能交给模型
_REMAIN_RE = re.compile(
    rf"(?:还有|再有|再上|再要|过)\s*(\d{{1,3}}|[{_CN_CHARS}]{{1,3}})\s*分钟"
)
_VAGUE_RE = re.compile(r"(快|快到|差不多|左右|前后|大概|大约|约|将近|马上|就快)")
# 「你几点下课」：没有具体数字，是在问对方
_WHEN_ASK_RE = re.compile(r"(几|啥|什么|哪)\s*点")

# ---------------------------------------------------------------- 事件词
DEFAULT_EVENTS = {
    "end": [
        "下课", "放学", "下班", "收工", "散会", "考完", "结束", "完事",
    ],
    "arrive": [
        "到家", "到学校", "到公司", "到宿舍", "到教室", "到门口", "抵达", "落地",
    ],
    "start": [
        "上班", "上课", "开会", "考试", "出门", "出发", "起飞", "开场",
        "吃饭", "吃早饭", "吃午饭", "吃晚饭", "吃宵夜",
    ],
}

# 「上到十二点」「忙到六点」：没有事件名词，但语义明确是「做到某个时刻」
_UP_TO_RE = re.compile(r"(上到|干到|弄到|做到|忙到|开到|学到|熬到|加到)")

# 时间与事件词之间允许的最大字数
_MAX_GAP = 10

NEGATIONS = ("不", "别", "没", "不用", "不必", "取消")


def _find_period(text: str, start: int):
    head = text[max(0, start - 4):start]
    best = None
    for words, period in _PERIOD_WORDS:
        for word in words:
            idx = head.rfind(word)
            if idx < 0:
                continue
            pos = idx + len(word)
            if best is None or pos > best[0]:
                best = (pos, period)
    return best[1] if best else None


def find_clock(text: str):
    """返回 {"h","m","idx","length","period"} 或 None。冒号写法优先。"""
    match = _COLON_RE.search(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return {
            "h": hour, "m": minute, "idx": match.start(),
            "length": match.end() - match.start(),
            "period": _find_period(text, match.start()),
        }

    match = _POINT_RE.search(text)
    if not match:
        return None
    hour = cn_to_int(match.group(1))
    if hour is None:
        return None
    if match.group(2):
        minute = _MINUTE_WORDS.get(match.group(2))
    elif match.group(3):
        minute = cn_to_int(match.group(3))
    else:
        minute = 0
    if minute is None or not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return {
        "h": hour, "m": minute, "idx": match.start(),
        "length": match.end() - match.start(),
        "period": _find_period(text, match.start()),
    }


def resolve_at(hour: int, minute: int, period: str | None,
               now: datetime, max_hours: int):
    """把「几点几分」落成时间戳；过去 / 太远 / 非法都返回 None。"""
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    day_offset = 0
    if period == "noon" and hour <= 2:
        hour += 12
    elif period == "pm":
        if hour == 12:
            # 「晚上十二点」是次日 0 点，不是中午
            hour = 0
            day_offset = 1
        elif hour <= 11:
            hour += 12

    base = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=day_offset)
    floor = now - timedelta(minutes=5)
    if base <= floor:
        passed = (now - base).total_seconds()
        if period is not None:
            # 说了时段但已经过了（22 点说「凌晨一点」）：算明天的
            base = base + timedelta(days=1)
        elif hour <= 11 and now.hour < 12 and passed >= 2 * 3600:
            # 只在上午做这个推断：10 点说「两点半」当下午。
            # 下午说「十一点五十」是「刚才的事」，晚上说「11:50」更不该推到次日，
            # 这两种一律丢弃，不猜。
            base = base + timedelta(hours=12)
        if base <= floor:
            return None

    if (base - now).total_seconds() > max_hours * 3600:
        return None
    return base.timestamp()


def _find_event(text: str, clock: dict, keywords: dict):
    """在时间附近找事件词，返回 (label, kind, idx) 或 None。"""
    start, end = clock["idx"], clock["idx"] + clock["length"]

    up = _UP_TO_RE.search(text)
    if up and abs(up.start() - start) <= _MAX_GAP:
        return ("结束", "end", up.start())

    best = None
    for kind, words in keywords.items():
        for word in words:
            idx = text.find(word)
            if idx < 0:
                continue
            gap = start - (idx + len(word)) if idx + len(word) <= start else idx - end
            if gap < 0 or gap > _MAX_GAP:
                continue
            # 长词优先，其次离时间近的
            score = (len(word), -gap)
            if best is None or score > best[0]:
                best = (score, word, kind, idx)
    if best:
        return (best[1], best[2], best[3])
    return None


def _day_seconds(ts: float, now: datetime) -> int:
    moment = datetime.fromtimestamp(ts)
    return moment.hour * 3600 + moment.minute * 60


def parse(text: str, now: datetime, cfg: dict, force: bool = False):
    """解析一句话。

    返回 {"hit": {...}|None, "gray": {...}|None, "dropped": [{rule, reason}]}。
    宁可漏，不要错——判不准的一律进 gray，由上层决定要不要问模型。
    force=True 是灰度答 YES 之后用：跳过「模糊」这一关，尽量落一个时刻。
    """
    out = {"hit": None, "gray": None, "dropped": []}

    def drop(rule, reason):
        out["dropped"].append({"rule": rule, "reason": reason})
        return out

    if not text or not text.strip():
        return out

    keywords = cfg.get("events") or DEFAULT_EVENTS
    max_hours = int(cfg.get("max_hours") or 8)

    clock = find_clock(text)
    if clock is None:
        if _WHEN_ASK_RE.search(text):
            return drop("Q1", "问的是几点，不是约定")
        remain = _REMAIN_RE.search(text)
        if remain:
            event = _find_event(text, {"idx": remain.start(), "length": 0}, keywords)
            if event and force:
                minutes = cn_to_int(remain.group(1))
                if minutes and 0 < minutes <= max_hours * 60:
                    at = now.timestamp() + minutes * 60
                    moment = datetime.fromtimestamp(at)
                    out["hit"] = {
                        "at": at, "label": event[0], "kind": event[1],
                        "time_text": moment.strftime("%H:%M"),
                        "raw": text.strip()[:20], "source": "gray",
                    }
                    return out
            if event:
                out["gray"] = {"text": text, "reason": "剩余时长需推算", "kind": "remain"}
        return out

    idx = clock["idx"]
    kw_len = clock["length"]

    if rules.is_question_clause(text, idx, kw_len=kw_len):
        return drop("Q1", "疑问句，分不清是问他还是说约定")
    if rules.is_third_person_subject(text, idx):
        return drop("P1", "说的是别人的事")
    if rules.negated_by_clause(text, idx, NEGATIONS, window=4):
        return drop("N1", "否定了，这件事不会发生")
    if rules.is_future_clause(text, idx) and not re.search(r"今天|今早|今晚|今儿", text):
        return drop("F1", "说的是别的日子")
    if re.search(r"(下课|放学|下班|收工|散会|考完|结束|到家|到了)[了啦咯喽]", text):
        return drop("D1", "已经发生了")

    event = _find_event(text, clock, keywords)
    if not event:
        return drop("E1", "有时间但没有对应的事")

    label, kind, _event_idx = event
    at = resolve_at(clock["h"], clock["m"], clock["period"], now, max_hours)
    if at is None:
        return drop("T1", "这个时刻已经过去，或者太远")

    seconds = _day_seconds(at, now)
    if kind not in ("end", "arrive") and (seconds >= 23 * 3600 or seconds < 7 * 3600):
        return drop("M1", "深夜，不吵醒他")

    if _VAGUE_RE.search(text) and not force:
        out["gray"] = {"text": text, "reason": "时间说法模糊", "kind": "vague"}
        return out

    moment = datetime.fromtimestamp(at)
    out["hit"] = {
        "at": at,
        "label": label,
        "kind": kind,
        "time_text": moment.strftime("%H:%M"),
        "raw": text.strip()[:20],
        "source": "gray" if force else "kw",
    }
    return out


# ---------------------------------------------------------------- 调度
def stage_of(ap, now: float, cfg: dict):
    """这次该按哪一拍走：lead / due / follow / None。纯读。"""
    if not isinstance(ap, dict):
        return None
    at = float(ap.get("at") or 0)
    if at <= 0:
        return None
    if int(ap.get("fails") or 0) >= 2:
        return None
    grace = int(cfg.get("grace_minutes") or 20) * 60
    if now > at + grace:
        return None

    if now >= at - 60:
        if not ap.get("due_sent_at"):
            return "due"
        follow_at = float(ap.get("follow_up_at") or 0)
        if (follow_at and now >= follow_at - 60
                and int(ap.get("follow_ups") or 0) < int(cfg.get("max_followups") or 0)):
            return "follow"
        return None

    if ap.get("stage") == "pending" and cfg.get("lead_enabled", True):
        lead_at = at - int(cfg.get("lead_minutes") or 0) * 60
        if now >= lead_at - 60:
            return "lead"
    return None


def next_delay(ap, now: float, current_minutes: float, cfg: dict):
    """把下一次触发往约定上靠；返回 None 表示不干预。"""
    if not isinstance(ap, dict):
        return None
    at = float(ap.get("at") or 0)
    if at <= 0 or int(ap.get("fails") or 0) >= 2:
        return None
    grace = int(cfg.get("grace_minutes") or 20) * 60
    if now > at + grace:
        return None

    stage = str(ap.get("stage") or "pending")

    # 到点了但还没发：不管之前排了多久，直接对准
    if not ap.get("due_sent_at") and now >= at - 60:
        return max(1.0, (at - now) / 60.0)

    if stage == "pending":
        lead_at = at - int(cfg.get("lead_minutes") or 0) * 60 if cfg.get("lead_enabled", True) else at
        if now >= lead_at - 60:
            return max(1.0, (at - now) / 60.0)
        candidate = now + float(current_minutes) * 60.0
        merge = int(cfg.get("merge_minutes") or 0) * 60
        if candidate >= lead_at - merge:
            # 采样点本来就快到了：把这个点让给提前拍，不额外多一条
            return max(1.0, (lead_at - now) / 60.0)
        return None

    if stage == "lead":
        # 提前条已经发过：这两分钟里不插任何普通消息
        return max(1.0, (at - now) / 60.0)

    if stage == "due":
        follow_at = float(ap.get("follow_up_at") or 0)
        if (follow_at > now and cfg.get("followup_enabled", True)
                and int(ap.get("follow_ups") or 0) < int(cfg.get("max_followups") or 0)):
            return max(1.0, (follow_at - now) / 60.0)
    return None


# ---------------------------------------------------------------- 提示词
DEFAULT_HINTS = {
    "lead": (
        "现在 {hhmm}，还有 {n} 分钟到 {time_text}。他之前说「{raw}」。"
        "别提前把话说满，轻轻提一句快到点了，或者问他是不是快好了。"
    ),
    "due": (
        "现在 {hhmm}，到 {time_text} 了。他之前说「{raw}」。"
        "按这件事已经发生来接话——别问「{label}了吗」，直接当成已经结束，说眼前的事。"
    ),
    "follow": (
        "现在 {hhmm}，{label}的点早过了，他还没回你。"
        "别追问、别质问，用一句轻的收个尾，或者换个话题自己起头。"
    ),
}


def build_hint(ap: dict, shot: str, now: datetime, template: str = "") -> str:
    """渲染这一拍的场景描述。template 可被 web_override 覆盖。"""
    if not isinstance(ap, dict) or not shot:
        return ""
    at = float(ap.get("at") or 0)
    moment = datetime.fromtimestamp(at) if at > 0 else now
    minutes_left = max(1, int(round((at - now.timestamp()) / 60))) if at > 0 else 0
    body = template or DEFAULT_HINTS.get(shot) or ""
    try:
        return body.format(
            hhmm=now.strftime("%H:%M"),
            time_text=str(ap.get("time_text") or moment.strftime("%H:%M")),
            label=str(ap.get("label") or ""),
            raw=str(ap.get("raw") or ""),
            n=minutes_left,
        ).strip()
    except (KeyError, IndexError, ValueError):
        return body.format(
            hhmm=now.strftime("%H:%M"),
            time_text=str(ap.get("time_text") or ""),
            label=str(ap.get("label") or ""),
            raw=str(ap.get("raw") or ""),
            n=minutes_left,
        ) if "{" not in body else body
