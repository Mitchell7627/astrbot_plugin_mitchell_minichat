"""判定记录：记住「这句话为什么被记录 / 为什么没被记录」，并支持一键撤销。

为什么要单独存一份：
- 关键词层再准也会偶发误判，用户需要一个「3 秒内改回来」的出口；
- 调规则时要能看见到底哪条规则在说话（reason 里带规则 ID 的中文解释）。

存储策略：内存为准，落盘节流（默认 30 秒一次）。记录很轻（每条 ~300B），
但没必要每条消息都写一次盘——它的价值是回看，不是持久化账本。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

VERSION = 1
# 原文截断长度：面板上够看清，又不至于把一整段聊天记进文件
TEXT_LIMIT = 40


def clip_text(text: str, limit: int = TEXT_LIMIT) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "…"


class DecisionLog:
    def __init__(self, path, limit: int = 50) -> None:
        self.path = Path(path)
        self.limit = max(0, int(limit))
        self._items: list[dict] = []
        self._last_save = 0.0
        self._dirty = False
        self.load()

    # ------------------------------------------------------------------ 读写
    def load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8-sig")
            data = json.loads(raw)
        except Exception:
            return
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, list):
            self._items = [x for x in items if isinstance(x, dict)]
            self._trim()

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"version": VERSION, "items": self._items}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
            self._last_save = time.time()
            self._dirty = False
        except Exception:
            # 记录失败不该影响主流程
            pass

    def maybe_save(self, min_interval: float = 30.0) -> bool:
        if not self._dirty:
            return False
        if time.time() - self._last_save < min_interval:
            return False
        self.save()
        return True

    # ------------------------------------------------------------------ 增查
    def _trim(self) -> None:
        if self.limit <= 0:
            self._items = []
            return
        if len(self._items) > self.limit:
            del self._items[: len(self._items) - self.limit]

    def add(self, record: dict) -> dict | None:
        """写入一条记录；decision_log_size=0 时直接丢弃。"""
        if self.limit <= 0 or not isinstance(record, dict):
            return None
        self._items.append(record)
        self._trim()
        self._dirty = True
        return record

    def items(self, umo: str | None = None, limit: int = 20) -> list[dict]:
        rows = [r for r in self._items if not umo or r.get("umo") == umo]
        rows.reverse()  # 新→旧
        return rows[: max(1, int(limit))]

    def get(self, did) -> dict | None:
        try:
            target = float(did)
        except (TypeError, ValueError):
            return None
        for r in self._items:
            try:
                if float(r.get("id")) == target:
                    return r
            except (TypeError, ValueError):
                continue
        return None

    def counts(self, umo: str | None = None) -> dict:
        rows = [r for r in self._items if not umo or r.get("umo") == umo]
        return {
            "total": len(rows),
            "applied": sum(1 for r in rows if r.get("action") == "applied"),
            "dropped": sum(1 for r in rows if r.get("action") == "dropped"),
            "gray": sum(1 for r in rows if r.get("verdict") == "gray"),
        }

    def clear(self) -> None:
        self._items = []
        self._dirty = True
