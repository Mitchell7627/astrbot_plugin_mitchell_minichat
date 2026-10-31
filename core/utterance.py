"""判断一个关键词是不是在说「用户此刻正在做这件事」。

识别层原来只有六道局部过滤（转述 / 否定 / 将来 / 过去 / 疑问 / 第三人称），
它们都在看「命中词前后几个字」，没有人问最要紧的那句：

    **这个词是在当谓语用，还是只是被提到？**

于是「讨厌上课喵」「好好的睡觉时间被补课占用了」这种句子会被当成
「他要去上课」「他要去睡了」，直接落成不在身边 8 小时 / 免打扰 8 小时。

这里给出一道**谓语闸门**，输出三档：

- ``accept``：明确是「我（现在/马上）要去做」→ 照旧记录
- ``reject``：明确只是提到/评价/名词短语/反问 → 丢弃
- ``gray``：看不出来 → 由调用方按「宁可漏，不要错」处理

设计取舍：

1. **只判结构，不判时态**。将来 / 过去继续交给 ``text_rules``，两套时态逻辑
   一旦并存必然漂移（v0.0.5→v0.0.7 已经踩过一次）。闸门排在六道之后。
2. **REJECT 优先于 ACCEPT**。误 accept 的代价是「人明明在床上，却以为他在教室」，
   误 reject 只是少一条提示；代价不对称，所以结构性问题一律先否。
3. **纯函数、零 IO、不读配置**。昵称由调用方传入，方便离线测试。
"""

from __future__ import annotations

import re
from functools import lru_cache

from . import text_rules as rules

VERDICT_ACCEPT = "accept"
VERDICT_REJECT = "reject"
VERDICT_GRAY = "gray"

# 命中词往前看的字数（不跨分句）：只够覆盖「我要去」「讨厌」这类短前缀
HEAD_WIN = 6


# ---------------------------------------------------------------- 词表
# R1：只是在说这个话题
_TOPIC_WORDS = ("提到", "说起", "谈到", "说到", "关于", "对于", "至于")

# R2：后面紧跟名词，说明命中词是定语/名词短语的一部分
_NOUN_SUFFIXES = (
    "时间", "时候", "的时候", "的事", "这件事", "这事儿", "的问题", "的重要性",
    "的安排", "的计划", "这回事", "的路上", "之前", "之后", "以前", "以后",
)
# R2b：后面是「的+名词」
_DE_NOUNS = ("的视频", "的课", "的录音", "的直播", "的照片", "的帖子")

# R4：情绪动词带宾语。「讨厌上课」里的「上课」是宾语，不是他在上课
_EMOTION_TAILS = (
    "讨厌", "不喜欢", "喜欢", "烦", "最怕", "好怕", "怕",
    "受不了", "恨", "嫌弃", "恶心", "崩溃", "抗拒", "抵触",
)
# 但转折/顺接说明后面才是真动作（「讨厌上课但还是得去」）
_TURN_WORDS = ("但还是", "但是", "可是", "不过", "还是", "得去", "必须", "只能", "也要", "也得")

# R5：泛指 / 说的是别人
_VAGUE_HEADS = ("没人", "没有", "没有人", "谁也不", "大家", "别人", "人家")

# R6：反问
_RHETORIC = ("谁要", "谁想", "难道", "哪有", "谁会")

# A1：谓语前的位移/情态助词（看分句头）
_SHIFT_AUX = (
    "去", "在", "正", "正在", "要", "得", "该", "准备",
    "开始", "马上", "这就", "先", "快", "赶紧",
)
# A1 的兜底：这些词出现在**整个分句**里也算「他正要做」。
# 不含「要/得/该」——「读书很重要」「觉得」里的「要/得」是构词，不是谓语。
_STRONG_AUX = (
    "去", "在", "正", "正在", "马上", "这就", "准备", "开始", "赶紧",
    "要去", "要出", "要做", "得去", "该去",
)
# A2：谓语后的体标记 / 动量词
_TAIL_MARKS = (
    "了", "啦", "咯", "喽", "呢", "中", "着", "着呢",
    "去了", "走了", "一趟", "一圈", "一下",
)
# A3：同行/伴随
_TOGETHER_MARKS = (
    "一起", "一块", "一道", "一同", "咱俩", "俩人", "两人",
    "陪你", "带你", "跟我", "跟我来", "陪我", "带我", "跟你", "结伴",
)
# A4：被别人叫去做 —— 也是「我正要去」
_CALL_WORDS = ("喊我", "叫我", "催我", "让我", "该我", "轮到我")

# A6：这几个词本身就是「我要睡了」
_STRONG_SLEEP = (
    "睡了", "睡觉", "睡觉了", "睡啦", "要睡了", "去睡了", "先睡了",
    "我睡了", "睡了哦", "困了睡了", "晚安",
)

# G1：后面接抽象评价 —— 明显不是在报告动作，直接判灰（不送模型，省 token）
_EVAL_TAILS = (
    "很重要", "挺重要", "挺好的", "不错", "太好", "太难", "好难",
    "最累", "好累", "怎么办", "好不好", "累不累", "有用吗",
    "真无聊", "无聊", "真好玩", "好玩", "真舒服", "好舒服", "真爽", "好爽",
)
# 「好难 / 真香」这种「程度词+形容词」也当评价
_EVAL_RE = re.compile(r"^(?:真|太|好|挺|很|最|超)(?:无聊|好玩|舒服|累|香|爽|难|疼|困|冷|热|烦|苦|惨)")

# 各 kind 启用哪些规则（没列出的规则不参与判定）
_SCOPE: dict[str, frozenset[str]] = {
    "event": frozenset(("R1", "R2", "R2b", "R3", "R4", "R5", "R6", "A1", "A2", "A3", "A4", "A5", "A8")),
    "away": frozenset(("R1", "R2", "R2b", "R3", "R4", "R5", "R6", "A1", "A2", "A3", "A4", "A5", "A8")),
    "return": frozenset(("R1", "R2", "R2b", "R3", "A1", "A2", "A5", "A8")),
    "sleep": frozenset(("R1", "R2", "R2b", "R3", "R4", "R5", "R6", "A1", "A2", "A3", "A4", "A5", "A6", "A8")),
    "wait": frozenset(("R1", "R2", "R2b", "R3", "R4", "R5", "R6", "A1", "A2", "A3", "A4", "A5", "A7", "A8")),
    # 结束词只做最窄的判断：名词化 / 定语链 / 情绪宾语 / 体标记
    "stop": frozenset(("R1", "R2", "R2b", "R4", "A2", "A5")),
}

_REASONS = {
    "R1": "只是在提这个话题",
    "R2": "后面接名词，说的是这件事",
    "R2b": "是「…的视频/的课」里的定语",
    "R3": "前面是「…的」定语",
    "R4": "「讨厌/喜欢」后面是宾语",
    "R5": "说的是别人或泛指",
    "R6": "反问句，不是在报告",
    "A1": "前面有「去/在/要」，是谓语",
    "A2": "后面带「了/中」，是谓语",
    "A3": "「一起/陪你」这类同行说法",
    "A4": "别人喊你去，说明正要去",
    "A5": "整句就是这个动作",
    "A8": "省略主语，就是他在做",
    "A6": "就是睡前那句话",
    "A7": "整句就是「晚点睡」的说法",
    "G1": "后面是评价，不是在报告动作",
    "G2": "看不出是不是在说此刻",
    "G3": "有转折，真正的动作在后面",
}

_INVALID_REASON = "命中位置不合法"


def _suffixes(head: str, words) -> bool:
    return bool(head) and head.endswith(words)


def _together_pattern(nicknames: tuple[str, ...]) -> re.Pattern[str]:
    """「陪妃爱洗澡」里的「陪+人」：认你/您、配置里的昵称，以及 2~3 字专名。"""
    alts = ["你", "您"]
    alts += [re.escape(n) for n in nicknames if n]
    alts.append(r"[\u4e00-\u9fa5]{2,3}")
    return re.compile(r"(?:陪|带|跟|和|同|与)(?:" + "|".join(alts) + ")")


@lru_cache(maxsize=512)
def _judge_cached(
    text: str, idx: int, kw: str, kind: str, nicknames: tuple[str, ...]
) -> tuple[str, str, str]:
    head = rules.clause_head(text, idx, window=HEAD_WIN)
    tail_start = idx + len(kw)
    tail = text[tail_start : rules.clause_end(text, tail_start)]
    # 「吃完了的时候」：先剥掉体标记再看是不是名词短语
    tail_bare = tail[1:] if tail[:1] in ("了", "啦", "咯", "喽", "着", "呢") else tail
    clause = text[rules.clause_start(text, idx, None) : rules.clause_end(text, tail_start)]
    scope = _SCOPE.get(kind) or _SCOPE["event"]

    def hit(rule: str, verdict: str) -> tuple[str, str, str]:
        return verdict, rule, _REASONS.get(rule, "")

    # ---------------- REJECT：结构性证据，一律先否
    if "R1" in scope and any(w in head for w in _TOPIC_WORDS):
        return hit("R1", VERDICT_REJECT)

    if "R2" in scope and (
        tail.startswith(_NOUN_SUFFIXES) or tail_bare.startswith(_NOUN_SUFFIXES)
    ):
        return hit("R2", VERDICT_REJECT)

    if "R2b" in scope and (
        tail.startswith(_DE_NOUNS)
        or tail_bare.startswith(_DE_NOUNS)
        or re.match(r".{0,2}的(?:视频|课|录音|直播|照片|帖子)", tail)
    ):
        return hit("R2b", VERDICT_REJECT)

    if (
        "R3" in scope
        and len(head) >= 3
        and _suffixes(head, ("的", "地"))
        and not any(a in head for a in ("去", "在", "要"))
        and not any(w in head[-3:] for w in ("真的", "似的", "目的", "别的", "有的", "总的"))
    ):
        return hit("R3", VERDICT_REJECT)

    if "R4" in scope and any(w in head for w in _EMOTION_TAILS):
        if any(w in clause for w in _TURN_WORDS):
            # 「讨厌上课但还是得去」：真有动作，但情绪词在前，判灰不判死
            return hit("G3", VERDICT_GRAY)
        return hit("R4", VERDICT_REJECT)

    if "R5" in scope and head.startswith(_VAGUE_HEADS) and any(w in head for w in _EMOTION_TAILS):
        return hit("R5", VERDICT_REJECT)

    if "R6" in scope and any(w in text for w in _RHETORIC):
        return hit("R6", VERDICT_REJECT)

    # G1 必须在 ACCEPT 之前：「读书很重要」「上课真无聊」不是动作
    if tail.startswith(_EVAL_TAILS) or _EVAL_RE.match(tail):
        return hit("G1", VERDICT_GRAY)

    # ---------------- ACCEPT：正面证据
    # A1 看整个分句：「跟朋友去吃饭」的关键词是「跟朋友」（名词性），
    # 但分句里有「去」，说明他确实是要去做这件事——这时关键词是谁不重要。
    if "A1" in scope and (
        any(a in head for a in _SHIFT_AUX) or any(a in clause for a in _STRONG_AUX)
    ):
        return hit("A1", VERDICT_ACCEPT)

    if "A2" in scope and tail.startswith(_TAIL_MARKS):
        return hit("A2", VERDICT_ACCEPT)

    if "A3" in scope and (
        any(w in head for w in _TOGETHER_MARKS) or _together_pattern(nicknames).search(head)
    ):
        return hit("A3", VERDICT_ACCEPT)

    if "A4" in scope and any(w in head for w in _CALL_WORDS):
        return hit("A4", VERDICT_ACCEPT)

    if "A5" in scope:
        bare = text.strip().strip(rules.CLAUSE_BOUNDARY)
        # 整句就是这个动作（「洗澡」「晚安」「打游戏」）。上限 3 字，
        # 再长就不是「孤立的一句动作」了（「今天看书」这种要留给灰区）。
        if bare == kw or (len(bare) <= 3 and kw in bare):
            return hit("A5", VERDICT_ACCEPT)

    # A8：中文常省略主语，「出门打车了」「吃完饭了」分句头是空的，
    # 但前面已经排除了名词短语和评价句，这里可以放心当成「他在做」。
    if "A8" in scope and not head:
        return hit("A8", VERDICT_ACCEPT)

    if "A6" in scope and kind == "sleep" and kw in _STRONG_SLEEP:
        return hit("A6", VERDICT_ACCEPT)

    if "A7" in scope and kind == "wait" and len(kw) >= 3:
        return hit("A7", VERDICT_ACCEPT)

    # ---------------- GRAY
    return hit("G2", VERDICT_GRAY)


def judge_detail(
    text: str, idx: int, kw: str, *, kind: str = "event", nicknames=()
) -> dict:
    """返回 ``{"verdict", "rule", "reason"}``，给判定记录用。"""
    text = str(text or "")
    kw = str(kw or "")
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return {"verdict": VERDICT_GRAY, "rule": "", "reason": _INVALID_REASON}
    if not text or not kw or idx < 0 or idx + len(kw) > len(text) or text[idx : idx + len(kw)] != kw:
        return {"verdict": VERDICT_GRAY, "rule": "", "reason": _INVALID_REASON}
    names = tuple(str(n).strip() for n in (nicknames or ()) if str(n).strip())
    verdict, rule, reason = _judge_cached(text, idx, kw, str(kind or "event"), names)
    return {"verdict": verdict, "rule": rule, "reason": reason}


def judge_use(
    text: str, idx: int, kw: str, *, kind: str = "event", nicknames=()
) -> tuple[str, str]:
    """返回 ``(verdict, reason)``；verdict ∈ accept / reject / gray。

    入参非法时返回 ``("gray", "命中位置不合法")``，绝不抛异常——
    这条链路跑在每条私聊消息上，不能因为一个奇怪的偏移量把整个插件带崩。
    """
    d = judge_detail(text, idx, kw, kind=kind, nicknames=nicknames)
    return d["verdict"], d["reason"]
