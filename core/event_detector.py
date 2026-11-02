"""用户活动事件识别（关键词规则，不调用 LLM）。

作用有两个：
1. 供调度器判断"用户当前是不是在做某件会持续一阵的事"，从而调整触发概率；
2. 为提示词生成一段 event_hint，让她能顺着这件事往下说，而不是重新问"你在干嘛"。

规则表落在 plugin_data/event_rules.json，用户可直接编辑，插件升级不会覆盖。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.core.message.components import Plain

from . import text_rules as rules
from . import utterance

BUILTIN_RULES = {
    "version": 1,
    "default_ttl_minutes": 45,
    "negation_prefixes": ["不想", "不要", "不用", "不必", "没空", "没时间", "没心情", "没去成",
                          "还没", "没去", "待会", "晚点", "不打算", "不饿", "不去", "别去", "不去了",
                          "取消", "不开了", "不上了"],
    "away_keywords": ["去上课", "上课去", "去上班", "上班去", "出门", "走了", "我走", "去公司",
                      "去学校", "出去", "下楼", "去一趟", "先走了", "去开个会"],
    "return_keywords": ["到家", "回来了", "我回来", "进家门", "进门", "我到了", "已经到家"],
    "rules": [
        {
            "name": "eating",
            "label": "在吃饭",
            "ttl_minutes": 40,
            "boost": 3.0,
            "boost_budget": 1,
            "keywords": ["吃饭", "吃面", "吃了", "干饭", "外卖", "点餐", "下馆子", "食堂", "吃早饭", "吃午饭", "吃晚饭", "吃宵夜"],
            "stop_keywords": ["吃完了", "吃饱", "不吃了", "吃撑", "收碗", "洗碗", "吃完"],
            "hint": "他 {elapsed} 分钟前提过在吃饭，这个点差不多吃完了。可以直接说\"吃完了吧\"这类已经知道答案的话，或者催他收拾一下；不要问\"吃完了吗\"，也别自己补他吃的是什么、用什么碗筷——你不知道，编出来就出戏。",
        },
        {
            "name": "cooking",
            "label": "在做饭",
            "ttl_minutes": 40,
            "boost": 2.5,
            "boost_budget": 1,
            "keywords": ["做饭", "炒菜", "做菜", "下厨", "烧菜", "做晚饭", "做午饭", "做早饭", "煲汤", "炖汤", "切菜", "准备做饭", "开火"],
            "stop_keywords": ["做好了", "做完了", "炒好了", "出锅", "上桌", "开饭"],
            "hint": "他 {elapsed} 分钟前开始在做饭，这会儿差不多该好了。可以催他快一点、或者问一句什么时候能吃；不要问\"饭好了没\"，也别自己补菜名、锅里有什么、要不要尝一口——你看不到。",
        },
        {
            "name": "drinking",
            "label": "在喝东西",
            "ttl_minutes": 25,
            "boost": 2.0,
            "boost_budget": 1,
            "keywords": ["喝奶茶", "喝咖啡", "喝水", "喝饮料", "喝果汁", "喝酒", "喝汤", "喝茶", "泡茶", "点奶茶", "点咖啡"],
            "stop_keywords": ["喝完了", "喝饱", "不喝了", "喝不下", "收杯子", "洗杯子"],
            "hint": "他 {elapsed} 分钟前说在喝东西，这会儿应该正喝着呢。可以凑过去讨一口、让他分你点，或者吐槽他喝的是什么；不要问\"你在喝什么\"。",
        },
        {
            "name": "commute",
            "label": "在路上",
            "ttl_minutes": 50,
            "boost": 2.5,
            "boost_budget": 1,
            "keywords": ["出门", "在路上", "地铁", "公交", "打车", "开车", "高铁", "机场", "上班路", "回家路", "到楼下"],
            "stop_keywords": ["到了", "下车", "进公司", "到家了", "到公司"],
            "hint": "他 {elapsed} 分钟前在路上，这会儿该到了。用陈述句接，比如\"到楼下了吧\"，或者直接说\"上来\"；不要问\"你到哪儿了\"。",
        },
        {
            "name": "working",
            "label": "在忙",
            "ttl_minutes": 90,
            "boost": 1.8,
            "boost_budget": 1,
            "keywords": ["开会", "加班", "写代码", "改bug", "赶论文", "复习", "考试", "上课", "写作业", "赶进度"],
            "stop_keywords": ["开完了", "忙完了", "写完了", "下课", "考完了", "弄完了", "搞定了"],
            "hint": "他 {elapsed} 分钟前说在忙，这会儿可能刚松手。别催进度，先伸手——拽袖子、把水推过去，或者说一句\"忙完没\"；不要问\"你在忙吗\"。",
        },
        {
            "name": "shower",
            "label": "在洗澡",
            "ttl_minutes": 30,
            "boost": 3.5,
            "boost_budget": 1,
            "keywords": ["洗澡", "冲个凉", "冲凉", "洗头", "洗漱", "去洗"],
            "stop_keywords": ["洗完了", "出来了", "洗完"],
            "hint": "他 {elapsed} 分钟前说去洗澡，现在该出来了。可以拿这个打趣，或者直接说\"头发别滴水\"、\"擦干了吗\"。",
        },
        {
            "name": "gaming",
            "label": "在打游戏",
            "ttl_minutes": 60,
            "boost": 2.0,
            "boost_budget": 1,
            "keywords": ["打游戏", "开黑", "上分", "开一把", "排位", "在玩"],
            "stop_keywords": ["不玩了", "下机", "关电脑", "打完了", "打完"],
            "hint": "他 {elapsed} 分钟前在打游戏，这会儿可能还在局里。可以凑过去、按他手机、抱怨一句被冷落；不要问\"你在玩什么呢\"。",
        },
        {
            "name": "errand",
            "label": "在忙家里的事",
            "ttl_minutes": 45,
            "boost": 2.5,
            "boost_budget": 1,
            "keywords": ["洗衣服", "收拾", "打扫", "遛狗", "买菜", "取快递", "超市", "倒垃圾", "拖地"],
            "stop_keywords": ["弄完了", "回来了", "买好了", "收拾完"],
            "hint": "他 {elapsed} 分钟前在忙家里的事，差不多该收尾了。可以接着问结果，比如\"弄完了？\"，或者直接要他过来。",
        },
    ],
}

# 「阶段性结束」：下课 / 放学 / 下班 / 散会 / 收工 / 考完。
# 命中的意思是「这件事结束了，人该回到你身边了」，和「回到家」不是一回事：
# 到家要清掉正在做的事，而这个只解除「离开」，别把吃饭/洗澡这类事件一起抹掉。
PERIOD_END_DEFAULT = ("下课", "放学", "下班", "散会", "收工", "考完")
# 前面出现这些，说明「还没到点」，不是已经结束。
END_BLOCKERS = (
    "快", "马上", "立刻", "立即", "就快", "快要", "就要", "要", "该",
    "准备", "才", "还有", "还差", "差", "不到", "等", "等到",
    "再过", "一会儿", "差不多", "赶紧",
)
# 后面跟这些才算「已经结束」，否则分不清是结束了还是要结束。
END_DONE_MARKS = ("了", "啦", "咯", "喽", "哦", "呀", "噢", "噜")
# 前面有这些也能确认已经结束（「已经下班」「终于考完」）。
END_DONE_HEADS = ("已经", "终于", "总算", "可算")

# 吃的让出去了：「剩下的妃爱吃吧」「给你吃」「你先吃吧」。
# 他把自己那份递过来，等于他这边已经吃完了——事件必须收尾，
# 否则半小时后她还会拿着「他在吃饭」的提示去催他收拾碗筷。
# 全用结构判断，不认名字；中间夹「我」的不算（「剩下的我吃吧」是他自己要吃）。
HANDOFF_EVENTS = ("eating", "drinking")
HANDOFF_RE = re.compile(
    r"(?:剩下的?|剩的?|都给你|给你|让给你|留给你|喂你)[^，。！？,.!?;；\s我]{0,6}(?:吃|喝)"
    r"|(?:你|妳|您)[^，。！？,.!?;；\s我]{0,3}(?:吃|喝)吧"
)


def extract_message_text(event) -> str:
    """从 AstrBot 事件里提取纯文本。"""
    parts = []
    try:
        for comp in event.get_messages() or []:
            # 只认 Plain。Reply.text 是废弃字段，但现行适配器仍在往里填引用消息的原文，
            # 全收进来会把「被引用的那条」当成用户此刻说的话（「我到家了吗」式误判）。
            if not isinstance(comp, Plain):
                continue
            text = getattr(comp, "text", None)
            if text:
                parts.append(str(text))
    except Exception:
        pass
    if parts:
        return " ".join(parts).strip()
    try:
        return str(getattr(event, "message_str", "") or "").strip()
    except Exception:
        return ""


class EventRuleStore:
    """事件规则表的加载与匹配。"""

    def __init__(self, data_dir, engine_config=None) -> None:
        self.path = Path(data_dir) / "event_rules.json"
        self._engine = engine_config if isinstance(engine_config, dict) else {}
        self._rules: list[dict] = []
        self._negations: list[str] = []
        self._away_keywords: list[str] = []
        self._return_keywords: list[str] = []
        self._default_ttl = 45
        self._mtime = 0.0
        self._last_check = 0.0
        self._from_config = False
        self._ensure_file()
        self._load()

    def _ensure_file(self) -> None:
        try:
            if not self.path.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(
                    json.dumps(BUILTIN_RULES, ensure_ascii=False, indent=4),
                    encoding="utf-8",
                )
                logger.info(f"[Minichat] 已生成事件词表模板喵: {self.path}")
        except Exception as e:
            logger.warning(f"[Minichat] 生成事件词表失败喵: {e}")

    @staticmethod
    def _split_kw(value):
        """把配置里的“a,b,c”或列表统一成关键词列表，兼容中文逗号。"""
        if isinstance(value, list):
            return [str(x).strip() for x in value if str(x).strip()]
        raw = str(value or "").replace("，", ",")
        return [x.strip() for x in raw.split(",") if x.strip()]

    @staticmethod
    def _builtin_by_name(name):
        for rule in BUILTIN_RULES.get("rules") or []:
            if rule.get("name") == name:
                return rule
        return {}

    @staticmethod
    def _as_float(value, default):
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _normalize_rule(self, rule: dict) -> dict:
        """把界面配置里的一条规则补齐成内部结构。

        boost / boost_budget 优先用规则里写的；没写就按 name 从内置规则回填，
        这样已存在的配置文件（7 条规则没有这两个字段）无需手工编辑也能拿到
        差异化加成，而不是所有事件都被拉平成同一个倍数。
        """
        name = rule.get("name")
        builtin = self._builtin_by_name(name)

        boost = rule.get("boost")
        if boost is None:
            boost = builtin.get("boost", 3.0)
        boost = max(0.01, self._as_float(boost, 3.0))

        budget = rule.get("boost_budget")
        if budget is None:
            budget = builtin.get("boost_budget", 1)
        try:
            budget = max(0, int(budget))
        except (TypeError, ValueError):
            budget = 1

        return {
            "name": name,
            "label": rule.get("label") or name,
            "ttl_minutes": int(rule.get("ttl_minutes") or self._default_ttl),
            "boost": boost,
            "boost_budget": budget,
            "keywords": self._split_kw(rule.get("keywords")),
            "stop_keywords": self._split_kw(rule.get("stop_keywords")),
            "hint": rule.get("hint") or "",
        }

    def _load(self) -> None:
        cfg_rules = self._engine.get("event_rules") or []
        if isinstance(cfg_rules, list) and cfg_rules:
            self._from_config = True
            self._rules = [self._normalize_rule(r) for r in cfg_rules
                           if isinstance(r, dict) and r.get("name")]
            cfg_away = self._split_kw(self._engine.get("away_keywords"))
            cfg_ret = self._split_kw(self._engine.get("return_keywords"))
            self._away_keywords = cfg_away
            self._return_keywords = cfg_ret
            negs = self._engine.get("negation_prefixes")
            self._negations = (list(BUILTIN_RULES["negation_prefixes"])
                               if not isinstance(negs, list) or not negs else [str(x) for x in negs])
            logger.info(
                f"[Minichat] 事件规则来自界面配置，共 {len(self._rules)} 类喵。"
            )
            return
        self._from_config = False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            rules = data.get("rules") or []
            self._default_ttl = int(data.get("default_ttl_minutes") or 45)
            # 文件模式和界面配置走同一套归一化：keywords 写成字符串时按逗号拆，
            # 而不是被逐字符遍历（那会让单个汉字都能命中）。
            self._rules = [
                self._normalize_rule(r) for r in rules
                if isinstance(r, dict) and r.get("name")
            ]
            # UI 里配了否定词就以 UI 为准（用户清空 event_rules 后落到文件模式时，
            # 面板上改的否定词原来会被静默忽略）
            cfg_negs = self._split_kw(self._engine.get("negation_prefixes"))
            self._negations = cfg_negs or [
                str(p) for p in (data.get("negation_prefixes") or []) if p
            ]
            self._away_keywords = [str(k) for k in (data.get("away_keywords") or []) if k]
            self._return_keywords = [str(k) for k in (data.get("return_keywords") or []) if k]
            self._mtime = self.path.stat().st_mtime
            logger.info(f"[Minichat] 事件词表已加载，共 {len(self._rules)} 类喵。")
        except Exception as e:
            logger.warning(f"[Minichat] 读取事件词表失败，退回内置规则喵: {e}")
            self._rules = [self._normalize_rule(r) for r in BUILTIN_RULES["rules"]
                           if isinstance(r, dict) and r.get("name")]
            self._negations = list(BUILTIN_RULES["negation_prefixes"])
            self._away_keywords = list(BUILTIN_RULES["away_keywords"])
            self._return_keywords = list(BUILTIN_RULES["return_keywords"])
            self._default_ttl = BUILTIN_RULES["default_ttl_minutes"]
            # 记下当前 mtime：文件坏着的时候别每 60 秒重试一次解析
            try:
                self._mtime = self.path.stat().st_mtime
            except OSError:
                self._mtime = 0

    def reload_if_changed(self) -> bool:
        """按 mtime 热加载，60 秒内最多检查一次。

        规则来自界面配置时无需热加载文件（改了配置得重载插件），直接跳过。
        """
        if self._from_config:
            return False
        now = time.time()
        if now - self._last_check < 60:
            return False
        self._last_check = now
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        if mtime != self._mtime:
            self._load()
            return True
        return False

    def _is_negated(self, text: str, idx: int) -> bool:
        # 命中词前 4 个字里出现否定词，就当成否定；回看**不跨分句**——
        # 「不困，再看会手机」里的「不」属于上一句，不该把「再看会手机」也否定掉。
        return rules.negated_by_clause(text, idx, self._negations, window=4)

    # 将来时/过去时/疑问/人称的判定统一在 core/text_rules.py，这里只做转发，
    # 保证事件、在场、睡眠三条链路口径一致。
    FUTURE_MARKERS = rules.FUTURE_MARKERS
    FUTURE_RE = rules.FUTURE_RE
    CLAUSE_BOUNDARY = rules.CLAUSE_BOUNDARY
    # 往回最多看这么多字，避免长句里串到很远的分句去。
    FUTURE_WINDOW = 14
    # 「明天，我去吃饭」：时间状语被逗号隔开时，并上看一眼紧邻的短分句（≤4 字）。
    FUTURE_PREV_MAX = 4

    def _future_head(self, text: str, idx: int) -> str:
        """取命中词前面、同一分句内的那一段文字（可并入紧邻的短时间状语分句）。"""
        return rules.clause_head(
            text, idx, window=self.FUTURE_WINDOW, prev_max=self.FUTURE_PREV_MAX
        )

    def _is_future(self, text: str, idx: int) -> bool:
        return rules.is_future_clause(
            text, idx, window=self.FUTURE_WINDOW, prev_max=self.FUTURE_PREV_MAX
        )

    def _is_past(self, text: str, idx: int, kw_len: int = 0) -> bool:
        """是不是在说已经发生的事（刚才吃了饭 / 上周看完电影 / 洗漱完了）。"""
        return rules.is_past_clause(
            text, idx, kw_len=kw_len,
            window=self.FUTURE_WINDOW, prev_max=self.FUTURE_PREV_MAX,
        )

    def _is_question(self, text: str, idx: int, kw_len: int) -> bool:
        """是不是在问（吃了吗 / 在开会吗）。"""
        return rules.is_question_clause(text, idx, kw_len=kw_len, window=self.FUTURE_WINDOW)

    def _is_third_person(self, text: str, idx: int) -> bool:
        """做事的是不是别人（我妈在做饭 / 他说他在洗澡）。"""
        return rules.is_third_person_subject(text, idx)

    def _is_reported(self, text: str, idx: int) -> bool:
        return rules.is_reported_clause(text, idx)

    def is_future_text(self, text: str) -> bool:
        """整句是否在说「以后/明天」的事（供语义识别前置过滤用）。"""
        if not text:
            return False
        if any(marker in text for marker in self.FUTURE_MARKERS):
            return True
        return bool(self.FUTURE_RE.search(text))

    def is_far_past_text(self, text: str) -> bool:
        """整句是否在说以前的事（「昨天一起吃了饭」），供在场状态判断用。"""
        if not text:
            return False
        return any(marker in text for marker in rules.PAST_FAR)

    def _accept(self, text: str, idx: int, kw: str, kind: str = "event") -> bool:
        """这次命中能不能当成「用户此刻正在做」。"""
        return self._evaluate(text, idx, kw, kind)["ok"]

    # ---------------------------------------------------------------- 谓语闸门
    # 六道过滤判断的是「时态 / 语气 / 谁在做」，但都没回答最后那个问题：
    # 这个词是在当谓语用，还是只是被提到（「讨厌上课」「睡觉时间被占了」）？
    # 这一步交给 core/utterance.py，三档：accept / reject / gray。
    def _judge_enabled(self) -> bool:
        return bool(self._engine.get("enable_utterance_judge", True))

    def _judge_strict(self) -> bool:
        return bool(self._engine.get("judge_strict", True))

    def _nicknames(self) -> tuple[str, ...]:
        raw = self._engine.get("bot_nicknames") or ""
        if isinstance(raw, (list, tuple)):
            parts = [str(x).strip() for x in raw]
        else:
            parts = [p.strip() for p in str(raw).replace("，", ",").split(",")]
        return tuple(p for p in parts if p)

    def _gate(self, text: str, idx: int, kw: str, kind: str) -> dict:
        """关掉开关时一律放行，等于回到 v0.0.7 的判定行为。"""
        if not self._judge_enabled():
            return {"verdict": utterance.VERDICT_ACCEPT, "rule": "", "reason": "闸门已关闭"}
        return utterance.judge_detail(text, idx, kw, kind=kind, nicknames=self._nicknames())

    @staticmethod
    def _filtered() -> dict:
        return {"ok": False, "verdict": "filtered", "rule": "", "reason": "前置过滤"}

    def _evaluate(self, text: str, idx: int, kw: str, kind: str = "event") -> dict:
        """前置过滤 + 闸门，返回 {"ok", "verdict", "rule", "reason"}。

        前置过滤保持各链路原有口径：在场状态用「明确过去」（刚才出门了人还在外面），
        事件用「刚才也算过去」，在场状态也不过滤第三人称。
        """
        if self._is_reported(text, idx):
            return self._filtered()
        if self._is_negated(text, idx):
            return self._filtered()
        # 「取消去开会」「开会取消了」：否定词离得远，4 字窗口照不到，单独判
        if rules.is_cancelled_clause(text, idx, kw_len=len(kw)):
            return self._filtered()
        if self._is_future(text, idx):
            return self._filtered()
        # 「如果我去洗澡」「要是明天下雨」：假设句里的动作不算发生
        if rules.is_conditional_clause(text, idx):
            return self._filtered()
        if kind == "away":
            # 「出差结束」「上班完了」：这是在说这件事收尾，不是现在要出门
            tail = text[idx + len(kw): rules.clause_end(text, idx + len(kw))]
            if tail.startswith(("结束", "完了", "完", "回来了")):
                return self._filtered()
        if kind in ("away", "return", "end"):
            if rules.is_far_past_clause(
                text, idx, kw_len=len(kw),
                window=self.FUTURE_WINDOW, prev_max=self.FUTURE_PREV_MAX,
            ):
                return self._filtered()
        elif self._is_past(text, idx, len(kw)):
            return self._filtered()
        if self._is_question(text, idx, len(kw)):
            return self._filtered()
        # 以前这里给 away/return 开了豁免，结果「我妈出门了」会标成本人离开、
        # 「我妈到家了」会清掉本人的 away。_scan_list 会继续试下一个候选，
        # 「我妈出门了，我也出门了」仍能命中后一个。
        if self._is_third_person(text, idx):
            return self._filtered()

        d = self._gate(text, idx, kw, kind)
        verdict = d["verdict"]
        if verdict == utterance.VERDICT_ACCEPT:
            ok = True
        elif verdict == utterance.VERDICT_GRAY:
            # 「宁可漏，不要错」：灰区默认不放行；但结束事件例外——
            # 事件一直挂着比误结束更糟，所以 stop 的灰区按放行处理。
            # 阶段结束同理：E1/E2 已经挡掉「快/还有/才」这类没到点的说法，
            # 能走到灰区的基本是「刚下课」这种完成态，放行才符合「解除离开」的意图。
            ok = (not self._judge_strict()) or kind in ("stop", "end")
        else:
            ok = False
        return {"ok": ok, "verdict": verdict, "rule": d.get("rule", ""), "reason": d.get("reason", "")}

    @staticmethod
    def _candidates(text: str, keywords):
        for kw in keywords or []:
            if not kw:
                continue
            idx = text.find(kw)
            while idx != -1:
                yield kw, idx
                idx = text.find(kw, idx + 1)

    @staticmethod
    def _drop(kind: str, kw: str, idx: int, ev: dict, name: str = "") -> dict:
        return {
            "kind": kind, "kw": kw, "idx": idx, "name": name,
            "verdict": ev["verdict"], "rule": ev["rule"], "reason": ev["reason"],
        }

    def match_detail(self, text: str) -> dict:
        """返回 ``{"rule", "kw", "idx", "verdict", "gate_rule", "reason", "dropped"}``。

        取「命中关键词最长」的那条：洗衣服(3) 优先于 去洗(2)，
        避免「去洗衣服」被认成在洗澡。同长度时按规则顺序。
        """
        if not text:
            return {"rule": None, "dropped": []}
        best = None  # (长度, -顺序, rule)
        detail = None
        dropped: list[dict] = []
        for order, rule in enumerate(self._rules):
            for kw, idx in self._candidates(text, rule.get("keywords") or []):
                ev = self._evaluate(text, idx, kw, "event")
                if ev["ok"]:
                    cand = (len(kw), -order, rule)
                    if best is None or cand > best:
                        best = cand
                        detail = {
                            "rule": rule, "kw": kw, "idx": idx,
                            "verdict": ev["verdict"], "gate_rule": ev["rule"],
                            "reason": ev["reason"],
                        }
                elif ev["verdict"] in ("reject", "gray"):
                    dropped.append(self._drop("event", kw, idx, ev, rule.get("name") or ""))
        if detail is None:
            return {"rule": None, "dropped": dropped}
        detail["dropped"] = dropped
        return detail

    def match(self, text: str):
        """返回命中的规则；未命中返回 None。"""
        return self.match_detail(text)["rule"]

    def _scan_list(self, text: str, keywords, kind: str):
        """在一组关键词里找第一个「此刻成立」的命中，返回 (hit, dropped)。"""
        dropped: list[dict] = []
        if not text:
            return None, dropped
        for kw, idx in self._candidates(text, keywords):
            ev = self._evaluate(text, idx, kw, kind)
            if ev["ok"]:
                return {
                    "kw": kw, "idx": idx, "verdict": ev["verdict"],
                    "gate_rule": ev["rule"], "reason": ev["reason"],
                }, dropped
            if ev["verdict"] in ("reject", "gray"):
                dropped.append(self._drop(kind, kw, idx, ev))
        return None, dropped

    def _match_list(self, text: str, keywords, kind: str = "away"):
        hit, _ = self._scan_list(text, keywords, kind)
        return hit["kw"] if hit else None

    def match_away_ex(self, text: str) -> dict:
        """判断这句话是否表示“我要离开，不在你身边了”。"""
        self.reload_if_changed()
        hit, dropped = self._scan_list(text, self._away_keywords, "away")
        return {"hit": hit, "dropped": dropped}

    def match_away(self, text: str):
        hit = self.match_away_ex(text)["hit"]
        return hit["kw"] if hit else None

    def match_return_ex(self, text: str) -> dict:
        """判断这句话是否表示“我回来了，回到你身边了”。"""
        self.reload_if_changed()
        hit, dropped = self._scan_list(text, self._return_keywords, "return")
        return {"hit": hit, "dropped": dropped}

    def match_return(self, text: str):
        hit = self.match_return_ex(text)["hit"]
        return hit["kw"] if hit else None

    # ---------------------------------------------------------------- 阶段结束
    def period_end_keywords(self) -> list[str]:
        """阶段结束词表。缺键用内置默认；显式配成空列表 = 关闭这个功能。"""
        raw = self._engine.get("period_end_keywords")
        if raw is None:
            return list(PERIOD_END_DEFAULT)
        return self._split_kw(raw)

    @staticmethod
    def _end_shape_reject(text: str, idx: int, kw: str):
        """两道形状检查：这句话像不像「已经结束了」。返回 (rule, reason) 或 None。"""
        head = rules.clause_head(text, idx, window=14)
        blocked = next((w for w in END_BLOCKERS if w in head), None)
        if blocked:
            return "E1", f"前面是「{blocked}」，说明还没到点"
        tail_start = idx + len(kw)
        tail = text[tail_start:rules.clause_end(text, tail_start)]
        if not tail.strip():
            return None                                  # 「下课」单独成句
        if tail[:1] in END_DONE_MARKS:
            return None                                  # 「下课了」「收工啦」
        if any(h in head for h in END_DONE_HEADS):
            return None                                  # 「已经下班」「终于考完」
        return "E2", "没有「了」，分不出是已经结束还是还没到"

    def match_period_end_ex(self, text: str) -> dict:
        """判断这句话是不是在说「某件事已经结束了」。

        只作用于「解除离开」这一条链路，所以不复用 _scan_list：
        这里要先过 E1/E2，否则「快下课」「50 分才下课」都会被放行。
        """
        self.reload_if_changed()
        dropped: list[dict] = []
        if not text:
            return {"hit": None, "dropped": dropped}
        for kw, idx in self._candidates(text, self.period_end_keywords()):
            shape = self._end_shape_reject(text, idx, kw)
            if shape:
                rule, reason = shape
                dropped.append({
                    "kind": "end", "kw": kw, "idx": idx,
                    "verdict": "reject", "rule": rule, "reason": reason,
                })
                continue
            ev = self._evaluate(text, idx, kw, "end")
            if ev["ok"]:
                return {
                    "hit": {
                        "kw": kw, "idx": idx, "verdict": ev["verdict"],
                        "gate_rule": ev["rule"], "reason": "阶段结束，解除「离开」",
                    },
                    "dropped": dropped,
                }
            if ev["verdict"] in ("reject", "gray"):
                dropped.append(self._drop("end", kw, idx, ev))
        return {"hit": None, "dropped": dropped}

    def stop_detail(self, event: dict, text: str) -> dict:
        """文本是否表示当前事件已结束（带闸门，挡住「吃完了的时候」这类名词化）。"""
        if not isinstance(event, dict) or not text:
            return {"stopped": False, "dropped": []}
        name = event.get("name")
        dropped: list[dict] = []
        if name in HANDOFF_EVENTS and self._is_handoff(text):
            return {
                "stopped": True, "kw": "让给你吃", "idx": 0,
                "verdict": "accept", "gate_rule": "H1",
                "reason": "把吃的让出去了，他自己那份已经吃完", "dropped": dropped,
            }
        for rule in self._rules:
            if rule.get("name") != name:
                continue
            for kw, idx in self._candidates(text, rule.get("stop_keywords") or []):
                if self._is_negated(text, idx):
                    continue
                # 结束词同样要看时态/是否取消：「明天开饭再说」「开饭取消了」
                # 不是「这顿饭现在结束了」——以前这里只过滤否定，会把做饭直接结束掉。
                if self._is_future(text, idx) or rules.is_cancelled_clause(text, idx, len(kw)):
                    continue
                # 结束词本身是完成体，不能用 is_past_clause（会把「吃完了」全挡掉），
                # 只挡明确过去；其余按分句判别人称/疑问/转述/假设。
                if (rules.is_third_person_subject(text, idx)
                        or rules.is_question_clause(text, idx, len(kw))
                        or rules.is_reported_clause(text, idx)
                        or rules.is_conditional_clause(text, idx)
                        or rules.is_far_past_clause(text, idx, kw_len=len(kw))):
                    continue
                d = self._gate(text, idx, kw, "stop")
                if d["verdict"] == utterance.VERDICT_REJECT:
                    dropped.append({
                        "kind": "stop", "kw": kw, "idx": idx, "verdict": d["verdict"],
                        "rule": d.get("rule", ""), "reason": d.get("reason", ""),
                    })
                    continue
                return {
                    "stopped": True, "kw": kw, "idx": idx,
                    "verdict": d["verdict"], "gate_rule": d.get("rule", ""),
                    "reason": d.get("reason", ""), "dropped": dropped,
                }
            return {"stopped": False, "dropped": dropped}
        return {"stopped": False, "dropped": dropped}

    def is_stop(self, event: dict, text: str) -> bool:
        return self.stop_detail(event, text)["stopped"]

    @staticmethod
    def _is_handoff(text: str) -> bool:
        """这句话是不是把吃的让给对方了（「剩下的妃爱吃吧」）。"""
        return bool(text and HANDOFF_RE.search(text))

    def build_hint(self, event: dict, elapsed_minutes: int) -> str:
        """渲染事件提示文本。

        默认只陈述事实（他说了什么、过了多久），不给出“该不该做完了”的结论，
        判断交给模型按常识推理。规则表若显式提供 hint 则优先使用，
        支持 {elapsed} 与 {label} 两个占位符。
        """
        if not isinstance(event, dict):
            return ""
        elapsed = str(max(1, int(elapsed_minutes)))
        label = str(event.get("label") or event.get("name") or "")
        # 容错：若在中文描述里填了多个别名（顿号/逗号分隔），只取第一个，
        # 否则拼进句子会变成“提过在吃饭、吃东西”这种不通顺的表达。
        for sep in ("、", "，", ",", "/", "|"):
            if sep in label:
                head = label.split(sep)[0].strip()
                if head:
                    label = head
                break
        template = str(event.get("hint") or "")
        if template:
            return template.replace("{elapsed}", elapsed).replace("{label}", label)
        return f"他 {elapsed} 分钟前提过{label}。"

    def rules_snapshot(self):
        """返回当前生效的规则列表（供语义识别使用）。"""
        self.reload_if_changed()
        return list(self._rules)

    def get_rule(self, name: str):
        """按标识取规则。"""
        self.reload_if_changed()
        for rule in self._rules:
            if rule.get("name") == name:
                return rule
        return None

    def ttl_for(self, rule: dict) -> int:
        try:
            return int(rule.get("ttl_minutes") or self._default_ttl)
        except (TypeError, ValueError):
            return self._default_ttl
