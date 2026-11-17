"""Local comparison of delivered proactive messages."""

import hashlib
import re
import unicodedata

from .appointment import find_clock


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
    def normalize(value):
        return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())

    whole = normalize(text)
    sentences = [normalize(s) for s in re.split(r"[\n。？！!?；;]+", text)]
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
