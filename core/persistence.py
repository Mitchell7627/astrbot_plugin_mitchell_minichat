"""Persist JSON before applying a new in-memory value."""
import json
import os
import stat
from pathlib import Path


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    try:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        temporary.touch(mode=mode, exist_ok=True)
        temporary.chmod(mode)
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
