"""Parse explicit, one-off bedtime reminders independently of recurring sleep windows."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import text_rules as rules
from .appointment import _alternative_clock, _dated_at, _selected_day, find_clock, resolve_clock

GRACE_SECONDS = 5 * 60

_SLEEP = re.compile(r"催(?:我|哥哥|你)?(?:去)?睡|睡觉|睡了|睡下|睡这么|去睡|晚安|(?:早|先)点?睡|闭眼|该睡|早点(?:歇|休息)")
_OTHER_ACTIVITY = re.compile(r"起床|下课|上课|下班|开会|吃(?:饭|早饭|午饭|晚饭)|发消息|交作业|打卡")
_REQUEST = re.compile(r"(?:再|到点(?:再)?|记得|请|麻烦)?(?:催|提醒|叫)(?:一下|一声|我|哥哥|你){0,3}(?:去?睡(?:觉)?|休息)?")
_ELLIPTICAL = re.compile(r"(?:再催(?:一下|一声|我|哥哥)?|(?:再|到点)提醒我?|到点(?:再)?叫我)(?:一次)?[吧喵呀啊啦呢咯哦～~\s]*$")
_CANCEL = re.compile(r"(?:取消(?:掉)?(?:之前|刚才|今晚|那个|的){0,3}(?:催睡|睡觉提醒|定点催睡)|(?:别|不用|不要|不必)(?:今晚|再|到点|一直){0,3}(?:催(?:我|哥哥)?(?:睡(?:觉)?)?|提醒(?:我|哥哥)?睡觉))(?:了|啦|吧|喵|呢|呀|啊|咯|哦|\s)*$")
_EDIT = re.compile(r"(?:把(?:刚才的|之前的|那个)?(?:催睡|睡觉提醒)(?:时间)?|催睡时间|睡觉提醒时间)?(?:改到|挪到|推迟到|改为|改成)")
_EDIT_TAIL = re.compile(r"(?:再?(?:催|提醒|叫)(?:我|哥哥)?(?:去?睡(?:觉)?)?)?[吧喵呀啊啦呢咯哦～~\s]*$")
_OTHER_LITERAL = re.compile(r"引用|复述|举例|只是说|这句|这句话|这几个字|(?:以前|昨天|上次).*催")
_CLAUSES = re.compile(r"[^，,。！？!?；;\r\n、]+")
_NEGATIVE_REQUEST = re.compile(r"(?:不要|不用|别|不必|不需要|没说要|没让|没叫|不是让|不想).{0,18}(?:催|提醒|叫)")
_QUOTED = re.compile(r'「[^」\n]*」|“[^”\n]*”|『[^』\n]*』|"[^"\n]*"|\x60[^\x60\n]*\x60')
_COMMIT = re.compile(
    r"(?:到点|到时候|届时).{0,14}(?:催|叫|提醒).{0,6}(?:睡|你|哥哥)?|"
    r"(?:会|就|再|一定|准时|负责|说好了|答应).{0,8}(?:催|提醒|叫)(?:我|你|哥哥)?(?:睡(?:觉)?)?|"
    r"(?:已经|已|都|刚).{0,6}(?:记(?:下|好|上|住|牢)|设(?:好|置)|安排好|排好|登记|定好)|"
    r"(?:点|[:：]\d{2}).{0,9}(?:催|提醒|叫)(?:你|哥哥)?(?:睡(?:觉)?)?"
)


@dataclass(frozen=True)
class Candidate:
    start: int
    end: int
    clock: dict | None = None
    explicit: bool = False
    cancel: bool = False


def candidates(text, *, pending=False):
    visible = _QUOTED.sub(lambda match: " " * len(match[0]), text)
    for clause in _CLAUSES.finditer(visible):
        value = clause[0].strip()
        if _OTHER_LITERAL.search(value):
            continue
        if any(guard(visible, clause.start()) for guard in (
                rules.is_reported_clause, rules.is_conditional_clause, rules.is_dream_clause,
                rules.is_far_past_clause, rules.is_third_person_clause)):
            continue
        explicit = bool(_SLEEP.search(value))
        if (explicit or pending) and _CANCEL.fullmatch(value):
            if not rules.is_question_clause(visible, clause.start(), kw_len=len(clause[0])):
                yield Candidate(*clause.span(), cancel=True)
            continue
        clock = find_clock(value)
        if _OTHER_ACTIVITY.search(value) or _NEGATIVE_REQUEST.search(value):
            continue
        edit = _EDIT.match(value)
        editing = bool(edit and (pending or explicit))
        request = _REQUEST.search(value)
        if not (editing or explicit and request or _ELLIPTICAL.search(value)):
            continue
        if not clock:
            if editing or re.search(r"[0-9一二三四五六七八九十两零]+(?:点|[:：])", value):
                yield Candidate(*clause.span(), explicit=explicit or editing)
            continue
        clock["idx"] += clause.start() + len(clause[0]) - len(clause[0].lstrip())
        if editing and not _EDIT_TAIL.fullmatch(visible[clock["idx"] + clock["length"]:clause.end()].strip()):
            continue
        if not pending and edit and not explicit:
            continue
        yield Candidate(*clause.span(), clock, explicit or editing)


def is_sleep_topic(text):
    value = _QUOTED.sub(lambda match: " " * len(match[0]), text)
    for clause in _CLAUSES.finditer(value):
        topic = _SLEEP.search(clause[0])
        if topic and not _OTHER_ACTIVITY.search(clause[0]) and not any(
                guard(value, clause.start() + topic.start()) for guard in (
                    rules.is_reported_clause, rules.is_conditional_clause, rules.is_dream_clause,
                    rules.is_third_person_clause)):
            return True
    return False


def target(text, candidate, now: datetime, *, night_start, wake, max_hours, days):
    clock = dict(candidate.clock)
    hour = clock["h"]
    explicit_day = _selected_day(text, clock["idx"], clock["length"], now=now)
    clause = text[candidate.start:candidate.end]
    offset = clock["idx"] - candidate.start
    remaining = clause[:offset] + " " * clock["length"] + clause[offset + clock["length"]:]
    if (_alternative_clock(text, clock) or find_clock(remaining)
            or re.search(r"(?:还是|或者|或是)\s*(?:凌晨|晚上)?\s*[一二三四五六七八九十零两\d]+(?:点|[:：])", remaining)):
        return None
    if not clock["period"] and not clock.get("reverse"):
        pair = (now.hour, now.minute)
        token = text[clock["idx"]:clock["idx"] + clock["length"]]
        if explicit_day is None and pair >= night_start and (hour <= 12):
            clock["period"] = "night"
        elif (1 <= hour <= 12 and not re.match(r"0[0-5][:：]", token)
              and not (explicit_day is None and pair < wake and hour <= 5)):
            return None
    if explicit_day is not None:
        at = _dated_at(clock, explicit_day, now, days, max_hours)
    else:
        at = resolve_clock(clock, now, max_hours)
    if at is None:
        return None
    return at if at > now.timestamp() else None


def night_of(stamp, night_start):
    local = datetime.fromtimestamp(stamp)
    if (local.hour, local.minute) < night_start:
        local -= timedelta(days=1)
    return local.date().isoformat()


def early_nudge(text):
    visible = _QUOTED.sub(" ", text)
    return not re.search(r"如果|下次|到点|等到|的时候", visible) and bool(re.search(
        r"(?:现在|该|快|赶紧|早点|去|先)(?:点|去|可以|就|该|乖乖|躺下|闭眼){0,4}(?:睡|休息)", visible))


def confirmation(result):
    status = result["status"]
    if status == "saved":
        return f"好，到{datetime.fromtimestamp(result['at']):%m月%d日 %H:%M}再催你睡。"
    if status == "cancelled":
        return "好，这次约好的催睡取消啦。"
    if status == "disabled":
        return "晚睡提醒现在关着，这次没排上喔。"
    if status == "failed":
        return "这次提醒刚才没存上，先别等我到点来催喔。"
    if status == "stale":
        return "刚才那条催睡安排已经更新啦。"
    if status == "sleeping":
        return "现在已经在睡眠免打扰里，这次没再安排提醒喔。"
    if status == "unknown_topic":
        return "你想让我到时候提醒哪件事呀？"
    return "你是想让我几点催你睡呀？说一下是今晚还是明天喔。"


def refine_confirmation(text, result):
    committed = _COMMIT.search(_QUOTED.sub(" ", text))
    status = result["status"]
    if status != "saved":
        answer = confirmation(result)
        parts = re.split(r"(?<=[。！？!?；;\n])", text)
        kept = [part.strip() for part in parts if part.strip()
                and not _COMMIT.search(_QUOTED.sub(" ", part)) and not _SLEEP.search(part)
                and not re.search(r"催|叫醒|提醒|记下|记住|排好|登记|你指|哪件事|什么时候|几点", part)]
        return "\n".join([answer, *kept]), "timed_sleep_" + status
    if committed:
        announced = find_clock(text)
        if announced:
            received = datetime.fromtimestamp(result["received_at"]).replace(tzinfo=None)
            probe = Candidate(0, len(text), announced, explicit=True)
            at = target(text, probe, received, night_start=result["night_start"], wake=result["wake"],
                        max_hours=result["max_hours"], days=result["days"])
            if at == result["at"]:
                return text, None
        return confirmation(result), "timed_sleep_corrected"
    return text, None
