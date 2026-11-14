"""Local comparison of delivered proactive messages."""

import hashlib
import re
import unicodedata


_TOPICS = {
    "shoulder": re.compile(
        r"(?:借|给|搭|靠|挨).{0,8}肩膀|肩膀.{0,12}(?:借|搭|靠|挨|给你)|"
        r"靠过来|让我挨着|往这边靠"
    ),
    "bed": re.compile(r"(?:挪|卷|抢|分).{0,8}被[子窝]|被[子窝].{0,10}(?:挪|卷|跑|抢)|"
                      r"再.{0,4}(?:眯|闭|赖|睡).{0,6}(?:会|五分钟|眼)|再眯|再五分钟"),
    "waiting": re.compile(r"(?:一直|还在|就在)?等(?:你|哥哥)(?:回[来家]|到家)"),
}


def topics(text):
    return sorted(name for name, pattern in _TOPICS.items() if pattern.search(text))


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
        if prior["scene"] == candidate["scene"] and any(
            topic_updates.get(topic, 0) <= prior["at"]
            for topic in set(prior["topics"]) & set(candidate["topics"])
        ):
            return "same_topic"
    return None
