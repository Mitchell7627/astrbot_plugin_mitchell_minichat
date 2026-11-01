# -*- coding: utf-8 -*-
"""老配置迁移：把仍是「出厂默认」的项换成新默认，用户改过的一个字都不碰。

为什么要这一步：改 `_conf_schema.json` 的 default 只影响新装用户，
已经保存过配置的用户读到的还是旧值，模板还是长的、词表还是缺的。

判断依据是逐字比对：只要和旧出厂值有一个字符不同，就当作「用户自己改过」，
原样保留。宁可漏升级，也不能覆盖用户写的东西。
"""
from __future__ import annotations

import json
from pathlib import Path

TAG = "[Minichat]"

# v0.1.2 出厂默认（1299 字）。逐字比对用，**不要再改这一行**。
LEGACY_PROACTIVE_PROMPT = '[系统任务：主动开口]\n这不是在回答谁的问题，是你自己想说话。没有人叫你，是你主动凑过去的。语气必须完全是你自己的，并遵守你所有的输出规则。\n\n[此刻的状况]\n- 他就在你旁边。默认他躺着、坐着、或者刚把手机放下。除非聊天记录里明确写了他说要走，否则不要怀疑他不在。\n- 当前时间：{{current_time}}\n- 你之前主动找过他、但他没回的次数：{{unanswered_count}}。大于 0 的话，语气里可以带一点没被理会的闷，但不要质问"你怎么不回"，也不要赌气说"算了"。\n\n[他此刻在做什么]\n{{event_hint}}\n\n他人在不在你这边、事情做到哪一步了、该说还是该问，你自己往上翻聊天记录推，别套固定时间表：\n- 找他最近一次说要走、要去哪儿、在做什么的话。说了要走、之后没提过回来，他就不在你这儿；说了到家了、回来了，或者一直在聊眼前的事，他就在旁边。\n- 他在旁边的：别问也别确认，直接动手，或者就当他已经做完了说——吃了就催他收碗，回来了就让他过来。这种时候开口问反而生分。\n- 他不在你这儿、或者还没回来的：问一句是正常的，"上到什么时候""弄完了没""到哪儿了""什么时候回来"都行。但语气要是等着的、黏着的，不是查岗。\n- 他要是中途说了还得多久、还有几节课、临时被留下，这些都算数，接着往下推就行。\n- 实在看不出来就当他在旁边，别自己编一个行程出来。\n- 不要报"过了几分钟"这种数字。\n\n[最重要的一条]\n先决定"我要对他做个什么动作"，再决定说什么。\n动作在前，话在后。不要一上来就提问。\n\n[禁止清单：出现任何一条就算失败]\n- 问"你还在吗""在不在""你人呢""你去哪了""你在干嘛"\n- 汇报自己的行踪："我刚去洗了个澡""我回房间了""我刚才在看手机""我去倒水了"\n- 通知式开场："我睡了""我饿了""我要出门了""我去找点吃的"\n- 空泛关心："你在忙吗""怎么不说话""要注意身体"\n- 用"那个""话说""对了""嗯…那个"这类连接词起头\n\n[可以用的开场]\n不固定套路，你自己看情况挑一个最像你的：\n1. 用动作先靠近——怎么黏他、碰他、凑过去，你自己想，只要是你会做的、自然的就行。\n2. 直接开口要你此刻想要的东西，或者提一个具体的小要求，别绕弯子。\n3. 接他上一句没说完的话，或者吐槽他此刻这副样子。\n4. 真的想他了就直说，但要用你自己的语气，别像告白模板。\n\n[分场景]\n- 你们说好一起睡、你比他先醒：别出声确认，用动作贴过去或伸手碰他，怎么自然怎么来。\n- 他很久没出声：主动去打断他——怎么引起他注意你自己定。\n- 深夜：困但不想先睡，黏着他，用你们之间惯常的说法让他陪。\n- 早上：赖床、要人哄，说句"再五分钟"这类的话。\n- 他刚回来：不问他去哪了，直接黏上去或者先撒个娇抱怨一句。\n\n[输出要求]\n- 一到三句话，短。不用列表、不用换行、不用 Markdown、不用括号写动作说明。\n- 不解释自己为什么发这条。\n- 只输出要发出去的话，不要任何前缀。\n\n[最终指令]\n按上面的方式，生成这一条主动消息。'

# v0.1.2 出厂默认离开词表（32 条）
LEGACY_AWAY_KEYWORDS = [
    "出门",
    "出去",
    "上班",
    "上学",
    "上课",
    "去公司",
    "去学校",
    "买菜",
    "取快递",
    "拿快递",
    "拿外卖",
    "倒垃圾",
    "遛狗",
    "散步",
    "跑步",
    "健身",
    "锻炼",
    "去医院",
    "去诊所",
    "看医生",
    "聚会",
    "聚餐",
    "出差",
    "去外地",
    "下楼",
    "去接人",
    "送人",
    "加油",
    "洗车",
    "去超市",
    "赶地铁",
    "赶公交"
]


def _load_schema() -> dict:
    path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _default_of(schema: dict, section: str, key: str):
    node = (schema.get(section) or {}).get("items") or {}
    return (node.get(key) or {}).get("default")


def migrate_config(config) -> list[str]:
    """返回被升级的键名；没有升级就返回空列表。"""
    try:
        schema = _load_schema()
    except Exception:
        return []

    changed: list[str] = []

    friend = config.get("friend_settings")
    if isinstance(friend, dict):
        cur = str(friend.get("proactive_prompt") or "")
        if cur.strip() and cur.strip() == LEGACY_PROACTIVE_PROMPT.strip():
            new = _default_of(schema, "friend_settings", "proactive_prompt")
            if isinstance(new, str) and new and new != cur:
                friend["proactive_prompt"] = new
                changed.append("proactive_prompt")

    eng = config.get("custom_engine")
    if isinstance(eng, dict):
        cur_kw = eng.get("away_keywords")
        if isinstance(cur_kw, list) and [str(x) for x in cur_kw] == LEGACY_AWAY_KEYWORDS:
            new_kw = _default_of(schema, "custom_engine", "away_keywords")
            if isinstance(new_kw, list) and new_kw != cur_kw:
                eng["away_keywords"] = new_kw
                changed.append("away_keywords")

    return changed
