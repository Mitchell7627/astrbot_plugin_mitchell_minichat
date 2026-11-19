"""Small, inspectable outcomes for the single-call proactive review."""

import json
import math
import os
import re
from pathlib import Path


_SEND = {"FOLLOW_UP": "model_follow_up", "CONFIRMED_TOPIC": "model_confirmed_topic",
         "GREETING": "model_greeting"}
_SKIP = {"ANSWERED": "model_answered", "NO_CONTEXT": "model_no_context", "NO_NATURAL_LINE": "model_no_natural_line"}
_CONTROL = re.compile(r"\[\[\s*(?:NO_SEND|SEND|REVIEW)\b|(?m:^[ \t]*(?:发前自检(?:摘要)?|自检(?:摘要)?|review summary)[ \t]*[:：])", re.I)
_HEADER = re.compile(r"\[\[(SEND|NO_SEND)(?::([A-Z_]+))?\]\]", re.I)
_SUMMARY = re.compile(r"\[\[REVIEW:([^\r\n\[\]]{1,240})\]\][ \t]*(?:\r?\n|$)", re.I)
_SUMMARY_TEXT = re.compile(r"""[\x00-\x1f\x7f<>\[\]{}‘’“”「」『』"'=\\/<>]|\d{5,}|https?:|@""", re.I)
_BASIS = {"RECENT", "RECALL", "APPOINTMENT", "NO_TOPIC", "ANSWERED"}
_INTENT = {"QUESTION", "CONTINUE", "GREET", "WAIT"}
PROMPT = (
    "\n发前在心里过一遍：先看对方实际有没有回、上一句你是否已经接过，"
    "再看有没有一句自然的新话。问句、打趣或你自己说过的话都不等于对方同意，"
    "不猜饭点或倒计时，不把对方简短回话说成他没回。"
    "没有具体话题时，用自己的口吻轻轻打个招呼；开场可以相似，不用为了换主题而编新事情。"
    "首行选 [[SEND:FOLLOW_UP]]、[[SEND:CONFIRMED_TOPIC]] 或 [[SEND:GREETING]]。"
    "第二行格式 [[REVIEW:RECALL/QUESTION|想起之前聊过的书，就问下次想读什么。]]。"
    "斜线前选 RECENT（最近对话）、RECALL（回顾旧话题）、APPOINTMENT（已确认约定）、NO_TOPIC（没具体话题）或 ANSWERED（已接过）；"
    "斜线后选 QUESTION（问一句）、CONTINUE（接着聊）、GREET（问候）或 WAIT（跳过）。"
    "竖线后只用一句话说明发送理由，最多72字；不原样引用聊天，不写个人资料或推理草稿。"
    "第三行起只写发送的正文，按自己的口吻说完就停。"
    "实在连轻问候也不适合时，可以首行写 [[NO_SEND:NO_NATURAL_LINE]]，第二行写同样格式的跳过理由，之后不写正文。"
)


def _safe_summary(value):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if len(value) > 72 or _SUMMARY_TEXT.search(value) or _CONTROL.search(value):
        return ""
    return value


def _safe_outline(value):
    if not isinstance(value, str):
        return ""
    basis, _, intent = value.strip().upper().partition("/")
    return f"{basis}/{intent}" if basis in _BASIS and intent in _INTENT else ""


def read_review(text):
    """Split a dashboard-facing reason from the text that may be sent."""
    body = text.strip()
    header = _HEADER.match(body)
    if header is None:
        return ("", "model_bad_label", "", "") if _CONTROL.search(body) else (text, "model_unreported", "", "")
    tag, code = header[1].upper(), (header[2] or "").upper()
    tail = body[header.end():].lstrip(" \t")
    summary = ""
    outline = ""
    if tail.startswith(("\n", "\r\n")):
        tail = tail.split("\n", 1)[1]
        review = _SUMMARY.match(tail)
        if review:
            choice, sep, detail = review[1].partition("|")
            outline = _safe_outline(choice) if sep else ""
            summary = _safe_summary(detail if sep else choice)
            tail = tail[review.end():]
    elif tail and tag == "SEND":
        return "", "model_bad_label", "", ""
    if tag == "NO_SEND":
        return "", _SKIP.get(code, "model_self_skip" if not code else "model_bad_label"), summary, outline
    if code not in _SEND or _CONTROL.search(tail):
        return "", "model_bad_label", "", ""
    return tail.strip(), _SEND[code], summary, outline


def read_draft(text):
    body, label, _, _ = read_review(text)
    return body, label


_ROUTES = {"topic", "catch_up", "persona_greeting", "greeting", "quiet", "social_answered"}
_MODELS = {*_SEND.values(), *_SKIP.values(), "model_self_skip", "model_unreported", "model_bad_label", "not_needed"}
_RESULTS = {
    "delivery", "proactive_self_skip", "proactive_filtered", "clock_conflict", "same_text", "similar_text", "same_topic",
    "outreach_waiting_for_reply", "social_answered", "stale_generation", "empty_generation", "sleeping", "cancelled", "error",
}
_CHECKS = {
    "draft_removed", "draft_blocked", "answered_echo", "reply_received", "no_reply_unverified", "unconfirmed_meal_pressure",
    "same_reply", "unconfirmed_schedule", "unconfirmed_presence", "unconfirmed_bath", "unconfirmed_dessert", "unconfirmed_inventory",
    "unconfirmed_experience", "unsupported_followup", "unsupported_plan", "unconfirmed_catch_up",
}


class ProactiveReview:
    limit = 50

    def __init__(self, path):
        self.path = Path(path)
        self._rows = []
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(rows, list):
                for row in rows[-self.limit:]:
                    normalized = self._safe(row)
                    if normalized:
                        self._rows.append(normalized)
        except (OSError, ValueError, TypeError):
            pass

    @staticmethod
    def _safe(row):
        if not isinstance(row, dict):
            return None
        at, umo = row.get("at"), row.get("umo")
        if not isinstance(at, (int, float)) or not math.isfinite(at) or not isinstance(umo, str) or len(umo) > 180:
            return None

        def enum(key, choices, default):
            value = row.get(key)
            return value if isinstance(value, str) and value in choices else default

        def segments(key):
            value = row.get(key)
            return min(99, max(0, value)) if type(value) is int else 0

        checks = row.get("checks")
        normalized = {
            "at": at, "umo": umo,
            "route": enum("route", _ROUTES, "topic"),
            "model": enum("model", _MODELS, "not_needed"),
            "reason": enum("reason", _RESULTS, "other"),
            "checks": sorted({x for x in checks if isinstance(x, str) and x in _CHECKS})
                      if isinstance(checks, list) else [],
            "planned_segments": segments("planned_segments"),
            "delivered_segments": segments("delivered_segments"),
        }
        summary = _safe_summary(row.get("summary"))
        if summary:
            normalized["summary"] = summary
        outline = _safe_outline(row.get("outline"))
        if outline:
            normalized["outline"] = outline
        return normalized

    def add(self, row):
        normalized = self._safe(row)
        if normalized is None:
            return
        self._rows.append(normalized)
        self._rows = self._rows[-self.limit:]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(self._rows, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, self.path)
        except OSError:
            pass  # Losing an audit row must not stop the schedule.

    def recent(self, limit=30):
        return list(reversed(self._rows[-max(1, min(self.limit, limit)):]))
