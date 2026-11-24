"""智能分段算法。

参考 astrbot_plugin_splitter 的核心思路，做成无依赖的纯函数：
- 成对符号保护：引号、括号、书名号内部不切断
- 代码块保护：``` 与 <think> 块整体保留
- 平衡分割：按非空白字符数控制每段长度，避免一段特别长一段特别短
- 中英混排判断：不在英文单词/数字中间断开
- 后处理：合并过短片段、限制段数、清理首尾空白
"""

from __future__ import annotations

import math
import random
import re

PAIR_MAP = {
    "《": "》", "（": "）", "(": ")", "[": "]", "{": "}",
    "【": "】", "「": "」", "『": "』", "<": ">",
    "“": "”", "‘": "’",
}
QUOTE_CHARS = {'"', "'", "`"}
DEFAULT_PATTERN = r"[。.？！?!\n…～]+"
SECONDARY = re.compile(r"[，,、；;]+")
CJK_CHAR = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
ALNUM_CHAR = re.compile(r"[a-zA-Z0-9]")
ASCII_PUNCT = re.compile(r"^[ \t.?!,;:\-']+$")


_CONTINUED_OBJECT = re.compile(r"(?:告诉|请问|听说|知道|想起|说起|提到|提起|聊到|看看|听听|说说|想想|帮帮|问问|提醒|通知|希望|因为|如果|要是)(?:了|啦|吧|呀|喵)?$")
_NOT_INDEPENDENT = re.compile(r"(?:以后|之后|之前|的时候|的样子|的感觉|的事情|的问题|的事|的)[呀啊啦呢喵]?$")
_INCOMPLETE_OPENING = re.compile(r"^(?:因为|如果|假如|要是|虽然|只要|除非|为了|例如|比如|关于|以及|或者|而且|并且)")
_INCOMPLETE_ENDING = re.compile(r"(?:听你说|说|告诉|叫|聊|是|要|把|的|着|以后|之前|的时候)$")
_ADDRESS_ONLY = re.compile(r"(?:哥哥|姐姐|宝贝|宝宝|同学|老师|主人|老[师公婆])[呀啊喵啦呢哦～]*")
_SHORT_UTTERANCE = re.compile(
    r"^(?:晚安|早安|你好|辛苦了|谢谢|抱抱|贴贴|摸摸头|戳戳|拍拍|揉揉|蹭蹭)"
    r"|(?:[吧呀啊喵啦咯哟呢哦]|起来了|回来啦|我在|到了|过来|喂|哈{2,})$"
)
_NEWLINE = re.compile(r"[\r\n]+")


def _line_key(text):
    return text.strip().rstrip("。.!！?？…～~").strip()


def explicit_short_lines(text):
    """Identify complete lines deliberately separated by the model, outside paired signs."""
    if not text:
        return frozenset()
    text = normalize_escapes(text)
    if "\n" not in text and "\r" not in text:
        return frozenset()
    lines = _smart_split(text, _NEWLINE, 1, False, 0, 1, ())
    if len(lines) < 2:
        return frozenset()
    result = set()
    for line in lines:
        body = _line_key(line)
        if (_NEWLINE.search(body) or _ADDRESS_ONLY.fullmatch(body) or _INCOMPLETE_OPENING.search(body)
                or _CONTINUED_OBJECT.search(body) or _NOT_INDEPENDENT.search(body)
                or _INCOMPLETE_ENDING.search(body) or sum(c.isalnum() for c in body) < 2):
            continue
        if _SHORT_UTTERANCE.search(body) or line.strip().endswith(("。", "？", "！", "?", "!", ".")):
            result.add(body)
    return frozenset(result)


def normalize_escapes(text: str) -> str:
    """把模型偶尔吐出来的**字面转义**还原成真字符。

    有些模型会把换行写成两个字符（反斜杠 + n），直接发出去就会在聊天里看到
    一个「\\n」。这里只处理最常见的三种，正常文本里不会出现反斜杠，不受影响。
    """
    if not isinstance(text, str) or "\\" not in text:
        return text
    return (
        text.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\r", "\n")
        .replace("\\t", " ")
    )


def split_text(
    text,
    *,
    pattern=r"[。.？！?!\n…～]+",
    smart=True,
    max_segments=5,
    min_length=12,
    balanced=True,
    ratio_min=0.4,
    ratio_max=0.9,
    no_split_around=None,
    trim=True,
    preserve_lines=frozenset(),
    breakdown=None,
):
    """把一段文本切成多条消息。返回字符串列表。"""
    if breakdown is not None:
        breakdown["candidates"] = 0
    if not text or not text.strip():
        return []
    text = normalize_escapes(text)
    try:
        max_segments = max(1, int(max_segments))
    except (TypeError, ValueError):
        max_segments = 1

    protected = [w for w in (no_split_around or []) if w]
    effective = sum(1 for c in text if not c.isspace())
    ideal = max(1, math.ceil(effective / max_segments))

    try:
        compiled = re.compile(pattern)
    except re.error:
        compiled = re.compile(DEFAULT_PATTERN)
    # 能空匹配的正则（`\s*`、`[。]?`…）会让分段循环原地打转：命中、len(delim)=0、i 不动。
    # 一旦写进配置就是死循环，直接挡在门口，回退默认正则。
    empty = compiled.match("")
    if empty is not None and empty.group() == "":
        compiled = re.compile(DEFAULT_PATTERN)

    if smart:
        segments = _smart_split(text, compiled, ideal, balanced, ratio_min, ratio_max, protected, preserve_lines)
    else:
        segments = _simple_split(text, compiled, protected)

    return _postprocess(segments, max_segments, min_length, trim, preserve_lines, breakdown)


def _simple_split(text, compiled, protected):
    parts = re.split("(%s)" % compiled.pattern, text)
    segments = []
    buf = ""
    for idx, part in enumerate(parts):
        if not part:
            continue
        if compiled.fullmatch(part):
            buf += part
            if _blocked_by_protected(parts, idx, protected):
                continue
            if buf.strip():
                segments.append(buf)
            buf = ""
        else:
            buf += part
    if buf.strip():
        segments.append(buf)
    return segments


def _blocked_by_protected(parts, idx, protected):
    if not protected:
        return False
    after = ""
    for k in range(idx + 1, len(parts)):
        if parts[k]:
            after = parts[k]
            break
    after = after.lstrip(" \t")
    for word in protected:
        if after.startswith(word):
            return True
    return False


def _has_closer(text: str, i: int, closer: str) -> bool:
    """i 之后还有没有配对的右符号。没有就不压栈，避免一个孤零零的开符号锁死整段。"""
    return text.find(closer, i + 1) != -1


def _smart_split(text, compiled, ideal, balanced, ratio_min, ratio_max, protected, preserve_lines=frozenset()):
    segments = []
    stack = []
    chunk = ""
    weight = 0
    i = 0
    n = len(text)

    while i < n:
        # 代码块整体保留
        if text.startswith("```", i):
            end = text.find("```", i + 3)
            end = n if end == -1 else end + 3
            chunk += text[i:end]
            weight += sum(1 for c in text[i:end] if not c.isspace())
            i = end
            continue

        match = compiled.match(text, pos=i)
        if match:
            delim = match.group()
            if not delim:
                # 兜底：万一上游漏了可空正则（比如直接调用 _smart_split），按单字符推进
                chunk += text[i]
                weight += 1
                i += 1
                continue
            should = not stack
            if should and balanced and weight < ideal * ratio_min:
                should = False
                if preserve_lines and _NEWLINE.search(delim):
                    end = match.end()
                    next_break = _NEWLINE.search(text, end)
                    next_line = text[end:next_break.start()] if next_break else text[end:]
                    should = _line_key(chunk + delim) in preserve_lines or _line_key(next_line) in preserve_lines
            if should and "\n" not in delim and ASCII_PUNCT.match(delim):
                prev_c = text[i - 1] if i > 0 else ""
                next_c = text[i + len(delim)] if i + len(delim) < n else ""
                if prev_c and next_c and ALNUM_CHAR.match(prev_c) and ALNUM_CHAR.match(next_c):
                    should = False
                if should and prev_c and next_c:
                    if CJK_CHAR.match(prev_c) and ALNUM_CHAR.match(next_c):
                        should = False
                    elif ALNUM_CHAR.match(prev_c) and CJK_CHAR.match(next_c):
                        should = False
            if should and protected:
                scan = i + len(delim)
                while scan < n and text[scan] in " \t":
                    scan += 1
                for word in protected:
                    if text.startswith(word, scan):
                        should = False
                        break

            chunk += delim
            if should:
                if chunk.strip():
                    segments.append(chunk)
                chunk = ""
                weight = 0
            else:
                weight += len(delim)
            i += len(delim)
            continue

        # 达到理想长度后，优先在次级标点（逗号）处断开
        if balanced and not stack and weight >= ideal * ratio_max:
            sec = SECONDARY.match(text, pos=i)
            if sec:
                chunk += sec.group()
                if chunk.strip():
                    segments.append(chunk)
                chunk = ""
                weight = 0
                i += len(sec.group())
                continue

        char = text[i]
        if char in QUOTE_CHARS:
            if stack and stack[-1] == char:
                stack.pop()
            elif _has_closer(text, i, char):
                stack.append(char)
        elif char in PAIR_MAP:
            if _has_closer(text, i, PAIR_MAP[char]):
                stack.append(char)
        elif stack and char == PAIR_MAP.get(stack[-1]):
            stack.pop()

        chunk += char
        weight += 0 if char.isspace() else 1
        i += 1

    if chunk.strip():
        segments.append(chunk)
    return segments


def _postprocess(segments, max_segments, min_length, trim, preserve_lines, breakdown):
    cleaned = []
    for seg in segments:
        s = seg.replace("\r", "")
        if trim:
            s = s.replace("\n", " ").strip()
        else:
            s = s.strip()
        s = re.sub(r"[ \t]{2,}", " ", s)
        if re.fullmatch(r"[\s，。、；：！？,.!?:;—…～~]+", s):
            if cleaned:
                cleaned[-1] += s
        elif s:
            cleaned.append(s)

    if breakdown is not None:
        breakdown["candidates"] = len(cleaned)
    if not cleaned:
        return []

    # 合并过短片段：优先并入前一段，第一段太短则与下一段合并
    merged = []
    for seg in cleaned:
        if merged and len(seg) < min_length and (not preserve_lines or _line_key(seg) not in preserve_lines):
            merged[-1] = _join_segments(merged[-1], seg)
        else:
            merged.append(seg)
    if (len(merged) > 1 and len(merged[0]) < min_length
            and (not preserve_lines or _line_key(merged[0]) not in preserve_lines)):
        merged[1] = _join_segments(merged[0], merged[1])
        merged.pop(0)

    # 段数超过上限时，把末尾多余的并进最后一段
    if len(merged) > max_segments:
        head = merged[: max_segments - 1]
        tail, *rest = merged[max_segments - 1 :]
        for segment in rest:
            tail = _join_segments(tail, segment)
        merged = head + [tail]

    return [s for s in merged if s.strip()]


def _join_segments(left, right):
    # Newline boundaries disappear during trimming. Keep their pause when
    # short sections (or overflow sections) share one QQ bubble.
    if CJK_CHAR.fullmatch(left[-1]) and CJK_CHAR.fullmatch(right[0]):
        separator = "，"
    elif ALNUM_CHAR.fullmatch(right[0]):
        separator = " "
    else:
        separator = ""
    return left + separator + right


def calc_delay(text, *, strategy="random", random_min=0.8, random_max=3.0,
               linear_base=0.5, linear_factor=0.1, log_base=0.5,
               log_factor=0.8, fixed_delay=1.5):
    """计算两段之间的等待秒数。"""
    try:
        if strategy == "random":
            lo = float(random_min)
            hi = max(lo, float(random_max))
            return random.uniform(lo, hi)
        if strategy == "linear":
            return float(linear_base) + len(text) * float(linear_factor)
        if strategy == "log":
            return min(float(log_base) + float(log_factor) * math.log(len(text) + 1), 5.0)
        return float(fixed_delay)
    except (TypeError, ValueError):
        return 1.5
