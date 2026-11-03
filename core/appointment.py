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

# 「差一刻五点」「差五分五点」：倒着说的时间，先算成 4:45 / 4:55
_DIFF_RE = re.compile(
    rf"差\s*(一刻|三刻|半|(\d{{1,2}}|[{_CN_CHARS}]{{1,3}})\s*分?)\s*"
    rf"(\d{{1,2}}|[{_CN_CHARS}]{{1,3}})\s*点"
)

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
# 「快十二点下课」是估摸，「约了人」是动词——「约」只有在单独出现时才算模糊
_VAGUE_RE = re.compile(r"(快|快到|差不多|左右|前后|大概|大约|约(?!了|好|会|人|在)|将近|马上|就快)")
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
        # 「8 点约了人」「9:05 有个会」：说法很常见，但原先一个事件词都没有
        "约了", "约好", "约会", "碰面", "见面",
        "有个会", "有个局", "面试", "值班", "出差",
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


def _degree_point(text: str, match: re.Match) -> bool:
    """裸「一点」需要钟点结构佐证；数量/程度和不确定用法不生成钟点拒绝。"""
    if match.group(1) != "一" or match.group(2) or match.group(3):
        return False
    head = text[rules.clause_start(text, match.start()):match.start()].rstrip()
    tail = text[match.end():].lstrip()
    if tail.startswith(("都", "也", "点", "儿")):
        return True
    if any(head.endswith(word) for words, _ in _PERIOD_WORDS for word in words):
        return False
    if re.search(r"(?:有|还有|剩|剩下|留|留给|给我|给你|给他|给她|喝|吃|买|拿|带|加|补|借|花|用|省)(?:了|着|上|下)?$", head):
        return True
    if head.endswith((
        "快", "慢", "轻", "重", "早", "晚", "多", "少", "大", "小",
        "好", "坏", "高", "低", "远", "近", "深", "浅", "长", "短",
        "暖", "冷", "热", "直白", "清楚", "明确", "详细", "色",
    )):
        return True
    # 看时间搭配，不枚举「水/精力/建议」等无限多的数量名词。
    if tail.startswith(("钟", "左右", "前后", "的时候", "以后", "之后", "之前", "多", "开始", "准时")):
        return False
    if any(tail.startswith(word) for words in DEFAULT_EVENTS.values() for word in words):
        return False
    actions = [word for words in DEFAULT_EVENTS.values() for word in words]
    actions += ["洗澡", "洗头", "睡觉", "运动", "跑步", "做饭", "看电影", "玩游戏", "喝水", "开始", "去"]
    if re.match(r"(?:我)?(?:就|才|再|先|要|会|准备|去|正在|能)?(?:" + "|".join(map(re.escape, actions)) + r")", tail):
        return False
    if not tail.strip("了啦咯喽呀啊 ") and re.search(r"(?:等到|到|定在|约在|现在|已经)$", head):
        return False
    return True


_TODAY_WORDS = ("今天", "今日", "今儿", "今早", "今晚", "今晨", "今夜")
_OTHER_DAY_WORDS = (
    "大后天", "后天", "明天", "明日", "明早", "明晚", "明晨", "明儿", "明个",
    "下周", "下星期", "下礼拜", "下个月", "下月", "下学期", "明年", "改天", "下次", "下回",
    "大前天", "前天", "昨天", "昨日", "昨晚", "昨儿", "昨夜", "上周", "上个月", "去年",
)
_DAY_RE = re.compile("|".join(map(re.escape, sorted(_TODAY_WORDS + _OTHER_DAY_WORDS, key=len, reverse=True))))


def _supported_day(text: str, idx: int, length: int = 0) -> bool:
    """日期绑定钟点所在分句，优先取钟点前最近的日期；不把其他日子改记今天。"""
    start = rules.clause_start(text, idx, None)
    end = rules.clause_end(text, idx + length)
    head = text[start:idx]
    changes = list(re.finditer(r"(?:改到|改为|改成|挪到|推迟到|安排在)(?:" + _DAY_RE.pattern + r")", text[start:end]))
    if changes:
        return _DAY_RE.search(changes[-1].group()).group() in _TODAY_WORDS
    dates = list(_DAY_RE.finditer(head))
    if dates:
        return dates[-1].group() in _TODAY_WORDS
    # 「三点开会改到明天」同样不能落成今天。
    if _DAY_RE.search(text[idx:end]):
        return False
    previous = rules.prev_clause(text, start, 4).strip()
    if previous in _OTHER_DAY_WORDS:
        return False
    return not rules.is_future_clause(text, idx)


def find_clock(text: str):
    """返回 {"h","m","idx","length","period"} 或 None。冒号写法优先。"""
    for match in _COLON_RE.finditer(text):
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            continue
        return {
            "h": hour, "m": minute, "idx": match.start(),
            "length": match.end() - match.start(),
            "period": _find_period(text, match.start()),
        }

    match = _DIFF_RE.search(text)
    if match:
        # 差一刻五点 = 4:45；差五分五点 = 4:55
        offset = _MINUTE_WORDS.get(match.group(1))
        if offset is None:
            offset = cn_to_int(match.group(2))
        hour = cn_to_int(match.group(3))
        if offset and hour:
            return {
                "h": (hour - 1) % 24, "m": 60 - offset, "idx": match.start(),
                "length": match.end() - match.start(),
                "period": _find_period(text, match.start()),
            }

    for match in _POINT_RE.finditer(text):
        if _degree_point(text, match):
            continue
        hour = cn_to_int(match.group(1))
        if hour is None:
            continue
        if match.group(2):
            minute = _MINUTE_WORDS.get(match.group(2))
        elif match.group(3):
            minute = cn_to_int(match.group(3))
        else:
            minute = 0
        if minute is None or not (0 <= hour <= 23 and 0 <= minute <= 59):
            continue
        return {
            "h": hour, "m": minute, "idx": match.start(),
            "length": match.end() - match.start(),
            "period": _find_period(text, match.start()),
        }
    return None


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
    """在时间附近找事件词，返回 (label, kind, idx, length) 或 None。"""
    start, end = clock["idx"], clock["idx"] + clock["length"]
    left = rules.clause_start(text, start, None)
    right = rules.clause_end(text, end)

    up = _UP_TO_RE.search(text, left, right)
    if up and abs(up.start() - start) <= _MAX_GAP:
        return ("结束", "end", up.start(), len(up.group(0)))

    best = None
    for kind, words in keywords.items():
        for word in words:
            idx = text.find(word, left, right)
            while idx >= 0:
                gap = start - (idx + len(word)) if idx + len(word) <= start else idx - end
                if 0 <= gap <= _MAX_GAP:
                    score = (len(word), -gap)
                    if best is None or score > best[0]:
                        best = (score, word, kind, idx)
                idx = text.find(word, idx + 1, right)
    if best:
        return (best[1], best[2], best[3], len(best[1]))
    return None


def _day_seconds(ts: float, now: datetime) -> int:
    moment = datetime.fromtimestamp(ts)
    return moment.hour * 3600 + moment.minute * 60


def parse(text: str, now: datetime, cfg: dict, force: bool = False):
    """逐个尝试钟点；拒绝一个候选不能吞掉其他分句的有效约定。"""
    work, clocks = text, []
    while clock := find_clock(work):
        clocks.append(clock)
        start, length = clock["idx"], clock["length"]
        work = work[:start] + " " * length + work[start + length:]
    if not clocks:
        return _parse_one(text, now, cfg, force)
    dropped, gray = [], None
    for clock in sorted(clocks, key=lambda c: c["idx"]):
        result = _parse_one(text, now, cfg, force, clock)
        dropped.extend(result["dropped"])
        if result["hit"]:
            result["dropped"] = dropped
            return result
        gray = gray or result["gray"]
    return {"hit": None, "gray": gray, "dropped": dropped}


def _parse_one(text: str, now: datetime, cfg: dict, force: bool = False, candidate=None):
    """解析一句话。

    返回 {"hit": {...}|None, "gray": {...}|None, "dropped": [{rule, reason}]}。
    宁可漏，不要错——判不准的一律进 gray，由上层决定要不要问模型。
    force=True 是灰度答 YES 之后用：跳过「模糊」这一关，尽量落一个时刻。
    """
    out = {"hit": None, "gray": None, "dropped": []}
    clock = None

    def drop(rule, reason, anchor=None):
        idx = clock["idx"] if clock else anchor
        if idx is None and (question := _WHEN_ASK_RE.search(text)):
            idx = question.start()
        out["dropped"].append({"rule": rule, "reason": reason, "idx": idx})
        return out

    if not text or not text.strip():
        return out

    keywords = cfg.get("events") or DEFAULT_EVENTS
    max_hours = int(cfg.get("max_hours") or 8)

    clock = candidate or find_clock(text)
    if clock is None:
        if _WHEN_ASK_RE.search(text):
            return drop("Q1", "问的是几点，不是约定")
        remain = _REMAIN_RE.search(text)
        if remain:
            event = _find_event(text, {"idx": remain.start(), "length": 0}, keywords)
            if event and not _supported_day(text, remain.start(), len(remain.group())):
                return drop("F1", "明确其他日期，本版未登记", remain.start())
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
                out["gray"] = {"text": text, "reason": "剩余时长需推算", "kind": "remain", "idx": remain.start()}
        return out

    idx = clock["idx"]
    kw_len = clock["length"]

    if rules.is_question_clause(text, idx, kw_len=kw_len):
        return drop("Q1", "疑问句，分不清是问他还是说约定")
    if rules.is_third_person_clause(text, idx) or rules.is_second_person_clause(text, idx):
        return drop("P1", "说的是别人的事")
    if rules.negated_by_clause(text, idx, NEGATIONS, window=4):
        return drop("N1", "否定了，这件事不会发生")
    clause = text[rules.clause_start(text, idx):rules.clause_end(text, idx + kw_len)]
    if not _supported_day(text, idx, kw_len):
        return drop("F1", "明确其他日期或未来表达，本版未登记")
    # 「如果我十二点下课」「示例：'十二点下课'」「我十二点下课的安排取消了」都不是有效约定
    if rules.is_reported_clause(text, idx):
        return drop("R1", "是转述/示例，不是本人约定")
    if rules.is_conditional_clause(text, idx):
        return drop("C1", "是假设，不是真实安排")
    if rules.is_cancelled_clause(text, idx, kw_len=kw_len, window=None):
        return drop("X1", "这个安排取消了")
    if re.search(r"(下课|放学|下班|收工|散会|考完|结束|到家|到了)[了啦咯喽]", clause):
        return drop("D1", "已经发生了")

    event = _find_event(text, clock, keywords)
    if not event:
        return drop("E1", "有时间但没有对应的事")

    label, kind, _event_idx, _event_len = event
    if rules.negated_by_clause(text, _event_idx, NEGATIONS, window=4):
        return drop("N1", "否定了，这件事不会发生")
    at = resolve_at(clock["h"], clock["m"], clock["period"], now, max_hours)
    if at is None:
        return drop("T1", "这个时刻已经过去，或者太远")

    seconds = _day_seconds(at, now)
    if kind not in ("end", "arrive") and (seconds >= 23 * 3600 or seconds < 7 * 3600):
        return drop("M1", "深夜，不吵醒他")

    clause = text[rules.clause_start(text, idx):rules.clause_end(text, idx + kw_len)]
    if _VAGUE_RE.search(clause) and not force:
        out["gray"] = {"text": text, "reason": "时间说法模糊", "kind": "vague", "idx": idx}
        return out

    moment = datetime.fromtimestamp(at)
    out["hit"] = {
        "at": at,
        "label": label,
        "kind": kind,
        "time_text": moment.strftime("%H:%M"),
        "raw": text.strip()[:20],
        "source": "gray" if force else "kw",
        "idx": idx,
        "length": kw_len,
        # 事件词的位置：上层用它把「几点 + 做什么」整段消费掉，
        # 不让同一句话再落成「当前正在做」。
        "event_idx": _event_idx,
        "event_len": _event_len,
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
# 到点那一拍按约定类型分口径：以前一律「当成已经结束」，
# 「十二点开会」到点就说「会开完了」，跟事实正好相反。
DUE_HINTS_BY_KIND = {
    "start": (
        "现在 {hhmm}，{label}的点到了。他之前说「{raw}」。"
        "按这件事**刚**开始来接：别说结束、别问进行到哪一步，可以说开始了、让他安心去忙。"
    ),
    "arrive": (
        "现在 {hhmm}，按他说的该{label}了。他之前说「{raw}」。"
        "当人已经到门口或者正在进门来接，别问「到哪了」「到了没」。"
    ),
}

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
    kind = str(ap.get("kind") or "end")
    body = template or (DUE_HINTS_BY_KIND.get(kind, "") if shot == "due" else "") \
        or DEFAULT_HINTS.get(shot) or ""
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
