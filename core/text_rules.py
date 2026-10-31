"""中文短句的轻量语义标记（纯函数，不依赖 astrbot、不读配置）。

识别层几个地方都要回答同一类问题：
- 这句话说的是什么时候？（将来 / 现在 / 过去）
- 是不是在问？（「吃了吗」）
- 做事的是谁？（我 / 你 / 第三人称 / 转述）

以前这些判断分散在 `event_detector`（只有将来时）和 `sleep_reminder`（否定/疑问/第三人称）
各写一套，容易互相漂移。这里统一实现，两边共用。

设计原则：全部按**分句**判断。
「不困，再看会手机」里的「不」属于上一分句，不该否定后半句；
「明天，我去吃饭」里的「明天」虽然被逗号隔开，但仍是这句的时间状语——
所以分句头为空时会并上看一眼**紧邻的短分句**（≤4 字）。
"""

from __future__ import annotations

import re

# 分句边界（中英文标点都算）
CLAUSE_BOUNDARY = "。！？；，、,.!?;~～…\n"

# ---------------------------------------------------------------- 时态：将来
FUTURE_MARKERS = (
    "明天", "明日", "明早", "明晚", "明晨", "明儿", "明个",
    "后天", "大后天", "改天", "下次", "下回", "下周", "下星期", "下礼拜",
    "下个月", "下学期", "回头", "以后", "将来",
    "等会", "等会儿", "待会", "待会儿", "晚点", "一会儿", "过会儿", "稍后",
)
# 兜底正则：明/后/下次 + 时间单位（明天、明早、明晚、后天早上、下次…）
FUTURE_RE = re.compile(r"(?:明|后|大后|次)(?:天|日|早|晚|儿|个|晨)")

# ---------------------------------------------------------------- 时态：过去
# 「刚才/刚刚」这类是"刚刚发生"，状态可能还在延续（「刚才出门了」= 人现在在外面），
# 所以只用来挡事件，不用来挡在场状态。
PAST_RECENT = ("刚才", "刚刚", "刚", "方才")
# 明确发生在以前：事件、在场状态、睡眠全部不算"此刻"。
PAST_FAR = (
    "昨天", "昨晚", "昨儿", "昨夜", "前天", "大前天",
    "上周", "上礼拜", "上个月", "上学期", "去年", "那天", "当时", "那时候",
)
# 单字「刚」的误伤保护：刚好 / 刚巧 不是时间
_RECENT_GUARD = ("刚好", "刚巧", "刚想")

# ---------------------------------------------------------------- 疑问
# 事件路径只认「吗/嘛/问号」：中文里「我在吃饭呢」的「呢」是陈述语气，
# 不能当疑问（睡眠路径另有一套更严格的规则）。
QUESTION_MARKS = ("吗", "嘛", "？", "?")

# ---------------------------------------------------------------- 人称
# 第三人称主语（只在这些词出现在**分句开头**、且后面跟谓语时才认）
THIRD_PERSON_SUBJECTS = (
    "我妈", "我爸", "我姐", "我哥", "我弟", "我妹", "我家",
    "他", "她", "孩子", "室友", "同学", "老板", "同事",
    "朋友", "老婆", "老公", "家里", "猫", "狗",
)
# 第三人称后面常见的谓语开头（「他在」「我妈在」「他说」「室友又」…）
PREDICATE_LEADS = ("在", "正", "还", "又", "刚", "已", "说", "要", "去", "想", "准备", "忙", "做", "睡", "吃", "玩")

# 转述/引用：这句话是在复述别人说的，不是用户自己正在做
_REPORTED_WORDS = ("他说", "她说", "听说", "据说", "别人说", "转述")
_QUOTE_CHARS = "「」『』“”"


def clause_start(text: str, idx: int, window: int | None = None) -> int:
    """命中词所在分句的起点；window 给定时最多往回看这么多字。"""
    start = 0 if window is None else max(0, idx - window)
    for pos in range(idx - 1, start - 1, -1):
        if text[pos] in CLAUSE_BOUNDARY:
            return pos + 1
    return start


def clause_end(text: str, idx: int) -> int:
    """从 idx 起该分句的终点（不含边界字符）。"""
    for pos in range(max(0, idx), len(text)):
        if text[pos] in CLAUSE_BOUNDARY:
            return pos
    return len(text)


def clause_head(text: str, idx: int, window: int = 14, prev_max: int = 0) -> str:
    """命中词前面、同一分句内的文字。

    prev_max > 0 时，若本分句头为空，把紧邻的上一分句也带上——
    处理「明天，我去吃饭」这种时间状语被逗号隔开的情况。
    """
    start = clause_start(text, idx, window)
    head = text[start:idx]
    if not head and prev_max and start > 0:
        prev = prev_clause(text, start, prev_max)
        if prev:
            head = prev
    return head


def prev_clause(text: str, start: int, max_len: int) -> str:
    """开始位置 start 前面的那个分句（长度不超过 max_len 才返回，否则空串）。"""
    if start <= 0 or max_len <= 0:
        return ""
    prev_end = start - 1
    prev_start = clause_start(text, prev_end, None)
    prev = text[prev_start:prev_end]
    return prev if 0 < len(prev) <= max_len else ""


def _has_marker(head: str, markers) -> bool:
    return any(m in head for m in markers)


def _has_recent_past(head: str) -> bool:
    if any(g in head for g in _RECENT_GUARD):
        # 「刚好」「刚巧」不是时间词，去掉后再看
        stripped = head
        for g in _RECENT_GUARD:
            stripped = stripped.replace(g, "")
        return _has_marker(stripped, PAST_RECENT)
    return _has_marker(head, PAST_RECENT)


def is_future_head(head: str) -> bool:
    return bool(head) and (_has_marker(head, FUTURE_MARKERS) or bool(FUTURE_RE.search(head)))


def is_future_clause(text: str, idx: int, window: int = 14, prev_max: int = 0) -> bool:
    if is_future_head(clause_head(text, idx, window=window)):
        return True
    # 「明天，我去吃饭」：本分句没有时间词时，看一眼紧邻的短分句
    return is_future_head(prev_clause(text, clause_start(text, idx, window), prev_max))


def _past_in(head: str) -> bool:
    return _has_marker(head, PAST_FAR) or _has_recent_past(head)


def is_past_clause(text: str, idx: int, kw_len: int = 0, window: int = 14, prev_max: int = 0) -> bool:
    """命中词是否在说已经发生的事（刚才/昨天…，或词后紧跟「完/过」）。"""
    if _past_in(clause_head(text, idx, window=window)):
        return True
    if prev_max and _past_in(prev_clause(text, clause_start(text, idx, window), prev_max)):
        return True
    after = text[idx + kw_len: idx + kw_len + 2]
    return after.startswith(("完", "过"))


def is_far_past_clause(text: str, idx: int, kw_len: int = 0, window: int = 14, prev_max: int = 0) -> bool:
    """只认「昨天/上周」这类明确过去，不含「刚才/刚」。

    在场状态（离开/回来）用它：「刚才出门了」人还在外面，不能当过去忽略；
    「昨天出门了」才是真过去。
    """
    if _has_marker(clause_head(text, idx, window=window), PAST_FAR):
        return True
    if prev_max and _has_marker(prev_clause(text, clause_start(text, idx, window), prev_max), PAST_FAR):
        return True
    after = text[idx + kw_len: idx + kw_len + 2]
    return after.startswith("过")


def is_question_clause(text: str, idx: int, kw_len: int = 0, window: int = 14) -> bool:
    """命中词所在分句是不是疑问句（「吃了吗」「在开会吗」）。"""
    start = clause_start(text, idx, window)
    end = clause_end(text, idx + kw_len)
    clause = text[start:end]
    if any(m in clause for m in QUESTION_MARKS):
        return True
    tail = text[idx + kw_len:end].strip()
    return tail[:1] == "没"


def is_third_person_subject(text: str, idx: int, extra_names=()) -> bool:
    """分句开头是第三人称、后面跟谓语（「我妈在做饭」「他说他在洗澡」）。

    只认分句开头，避免「给狗洗澡」这种第三人称是宾语的情况；宾语场景由主体识别处理。
    """
    head = clause_head(text, idx, window=14)
    if not head:
        return False
    for who in tuple(THIRD_PERSON_SUBJECTS) + tuple(extra_names):
        if not who or not head.startswith(who):
            continue
        rest = head[len(who):]
        if not rest:
            return True
        if rest[:2].startswith(PREDICATE_LEADS) or rest[:1] in "在正还又刚已说要去":
            return True
    return False


def is_reported_speech(text: str) -> bool:
    """整句是不是在复述/引用别人的话（「他说他在洗澡」「『我洗澡了』这是他发的」）。"""
    if not text:
        return False
    if any(q in text for q in _QUOTE_CHARS):
        return True
    return any(w in text for w in _REPORTED_WORDS)


def is_reported_clause(text: str, idx: int) -> bool:
    """命中词所在的这句是不是转述（只看本分句前的「他说/听说」，引号则整句算）。"""
    if not text:
        return False
    if any(q in text for q in _QUOTE_CHARS):
        return True
    head = clause_head(text, idx, window=len(text))
    return any(w in head for w in _REPORTED_WORDS)


def negated_by_clause(text: str, idx: int, negations, window: int = 4) -> bool:
    """命中词前 4 个字内出现否定词才算否定，且不跨分句。

    「不困，再看会手机」：`再看会手机` 的分句头在逗号之后，前面的「不」不影响它。
    """
    if idx <= 0 or not negations:
        return False
    head = clause_head(text, idx, window=window)
    if not head:
        return False
    if any(n in head for n in negations):
        return True
    return head[-1] in ("不", "别")
