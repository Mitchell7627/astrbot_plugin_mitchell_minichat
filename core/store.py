"""会话数据持久化。

只保存调度与事件相关的少量状态，文件放在插件数据目录下的 session_data.json。
写入使用临时文件替换，避免断电/异常退出把文件写坏。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from astrbot.api import logger


class Store:
    def __init__(self, data_dir) -> None:
        self.path = Path(data_dir) / "session_data.json"
        self.data: dict = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                raw = self.path.read_text(encoding="utf-8-sig")
                parsed = json.loads(raw) if raw.strip() else {}
                if isinstance(parsed, dict):
                    self.data = parsed
                    logger.info(f"[Minichat] 已载入 {len(self.data)} 个会话状态")
                    return
        except Exception as e:
            logger.warning(f"[Minichat] 读取状态失败，将从空状态开始: {e}")
        self.data = {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=4),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except Exception as e:
            logger.warning(f"[Minichat] 保存状态失败: {e}")

    def session(self, umo: str) -> dict:
        return self.data.setdefault(umo, {})

    def get(self, umo: str, key: str, default=None):
        return self.data.get(umo, {}).get(key, default)

    def set(self, umo: str, key: str, value) -> None:
        self.data.setdefault(umo, {})[key] = value

    def pop(self, umo: str, key: str) -> None:
        self.data.get(umo, {}).pop(key, None)

    def reset_unanswered(self, umo: str) -> None:
        self.set(umo, "unanswered", 0)

    def bump_unanswered(self, umo: str) -> int:
        current = int(self.get(umo, "unanswered", 0) or 0) + 1
        self.set(umo, "unanswered", current)
        return current

    def touch_message(self, umo: str) -> None:
        self.set(umo, "last_message_at", time.time())

    def minutes_since_message(self, umo: str) -> float:
        last = float(self.get(umo, "last_message_at", 0) or 0)
        if last <= 0:
            return 1e9
        return (time.time() - last) / 60.0
