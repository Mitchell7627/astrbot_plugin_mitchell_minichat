"""Local comparison of delivered proactive messages."""

import hashlib
import re
import unicodedata
from datetime import datetime, timedelta

from .appointment import _day_markers, find_clock, resolve_clock
from .splitter import split_text


_TOPICS = {
    "shoulder": re.compile(
        r"(?:借|给|搭|靠|挨).{0,8}肩膀|肩膀.{0,12}(?:借|搭|靠|挨|给你)|"
        r"靠过来|让我挨着|往这边靠"
    ),
    "bed": re.compile(r"(?:挪|卷|抢|分).{0,8}被[子窝]|被[子窝].{0,10}(?:挪|卷|跑|抢)|"
                      r"再.{0,4}(?:眯|闭|赖|睡).{0,6}(?:会|五分钟|眼)|再眯|再五分钟"),
    "waiting": re.compile(r"(?:一直|还在|就在)?等(?:你|哥哥)(?:回[来家]|到家)"),
    "arrival_check": re.compile(r"还没到|走到哪|到(?:教室|店里)了没|离店还远|到了(?:记得|说一声)"),
}
_DESSERT = re.compile(r"布丁|甜点|蛋糕|冰淇淋")
_DESSERT_STATE = re.compile(r"冰箱|还剩|凉着|留(?:着|给)|买好了|摆桌上|拿出来|有得吃")

_ARRIVAL_PROMPT = re.compile(
    r"还没到(?:吗|呢|[？?]|$)|走到哪|到(?:家|学校|教室|店里)了没|离店还远|"
    r"到(?:家|学校|教室|店里)?了?.{0,4}(?:说一声|告诉我|报个?平安)"
)
_DESTINATION = re.compile(r"到(家|学校|教室|店里)")
_BATH_CONTEXT = re.compile(r"洗澡|浴缸|泡澡|出浴|再泡|别泡|水.{0,5}(?:凉|热)|还没洗完")
_BATH_EXIT = re.compile(r"还不出来|还没出来|起来吧|快(?:从浴缸)?出来|该出来|再泡.{0,8}晕|出来换|出来吧")
_BATH_NOW = re.compile(r"还没洗完|还在泡|再泡|泡一会儿再出来|出来后.{0,10}(?:擦干|擦头发|换衣)")
_BATH_DETAIL = re.compile(r"头发|擦干|毛巾|换衣|水温|别着凉")
_BOOKING = re.compile(r"(?:票|座位|行程)(?:都|也|刚|已经|早就|总算|是|已经都|都已经){0,3}"
                      r"(?:订好|定好|买好|买了|已订)|"
                      r"(?:订好|定好|买好)了[^，。！？；\n]{0,5}(?:票|座位)")
_PLAN_OBJECT = re.compile(r"演唱会|音乐会|电影票|电影|车票|机票|饭局|聚会|开会|吃饭|门票")
_PLAN_CLAIM = re.compile(r"有个|有一场|说好了|约好了|定好了|已经安排|已经预约|已经订了|已经定了|安排好了")
_PLAN_DIRECTIVE = re.compile(r"(?:到时候|届时|到时|那天).{0,10}(?:要|得|记得)(?:到|去|在).{1,24}")
_PLAN_TAIL = re.compile(r"^(?:到时候|届时|那天|票|座位|后台|到时|记得去后台)")
_QUOTED = re.compile(r'```[\s\S]*?```|`[^`\n]+`|[「“『][^」”』\n]*[」”』]|"[^"\n]*"')
_UNCONFIRMED = re.compile(r"没|不|取消|如果|假如|打算|想去|要不要|[?？]|吗[呀呢喵\s]*$")
_CONFIRMATION = re.compile(r"说好|约好|确认|就定|确实|要去|会去|去看|去参加|有个|有一场")
_RETRACTED = re.compile(r"取消|不去了|不去看|退票|票退了|改期|改到")


def _normalize(value):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())


def _bigrams(value):
    return {value[i:i+2] for i in range(len(value)-1)}


def _near_duplicate(first, second):
    if first == second:
        return True
    if min(len(first), len(second)) < 7:
        return False
    a, b = _bigrams(first), _bigrams(second)
    return len(a & b) / len(a | b) >= .78


def _concrete_plan(sentence):
    return not re.search(r"[?？]|^(?:如果|假如|要是)|吗[喵呀呢\s。]*$", sentence.strip()) and bool(
        _BOOKING.search(sentence) or _PLAN_DIRECTIVE.search(sentence)
        or _PLAN_CLAIM.search(sentence) and _PLAN_OBJECT.search(sentence)
    )


def needs_plan_evidence(text):
    return any(_concrete_plan(statement) for statement in
               re.split(r"(?<=[。！？!?；;])|\r?\n", _QUOTED.sub(" ", text)))


def _matched_plan(sentence, user_turns, appointments, now, anchor=""):
    names = set(_PLAN_OBJECT.findall(sentence)) or set(_PLAN_OBJECT.findall(anchor))
    clock = find_clock(sentence)
    dates = {marker.target for marker in _day_markers(sentence, now)}
    booking = bool(_BOOKING.search(sentence))
    directive = _PLAN_DIRECTIVE.search(sentence)
    if None in dates or len(dates) > 1:
        return False
    if not booking and not directive:
        for item in appointments:
            at = item.get("at")
            if not isinstance(at, (float, int)) or item.get("stage") == "done":
                continue
            target = datetime.fromtimestamp(at)
            if (not any(name in str(item.get("label") or "") for name in names)
                    or target < now or dates and target.date() not in dates):
                continue
            if not clock or resolve_clock(clock, target - timedelta(minutes=1), 24) == target.timestamp():
                return True
    for turn, stamp in user_turns:
        turn = _QUOTED.sub(" ", turn)
        matched_object = names.intersection(_PLAN_OBJECT.findall(turn))
        if matched_object and _RETRACTED.search(turn) and not re.search(r"[?？]|如果|假如", turn):
            return False
        if _UNCONFIRMED.search(turn) or not dates.issubset(
            marker.target for marker in _day_markers(turn, stamp or now)
        ):
            continue
        if directive:
            if _normalize(directive.group()) in _normalize(turn):
                return True
            continue
        if not matched_object:
            continue
        other = find_clock(turn)
        if clock and (not other or any(clock.get(k) != other.get(k) for k in ("h", "m", "period"))):
            continue
        evidence = _BOOKING if booking else _CONFIRMATION
        if evidence.search(turn):
            return True
    return False


def polish_reply(text, scene, user_turns=(), appointments=(), *, now=None):
    """Filter clear contradictions and repeated actions before QQ segmentation."""
    now = now or datetime.now()
    sentences = split_text(text, pattern=r"[。！？!?；;\r\n]+", min_length=0,
                           balanced=False, max_segments=len(text) + 1, trim=False)
    visible = [_QUOTED.sub(" ", sentence) for sentence in sentences]
    bath = any(_BATH_CONTEXT.search(sentence) for sentence in visible)
    kept, seen, reasons = [], [], set()
    plan_rejected, plan_anchor = False, ""
    for sentence, content in zip(sentences, visible):
        if plan_rejected and _PLAN_TAIL.search(content.strip()):
            reasons.add("unsupported_plan")
            continue
        if _concrete_plan(content):
            plan_rejected = not _matched_plan(content, user_turns, appointments, now, plan_anchor)
            if plan_rejected:
                reasons.add("unsupported_plan")
                continue
            if _PLAN_OBJECT.search(content):
                plan_anchor = content
        bath_exit = _BATH_EXIT.search(content)
        if bath and (scene[1] != "shower" or scene[2] == "her") and (bath_exit or _BATH_NOW.search(content)):
            reasons.add("unconfirmed_bath")
            continue
        key = _normalize(sentence)
        arrival = _ARRIVAL_PROMPT.search(content)
        action = "bath_exit" if bath and bath_exit else "arrival_check" if arrival else ""
        details = _BATH_DETAIL.findall(content) if action == "bath_exit" else []
        if action == "arrival_check":
            details = _DESTINATION.findall(content)
            # A destination or a separate substantive clause can add information.
            tail = re.split(r"[，,]", content[arrival.end():], maxsplit=1)
            if len(tail) == 2 and len(_normalize(tail[1])) >= 5 and not _ARRIVAL_PROMPT.search(tail[1]):
                details.append(_normalize(tail[1]))
        details = set(details)
        if key and any(
            _near_duplicate(key, old_key) or
            action and action == old_action and not details.difference(old_details)
            for old_key, old_action, old_details in seen
        ):
            reasons.add("same_reply")
            continue
        seen.append((key, action, details))
        kept.append(sentence)
    return ("\n".join(kept) if reasons else text), reasons


def topics(text):
    found = [name for name, pattern in _TOPICS.items() if pattern.search(text)]
    for clause in re.split(r"[。！？!?\n；;]+", text):
        if _DESSERT_STATE.search(clause):
            found.extend("dessert:" + name for name in _DESSERT.findall(clause))
    return sorted(set(found))


def contradictory_clock(text, now):
    """Check clear claims about now, leaving quoted times and plans alone."""
    for match in re.finditer(
        r"(?:^|[，。！？\n])\s*(还没到|还不到|都还没到|已经过了|已经|现在是|现在|都已经)"
        r"\s*((?:(?:凌晨|早上|上午|中午|下午|傍晚|晚上)\s*)?"
        r"(?:\d{1,2}[:：]\d{2}|[零〇一二两三四五六七八九十\d]{1,3}[点時时](?:半|[零〇一二两三四五六七八九十\d]{1,3}分)?))",
        text,
    ):
        tail = text[match.end():]
        if re.match(r"\s*(?:过|半|多|左右|以前|以后|之前|之后|下课|开会|(?:啦|了|喵|呢)*[吗么？?])", tail):
            continue
        clock = find_clock(match[2] + "钟")
        if not clock:
            continue
        hour = clock["h"]
        period = clock.get("period")
        if period in {"night", "pm", "noon"} and hour < 12:
            hours = [hour + 12]
        elif period == "dawn" and hour == 12:
            hours = [0]
        elif period is None and 1 <= hour <= 12:
            hours = [hour % 12, hour % 12 + 12]
        else:
            hours = [hour]
        minute = now.hour * 60 + now.minute
        target = min((h * 60 + clock["m"] for h in hours), key=lambda m: abs(m - minute))
        # Ignore distant unqualified times and midnight rollover instead of
        # correcting the model's intended date by guessing.
        if abs(target - minute) > 180:
            continue
        head = match[1]
        if "不到" in head or "没到" in head:
            if minute >= target:
                return True
        elif head.startswith(("已经", "都已经")):
            if minute < target:
                return True
        else:
            exact = re.match(r"\s*整", tail) or ":" in match[2] or "：" in match[2] or clock["m"]
            if ((exact and minute != target)
                    or (not exact and now.hour != target // 60)):
                return True
    return False


def _hash(text):
    return hashlib.blake2s(text.encode("utf-8"), digest_size=12).hexdigest()


def fingerprint(text, at, scene):
    whole = _normalize(text)
    sentences = [_normalize(s) for s in re.split(r"[\n。？！!?；;]+", text)]
    grams = {
        _hash(s[i:i+2])
        for s in sentences if len(s) >= 6 for i in range(len(s)-1)
    }
    return {"at": at, "text": _hash(whole), "grams": sorted(grams),
            "sentences": sorted({_hash(s) for s in sentences if len(s) >= 9}),
            "topics": topics(text), "scene": scene}


def recent(rows, now):
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("at"), (int, float))
            and 0 <= now - r["at"] < 86400 and {"text", "grams", "topics", "scene"} <= r.keys()][-8:]


def duplicate(candidate, rows, topic_updates):
    for prior in rows:
        if prior["text"] == candidate["text"]:
            return "same_text"
        grams, previous = set(candidate["grams"]), set(prior["grams"])
        if (set(candidate["sentences"]).intersection(prior.get("sentences", ()))
                or grams and previous and len(grams & previous) / len(grams | previous) >= .72):
            return "similar_text"
        if any(
            topic_updates.get(topic, 0) <= prior["at"]
            for topic in set(prior["topics"]) & set(candidate["topics"])
            if prior["scene"] == candidate["scene"] or topic.startswith("dessert:")
        ):
            return "same_topic"
    return None
