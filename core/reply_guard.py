"""Keep drafting notes out of the visible answer and its saved text."""

import re

from astrbot.core.message.components import Plain


FALLBACK = "刚刚那句没说好，等我一下哦"
_DRAFT_TAG = re.compile(r"</?(?:think|analysis|reasoning)\b[^>]*>", re.I)
_DRAFT_BLOCK = re.compile(
    r"<(think|analysis|reasoning)\b[^>]*>.*?</\1\s*>", re.I | re.S
)
_DRAFT_LINE = re.compile(
    r'(?im)^[ \t]*(?:Hmm\b[^\n]{0,80}\bopener\b|draft\s*[:：]|思考过程\s*[:：])'
)
_ANALYSIS_LINE = re.compile(r"(?im)^[ \t]*(?:analysis|reasoning)\s*[:：]")
_DRAFT_SELF_TALK = re.compile(
    r"(?is)\b(?:he|she|the user)\s+said\b.{0,240}"
    r"\b(?:let['’]s do|i should|maybe avoid)\b"
)
_FINAL_LINE = re.compile(r"(?im)^[ \t]*(?:final answer|final|最终回复|正式回复)\s*[:：][ \t]*")


def inspect_text(text: str) -> tuple[str, str | None]:
    """Return the safe text and a reason when the visible text was contaminated."""
    # Explicit code blocks and quoted examples are content, not channel markers.
    protected = [
        (m.start(), m.end())
        for m in re.finditer(r'```[\s\S]*?```|`[^`\n]+`|[「“][^」”\n]+[」”]|(?m:^[ \t]*>[^\n]*)', text)
    ]

    def visible(match):
        return not any(a <= match.start() and match.end() <= b for a, b in protected)

    blocks = [m for m in _DRAFT_BLOCK.finditer(text) if visible(m)]
    if blocks:
        for match in reversed(blocks):
            text = text[:match.start()] + " " * (match.end() - match.start()) + text[match.end():]
    tags = [m for m in _DRAFT_TAG.finditer(text) if visible(m)]
    lines = [m for m in _DRAFT_LINE.finditer(text) if visible(m)]
    for match in _ANALYSIS_LINE.finditer(text):
        if visible(match) and _FINAL_LINE.search(text, match.end()):
            lines.append(match)
    if tags or lines:
        start = min(m.start() for m in tags + lines)
        final = next((m for m in _FINAL_LINE.finditer(text, start) if visible(m)), None)
        if final and not _DRAFT_TAG.search(text, final.end()) and not _DRAFT_LINE.search(text, final.end()):
            return text[final.end():].strip(), "draft_removed"
        prefix = text[:start].strip()
        # The incident had a complete reply followed by self-editing and a
        # second version. Keep only the original complete paragraph.
        if (prefix and not tags and re.search(r"\n\s*\n\s*$", text[:start])
                and _DRAFT_SELF_TALK.search(text[start:])):
            return prefix, "draft_removed"
        return "", "draft_blocked"
    if any(visible(m) for m in _DRAFT_SELF_TALK.finditer(text)):
        return "", "draft_blocked"
    return (text.strip(), "draft_removed") if blocks else (text, None)


def clean_history(history: list) -> list:
    """Filter existing assistant text without editing the history on disk."""
    result = []
    for message in history:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            result.append(message)
            continue
        content = message.get("content")
        if isinstance(content, str):
            clean, reason = inspect_text(content)
            if reason and not clean and not message.get("tool_calls"):
                continue
            result.append({**message, "content": clean} if reason else message)
        elif isinstance(content, list):
            texts = [p for p in content if isinstance(p, dict) and p.get("type") == "text"]
            clean, reason = inspect_text("\n".join(str(p.get("text", "")) for p in texts))
            if not reason:
                result.append(message)
                continue
            parts = [p for p in content if p not in texts]
            if clean:
                parts.append({"type": "text", "text": clean})
            if parts or message.get("tool_calls"):
                result.append({**message, "content": parts})
        else:
            result.append(message)
    return result


def response_text(response):
    if response is None or getattr(response, "role", "") != "assistant":
        return ""
    chain = getattr(response, "result_chain", None)
    visible = [p for p in chain.chain if isinstance(p, Plain)] if chain else []
    return "".join(p.text for p in visible) if visible else response.completion_text or ""


def clean_final_response(run_context, response, *, refine=None) -> str | None:
    """Use on_agent_done, before AstrBot yields the chain or persists its messages."""
    if response is None or getattr(response, "role", "") != "assistant":
        return None
    chain = getattr(response, "result_chain", None)
    visible = [p for p in chain.chain if isinstance(p, Plain)] if chain else []
    text = "".join(p.text for p in visible) if visible else response.completion_text
    clean, reason = inspect_text(text or "")
    if refine and clean:
        clean, content_reason = refine(clean)
        reason = content_reason or reason
    if not reason:
        return None
    clean = clean or ("你刚才说的是哪件事呀？" if reason == "unconfirmed_schedule" else FALLBACK)
    if visible:
        first = visible[0]
        first.text = clean
        chain.chain[:] = [p for p in chain.chain if not isinstance(p, Plain) or p is first]
    else:
        response.completion_text = clean
    messages = run_context.messages
    if messages and messages[-1].role == "assistant" and not messages[-1].tool_calls:
        from astrbot.core.agent.message import TextPart

        saved = messages[-1]
        parts = [p for p in (saved.content or []) if getattr(p, "type", None) != "text"] if isinstance(saved.content, list) else []
        saved.content = [*parts, TextPart(text=clean)]
    return reason


def clean_result_chain(chain) -> str | None:
    visible = [p for p in chain if isinstance(p, Plain)]
    clean, reason = inspect_text("".join(p.text for p in visible))
    if reason:
        first = visible[0]
        first.text = clean or FALLBACK
        chain[:] = [p for p in chain if not isinstance(p, Plain) or p is first]
    return reason


def clean_loaded_messages(messages) -> list:
    """Keep the original model objects and their checkpoint metadata for saving."""
    from astrbot.core.agent.message import TextPart

    originals = []
    for message in messages:
        if message.role != "assistant":
            continue
        content = message.content
        if isinstance(content, str):
            clean, reason = inspect_text(content)
            if reason:
                originals.append((message, content))
                message.content = clean or "[旧回复已省略]"
        elif isinstance(content, list):
            text = "\n".join(part.text for part in content if isinstance(part, TextPart))
            clean, reason = inspect_text(text)
            if reason:
                originals.append((message, content))
                parts = [p for p in content if not isinstance(p, TextPart)]
                message.content = [*parts, TextPart(text=clean or "[旧回复已省略]")]
    return originals
