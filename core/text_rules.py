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
    # 「打算去健身房」是计划，不是此刻（「准备」不算：准备…了=正要做）
    "打算",
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
# 「回头」既是将来（回头再说）又是字面回头（回头一看），后者要剔掉再判
_FUTURE_GUARDS = ("回头一看", "回头一瞅", "回头看了眼", "回头看了", "等了一会儿", "过了一会儿")

# ---------------------------------------------------------------- 假设/条件
# 「如果我去洗澡，你就等我」「要是我在洗澡就好了」不是在陈述事实。
CONDITIONAL_MARKERS = ("如果", "要是", "假如", "万一", "除非", "倘若", "如若", "的话")
# 条件从句的开头（跨逗号回看上一分句时只认这些，不能认「如果」出现在句中）
CONDITIONAL_LEADS = ("如果", "要是", "假如", "万一", "除非", "倘若", "如若", "如果说", "哪天")
# 条件句的后件连词。只有本分句以这些开头，才把上一分句的条件算到本分句头上；
# 「如果下雨怎么办，现在我们确实在一起吃饭」的后件是「现在我们…」，不算。
CONDITIONAL_THENS = ("就", "那", "便", "则", "于是", "那么", "我", "你", "他", "她", "咱")
# 「的话」有两种：条件助词（下雨的话，我就不去）和名词（听你的话 / 我说的话）。
# 前一字是人称、常见人物名词，或言语/认知动词时按名词处理，判定前等长遮蔽，索引不变。
_DE_HUA_PRONOUNS = "我你您他她它咱俺"
_DE_HUA_NOUNS = (
    "朋友", "医生", "老师", "妈妈", "爸爸", "家人", "别人", "老板", "同事",
    "同学", "老婆", "老公", "孩子", "室友", "父母", "我妈", "我爸", "人家",
)
_DE_HUA_VERBS = (
    "说", "讲", "听", "问", "写", "记", "在乎", "相信", "明白", "懂",
    "打断", "回", "理", "留", "信", "管",
)
# 「梦见我起床了」：梦里的事不算发生
DREAM_MARKERS = ("梦见", "梦到", "做梦", "梦里")

# ---------------------------------------------------------------- 疑问
# 事件路径只认「吗/嘛/问号」：中文里「我在吃饭呢」的「呢」是陈述语气，
# 不能当疑问（睡眠路径另有一套更严格的规则）。
QUESTION_MARKS = ("吗", "嘛", "？", "?")
# 谓语后面的特殊疑问词：「开会开到几点」「下课要多久」——没有问号也是在问
_INTERROGATIVE_RE = re.compile(r"(几|多少|多久|多长时间|啥|什么|哪|怎么)")
# 前置特殊疑问：「什么时候去开会」「几点洗澡」——疑问词在谓语前面
_Q_HEAD_RE = re.compile(r"(什么时候|啥时候|几点|几时|谁|哪里|哪儿|哪个|怎么|咋)")

# ---------------------------------------------------------------- 人称
# 第三人称主语（只在这些词出现在**分句开头**、且后面跟谓语时才认）
THIRD_PERSON_SUBJECTS = (
    "我妈", "我爸", "我姐", "我哥", "我弟", "我妹", "我家",
    "他", "她", "孩子", "室友", "同学", "老板", "同事",
    "朋友", "老婆", "老公", "家里", "猫", "狗",
)
# 第三人称后面常见的谓语开头（「他在」「我妈在」「他说」「室友又」…）
PREDICATE_LEADS = ("在", "正", "还", "又", "刚", "已", "说", "要", "去", "想", "准备", "忙", "做", "睡", "吃", "玩", "觉得", "嫌", "夸")

# 转述/引用：这句话是在复述别人说的，不是用户自己正在做
_REPORTED_WORDS = ("他说", "她说", "听说", "据说", "别人说", "转述", "示例", "例如")
# 成对引号：只有「命中词落在引号内部」才算转述。以前只要句子里出现引号就整句判转述，
# 「他说“我去洗澡了”，我在吃饭」把自己的动作也一起挡掉了。
_QUOTE_PAIRS = {"「": "」", "『": "』", "“": "”"}

# 取消/作废：和否定分开一套。「取消去开会」里否定词离「开会」太远，
# 4 字窗口照不到；而「开会取消了」的取消在词后面。两头都要看。
CANCEL_WORDS = ("取消", "不开了", "不去了", "不上了", "改期", "延期", "不去了", "不办了")



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
    if not head:
        return False
    for guard in _FUTURE_GUARDS:
        head = head.replace(guard, "")
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
    """命中词所在分句是不是疑问句（「吃了吗」「开会开到几点」）。"""
    start = clause_start(text, idx, window)
    end = clause_end(text, idx + kw_len)
    if end < len(text) and text[end] in "？?":
        return True
    clause = text[start:end]
    if any(m in clause for m in QUESTION_MARKS):
        return True
    tail = text[idx + kw_len:end].strip()
    # 「吃饭了没」「睡了没」：体标记「了」把「没」推到了句尾
    if tail[:1] == "没" or tail.endswith("没"):
        return True
    # 特殊疑问词跟在谓语后面：「开会开到几点」「下课要多久」
    if _INTERROGATIVE_RE.search(tail):
        return True
    # 前置特殊疑问：「什么时候去开会」「几点洗澡」——疑问词在命中词前面
    head = clause_head(text, idx, window=14)
    match = _Q_HEAD_RE.search(head)
    return bool(match) and (match.end() == len(head) or head[match.end()] in "去在要能会想")


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
        # 时间/程度副词不改变主语；「我妈现在正在」仍是在说别人。
        rest = re.sub(r"^(?:(?:现在|目前|此刻|当下|这会儿?|今天|刚刚|已经|一直|仍然|马上|快要|就要|快|也|才|就|都)\s*)+", "", rest)
        if not rest:
            return True
        if rest.startswith(PREDICATE_LEADS):
            return True
    return False


def is_third_person_clause(text: str, idx: int, extra_names=()) -> bool:
    """分句开头就是第三人称，不要求后面紧跟谓语。

    只给结束词（stop/end）用：「我妈约会结束了」「室友的电影看完了」里
    命中的是「结束了 / 看完了」，人称和谓语之间隔着名词，不能再按
    `is_third_person_subject` 的「人称+谓语」严格形态判。
    """
    head = clause_head(text, idx, window=14)
    if not head:
        return False
    return any(
        head.startswith(who)
        for who in tuple(THIRD_PERSON_SUBJECTS) + tuple(extra_names)
        if who
    )


def is_second_person_clause(text: str, idx: int) -> bool:
    """分句主语是「你/您」——是在对机器人说话，不是用户本人。

    事件侧会把它记成 her；但 away/return/end 没有主体概念，
    「你去上班吧」会被当成用户自己出门。这三条链路要先拒掉。
    """
    return clause_head(text, idx, window=14).startswith(("你", "您"))


def _quote_spans(text: str) -> list[tuple[int, int]]:
    """成对引号覆盖的区间（左引号位置, 右引号位置）。没闭合的中文引号算到句尾。

    ASCII 直引号（" 和 '）只能按出现顺序两两配对：落单的不认，避免
    「don't」「5'10"」这类把后半句整段吞进引号里。
    """
    spans: list[tuple[int, int]] = []
    start = -1
    opener = ""
    for i, ch in enumerate(text):
        if start < 0 and ch in _QUOTE_PAIRS:
            start, opener = i, ch
        elif start >= 0 and ch == _QUOTE_PAIRS.get(opener):
            spans.append((start, i))
            start, opener = -1, ""
    if start >= 0:
        spans.append((start, len(text)))

    for mark in ('"', "'"):
        positions = [i for i, ch in enumerate(text) if ch == mark]
        for a, b in zip(positions[0::2], positions[1::2]):
            spans.append((a, b))
    return spans


def is_reported_clause(text: str, idx: int) -> bool:
    """命中词是不是在引号里、或者本分句前有「他说/听说」。

    只看「命中词是否落在引号区间内」，不再因为句子里出现了引号就整句判转述——
    「他说“我去洗澡了”，我在吃饭」的后半句是用户自己的动作，要照常识别。
    """
    if not text:
        return False
    for begin, end in _quote_spans(text):
        if begin < idx < end:
            return True
    head = clause_head(text, idx, window=len(text))
    return any(w in head for w in _REPORTED_WORDS)


def _mask_possessive_de_hua(text: str) -> str:
    """把名词性的「的话」等长遮蔽成「的□」，索引不变。

    「我正在开会先别打断我的话」「我在听你的话」里的「的话」是「的+话」，
    不是条件助词；只有「下雨的话，我就不去」这种才该拦。
    """
    if "的话" not in text:
        return text
    out = None
    pos = text.find("的话")
    while pos != -1:
        prev = text[max(0, pos - 2):pos]
        possessive = (
            (pos >= 1 and text[pos - 1] in _DE_HUA_PRONOUNS)
            or prev.endswith(_DE_HUA_VERBS)
            or prev.endswith(_DE_HUA_NOUNS)
        )
        if possessive:
            if out is None:
                out = list(text)
            out[pos] = "的"
            out[pos + 1] = "□"
        pos = text.find("的话", pos + 1)
    return "".join(out) if out is not None else text


def _conditional_in_prev(text: str, idx: int) -> bool:
    """条件写在前一分句：「如果下雨，我就去洗澡」。

    只认「上一分句以条件词开头」且「本分句以就/那/便/则…开头」，
    避免「刚才我们讨论了如果下雨怎么办，现在我们确实正在一起吃饭」被误杀。
    """
    start = clause_start(text, idx, 14)
    prev = prev_clause(text, start, 24)
    if not prev:
        return False
    # 条件也可以写成「下雨的话，我就去洗澡」：前句不以「如果」开头，
    # 而是以「的话」收尾。先遮掉名词性的「我说的话 / 听你的话」再判结尾。
    prev_view = _mask_possessive_de_hua(prev)
    if not (
        prev_view.startswith(CONDITIONAL_LEADS)
        or prev_view.rstrip().endswith("的话")
    ):
        return False
    head = clause_head(text, idx, window=14)
    return head.startswith(CONDITIONAL_THENS)


def is_conditional_clause(text: str, idx: int, window: int = 14, prev_max: int = 4) -> bool:
    """命中词所在分句是不是假设/条件（「如果我去洗澡」「要是明天下雨」）。

    「的话」通常在句尾，所以 head 和 tail 都要看；同时允许并上紧邻的短分句
    （「如果明天，我就去洗澡」）。prev_max 保留在签名里兼容旧调用，
    真正的跨分句判断交给 `_conditional_in_prev`。
    """
    if not text:
        return False
    view = _mask_possessive_de_hua(text)
    head = clause_head(view, idx, window=window)
    if any(w in head for w in CONDITIONAL_MARKERS):
        return True
    tail = view[idx: clause_end(view, idx)]
    if any(w in tail for w in CONDITIONAL_MARKERS):
        return True
    return _conditional_in_prev(view, idx)


def is_dream_clause(text: str, idx: int, window: int = 14) -> bool:
    """「梦见/梦到/做梦」里的动作不算真实发生。"""
    if not text:
        return False
    head = clause_head(text, idx, window=window)
    if any(w in head for w in DREAM_MARKERS):
        return True
    return any(w in text[idx: clause_end(text, idx)] for w in DREAM_MARKERS)


def is_cancelled_clause(text: str, idx: int, kw_len: int = 0, window: int = 6) -> bool:
    """命中词所在分句是不是在说「这件事取消了」。

    「取消去开会」「会不开了」「开会取消了」都算；「取消」和事件词隔得远近不论，
    但只看本分句，不跨逗号。window=None 表示看整个分句（约定里「取消」可能在时钟
    后面好几个字）。
    """
    if not text or not CANCEL_WORDS:
        return False
    head = clause_head(text, idx, window=window if window is not None else len(text))
    if any(w in head for w in CANCEL_WORDS):
        return True
    tail = text[idx + kw_len: clause_end(text, idx + kw_len)]
    if window is not None:
        tail = tail[:window]
    return any(w in tail for w in CANCEL_WORDS)


def drop_keyword(text: str, idx: int, kw_len: int) -> str:
    """摘掉命中的词，保留其余文字（用于「回归词处理完，剩下的话继续走事件判定」）。"""
    if not text:
        return ""
    rest = text[:max(0, idx)] + text[idx + kw_len:]
    return rest.strip(" ，,。！!？?、~～…\n")


# 判断「这是用户本人在做」时用的否定词表。比事件词表窄：只要紧贴的否定就够，
# 太长的词（「不打算」）交给 is_future_clause 处理。
SELF_NEGATIONS = ("不想", "不要", "不用", "不必", "没空", "没时间", "没心情", "还没", "没在")
# 命中词前的否定尾巴：「没有在」「不是我在」「不在」「别在」……
_NEG_TAIL_RE = re.compile(r"(?:(?:没有|没|不|别)(?:在|正|是)?|不是)(?:我)?(?:在|正)?$")
# 含否定字但不是否定的词：先等长替换掉，避免「不过我在洗澡」被误判。
_NEG_FALSE_FRIENDS = (
    "不小心", "不但", "不过", "不仅", "不断", "差不多", "不得不", "忍不住",
    "恨不得", "不见得", "不由得", "不管", "不论", "不光", "不只", "不一会",
)
# 强否定：否定词和关键词之间可以隔「在/正/是 + 我 + 一个短动词」。
# 「我没有在坐地铁」「不是我在看电影」都靠它收。
_NEG_STRONG_RE = re.compile(
    r"(?:没有|不是|并非|并没|根本没有|根本没|从来没|从没|从未|不曾)"
    r"(?:是|在|正|有)?[我]{0,1}[^\s，。！？；,.!?;]{0,3}$"
)
# 弱否定：单个否定字 + 最多两个动词/助词。「我没在看电影」「我不坐地铁」。
_NEG_WEAK_RE = re.compile(
    r"(?:没|不|别)[在正看坐听写玩吃洗做待呆逛跑走读学练刷等]{0,2}$"
)


def _strip_neg_false_friends(head: str) -> str:
    for word in _NEG_FALSE_FRIENDS:
        head = head.replace(word, "　" * len(word))
    return head


def is_self_action_clause(text: str, idx: int, kw_len: int = 0) -> bool:
    """这句话是不是「用户本人此刻正在做」——六道闸门的公共封装。

    依次排除：转述/引用、否定、取消、将来、疑问、第三人称。
    各条链路（唤醒、做饭交接、事件匹配）都该走这一套，避免各写各的 `in text`。
    """
    if not text:
        return False
    if is_reported_clause(text, idx):
        return False
    if negated_by_clause(text, idx, SELF_NEGATIONS):
        return False
    if is_cancelled_clause(text, idx, kw_len):
        return False
    if is_conditional_clause(text, idx):
        return False
    if is_dream_clause(text, idx):
        return False
    if is_future_clause(text, idx, prev_max=4):
        return False
    if is_question_clause(text, idx, kw_len):
        return False
    if is_third_person_subject(text, idx):
        return False
    if is_far_past_clause(text, idx, kw_len):
        return False
    return True


def negated_by_clause(text: str, idx: int, negations, window: int = 4) -> bool:
    """命中词前出现否定词才算否定，且不跨分句。

    「不困，再看会手机」：`再看会手机` 的分句头在逗号之后，前面的「不」不影响它。

    先看命中词前 4 字（原有口径），再放宽到 10 字，专门收
    「我没有在坐地铁」「不是我在看电影」这种否定与关键词之间夹了动词的形态。
    放宽前先剔除「不小心 / 不但 / 不过 / 不仅」这类含否定字但不是否定的词。
    """
    if idx <= 0 or not negations:
        return False
    # 先在一个更宽的头里剔除「不得不 / 不过 / 不但」这类假否定，再取紧贴窗口。
    # 「我现在不得不去坐地铁」里「不得不」正好跨过 4 字边界（tight=「得不去坐」），
    # 只清 tight 会漏掉，必须先清宽窗口再切尾部。
    wide = clause_head(text, idx, window=max(window, 14))
    wide_clean = _strip_neg_false_friends(wide)
    tight = wide_clean[-window:] if window else wide_clean
    if tight:
        if any(n in tight for n in negations):
            return True
        if tight[-1] in ("不", "别", "没"):
            return True
        if _NEG_TAIL_RE.search(tight):
            return True
    if not wide_clean:
        return False
    if _NEG_STRONG_RE.search(wide_clean):
        return True
    return bool(_NEG_WEAK_RE.search(wide_clean))


# ---------------------------------------------------------------- 消费跨度
# 处理完一个动作后，只遮住「这个动作本身」，不要整句/整个分句一起抹掉。
_CONSUME_PARTICLES = "了啦咯喽哦呀噢噜着"
_CONSUME_CONNECTORS = ("然后", "接着", "随后", "于是", "现在", "马上", "就", "再", "先")
_CONSUME_TAIL_MARKS = "，,。！？!?；;、~～… \t\n"


def consume_span(text: str, idx: int, kw_len: int = 0) -> tuple[int, int]:
    """返回「这个动作已经消费掉」的区间 [start, end)。

    start 是该分句开头（把主语和前面的状语一起吃掉）；
    end 停在命中词 + 语气词 + 一个转接词 + 尾随标点之后。
    「吃完了我正在洗澡」→ 只吃掉「吃完了」，留下「我正在洗澡」；
    「吃完了，我正在洗澡」→ 连逗号一起吃掉。
    """
    if not text:
        return 0, 0
    start = clause_start(text, idx, None)
    end = min(len(text), idx + max(0, kw_len))
    while end < len(text) and text[end] in _CONSUME_PARTICLES:
        end += 1
    for word in _CONSUME_CONNECTORS:
        if text.startswith(word, end):
            end += len(word)
            break
    while end < len(text) and text[end] in _CONSUME_TAIL_MARKS:
        end += 1
    return start, end
