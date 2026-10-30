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
}
QUOTE_CHARS = {'"', "'", "`"}
SECONDARY = re.compile(r"[，,、；;]+")
CJK_CHAR = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
ALNUM_CHAR = re.compile(r"[a-zA-Z0-9]")
ASCII_PUNCT = re.compile(r"^[ \t.?!,;:\-']+$")


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
):
    """把一段文本切成多条消息。返回字符串列表。"""
    if not text or not text.strip():
        return []
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
        compiled = re.compile(r"[。.？！?!\n…～]+")

    if smart:
        segments = _smart_split(text, compiled, ideal, balanced, ratio_min, ratio_max, protected)
    else:
        segments = _simple_split(text, compiled, protected)

    return _postprocess(segments, max_segments, min_length, trim)


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


def _smart_split(text, compiled, ideal, balanced, ratio_min, ratio_max, protected):
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
            should = not stack
            if should and balanced and weight < ideal * ratio_min:
                should = False
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
            elif not stack:
                stack.append(char)
        elif not stack and char in PAIR_MAP:
            stack.append(char)
        elif stack and char == PAIR_MAP.get(stack[-1]):
            stack.pop()

        chunk += char
        weight += 0 if char.isspace() else 1
        i += 1

    if chunk.strip():
        segments.append(chunk)
    return segments


def _postprocess(segments, max_segments, min_length, trim):
    cleaned = []
    for seg in segments:
        s = seg.replace("\r", "")
        if trim:
            s = s.replace("\n", " ").strip()
        else:
            s = s.strip()
        s = re.sub(r"[ \t]{2,}", " ", s)
        if s:
            cleaned.append(s)

    if not cleaned:
        return []

    # 合并过短片段：优先并入前一段，第一段太短则与下一段合并
    merged = []
    for seg in cleaned:
        if merged and len(seg) < min_length:
            merged[-1] = merged[-1] + seg
        else:
            merged.append(seg)
    if len(merged) > 1 and len(merged[0]) < min_length:
        merged[1] = merged[0] + merged[1]
        merged.pop(0)

    # 段数超过上限时，把末尾多余的并进最后一段
    if len(merged) > max_segments:
        head = merged[: max_segments - 1]
        tail = "".join(merged[max_segments - 1 :])
        merged = head + [tail]

    return [s for s in merged if s.strip()]


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
