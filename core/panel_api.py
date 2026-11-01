"""面板业务层。

这里只写「做什么」，不关心请求从哪来：
- 官方插件页（6185，bridge SDK）→ PanelPageApi 适配成 register_web_api 的 handler
- 独立面板（7627，uvicorn）  → web_panel 直接调同一批方法

两条通道共用一份实现，避免行为漂移。
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path

from astrbot.api import logger

from .probability import dual_forecast
from .splitter import split_text

TAG = "[Minichat]"
PLUGIN_NAME = "astrbot_plugin_mitchell_minichat"
API_PREFIX = f"/{PLUGIN_NAME}/page"
# 这些 GET 接口需要读 query（其余 GET 一律 fn()）
_QUERY_METHODS = {"probability", "decisions"}
MAX_RULES_BYTES = 256 * 1024

# 与 _conf_schema.json 的 slider 范围保持一致。
# Web 面板是可自由输入的数字框，这里必须再夹一次：
# 手改请求或前端漏拦时，别让 3000 分钟这种值写进配置。
_LIMITS: dict[tuple[str, str], tuple[float, float, type]] = {
    ("friend_settings", "schedule_settings.min_interval_minutes"): (1, 2880, int),
    ("friend_settings", "schedule_settings.max_interval_minutes"): (1, 2880, int),
    ("friend_settings", "schedule_settings.max_unanswered_times"): (0, 20, int),
    ("friend_settings", "segmented_reply_settings.max_segments"): (1, 10, int),
    ("friend_settings", "segmented_reply_settings.min_segment_length"): (1, 60, int),
    ("custom_engine", "early_weight"): (0, 10, float),
    ("custom_engine", "early_weight_event"): (0, 10, float),
    ("custom_engine", "mid_weight"): (0, 10, float),
    ("custom_engine", "tail_head_weight"): (0, 10, float),
    ("custom_engine", "tail_floor_weight"): (0, 10, float),
    ("custom_engine", "tail_decay_power"): (0.05, 2, float),
    # 「等会再睡」的一次性提醒：区间与三段权重
    ("custom_engine", "wait_remind_min_minutes"): (5, 180, int),
    ("custom_engine", "wait_remind_max_minutes"): (10, 360, int),
    ("custom_engine", "wait_weight_early"): (0, 10, float),
    ("custom_engine", "wait_weight_mid"): (0, 10, float),
    ("custom_engine", "wait_weight_tail"): (0, 10, float),
    ("custom_engine", "night_weight_early"): (0, 10, float),
    ("custom_engine", "night_weight_mid"): (0, 10, float),
    ("custom_engine", "night_weight_tail"): (0, 10, float),
    ("custom_engine", "nap_weight_early"): (0, 10, float),
    ("custom_engine", "nap_weight_mid"): (0, 10, float),
    ("custom_engine", "nap_weight_tail"): (0, 10, float),
    ("web_panel", "port"): (1024, 65535, int),
    # 柱形图纵轴封顶（%）：纯显示设置，改了不用重载插件
    ("web_panel", "chart_ceiling_pct"): (1, 100, int),
    # 省 token：上下文窗口（与 schema slider 保持一致）
    ("friend_settings", "context_settings.history_hard_limit"): (0, 500, int),
    ("friend_settings", "context_settings.context_window_count"): (2, 200, int),
    ("friend_settings", "context_settings.context_window_hours"): (1, 168, int),
    ("friend_settings", "context_settings.context_window_min_messages"): (0, 50, int),
    # 谓语闸门 / 判定记录
    ("custom_engine", "gray_review_per_hour"): (0, 60, int),
    ("custom_engine", "gray_review_per_day"): (0, 300, int),
    ("custom_engine", "decision_log_size"): (0, 200, int),
    # 约定到点提醒
    ("custom_engine", "appointment_lead_minutes"): (0, 15, int),
    ("custom_engine", "appointment_merge_minutes"): (5, 60, int),
    ("custom_engine", "appointment_followup_minutes"): (3, 30, int),
    ("custom_engine", "appointment_max_followups"): (0, 2, int),
    ("custom_engine", "appointment_max_hours"): (1, 24, int),
    ("custom_engine", "appointment_max_per_day"): (0, 20, int),
    ("custom_engine", "appointment_gray_per_hour"): (0, 20, int),
    ("custom_engine", "appointment_gray_per_day"): (0, 100, int),
}

# 这些键只影响面板显示，不参与 need_reload 判定
_UI_ONLY_PATHS = ("web_panel.chart_ceiling_pct",)

# 这些键每次用到时都从 self.engine_cfg 现读，改完立刻生效，不需要重载插件。
# （重载会让面板断 8 秒、还会重置排程，对"改个开关"来说太重了）
_HOT_PATHS = tuple("custom_engine." + k for k in (
    "enable_sleep_reminder", "remind_window_start", "remind_window_end",
    "nap_remind_enabled", "nap_window_start", "nap_window_end",
    "wake_time", "night_start_time", "sleep_quiet_hours",
    "sleep_keywords", "wait_keywords", "wake_keywords",
    "wait_remind_min_minutes", "wait_remind_max_minutes",
    "wait_weight_early", "wait_weight_mid", "wait_weight_tail",
    "night_weight_early", "night_weight_mid", "night_weight_tail",
    "nap_weight_early", "nap_weight_mid", "nap_weight_tail",
    # 谓语闸门：都在 runner 里每次现读，改完立即生效
    "enable_utterance_judge", "judge_strict", "enable_gray_review",
    "gray_review_per_hour", "gray_review_per_day", "decision_log_size",
    # 阶段结束词：每次判定现读，改完立即生效
    "period_end_keywords",
    # 约定到点提醒：调度每轮现读
    "enable_appointment", "appointment_lead_enabled", "appointment_lead_minutes",
    "appointment_merge_minutes", "appointment_followup_enabled",
    "appointment_followup_minutes", "appointment_max_followups",
    "appointment_max_hours", "appointment_max_per_day", "appointment_keywords",
    "appointment_gray_per_hour", "appointment_gray_per_day",
))


def _dig(source, path: str):
    cur = source
    for key in str(path).split("."):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _dig_set(target: dict, path: str, value) -> bool:
    keys = str(path).split(".")
    cur = target
    for key in keys[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            return False
        cur = nxt
    cur[keys[-1]] = value
    return True


def _clamp_patch(section: str, sub: dict) -> list[str]:
    """把 patch 里越界的数字夹回范围，返回被改动的项。"""
    fixed: list[str] = []
    for (sec, path), (lo, hi, cast) in _LIMITS.items():
        if sec != section:
            continue
        raw = _dig(sub, path)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            num = float(raw)
        except (TypeError, ValueError):
            continue
        value = min(hi, max(lo, num))
        value = int(value) if cast is int else float(value)
        if value != raw:
            if _dig_set(sub, path, value):
                fixed.append(f"{path} → {value}")
    return fixed


class PanelError(Exception):
    """带机器码的错误，前端按 code 做分支。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _as_int(value, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _deep_merge(base: dict, patch: dict, prefix: str = "") -> list[str]:
    """把 patch 递归并进 base，返回发生变化的点分路径（带完整层级）。

    以前只回叶子键名（"min_interval_minutes"），拼上 section 后变成
    "friend_settings.min_interval_minutes"，丢了 schedule_settings 这一层，
    前端按前缀判断「改的是不是排程/权重」就会漏。
    """
    changed: list[str] = []
    for key, value in patch.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            changed.extend(_deep_merge(base[key], value, path + "."))
        elif base.get(key) != value:
            base[key] = value
            changed.append(path)
    return changed


class _Busy:
    """同一会话的动作互斥，避免连点导致双发。"""

    def __init__(self, service: "PanelService", umo: str, kind: str) -> None:
        self.service = service
        self.umo = umo
        self.kind = kind

    def __enter__(self) -> None:
        item = self.service._inflight.get(self.umo)
        if item and time.time() - item[1] < 180:
            raise PanelError("E_BUSY", f"该会话正在{self._kind_cn(item[0])}中")
        self.service._inflight[self.umo] = (self.kind, time.time())

    def __exit__(self, *exc) -> None:
        self.service._inflight.pop(self.umo, None)

    @staticmethod
    def _kind_cn(kind: str) -> str:
        return {"preview": "生成预览", "send": "发送", "fire": "发送"}.get(kind, "处理")


class PanelService:
    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self._inflight: dict[str, tuple[str, float]] = {}
        self._reloading = False
        self._restarting = False
        self._last_reload = {"at": 0, "ok": True, "error": None, "by": ""}
        self.state_path = Path(plugin.data_dir) / "panel_state.json"
        self._load_state()

    # ------------------------------------------------------------------
    # 基础访问
    # ------------------------------------------------------------------
    @property
    def runner(self):
        return getattr(self.plugin, "runner", None)

    @property
    def cfg(self) -> dict:
        try:
            return self.plugin.config.get("friend_settings") or {}
        except Exception:
            return {}

    @property
    def engine_cfg(self) -> dict:
        try:
            return self.plugin.config.get("custom_engine") or {}
        except Exception:
            return {}

    @property
    def store(self):
        return self.plugin.store

    def sessions(self) -> list[str]:
        return [s for s in (self.cfg.get("session_list") or []) if str(s).strip()]

    def _require_runner(self):
        if not bool(self.cfg.get("enable", False)):
            raise PanelError("E_PLUGIN_OFF", "插件未启用，先在配置里打开「启用插件」")
        runner = self.runner
        if runner is None or not runner.started:
            raise PanelError("E_PLUGIN_OFF", "运行器未启动，请保存后重载插件")
        return runner

    def _load_state(self) -> None:
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._last_reload.update(data)
        except Exception:
            pass

    def _save_state(self) -> None:
        try:
            self.state_path.write_text(
                json.dumps(self._last_reload, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.debug(f"{TAG} 写入面板状态失败: {e}")

    # ------------------------------------------------------------------
    # 状态与任务
    # ------------------------------------------------------------------
    def _appointment_view(self, runner, umo: str):
        """任务卡上的「约定」那一格：最近一条未完成的。"""
        if runner is None:
            return None
        try:
            item = runner._next_appointment(umo, time.time())
            if not isinstance(item, dict) or not item.get("at"):
                return None
            cfg = runner._appointment_cfg()
            return {
                "at": item.get("at"),
                "label": item.get("label"),
                "time_text": item.get("time_text"),
                "kind": item.get("kind"),
                "stage": item.get("stage"),
                "raw": item.get("raw"),
                "lead_at": float(item["at"]) - cfg["lead_minutes"] * 60,
                "follow_up_at": item.get("follow_up_at") or None,
                "follow_ups": _as_int(item.get("follow_ups"), 0),
                "source": item.get("source"),
                "pending": len(runner._appointments(umo)),
            }
        except Exception:
            return None

    def _task_item(self, umo: str) -> dict:
        now = time.time()
        state = self.store.data.get(umo, {}) or {}
        runner = self.runner

        next_at = _as_float(state.get("next_at"), 0.0)
        # 跑完但还没被替换的 task 不能再算「已排程」，否则暂停会话会显示成 due
        timer = getattr(runner, "timers", {}).get(umo) if runner else None
        alive = bool(timer is not None and not timer.done())
        if alive and next_at > now:
            phase = "scheduled"
        elif next_at > 0 and next_at <= now:
            phase = "paused" if not alive else "due"
        else:
            phase = "unscheduled"

        event = None
        if runner is not None:
            try:
                event = runner._active_event(umo)
            except Exception:
                event = None
        away = state.get("away") if isinstance(state.get("away"), dict) else None
        away_active = None
        if isinstance(away, dict):
            away_active = bool(away.get("active"))
            # 和 event_hint 口径一致：过了 expires_at 就不再算「不在身边」
            if away_active:
                exp = _as_float(away.get("expires_at"), 0.0)
                if exp and now >= exp:
                    away_active = False
        sleep = None
        if runner is not None:
            try:
                ss = runner._sleep_state(umo)
                if isinstance(ss, dict) and ss.get("state"):
                    sleep = {
                        "state": ss.get("state"),
                        "keyword": ss.get("keyword"),
                        "started_at": ss.get("started_at"),
                        "wake_at": ss.get("wake_at"),
                        "remind_at": ss.get("remind_at"),
                        "last_reminded_at": ss.get("last_reminded_at"),
                        "last_reminded_window": ss.get("last_reminded_window"),
                    }
            except Exception:
                sleep = None

        return {
            "umo": umo,
            "phase": phase,
            "next_at": next_at or None,
            "next_in_seconds": max(0, int(next_at - now)) if alive and next_at > now else None,
            "unanswered": _as_int(state.get("unanswered"), 0),
            "last_message_at": _as_float(state.get("last_message_at"), 0.0) or None,
            "scheduled": alive,
            "event": None if not isinstance(event, dict) else {
                "name": event.get("name"),
                "label": event.get("label"),
                "expires_at": event.get("expires_at"),
                "boost": event.get("boost"),
                "boost_used": _as_int(event.get("boost_used"), 0),
                "boost_budget": _as_int(event.get("boost_budget"), 1),
            },
            "away": None if not isinstance(away, dict) else {
                "active": away_active,
                "reason": away.get("reason"),
                "expires_at": away.get("expires_at"),
            },
            "together": None if not isinstance(state.get("together"), dict) else {
                "active": bool(state["together"].get("active")),
                "since": state["together"].get("since"),
                "until": state["together"].get("until"),
            },
            "sleep": sleep,
            "appointment": self._appointment_view(runner, umo),
        }

    def status(self) -> dict:
        now = time.time()
        runner = self.runner
        tasks = [self._task_item(umo) for umo in self.sessions()]
        panel = self.plugin.panel_status()
        try:
            rules = self.plugin.engine.rules_snapshot()
        except Exception:
            rules = []
        return {
            "now": now,
            "plugin": {
                "name": PLUGIN_NAME,
                "version": self.plugin.version,
                "enable": bool(self.cfg.get("enable", False)),
                "runner": runner is not None,
                "started": bool(runner and runner.started),
                "rules": len(rules),
            },
            "panel": panel,
            "schedule": {
                "min": _as_int(self.cfg.get("schedule_settings", {}).get("min_interval_minutes"), 5),
                "max": _as_int(self.cfg.get("schedule_settings", {}).get("max_interval_minutes"), 240),
                "max_unanswered": _as_int(
                    self.cfg.get("schedule_settings", {}).get("max_unanswered_times"), 3
                ),
                "weighted": bool(self.engine_cfg.get("enable_weighted", True)),
                "event": bool(self.engine_cfg.get("enable_event", True)),
            },
            "summary": {
                "sessions": len(tasks),
                "scheduled": sum(1 for t in tasks if t["scheduled"]),
                "events": sum(1 for t in tasks if t["event"]),
                "away": sum(1 for t in tasks if t["away"] and t["away"].get("active")),
                "unanswered": sum(t["unanswered"] for t in tasks),
                "appointments": sum(1 for t in tasks if t.get("appointment")),
            },
            "inflight": sorted(self._inflight.keys()),
            "last_reload": dict(self._last_reload),
        }

    def tasks(self) -> dict:
        items = [self._task_item(umo) for umo in self.sessions()]
        return {
            "sessions": items,
            "counts": {
                "total": len(items),
                "scheduled": sum(1 for t in items if t["scheduled"]),
                "paused": sum(1 for t in items if t["phase"] == "paused"),
            },
        }

    def decisions(self, query: dict | None = None) -> dict:
        """最近判定记录（只读，本地环形缓冲，不联网不花 token）。"""
        query = query or {}
        umo = str(query.get("umo") or "").strip() or None
        try:
            limit = int(query.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(50, limit))
        log = getattr(self.runner, "decisions", None) if self.runner else None
        if log is None:
            return {"items": [], "counts": {"total": 0, "applied": 0, "dropped": 0, "gray": 0}}
        return {"items": log.items(umo, limit=limit), "counts": log.counts(umo)}

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def config_get(self) -> dict:
        data = {
            "friend_settings": copy.deepcopy(self.cfg),
            "custom_engine": copy.deepcopy(self.engine_cfg),
            "web_panel": copy.deepcopy(
                self.plugin.config.get("web_panel") or {}
            ),
        }
        wp = data.get("web_panel") or {}
        wp["has_password"] = bool(str(wp.pop("password", "") or ""))
        data["web_panel"] = wp
        return data

    async def config_save(self, payload: dict) -> dict:
        patch = (payload or {}).get("patch") or {}
        if not isinstance(patch, dict) or not patch:
            raise PanelError("E_BAD_REQUEST", "没有可保存的内容")

        config = self.plugin.config
        clamped: list[str] = []
        for section in ("friend_settings", "custom_engine", "web_panel"):
            sub = patch.get(section)
            if isinstance(sub, dict):
                clamped.extend(_clamp_patch(section, sub))

        # 有效区间校验：patch 里的值优先，其次看当前配置
        def _effective(path: str):
            sub = patch.get("friend_settings")
            if isinstance(sub, dict):
                value = _dig(sub, path)
                if value is not None:
                    return value
            return _dig(config.get("friend_settings") or {}, path)

        lo = _effective("schedule_settings.min_interval_minutes")
        hi = _effective("schedule_settings.max_interval_minutes")
        try:
            if lo is not None and hi is not None and int(lo) > int(hi):
                raise PanelError(
                    "E_BAD_REQUEST", f"最短间隔（{int(lo)}）不能大于最长间隔（{int(hi)}）"
                )
        except (TypeError, ValueError):
            pass

        changed: list[str] = []
        for section in ("friend_settings", "custom_engine", "web_panel"):
            sub = patch.get(section)
            if not isinstance(sub, dict):
                continue
            current = config.get(section)
            if not isinstance(current, dict):
                config[section] = copy.deepcopy(sub)
                changed.append(section)
            else:
                for path in _deep_merge(current, sub):
                    changed.append(f"{section}.{path}")

        if not changed:
            return {"saved": False, "changed": [], "need_reload": False, "clamped": clamped}

        try:
            saver = getattr(config, "save_config_async", None)
            if saver is not None:
                await saver()
            else:
                config.save_config()
        except Exception as e:
            raise PanelError("E_CONFIG_SAVE", f"写盘失败: {e}")

        need_reload = any(
            (c.startswith("custom_engine") or c.startswith("web_panel"))
            and c not in _UI_ONLY_PATHS
            and c not in _HOT_PATHS
            for c in changed
        )
        return {
            "saved": True,
            "changed": changed,
            "need_reload": need_reload,
            "clamped": clamped,
            "reasons": [
                c for c in changed
                if c.startswith(("custom_engine", "web_panel"))
                and c not in _UI_ONLY_PATHS and c not in _HOT_PATHS
            ],
        }

    # ------------------------------------------------------------------
    # 重载
    # ------------------------------------------------------------------
    def reload(self, _payload=None) -> dict:
        # 注意：两个传输层都会把请求体当第一个位置参数传进来，这里显式忽略。
        if self._reloading:
            raise PanelError("E_BUSY", "重载已在进行")
        self._reloading = True
        asyncio.create_task(self._reload_task("panel"))
        return {"accepted": True, "poll_ms": 800, "expect_down_ms": 8000}

    async def _reload_task(self, by: str) -> None:
        await asyncio.sleep(0.15)  # 先让 202 回包，避免 shutdown 等当前连接
        ok, err = False, "star_manager 不可用"
        try:
            mgr = getattr(self.plugin.context, "_star_manager", None)
            if mgr is None:
                mgr = getattr(getattr(self.plugin.context, "star_manager", None), "_pm", None)
            if mgr is not None:
                result = await mgr.reload(PLUGIN_NAME)
                if isinstance(result, tuple):
                    ok, err = bool(result[0]), result[1]
                else:
                    ok, err = bool(result), None
        except Exception as e:
            err = repr(e)
            logger.exception(f"{TAG} 重载失败")
        finally:
            self._reloading = False
            self._last_reload = {"at": time.time(), "ok": ok, "error": err, "by": by}
            self._save_state()

    def reload_state(self) -> dict:
        data = dict(self._last_reload)
        data["running"] = self._reloading
        return data

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------
    def _validate_umo(self, umo: str) -> str:
        umo = str(umo or "").strip()
        if not umo or ":" not in umo or len(umo) > 200:
            raise PanelError("E_BAD_REQUEST", "会话标识不合法")
        return umo

    async def preview(self, payload: dict) -> dict:
        umo = self._validate_umo((payload or {}).get("umo"))
        runner = self._require_runner()
        started = time.time()
        with _Busy(self, umo, "preview"):
            try:
                text = await asyncio.wait_for(runner._generate(umo), timeout=90)
            except asyncio.TimeoutError:
                raise PanelError("E_UPSTREAM", "生成超时（90 秒）")
            except PanelError:
                raise
            except Exception as e:
                raise PanelError("E_UPSTREAM", f"生成失败: {e}")

        segments = []
        if text:
            try:
                conf = self.cfg.get("segmented_reply_settings", {}) or {}
                segments = split_text(
                    text,
                    pattern=str(conf.get("split_regex") or r"[。.？！?!\n…～]+"),
                    smart=bool(conf.get("enable_smart_split", True)),
                    max_segments=_as_int(conf.get("max_segments"), 5),
                    min_length=_as_int(conf.get("min_segment_length"), 12),
                    balanced=bool(conf.get("balanced_split", True)),
                    ratio_min=_as_float(conf.get("balanced_split_ratio_min"), 0.4),
                    ratio_max=_as_float(conf.get("balanced_split_ratio_max"), 0.9),
                    no_split_around=conf.get("no_split_around") or [],
                    trim=bool(conf.get("trim_edge_blank", True)),
                )
            except Exception:
                segments = [text]
        return {
            "umo": umo,
            "text": text,
            "segments": segments,
            "chars": len(text or ""),
            "elapsed_ms": int((time.time() - started) * 1000),
        }

    async def send(self, payload: dict) -> dict:
        payload = payload or {}
        umo = self._validate_umo(payload.get("umo"))
        runner = self._require_runner()
        text = str(payload.get("text") or "").strip()

        with _Busy(self, umo, "send"):
            if not text:
                try:
                    text = await asyncio.wait_for(runner._generate(umo), timeout=90)
                except asyncio.TimeoutError:
                    raise PanelError("E_UPSTREAM", "生成超时（90 秒）")
                if not text:
                    raise PanelError("E_UPSTREAM", "生成为空，未发送")
            try:
                ok = await runner._send_split(umo, text)
            except Exception as e:
                raise PanelError("E_UPSTREAM", f"发送失败: {e}")

        state = self.store.data.get(umo, {}) or {}
        if ok:
            count = self.store.bump_unanswered(umo)
            self.store.save()
            # 手动发送的主动消息同样写回历史，保持上下文连续
            try:
                await runner._append_assistant_history(umo, text)
            except Exception:
                pass
        else:
            count = _as_int(state.get("unanswered"), 0)

        if ok and payload.get("reschedule"):
            runner.reschedule(umo)
            state = self.store.data.get(umo, {}) or {}

        return {
            "sent": bool(ok),
            "text": text,
            "unanswered": count,
            "next_at": _as_float(state.get("next_at"), 0.0) or None,
        }

    async def fire(self, payload: dict) -> dict:
        umo = self._validate_umo((payload or {}).get("umo"))
        runner = self._require_runner()
        before = _as_int((self.store.data.get(umo, {}) or {}).get("unanswered"), 0)
        with _Busy(self, umo, "fire"):
            try:
                await asyncio.wait_for(runner._fire(umo), timeout=120)
            except asyncio.TimeoutError:
                raise PanelError("E_UPSTREAM", "发送超时（120 秒）")
            except Exception as e:
                raise PanelError("E_UPSTREAM", f"发送异常: {e}")
        state = self.store.data.get(umo, {}) or {}
        after = _as_int(state.get("unanswered"), 0)
        return {
            "fired": True,
            "sent": after > before,
            "unanswered": after,
            "next_at": _as_float(state.get("next_at"), 0.0) or None,
        }

    def reschedule(self, payload: dict) -> dict:
        payload = payload or {}
        umo = self._validate_umo(payload.get("umo"))
        runner = self._require_runner()
        minutes = payload.get("minutes")
        if minutes is None or minutes == "":
            runner.reschedule(umo)
        else:
            runner.reschedule(umo, delay_minutes=max(0.0, _as_float(minutes, 0.0)))
        state = self.store.data.get(umo, {}) or {}
        next_at = _as_float(state.get("next_at"), 0.0)
        return {"next_at": next_at or None, "next_in_seconds": max(0, int(next_at - time.time()))}

    def task_action(self, payload: dict) -> dict:
        payload = payload or {}
        action = str(payload.get("action") or "")
        umo = str(payload.get("umo") or "").strip()
        sessions = self.sessions()

        if action == "add":
            umo = self._validate_umo(umo)
            if umo not in sessions:
                sessions.append(umo)
                self.plugin.config["friend_settings"]["session_list"] = sessions
                self.plugin.config.save_config()
            runner = getattr(self.plugin, "runner", None)
            if runner is not None and runner.started:
                runner.reschedule(umo, delay_minutes=1)
        elif action == "remove":
            if umo not in sessions:
                raise PanelError("E_NOT_FOUND", "该会话不在列表里")
            sessions.remove(umo)
            self.plugin.config["friend_settings"]["session_list"] = sessions
            self.plugin.config.save_config()
            runner = getattr(self.plugin, "runner", None)
            if runner is not None:
                task = getattr(runner, "timers", {}).pop(umo, None)
                if task is not None:
                    task.cancel()
        elif action == "reset_unanswered":
            umo = self._validate_umo(umo)
            self.store.reset_unanswered(umo)
            self.store.save()
        elif action == "clear_event":
            umo = self._validate_umo(umo)
            self.store.pop(umo, "active_event")
            self.store.save()
        elif action == "undo_decision":
            rid = payload.get("id")
            if rid is None:
                raise PanelError("E_BAD_REQUEST", "缺少 id")
            runner = getattr(self.plugin, "runner", None)
            fn = getattr(runner, "undo_decision", None) if runner is not None else None
            if fn is None:
                raise PanelError("E_PLUGIN_OFF", "当前版本不支持撤销")
            try:
                fn(rid)
            except KeyError:
                raise PanelError("E_NOT_FOUND", "找不到这条判定")
            except ValueError as e:
                raise PanelError("E_BAD_REQUEST", str(e))
        elif action == "clear_away":
            umo = self._validate_umo(umo)
            self.store.pop(umo, "away")
            self.store.save()
        elif action == "clear_sleep":
            umo = self._validate_umo(umo)
            self.store.pop(umo, "sleep_state")
            self.store.save()
        elif action == "clear_appointment":
            umo = self._validate_umo(umo)
            self.store.pop(umo, "appointments")
            self.store.pop(umo, "appointment_counter")
            self.store.save()
            runner = getattr(self.plugin, "runner", None)
            if runner is not None and runner.started:
                runner.reschedule(umo)
        elif action == "mark_together":
            umo = self._validate_umo(umo)
            runner = self._require_runner()
            runner._touch_together(umo, time.time(), "面板手动")
            self.store.pop(umo, "away")
            self.store.save()
        elif action == "mark_away":
            # 手动校正：人确实不在身边（和「设为在一起」对称，方便判错时一键纠正）
            umo = self._validate_umo(umo)
            now = time.time()
            self.store.pop(umo, "together")
            self.store.set(umo, "away", {
                "active": True,
                "reason": str(payload.get("reason") or "面板手动"),
                "keyword": "",
                "started_at": now,
                "expires_at": now + 8.0 * 3600,
                "cleared_at": None,
            })
            self.store.save()
        elif action == "clear_together":
            umo = self._validate_umo(umo)
            self.store.pop(umo, "together")
            self.store.save()
        else:
            raise PanelError("E_BAD_REQUEST", f"未知动作 {action}")
        return self.tasks()

    # ------------------------------------------------------------------
    # 事件识别自测
    # ------------------------------------------------------------------
    async def debug_classify(self, payload: dict) -> dict:
        """跑一遍关键词 + 语义归类，只返回结果，不改任何状态。"""
        payload = payload or {}
        umo = str(payload.get("umo") or "").strip()
        if not umo and self.sessions():
            umo = self.sessions()[0]
        umo = self._validate_umo(umo)
        text = str(payload.get("text") or "").strip()
        if not text:
            raise PanelError("E_BAD_REQUEST", "缺少要测试的句子")
        runner = self._require_runner()

        keyword = None
        try:
            rule = runner.engine.match(text)
            keyword = rule.get("name") if rule else None
        except Exception:
            keyword = None

        soon = None
        try:
            kw = runner._return_soon_kw(text)
            if kw:
                ev = runner._active_event(umo)
                if isinstance(ev, dict) and ev.get("name") in runner.RETURN_SOON_BOOSTABLE:
                    soon = f"叠加到 {ev.get('label')}"
                else:
                    soon = "快回来了"
        except Exception:
            soon = None

        llm = await runner.classify_text(umo, text)
        return {"umo": umo, "text": text, "keyword": keyword, "return_soon": soon, "llm": llm}

    # ------------------------------------------------------------------
    # 概率
    # ------------------------------------------------------------------
    def probability(self, query: dict) -> dict:
        query = query or {}
        umo = str(query.get("umo") or "").strip()
        if not umo and self.sessions():
            umo = self.sessions()[0]

        schedule = self.cfg.get("schedule_settings", {}) or {}
        active_event = None
        if umo and self.runner is not None:
            try:
                active_event = self.runner._active_event(umo)
            except Exception:
                active_event = None

        result = dual_forecast(
            schedule=schedule,
            engine_cfg=self.engine_cfg,
            active_event=active_event,
        )
        state = self.store.data.get(umo, {}) or {} if umo else {}
        result["meta"]["umo"] = umo
        result["meta"]["scheduled_at"] = _as_float(state.get("next_at"), 0.0) or None
        return result

    def probability_preview(self, payload: dict) -> dict:
        """用「还没保存」的表单值算一次分布，只用来画图。

        不读也不写插件配置：schedule / engine 都是深拷贝后临时覆盖。
        """
        payload = payload or {}
        patch = payload.get("patch")
        if not isinstance(patch, dict):
            raise PanelError("E_BAD_REQUEST", "patch 必须是对象")

        umo = str(payload.get("umo") or "").strip()
        if not umo and self.sessions():
            umo = self.sessions()[0]

        schedule = copy.deepcopy(self.cfg.get("schedule_settings", {}) or {})
        engine = copy.deepcopy(self.engine_cfg)

        fs = patch.get("friend_settings")
        if isinstance(fs, dict) and isinstance(fs.get("schedule_settings"), dict):
            for key, value in fs["schedule_settings"].items():
                if value is not None:
                    schedule[key] = value
        ce = patch.get("custom_engine")
        if isinstance(ce, dict):
            for key, value in ce.items():
                if value is not None:
                    engine[key] = value

        # 边界兜底：前端会夹，手改请求也不能让采样器炸掉
        try:
            lo = int(schedule.get("min_interval_minutes") or 5)
            hi = int(schedule.get("max_interval_minutes") or 240)
        except (TypeError, ValueError):
            lo, hi = 5, 240
        lo = min(2880, max(1, lo))
        hi = min(2880, max(1, hi))
        if lo > hi:
            lo, hi = hi, lo
        schedule["min_interval_minutes"] = lo
        schedule["max_interval_minutes"] = hi

        active_event = None
        if umo and self.runner is not None:
            try:
                active_event = self.runner._active_event(umo)
            except Exception:
                active_event = None

        result = dual_forecast(
            schedule=schedule,
            engine_cfg=engine,
            active_event=active_event,
        )
        state = self.store.data.get(umo, {}) or {} if umo else {}
        result["meta"]["umo"] = umo
        result["meta"]["preview"] = True
        result["meta"]["scheduled_at"] = _as_float(state.get("next_at"), 0.0) or None
        return result

    # ------------------------------------------------------------------
    # 配置结构（文案/边界直接取自 AstrBot 配置页）
    # ------------------------------------------------------------------
    def schema_get(self) -> dict:
        path = Path(self.plugin.plugin_dir) / "_conf_schema.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as e:
            raise PanelError("E_NOT_FOUND", f"读取配置结构失败: {e}")

        fields: dict[str, dict] = {}

        def walk(items: dict, prefix: str, section: str) -> None:
            for key, meta in (items or {}).items():
                if not isinstance(meta, dict):
                    continue
                kind = meta.get("type")
                node = f"{prefix}{key}"
                if kind == "object" and isinstance(meta.get("items"), dict):
                    walk(meta["items"], f"{node}.", section)
                    continue
                if kind == "template_list":
                    continue
                if kind is None:
                    continue
                item = {
                    "type": kind,
                    "description": meta.get("description") or key,
                    "hint": meta.get("hint") or "",
                    "default": meta.get("default"),
                }
                if isinstance(meta.get("slider"), dict):
                    item["slider"] = meta["slider"]
                if isinstance(meta.get("options"), list):
                    item["options"] = meta["options"]
                    item["labels"] = meta.get("labels") or []
                if isinstance(meta.get("condition"), dict):
                    # 同级键 -> 期望值，前端据此隐藏不适用的项
                    item["condition"] = meta["condition"]
                fields[f"{section}.{node}"] = item

        for section in ("friend_settings", "custom_engine", "web_panel"):
            sec = data.get(section) or {}
            walk(sec.get("items") or {}, "", section)

        return {"fields": fields}

    # ------------------------------------------------------------------
    # 事件规则
    # ------------------------------------------------------------------
    def _rules_file(self) -> Path:
        return Path(self.plugin.data_dir) / "event_rules.json"

    def rules_get(self) -> dict:
        cfg_rules = self.engine_cfg.get("event_rules")
        source = "config" if isinstance(cfg_rules, list) and cfg_rules else "file"
        if source == "config":
            rules = copy.deepcopy(cfg_rules)
        else:
            path = self._rules_file()
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except Exception as e:
                raise PanelError("E_NOT_FOUND", f"读取规则文件失败: {e}")
            rules = data.get("rules") or []
            return {
                "source": "file",
                "path": str(path),
                "rules": rules,
                "away_keywords": data.get("away_keywords") or [],
                "return_keywords": data.get("return_keywords") or [],
                "need_reload": False,
            }
        return {
            "source": "config",
            "path": str(self._rules_file()),
            "rules": rules,
            "away_keywords": self.engine_cfg.get("away_keywords") or [],
            "return_keywords": self.engine_cfg.get("return_keywords") or [],
            "need_reload": True,
        }

    async def rules_save(self, payload: dict) -> dict:
        payload = payload or {}
        rules = payload.get("rules")
        if not isinstance(rules, list) or not rules:
            raise PanelError("E_BAD_REQUEST", "规则不能为空")
        if len(json.dumps(rules, ensure_ascii=False).encode("utf-8")) > MAX_RULES_BYTES:
            raise PanelError("E_BAD_REQUEST", "规则过大")

        for rule in rules:
            if not isinstance(rule, dict) or not str(rule.get("name") or "").strip():
                raise PanelError("E_BAD_REQUEST", "每条规则都需要一个内部标识")
            rule.setdefault("__template_key", "event_rule")

        current = self.rules_get()
        if current["source"] == "config":
            self.plugin.config["custom_engine"]["event_rules"] = rules
            await self.plugin.config.save_config_async()
            return {"saved": True, "source": "config", "need_reload": True}

        path = self._rules_file()
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        data["rules"] = rules
        if isinstance(payload.get("away_keywords"), list):
            data["away_keywords"] = payload["away_keywords"]
        if isinstance(payload.get("return_keywords"), list):
            data["return_keywords"] = payload["return_keywords"]
        path.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
        try:
            self.plugin.engine._load()
        except Exception:
            pass
        return {"saved": True, "source": "file", "need_reload": False}

    async def panel_restart(self, payload: dict) -> dict:
        """重启独立面板。

        不能在请求里同步 await restart：7627 通道下等于停掉正在服务自己的服务器，
        请求会直接 500 且新进程来不及起来。改成先回包、再后台重启。
        """
        payload = payload or {}
        host = str(payload.get("host") or "").strip() or None
        port = _as_int(payload.get("port"), 0) or None
        if self._restarting:
            raise PanelError("E_BUSY", "面板正在重启")

        changed = False
        wp = self.plugin.config.get("web_panel")
        if isinstance(wp, dict):
            if host and wp.get("host") != host:
                wp["host"] = host
                changed = True
            if port and wp.get("port") != port:
                wp["port"] = port
                changed = True
        if changed:
            try:
                saver = getattr(self.plugin.config, "save_config_async", None)
                if saver is not None:
                    await saver()
                else:
                    self.plugin.config.save_config()
            except Exception as e:
                logger.warning(f"{TAG} 保存面板地址失败: {e}")

        self._restarting = True

        async def _run() -> None:
            await asyncio.sleep(0.3)
            try:
                await self.plugin.restart_panel(host=host, port=port)
            except Exception as e:
                logger.warning(f"{TAG} 面板重启失败: {e}")
            finally:
                self._restarting = False

        asyncio.create_task(_run())
        return {"accepted": True, "host": host, "port": port, "poll_ms": 800}

    # ------------------------------------------------------------------
    # Web 覆盖的提示词（不进原生 schema）
    # ------------------------------------------------------------------
    def overrides_get(self) -> dict:
        return {
            "overrides": self.plugin.web_overrides_all(),
            "defaults": {
                "classify_sys_prompt": (
                    "判断用户这句话是否表示他【此刻正在做】或【几分钟内就要去做】某件事。\n"
                    "只从类别表里选一个，或者 none。拿不准就 none，不要猜。\n\n"
                    "类别表：\n"
                    "{{catalog}}\n\n规则：\n"
                    "- 将来的事（明天、等会、晚点、以后、待会）→ none\n"
                    "- 假设条件（如果、要是、打算但没动身）→ none\n"
                    "- 聊天、调侃、反问、回答提问、复述别人的话 → none\n"
                    "- 句子里有明确第三人（我妈、他、室友）在做事，主语不是用户本人 → none\n"
                    "- 先看整句意思再看词：双重否定（「不是不想洗澡」）按真实意思算；引号里引用别人的话、反问夸张（「谁要洗澡啊」）都不算\n"
                    "- 用户常用昵称指代自己时，主语按用户本人理解\n"
                    "- 语义相近归到已有类别（炒菜/做饭→cooking，开会→meeting，复习/上课→study）\n"
                    "- 宁可 none，不要硬塞：表里没有贴切的类别就 none\n"
                    "- 说的是「这件事」不等于「正在做」：只是聊到、提一嘴、商量（「要不」「你觉得」「找个时间」「改天」）→ none\n"
                    "- commute 只算通勤赶路（上下班路上、赶车、在车上）；出去玩、去公园、散步、遛弯都不算\n"
                    "- 只有明确做完才 done=true，且必须同时给出类别名\n\n"
                    "示例：\n"
                    '「我去洗澡了」→ {"event":"shower","done":false}\n'
                    '「宝宝去炒菜咯」→ {"event":"cooking","done":false}\n'
                    '「明天晚上才有课」→ {"event":"none","done":false}\n'
                    '「我妈去洗澡了」→ {"event":"none","done":false}\n'
                    '「会开完了」→ {"event":"meeting","done":true}\n'
                    '「（上文在炒菜）做好咯」→ {"event":"cooking","done":false}\n'
                    '「开饭了，一起吃吧」→ {"event":"eating","done":false}\n'
                    '「饭好了，快来吃」→ {"event":"eating","done":false}\n'
                    '「出公园去玩」→ {"event":"none","done":false}\n'
                    '「要不找个时间出去走走」→ {"event":"none","done":false}\n\n'
                    '只输出 JSON，不要解释：{"event":"类别名或none","done":false}'
                ),
                "reminder_hint": (
                    "现在 {{time}}，到了该提醒他睡觉的时间点。"
                    "催他一句该睡了，语气要像在意的黏人，别冷冰冰地下命令；"
                    "可以提一下时间晚了、明天还要早起之类的由头。"
                ),
                "appointment_hint": (
                    "现在 {{time}}，到 {{time_text}} 了。他之前说「{{raw}}」。"
                    "按这件事已经发生来接话——别问「{{label}}了吗」，"
                    "直接当成已经结束，说眼前的事。"
                ),
            },
        }

    def overrides_save(self, payload: dict) -> dict:
        payload = payload or {}
        patch = payload.get("overrides")
        if not isinstance(patch, dict):
            raise PanelError("E_BAD_REQUEST", "缺少 overrides")
        # 只允许写入已知的两个键，避免塞进任意东西
        allowed = {"classify_sys_prompt", "reminder_hint", "appointment_hint"}
        cleaned = {k: str(v) for k, v in patch.items() if k in allowed and isinstance(v, str)}
        self.plugin.set_web_overrides(cleaned)
        return {"saved": True, "overrides": self.plugin.web_overrides_all()}


class PanelPageApi:
    """把 PanelService 适配成 AstrBot 插件页 API（bridge 通道）。"""

    ROUTES = (
        ("status", "status", "GET"),
        ("tasks", "tasks", "GET"),
        ("decisions", "decisions", "GET"),
        ("config", "config_get", "GET"),
        ("config/save", "config_save", "POST"),
        ("reload", "reload", "POST"),
        ("reload/state", "reload_state", "GET"),
        ("preview", "preview", "POST"),
        ("send", "send", "POST"),
        ("fire", "fire", "POST"),
        ("reschedule", "reschedule", "POST"),
        ("tasks/action", "task_action", "POST"),
        ("probability", "probability", "GET"),
        ("probability/preview", "probability_preview", "POST"),
        ("schema", "schema_get", "GET"),
        ("debug/classify", "debug_classify", "POST"),
        ("rules", "rules_get", "GET"),
        ("rules/save", "rules_save", "POST"),
        ("overrides", "overrides_get", "GET"),
        ("overrides/save", "overrides_save", "POST"),
        ("panel/restart", "panel_restart", "POST"),
    )

    def __init__(self, plugin, service: PanelService) -> None:
        self.plugin = plugin
        self.service = service

    def register_routes(self) -> None:
        register = getattr(self.plugin.context, "register_web_api", None)
        if register is None:
            logger.warning(f"{TAG} 当前 AstrBot 不支持插件页 API，仅 7627 面板可用")
            return
        for name, method_name, http_method in self.ROUTES:
            register(
                f"{API_PREFIX}/{name}",
                self._make_handler(method_name, http_method),
                [http_method],
                f"主动私聊Mini 面板 · {name}",
            )
        logger.info(f"{TAG} 已注册插件页 API {len(self.ROUTES)} 条")

    def _make_handler(self, method_name: str, http_method: str):
        service = self.service

        async def handler(**_):
            from astrbot.api.web import request as req_proxy

            try:
                if http_method == "GET":
                    query = dict(req_proxy.query)
                    fn = getattr(service, method_name)
                    data = fn(query) if method_name in _QUERY_METHODS else fn()
                else:
                    payload = await req_proxy.json(default={}) or {}
                    if not isinstance(payload, dict):
                        raise PanelError("E_BAD_REQUEST", "请求体必须是对象")
                    data = getattr(service, method_name)(payload)
                    if asyncio.iscoroutine(data):
                        data = await data
                return {"status": "ok", "data": data}
            except PanelError as e:
                return {"status": "error", "message": f"{e.code}: {e.message}"}
            except Exception as e:
                logger.exception(f"{TAG} 面板接口 {method_name} 异常")
                return {"status": "error", "message": f"E_INTERNAL: {e}"}

        return handler
