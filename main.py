"""主动私聊Mini —— 轻量主动消息插件。

只做私聊：加权触发 + 事件感知 + 在场状态 + 智能分段发送。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import astrbot.api.star as star
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.config.astrbot_config import AstrBotConfig

from .core.event_detector import EventRuleStore, extract_message_text
from .core.migrations import migrate_config
from .core.panel_api import PanelPageApi, PanelService
from .core.runner import Runner
from .core.store import Store
from .core.web_panel import PanelHttpServer

TAG = "[Minichat]"
VERSION = "v0.2.19"


class ProactiveMiniPlugin(star.Star):
    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.config = config

        self.data_dir = star.StarTools.get_data_dir("astrbot_plugin_mitchell_minichat")
        self.store = Store(self.data_dir)

        engine_cfg = {}
        try:
            engine_cfg = self.config.get("custom_engine") or {}
        except Exception:
            engine_cfg = {}
        self.engine_cfg = engine_cfg if isinstance(engine_cfg, dict) else {}

        try:
            self.engine = EventRuleStore(self.data_dir, self.engine_cfg)
        except Exception as e:
            self.engine = None
            logger.error(f"{TAG} 事件规则初始化失败: {e}")

        self.runner = None
        # 后台任务的强引用：asyncio 只保留弱引用，长时间 LLM 调用可能被 GC 掉
        self._bg_tasks: set = set()

        self.plugin_dir = Path(__file__).resolve().parent
        self.version = VERSION

        # Web 面板专属的提示词覆盖（不进 AstrBot 原生配置页，只在 Web 端编辑）
        self._web_overrides_path = Path(self.data_dir) / "web_overrides.json"
        self._web_overrides: dict = {}
        self._load_web_overrides()

        # 面板：先建业务层，再注册官方页面 API（低版本 AstrBot 会自行跳过）
        self.panel_service = None
        self.panel_page_api = None
        self.panel_http = None
        try:
            self.panel_service = PanelService(self)
            self.panel_page_api = PanelPageApi(self, self.panel_service)
            self.panel_page_api.register_routes()
        except Exception as e:
            logger.warning(f"{TAG} 面板 API 初始化失败: {e}")

        if self.panel_service is not None:
            try:
                self.panel_http = PanelHttpServer(self, self.panel_service)
            except Exception as e:
                logger.warning(f"{TAG} 独立面板初始化失败: {e}")

    @property
    def friend_cfg(self) -> dict:
        try:
            cfg = self.config.get("friend_settings") or {}
        except Exception:
            cfg = {}
        return cfg if isinstance(cfg, dict) else {}

    # ------------------------------------------------------------------
    # Web 覆盖的提示词（不进原生 schema，仅 Web 面板编辑）
    # ------------------------------------------------------------------
    def _load_web_overrides(self) -> None:
        try:
            if self._web_overrides_path.exists():
                raw = self._web_overrides_path.read_text(encoding="utf-8-sig")
                data = json.loads(raw) if raw.strip() else {}
                if isinstance(data, dict):
                    self._web_overrides = data
        except Exception as e:
            logger.warning(f"{TAG} 读取 web_overrides 失败: {e}")

    def web_override(self, key: str):
        """取某块提示词的 Web 覆盖值；未设置返回 None（用内置默认）。"""
        val = self._web_overrides.get(key)
        return val if isinstance(val, str) and val.strip() else None

    def web_overrides_all(self) -> dict:
        return dict(self._web_overrides)

    def set_web_overrides(self, patch: dict) -> None:
        for key, val in (patch or {}).items():
            if isinstance(val, str):
                self._web_overrides[key] = val
        try:
            self._web_overrides_path.parent.mkdir(parents=True, exist_ok=True)
            self._web_overrides_path.write_text(
                json.dumps(self._web_overrides, ensure_ascii=False, indent=4),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"{TAG} 保存 web_overrides 失败: {e}")

    async def initialize(self) -> None:
        # 面板先起：即使插件被关闭，也能打开面板把配置改回来
        await self._start_panel()
        if self.engine is None:
            logger.error(f"{TAG} 事件规则不可用，插件不启动")
            return
        if not self.friend_cfg.get("enable", False):
            logger.info(f"{TAG} 插件未启用")
            return
        # 老配置升级：只把仍是出厂默认的项换成新默认，用户改过的原样保留
        try:
            changed = migrate_config(self.config)
            if changed:
                saver = getattr(self.config, "save_config_async", None)
                if saver is not None:
                    await saver()
                else:
                    self.config.save_config()
                logger.info(f"{TAG} 已升级出厂默认配置：{'、'.join(changed)}")
        except Exception as e:
            logger.warning(f"{TAG} 配置升级失败（不影响启动）: {e}")
        if not self.runner:
            self.runner = Runner(self)
        await self.runner.start()

    async def terminate(self) -> None:
        # 消息入口把语义识别任务放在插件自己的 _bg_tasks 里，
        # runner.stop() 只回收 runner._bg_tasks；不在这里取消，
        # 卸载/重载后旧任务仍会跑完并写回旧 Store（覆盖新实例的数据）。
        await self._cancel_bg_tasks()
        if self.runner:
            await self.runner.stop()
            self.runner = None
        await self._stop_panel()

    async def _cancel_bg_tasks(self) -> None:
        """取消并等待插件入口创建的后台识别任务。"""
        tasks = [task for task in list(self._bg_tasks) if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._bg_tasks.difference_update(tasks)

    # ------------------------------------------------------------------
    # 面板
    # ------------------------------------------------------------------
    async def _start_panel(self) -> None:
        if self.panel_http is None:
            return
        try:
            await self.panel_http.start()
        except Exception as e:
            logger.error(f"{TAG} 面板启动异常: {e}")

    async def _stop_panel(self) -> None:
        if self.panel_http is None:
            return
        try:
            await self.panel_http.stop()
        except Exception as e:
            logger.debug(f"{TAG} 面板关闭异常: {e}")

    def panel_status(self) -> dict:
        if self.panel_http is None:
            return {
                "enabled": False, "running": False, "host": "", "port": 0,
                "url": "", "has_password": False, "allow_embed": False,
                "error": "面板未初始化", "insecure": False,
            }
        return self.panel_http.status()

    async def restart_panel(self, host: str | None = None, port: int | None = None) -> dict:
        if self.panel_http is None:
            return {"running": False, "error": "面板未初始化"}
        return await self.panel_http.restart(host=host, port=port)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=100)
    async def on_private_message(self, event: AstrMessageEvent):
        """记录消息、更新事件状态、重排下一次主动消息。"""
        if self.runner is None:
            return
        umo = event.unified_msg_origin
        if umo not in self.runner.sessions():
            return

        text = extract_message_text(event)

        self.store.touch_message(umo)
        self.store.reset_unanswered(umo)

        pending = None
        try:
            self.runner.update_from_message(umo, text)
            # 与 update 同步取走，保留原会话、消息序号及未消费片段。
            pending = self.runner.take_llm_candidate(umo)
        except Exception as e:
            logger.warning(f"{TAG} 事件识别异常: {e}")

        self.store.save()
        # 用户说话了，重新计时
        self.runner.reschedule_after_message(umo)

        if pending:
            try:
                task = asyncio.create_task(
                    self.runner.classify_with_llm(
                        umo, pending["text"], pending["turn_seq"],
                        **({"gray_fallback": pending["gray_fallback"]}
                           if pending.get("gray_fallback") else {}),
                    )
                )
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)
            except Exception as e:
                logger.debug(f"{TAG} 语义识别任务创建失败: {e}")
        else:
            # 关键词命中但被判灰区的句子，才是真的「语义层能帮上忙」的场合。
            # 关键词明确否决（reject）的不再问模型——那正是省 token 的地方。
            gray = self.runner.take_gray_review()
            if gray:
                try:
                    task = asyncio.create_task(self.runner.review_gray(umo, gray))
                    self._bg_tasks.add(task)
                    task.add_done_callback(self._bg_tasks.discard)
                except Exception as e:
                    logger.debug(f"{TAG} 灰区复核任务创建失败: {e}")

        # 约定灰度与上面两条并列：模糊句（「快十二点下课」「还有十分钟」）才走到这里
        appt_gray = self.runner.take_appointment_review()
        if appt_gray:
            try:
                task = asyncio.create_task(
                    self.runner.review_appointment_gray(umo, appt_gray)
                )
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)
            except Exception as e:
                logger.debug(f"{TAG} 约定灰度任务创建失败: {e}")

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        """她（AstrBot）刚把回复发出去：把下一次主动开口往后挪。

        不这么做的话，用户说完话→她回完（常常以表情收尾）→ 一分钟后插件又发
        一条，看起来像她在自问自答。这里只在她自己就是那条消息的发送方时生效，
        插件自己发的主动消息走 context.send_message，不会触发这个钩子。
        """
        if self.runner is None:
            return
        try:
            self.runner.note_bot_reply(event.unified_msg_origin)
        except Exception as e:
            logger.debug(f"{TAG} 回复后重排失败: {e}")
