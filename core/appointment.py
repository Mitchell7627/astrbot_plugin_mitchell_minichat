# -*- coding: utf-8 -*-
"""约定时间点：从一句话里认出「他几点会做什么」。

和事件系统互补——事件管「正在做什么」（吃饭、洗澡，按 TTL 过期），
本模块管「几点会到一个时刻」（11:50 下课、上到十二点）。

纯函数、零 IO、不读配置（配置由 runner 传入），便于离线单测。
"""

from __future__ import annotations

import re
from datetime import date as CalendarDate, datetime, timedelta
from typing import NamedTuple

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

# 时段词按所属分句中「离钟点越近越优先」找。
_PERIOD_WORDS = (
    (("凌晨",), "dawn"),
    (("早上", "早晨", "上午", "今早", "明早", "明晨"), "am"),
    (("中午", "正午"), "noon"),
    (("下午", "午后"), "pm"),
    (("傍晚",), "pm"),
    (("晚上", "夜里", "今晚", "晚间", "明晚"), "night"),
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
    "wake": ["起床", "起来", "叫醒", "喊醒"],
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
    left = rules.clause_start(text, start, None)
    head = text[left:start]
    best = None
    for words, period in _PERIOD_WORDS:
        for word in words:
            idx = head.rfind(word)
            if idx < 0:
                continue
            pos = idx + len(word)
            if best is None or pos > best[0]:
                best = (pos, period)
    if best:
        return best[1]
    # 仅继承相邻的纯时段短句，不借用「今晚有事」等其他活动的时段。
    return _pure_period(rules.prev_clause(text, left, 32).strip())


def _degree_point(text: str, match: re.Match) -> bool:
    """裸「一点」需要钟点结构佐证；数量/程度和不确定用法不生成钟点拒绝。"""
    # 在可选分钟之前排除叠词：「一点一点」中的第二个「一」不是分钟。
    if re.match(r"一\s*点\s*一\s*点", text[match.start():]):
        return True
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
_DATE_NUMBER = rf"[0-9{_CN_CHARS}]+"
_CALENDAR_RE = re.compile(
    rf"(?<![0-9{_CN_CHARS}])(?:(?P<year>{_DATE_NUMBER})年)?"
    rf"(?P<month>{_DATE_NUMBER})月(?P<day>{_DATE_NUMBER})(?:日|号)"
)
_NUMERIC_DATE_RE = re.compile(
    r"(?<![\d./-])(?:(?P<year>\d{4})[-/.])?"
    r"(?P<month>\d{1,2})[-/.](?P<day>\d{1,2})(?![\d./-])"
)
_MONTH_DAY_RE = re.compile(
    rf"(?<![0-9{_CN_CHARS}])(?:(?:本|这个|这)月)?(?P<day>{_DATE_NUMBER})(?:号|日)"
)
_RELATIVE_DAY_RE = re.compile(
    rf"(?:(?:再过|过)\s*(?P<after>{_DATE_NUMBER}|几)\s*(?:天|日)(?:以后|之后|后)?"
    rf"|(?P<count>{_DATE_NUMBER}|几)\s*(?:天|日)(?:以后|之后|后))"
)
_EXTRA_RELATIVE_RE = re.compile(rf"(?:(?:前)(?P<past>{_DATE_NUMBER})(?:天|日)|(?P<n>{_DATE_NUMBER})(?P<unit>天|日|周|星期)(?P<direction>前|后))")
_UNKNOWN_DATE_RE = re.compile(r"(?<![\d/])\d{2}/\d{1,2}/\d{1,2}(?![\d/])")
_DATE_PATTERNS = (_CALENDAR_RE, _NUMERIC_DATE_RE, _MONTH_DAY_RE, _RELATIVE_DAY_RE)
_CHANGE_RE = re.compile(r"改到|改为|改成|挪到|推迟到")
# 必须带星期单位，避免把普通「一」当作日期；允许「下周星期一」。
_WEEKDAY_RE = re.compile(
    r"(?:(?P<week>上+|下+|这|本)(?:一?个|一)?)?"
    r"(?:周|星期|礼拜)(?:(?:周|星期|礼拜))?(?P<weekday>[一二三四五六日天1-7末])"
)


def _pure_period(text: str):
    """纯时段或「日期+时段」短句可以继承，不能含另一个活动。"""
    for words, period in _PERIOD_WORDS:
        for word in words:
            if text.endswith(word):
                date = text[:-len(word)].strip()
                if not date or any(p.fullmatch(date) for p in (_DAY_RE, *_DATE_PATTERNS, _WEEKDAY_RE)):
                    return period
    return None


class _DayMarker(NamedTuple):
    start: int
    end: int
    is_today: bool
    target: CalendarDate | None = None


def _date_number(raw: str, *, year: bool = False) -> int:
    if raw.isdigit():
        return int(raw)
    if year:
        return int("".join(str(_CN_DIGITS[c]) for c in raw))
    value = cn_to_int(raw)
    if value is None:
        raise ValueError("invalid calendar number")
    return value


def _day_markers(text: str, now: datetime) -> list[_DayMarker]:
    """日期与本日比较；非法日期也保留为拒绝标记，不能回退成没有日期。"""
    offsets = {w: 0 for w in _TODAY_WORDS}
    offsets.update({w: 1 for w in ('明天', '明日', '明早', '明晚', '明晨', '明儿', '明个')})
    offsets.update({'后天': 2, '大后天': 3, '昨天': -1, '昨日': -1, '昨晚': -1, '昨儿': -1, '昨夜': -1, '前天': -2, '大前天': -3})
    text = text.translate(str.maketrans("／．－", "/.-"))
    found = [_DayMarker(m.start(), m.end(), m.group() in _TODAY_WORDS,
                        now.date() + timedelta(days=offsets[m.group()]) if m.group() in offsets else None)
             for m in _DAY_RE.finditer(text)]
    for match in _CALENDAR_RE.finditer(text):
        try:
            year = _date_number(match['year'], year=True) if match['year'] else now.year
            target = datetime(year, _date_number(match['month']), _date_number(match['day'])).date()
            today = target == now.date()
        except (ValueError, KeyError, OverflowError):
            today = False
            target = None
        found.append(_DayMarker(match.start(), match.end(), today, target))
    for pattern in (_NUMERIC_DATE_RE, _MONTH_DAY_RE, _RELATIVE_DAY_RE):
        for match in pattern.finditer(text):
            try:
                parts = match.groupdict()
                if pattern is _RELATIVE_DAY_RE:
                    days = _date_number(parts.get('after') or parts['count'])
                    target = now.date() + timedelta(days=days)
                else:
                    target = CalendarDate(int(parts.get('year') or now.year),
                                          int(parts.get('month') or now.month),
                                          _date_number(parts['day']))
            except (ValueError, KeyError, OverflowError):
                target = None
            found.append(_DayMarker(match.start(), match.end(), target == now.date(), target))
    for match in _EXTRA_RELATIVE_RE.finditer(text):
        try:
            count = _date_number(match['past'] or match['n'])
            delta = count * (7 if match['unit'] in ('周', '星期') else 1)
            if match['past'] or match['direction'] == '前':
                delta = -delta
            target = now.date() + timedelta(days=delta)
        except (ValueError, OverflowError):
            target = None
        found.append(_DayMarker(match.start(), match.end(), target == now.date(), target))
    for match in _UNKNOWN_DATE_RE.finditer(text):
        found.append(_DayMarker(match.start(), match.end(), False, None))
    weekdays = {c: i for i, c in enumerate("一二三四五六日")}
    weekdays.update({str(i + 1): i for i in range(7)})
    weekdays['天'] = 6
    for match in _WEEKDAY_RE.finditer(text):
        weekday = weekdays.get(match['weekday'])
        target = None
        if weekday is not None:
            offset = weekday - now.weekday()
            if match['week'] and match['week'].startswith('下'):
                offset += 7 * len(match['week'])
            elif match['week'] and match['week'].startswith('上'):
                offset -= 7 * len(match['week'])
            elif match['week'] is None:
                offset %= 7  # 裸星期取最近一次，含今天；本/这周不滚动。
            target = now.date() + timedelta(days=offset)
        found.append(_DayMarker(match.start(), match.end(), target == now.date(), target))
    out = []
    for marker in sorted(found, key=lambda m: (m.start, -(m.end - m.start))):
        if not out or marker.start >= out[-1].end:
            out.append(marker)
    return out


def _previous_day_markers(text: str, start: int, now: datetime) -> list[_DayMarker]:
    # 只穿过连续的纯时间短句，不跨过另一个活动或完整句号。
    for _ in range(4):
        if start <= 0 or text[start - 1] not in '，,、 ':
            break
        previous = rules.prev_clause(text, start, 32).strip()
        markers = _day_markers(previous, now)
        if len(markers) == 1 and (markers[0].start == 0 and markers[0].end == len(previous)
                                  or _pure_period(previous) is not None):
            return markers
        if _pure_period(previous) is None:
            break
        start = rules.clause_start(text, start - 1)
    return []


def _selected_day(text: str, idx: int, length: int = 0, *, now: datetime) -> _DayMarker | None:
    """日期绑定钟点所在分句，优先取钟点前最近的日期；不把其他日子改记今天。"""
    text = _date_safe_text(text)
    start = rules.clause_start(text, idx, None)
    end = rules.clause_end(text, idx + length)
    clause = text[start:end]
    markers = _day_markers(clause, now)
    for left, right in zip(markers, markers[1:]):
        if (left.target != right.target
                and re.fullmatch(r"[\s的（(]*(?:(?:也就是|也即|即|是)[\s的（(]*)?", clause[left.end:right.start])):
            # 「11月4日（周一）」等互相矛盾的同一日期描述不能猜。
            return _DayMarker(left.start, right.end, False, None)
    changes = [m for m in markers if re.search(r"(?:改到|改为|改成|挪到|推迟到|安排在)\s*$", clause[:m.start])]
    if changes:
        return changes[-1]
    dates = [m for m in markers if m.end <= idx - start]
    if dates:
        return dates[-1]
    if markers:
        return markers[-1]
    previous_dates = _previous_day_markers(text, start, now)
    if previous_dates:
        return previous_dates[0]
    return None


def _supported_day(text: str, idx: int, length: int = 0, *, now: datetime) -> bool:
    marker = _selected_day(text, idx, length, now=now)
    if marker is not None:
        return marker.is_today
    return not rules.is_future_clause(text, idx)


def _dated_at(clock: dict, marker: _DayMarker, now: datetime, days: int, max_hours: int = 48) -> float | None:
    if marker.target is None or not 0 <= (marker.target - now.date()).days <= days:
        return None
    if marker.is_today:
        at = resolve_clock(clock, now, max_hours, anchored=bool(clock.get('reverse')))
        if at is not None and at <= now.timestamp() - 300:
            return None
        if at is not None and clock.get('period') != 'night' and datetime.fromtimestamp(at).date() != now.date():
            return None
        return at
    anchor = now.replace(year=marker.target.year, month=marker.target.month, day=marker.target.day,
                         hour=0, minute=0, second=0, microsecond=0)
    at = resolve_clock(clock, anchor, (days + 2) * 24, anchored=bool(clock.get('reverse')))
    if at is None or at <= now.timestamp() - 300:
        return None
    if (datetime.fromtimestamp(at).date() - now.date()).days > days:
        return None
    return at


def _date_safe_text(text: str) -> str:
    # 日期里的小数点不是分句边界；保持字符串长度及索引不变。
    text = text.translate(str.maketrans('／．－', '/.-'))
    return _NUMERIC_DATE_RE.sub(lambda m: m.group().replace('.', '/'), text)


def find_clock(text: str):
    """返回 {"h","m","idx","length","period"} 或 None。冒号写法优先。"""
    # 「周一一点」不能把星期中的「一」吞进小时；遮罩保持原索引。
    text = _date_safe_text(text)
    scan = text
    for pattern in (*_DATE_PATTERNS, _WEEKDAY_RE):
        for date in pattern.finditer(text):
            scan = scan[:date.start()] + " " * (date.end() - date.start()) + scan[date.end():]
    for match in _COLON_RE.finditer(scan):
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            continue
        return {
            "h": hour, "m": minute, "idx": match.start(),
            "length": match.end() - match.start(),
            "period": _find_period(text, match.start()),
        }

    for match in _DIFF_RE.finditer(scan):
        # 差一刻五点 = 4:45；差五分五点 = 4:55
        offset = _MINUTE_WORDS.get(match.group(1))
        if offset is None:
            offset = cn_to_int(match.group(2))
        hour = cn_to_int(match.group(3))
        if hour is not None and offset is not None and 0 <= hour <= 23 and 0 <= offset <= 59:
            return {
                "h": (hour - 1) % 24 if offset else hour, "m": 60 - offset if offset else 0,
                "idx": match.start(),
                "length": match.end() - match.start(),
                "period": _find_period(text, match.start()),
                "reverse": {"hour": hour, "minutes": offset},
            }
    # 非法差分短语不能再从内部捡出「五点」；后续独立的合法钟点仍可识别。
    scan = _DIFF_RE.sub(lambda m: ' ' * len(m.group()), scan)

    for match in _POINT_RE.finditer(scan):
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


def _clock_base(hour: int, minute: int, period: str | None, now: datetime):
    """先转换原始钟点的时段，再做差分，避免跨午夜/中午时错换十二小时。"""
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    day_offset = 0
    if period in ('am', 'dawn') and hour > 12:
        return None
    if period == "dawn" and hour == 12:
        hour = 0
    elif period == "noon" and hour <= 2:
        hour += 12
    elif period == "night":
        if hour <= 5 or hour == 12:
            # 夜里一点 / 晚上十二点属于这一晚跨过午夜后的时刻。
            hour = 0 if hour == 12 else hour
            day_offset = 1
        elif hour <= 11:
            hour += 12
        elif hour < 18:
            # 不把「晚上十三点」这种矛盾时段改记为下午。
            return None
    elif period == "pm":
        # 午夜由 night 分支处理；下午十二点仍是当日中午。
        if hour <= 11:
            hour += 12

    return now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=day_offset)


def _resolve_base(base: datetime, period: str | None, now: datetime, max_hours: int):
    hour = base.hour
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


def resolve_at(hour: int, minute: int, period: str | None,
               now: datetime, max_hours: int):
    """把普通钟点落成时间戳；过去 / 太远 / 非法返回 None。"""
    base = _clock_base(hour, minute, period, now)
    return _resolve_base(base, period, now, max_hours) if base is not None else None


def resolve_clock(clock: dict, now: datetime, max_hours: int, *, anchored: bool = False):
    reverse = clock.get('reverse')
    if not reverse:
        return resolve_at(clock['h'], clock['m'], clock.get('period'), now, max_hours)
    base = _clock_base(reverse['hour'], 0, clock.get('period'), now)
    if base is None:
        return None
    base -= timedelta(minutes=reverse['minutes'])
    if anchored:
        # 明确日期以目标钟点为锚；「明天凌晨差一刻零点」落在今天23:45。
        return base.timestamp() if base <= now + timedelta(hours=max_hours) else None
    return _resolve_base(base, clock.get('period'), now, max_hours)


def _find_event(text: str, clock: dict, keywords: dict):
    """在时间附近找事件词，返回 (label, kind, idx, length) 或 None。"""
    start, end = clock["idx"], clock["idx"] + clock["length"]
    left = rules.clause_start(text, start, None)
    right = rules.clause_end(text, end)

    # 「明天，下午三点，我要开会」只允许纯时间短句连接紧邻活动。
    temporal = text[left:right]
    for marker in sorted(_day_markers(temporal, datetime.now()), reverse=True):
        temporal = temporal[:marker.start] + ' ' * (marker.end - marker.start) + temporal[marker.end:]
    local_start, local_end = start - left, end - left
    temporal = temporal[:local_start] + ' ' * (local_end - local_start) + temporal[local_end:]
    for words, _ in _PERIOD_WORDS:
        for word in words:
            temporal = temporal.replace(word, ' ')
    if not temporal.strip() and right < len(text) and text[right] in '，,、':
        right = rules.clause_end(text, right + 1)

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
    text = _date_safe_text(text)
    work, clocks = text, []
    while clock := find_clock(work):
        clocks.append(clock)
        start, length = clock["idx"], clock["length"]
        work = work[:start] + " " * length + work[start + length:]
    if not clocks:
        return _parse_one(text, now, cfg, force)
    dropped, gray = [], None
    for clock in sorted(clocks, key=lambda c: c["idx"]):
        left = rules.clause_start(text, clock['idx'])
        right = rules.clause_end(text, clock['idx'] + clock['length'])
        changes = list(_CHANGE_RE.finditer(text, left, right))
        if changes and any(c['idx'] > changes[-1].end() and c['idx'] < right for c in clocks):
            if clock['idx'] < changes[-1].start():
                continue
        result = _parse_one(text, now, cfg, force, clock)
        dropped.extend(result["dropped"])
        if result["hit"]:
            result["dropped"] = dropped
            return result
        gray = gray or result["gray"]
    return {"hit": None, "gray": gray, "dropped": dropped}


def _wake_request(text, idx):
    clause = text[rules.clause_start(text, idx):rules.clause_end(text, idx)]
    return bool(re.search(r'(?:叫醒|喊醒)(?:一下)?我|(?:叫|喊|提醒|让)我(?:一下)?(?:起床|起来)', clause))


def wake_target_is_self(text, idx, length, label):
    """同一套对象检查覆盖「叫醒我」与「叫我起床」，语气尾不充当对象。"""
    left, right = rules.clause_start(text, idx), rules.clause_end(text, idx + length)
    if label in ('叫醒', '喊醒'):
        object_before = re.search(r'(?:把|将)([^，,。！？!?；;\n]{1,12})$', text[left:idx])
        if object_before and not re.fullmatch(r'我(?:自己)?', object_before.group(1)):
            return False
        target = text[idx + length:right].strip()
        return not target or bool(re.fullmatch(
            r'(?:一下)?(?:我(?:一下)?)?(?:吧|呀|啊|啦|哦|喔|哟|噢|哈|就行|就好|就可以了|谢谢|拜托|\s)*', target))
    request = re.search(r'(?:叫|喊|提醒|通知|让)([^，,。！？!?；;\n]{0,16})$', text[left:idx])
    return request is None or bool(re.fullmatch(r'(?:一下)?我(?:自己)?(?:一下)?', request.group(1)))


def _alternative_clock(text, clock):
    left = rules.clause_start(text, clock['idx'])
    right = rules.clause_end(text, clock['idx'] + clock['length'])
    clause = text[left:right]
    if re.search(r'或者|或是|或|二选一', clause):
        return True
    # 备选关系可跨逗号；句号、分号和独立转折不继承。
    if right < len(text) and text[right] in '，,':
        following = text[right + 1:rules.clause_end(text, right + 1)].lstrip()
        if re.match(r'(?:或者|或是|或)', following):
            return True
        cursor = right
        while cursor < len(text) and text[cursor] in '，,':
            end = rules.clause_end(text, cursor + 1)
            following = text[cursor + 1:end].strip()
            if following.startswith('二选一'):
                return True
            if not find_clock(following):
                break
            cursor = end
    return False


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
            if event and not _supported_day(text, remain.start(), len(remain.group()), now=now):
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
    if rules.is_third_person_clause(text, idx) or (rules.is_second_person_clause(text, idx) and not _wake_request(text, idx)):
        return drop("P1", "说的是别人的事")
    if rules.negated_by_clause(text, idx, NEGATIONS, window=4):
        return drop("N1", "否定了，这件事不会发生")
    clause = text[rules.clause_start(text, idx):rules.clause_end(text, idx + kw_len)]
    changes = list(_CHANGE_RE.finditer(clause))
    if changes:
        change_idx = rules.clause_start(text, idx) + changes[-1].start()
        if (rules.negated_by_clause(text, change_idx, NEGATIONS, window=len(clause))
                or re.search(r'考虑|可能|也许|打算|准备|失败|没成功|不成功', clause)):
            return drop('N1', '否定、意向或失败的改期，不修改约定')
    if _alternative_clock(text, clock):
        return drop('A2', '备选时间尚未确定，不登记')
    day = _selected_day(text, idx, kw_len, now=now)
    future_days = max(0, min(365, int(cfg.get('future_days', 0))))
    if ((day is not None and (day.target is None or not 0 <= (day.target - now.date()).days <= future_days))
            or day is None and not _supported_day(text, idx, kw_len, now=now)):
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
    if kind == 'wake' and not wake_target_is_self(text, _event_idx, _event_len, label):
        return drop('P1', '唤醒对象不是明确的本人，不登记')
    if kind == 'wake' and (re.match(r'了|啦|咯|过', text[_event_idx + _event_len:]) or re.search(r'已经|刚刚|刚才', text[rules.clause_start(text, _event_idx):_event_idx])):
        return drop('D1', '已经起床，不建立新的唤醒预约')
    if kind == 'wake' and label == '起来' and _event_idx > 0:
        if text[_event_idx - 1] not in '再才就要会先我点分整半 ，,：:0123456789':
            return drop('E1', '不是起床语境，不建立唤醒预约')
    if rules.clause_start(text, _event_idx) != rules.clause_start(text, idx):
        action_start = rules.clause_start(text, _event_idx)
        action_clause = text[action_start:rules.clause_end(text, _event_idx + _event_len)]
        action_days = _day_markers(action_clause, now)
        target_day = day.target if day else now.date()
        if any(m.target != target_day for m in action_days):
            return drop('F1', '相邻活动日期不同，不借用钟点')
        if find_clock(action_clause) or re.search(r'现在|正在|此刻|刚才|刚刚|已经', action_clause):
            return drop('E1', '相邻活动另有时间，不借用钟点')
        if not action_days and rules.is_future_clause(action_clause, _event_idx - action_start):
            return drop('F1', '相邻活动另有未来安排')
        if re.search(r'(下课|放学|下班|收工|散会|考完|结束|到家|到了)[了啦咯喽]', action_clause):
            return drop('D1', '相邻活动已经发生')
    # 跨分句时必须检查活动所在片段，不能借纯时间短句绕过语义保护。
    for anchor, width in ((idx, kw_len), (_event_idx, _event_len)):
        if rules.is_question_clause(text, anchor, kw_len=width):
            return drop('Q1', '疑问句，不登记')
        if rules.is_third_person_clause(text, anchor) or (rules.is_second_person_clause(text, anchor) and not _wake_request(text, anchor)):
            return drop('P1', '说的是别人的事')
        if rules.is_reported_clause(text, anchor) or rules.is_dream_clause(text, anchor):
            return drop('R1', '引用或梦境，不是本人约定')
        if rules.is_conditional_clause(text, anchor):
            return drop('C1', '是假设，不是真实安排')
        if rules.is_cancelled_clause(text, anchor, kw_len=width, window=None):
            return drop('X1', '安排已取消')
    if rules.negated_by_clause(text, _event_idx, NEGATIONS, window=4):
        return drop("N1", "否定了，这件事不会发生")
    # 否定计划/否认说过可以隔着完整日期与钟点，不受四字窗口限制。
    # 只去掉时间片段后检查紧邻谓语的否定，避免否定其他活动时牵连本约定。
    prefix_start = rules.clause_start(text, _event_idx)
    intent = text[prefix_start:_event_idx]
    if prefix_start <= idx < _event_idx:
        pos = idx - prefix_start
        intent = intent[:pos] + ' ' * kw_len + intent[pos + kw_len:]
    for marker in sorted(_day_markers(intent, now), reverse=True):
        intent = intent[:marker.start] + ' ' * (marker.end - marker.start) + intent[marker.end:]
    for words, _ in _PERIOD_WORDS:
        for word in words:
            intent = intent.replace(word, ' ' * len(word))
    if re.search(r'(?<!不是)(?:不打算|没(?:有)?打算|不准备|没(?:有)?准备|不计划|没(?:有)?计划|没(?:有)?说(?:过)?|从未说(?:过)?|不曾说(?:过)?)[\s我自己在要会再就]*$', intent):
        return drop('N1', '否定计划或否认说过，不建立约定')
    suffix = text[_event_idx + _event_len:rules.clause_end(text, _event_idx + _event_len)].strip()
    if re.match(r'(?:不成了|办不成了|开不成了|行不通了)', suffix):
        return drop('N1', '安排未能成立，不建立约定')
    # 唤醒的裸十二小时钟点取接下来最近一次，不影响普通约定的保守规则。
    if kind == 'wake' and day is None and clock.get('period') is None and clock['h'] <= 12:
        choices = [now.replace(hour=h, minute=clock['m'], second=0, microsecond=0)
                   for h in sorted({clock['h'] % 12, clock['h'] % 12 + 12})]
        choices += [v + timedelta(days=1) for v in choices]
        nearest = min(v for v in choices if v.timestamp() > now.timestamp())
        clock = {**clock, 'h': nearest.hour}
        if nearest.date() != now.date():
            day = _DayMarker(0, 0, False, nearest.date())
    at = (_dated_at(clock, day, now, future_days, max_hours) if day is not None
          else resolve_clock(clock, now, max_hours))
    if at is None:
        return drop("T1", "这个时刻已经过去，或者太远")

    seconds = _day_seconds(at, now)
    if kind not in ("end", "arrive", "wake") and (seconds >= 23 * 3600 or seconds < 7 * 3600):
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
        "date_text": moment.strftime("%Y-%m-%d"),
        "raw": text.strip()[:20],
        "source": "gray" if force else "kw",
        "idx": idx,
        "length": kw_len,
        # 事件词的位置：上层用它把「几点 + 做什么」整段消费掉，
        # 不让同一句话再落成「当前正在做」。
        "event_idx": _event_idx,
        "event_len": _event_len,
    }
    change = list(_CHANGE_RE.finditer(clause))
    if change:
        out['hit']['reschedule'] = True
        original = _parse_one(clause[:change[-1].start()], now, cfg, True)
        if original.get('hit'):
            out['hit']['replace_at'] = original['hit']['at']
    return out


# ---------------------------------------------------------------- 调度
def stage_of(ap, now: float, cfg: dict):
    """这次该按哪一拍走：lead / due / follow / None。纯读。"""
    if isinstance(ap, dict) and ap.get('kind') == 'wake':
        if now < float(ap.get('at') or 0) or ap.get('due_sent_at'):
            return None
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

    if ap.get('kind') == 'wake':
        return None if ap.get('due_sent_at') else max(1.0, (at - now) / 60.0)
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
    "wake": "现在 {hhmm}，他约好 {time_text} 叫醒。温柔叫他起床，不要声称他已经醒了。",
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
            time_text=(str(ap.get("date_text") or "") + " " + str(ap.get("time_text") or moment.strftime("%H:%M"))).strip(),
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
