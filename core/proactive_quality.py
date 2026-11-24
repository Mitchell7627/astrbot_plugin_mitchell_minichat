"""Local comparison of delivered proactive messages."""

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import text_rules as rules
from .appointment import _day_markers, find_clock, resolve_clock
from .splitter import split_text


NO_SEND = "[[NO_SEND]]"


_TOPICS = {
    "shoulder": re.compile(
        r"(?:借|给|搭|靠|挨).{0,8}肩膀|肩膀.{0,12}(?:借|搭|靠|挨|给你)|"
        r"靠过来|让我挨着|往这边靠"
    ),
    "bed": re.compile(r"(?:挪|卷|抢|分).{0,8}被[子窝]|被[子窝].{0,10}(?:挪|卷|跑|抢)|"
                      r"再.{0,4}(?:眯|闭|赖|睡).{0,6}(?:会|五分钟|眼)|再眯|再五分钟"),
    "waiting": re.compile(r"(?:一直|还在|就在)?等(?:你|哥哥)(?:回[来家]|到家)"),
    "arrival_check": re.compile(r"还没到|走到哪|到(?:教室|店里)了没|离店还远|到了(?:记得|说一声)"),
}
_DESSERT = re.compile(r"布丁|甜点|蛋糕|冰淇淋")
_DESSERT_STATE = re.compile(r"冰箱|还剩|凉着|留(?:着|给)|买好了|摆桌上|拿出来|有得吃")
_DESSERT_PRESSURE = re.compile(r"(?:再不|不来|不吃).{0,16}(?:就|先).{0,16}吃(?:掉|了|完)|(?:就|先).{0,12}把.{0,12}吃(?:掉|了|完)")
_DESSERT_INVITE = re.compile(r"想吃|一起吃|要吃|想来点|吃.{0,10}(?:吧|喵|咯|啦)(?:[。！？!?]|$)")
_DESSERT_FACTS = {
    "reserved": re.compile(r"留(?:好|了|着|给)|给.{0,6}留"),
    "bought": re.compile(r"买(?:好|了)|刚买"),
    "stock": re.compile(r"还剩|还有|有得吃|凉着|在冰箱|冰箱.{0,5}(?:有|放|里那|里还)|摆桌上|放(?:在|进)?冰箱"),
}
_DESSERT_GONE = re.compile(r"吃完|吃光|吃掉了|没有|没剩|没留|没买|不剩|不留")
_DESSERT_ACTOR = re.compile(r"([我你])(?:还|先|也|已经|刚|亲自|特地|帮你|给你|给我){0,4}(?:留|买)")
_CLAUSE_BREAK = re.compile(r"[，,、。！？!?；;\n]+")
_IMAGINED = re.compile(r"如果|假如|要是|假设|假装|想象|的话")
_FICTIONAL = re.compile(r"^(?:我们|我|就|来|不如|一起|再){0,4}(?:假装|想象)")
_QUESTION = re.compile(r"[?？]|要不要|会不会|是不是|(?:吗|么|了没(?:有)?)[喵呀呢啊。！\s]*$")
_SPECULATION = re.compile(r"听说|据说|好像|大概|可能|应该|估计|吧[喵呀呢啊。！\s]*$")
_RECENT_DEED = re.compile(
    r"^(?P<actor>我|你)?\s*(?:今天|刚才|刚刚|这会儿)?\s*(?:已经|刚|才)?\s*"
    r"(?P<deed>收工|下班|到家)(?P<tail>[^，。！？；\n]*)$"
)
_RECENT_SIGHT = re.compile(
    r"^(?P<actor>我|你)?\s*(?:刚才|刚刚|今天|回来的时候|回家路上|路上|刚)"
    r"(?P<lead>.{0,8}?)(?:看见|看到|瞧见|遇见|遇到|碰见)(?:了)?(?P<object>.{2,32})$"
)

_AFFECTION = r"(?:抱(?:抱|一下)|亲(?:亲|一下)|贴贴|蹭蹭|摸摸头|啵+|么么+|mua+|(?:有点|好|很)?想你了?|喜欢你|爱你)"
_AFFECTIVE_SIGNAL = re.compile(_AFFECTION, re.I)
_SOCIAL_ONLY = re.compile(
    r"(?:(?:好[的呀啊啦喵哦]?|嗯+|哦+|噢+|啊+|喵+|哈+|嘿+|在吗|收到|知道了|我知道了|明白了|行[呀啊吧]?|"
    rf"可以[呀啊]?|是的|对[呀啊哦]?|没事|谢谢|早安|晚安|你好|{_AFFECTION}|"
    r"你在吗|哥哥|姐姐|宝贝|笨笨(?:的)?|小笨蛋|ok(?:ay)?|yes|yep|lol|hi|hey|hello|3|"
    r"[喔咯呢诶欸呀啦哒呜唔]+))+", re.I,
)
_CHAT_MARKER = re.compile(r"\[CQ:[^\]\n]+\]|\[(?:表情|图片|语音|视频|动画表情|贴纸|戳一戳)(?::[^\]\n]*)?\]")
_USER_OUTSIDE = re.compile(
    r"(?:在(?:外面|外头|外边|路上|回去的路上)|(?:还)?没(?:回家|回来|到家)|"
    r"(?:刚|已经)出门|还没回(?:过?来|去|[呢呀吗喵\s。？！!?]|$)|"
    r"还没到(?:家|学校|教室|店里|公司|宿舍|车站|那儿|那里|(?=[喵呀呢啊哦吗么吧\s]*$))|"
    r"走到哪(?!一步|个阶段)|离店还远|"
    r"(?:到家|到学校|到教室|到店里|回家|回来)(?:了)?(?:没[有]?|了[吗么]|了吗))"
)
_OUTSIDE_SUBJECT = re.compile(r"(?:你们|你|哥哥|姐姐|他|她|我)(?:现在|这会儿|还|已经|刚|是不是|该不会|一直|都|又){0,4}$")
_AWAY_TAIL = re.compile(r"^(?:(?:要是|如果)(?:你|哥哥|姐姐)?还没回|(?:到(?:家|店里|教室)?|回家)了?(?:再|记得|就|跟我|告诉我)|路上(?:慢点|小心)|(?:先|就先)(?:把|给你).{0,12}(?:焐热|暖好))")
_AWAY_NEGATIVE = re.compile(r"(?:我|咱俩|我们|^)(?:现在|其实|一直|已经|刚|还|早就|都|又){0,4}(?:在家|在一起|回到家|没出门|不在外面|到家了|回来了|回家了|在你身边|在你旁边|在一起吗)")
_AWAY_POSITIVE = re.compile(r"^(?:我)?(?:现在|这会儿|还|刚刚|刚|已经|仍然|仍){0,3}(?:在外面|在外头|在外边|在路上|在(?:回家|去.{1,12})的路上|没到家|没回家|出门了)")

_STORAGE = (r"冰箱|冷藏(?:柜|室)|冷冻(?:柜|室)|柜子|抽屉|桌(?:子)?上|篮子|烤箱|锅里|厨房")
_EXISTENCE = r"(?:还)?(?:有|剩(?:下|着)?|留着|放着|摆着|躺着)"
_GONE = r"(?:已经)?(?:没有|没剩|不剩|吃完|吃光|用完|扔掉|卖光|空了|没了)"
_AMOUNT = r"(?:(?:那|这|一|两|几|[二三四五六七八九十\d]+)(?:袋|盒|包|份|块|瓶|杯|盘|碗|个|张|颗|条|串|把|本|支|件))"
_ITEM_END = r"(?:还(?:有|剩|在|放|摆|躺|留)|没了|没有|没剩|吃完|吃光|用完|扔掉|放着|摆着|躺着|留着|[了呢喵呀啊哦喔啦咯吧吗么]+$|$)"
_INVENTORY_HEAD = re.compile(
    rf"(?P<place>{_STORAGE})(?:里面|里|内|上)?(?:现在|其实|已经|就|真的|还|都|又){{0,3}}"
    rf"(?P<verb>{_GONE}|{_EXISTENCE}|(?={_AMOUNT}))\s*(?:{_AMOUNT})?"
    rf"(?P<item>[\w\u4e00-\u9fff]{{1,18}}?)(?={_ITEM_END})", re.I,
)
_STOCK_HEAD = re.compile(
    rf"^(?P<place>家里|这儿|这里)?(?:现在|目前)?\s*(?P<verb>还(?:有|剩(?:下|着)?)|没有|没剩)\s*"
    rf"(?P<amount>{_AMOUNT})?(?P<item>[\w\u4e00-\u9fff]{{1,18}}?)(?={_ITEM_END})"
)
_INVENTORY_TAIL = re.compile(
    rf"^(?:{_AMOUNT})?(?P<item>[\w\u4e00-\u9fff]{{1,18}}?)"
    rf"(?P<verb>{_GONE}|(?:还)?(?:在|放在|摆在|放进))(?:(?P<place>{_STORAGE}))?"
    r"(?:里面|里|内|上)?[了喵呀呢吧咯啊吗么啦哦喔]*$"
)
_PREPARATION = r"(?:烤好|煮好|做好|泡好|烧好|热好|准备好|备好|切好|洗好|包好|买好|煮好了?|做了|买了)"
_PREP = re.compile(
    rf"^(?P<actor>我|你|哥哥|姐姐)?(?:今天|刚刚|刚才|刚|已经|早就|都|先|给你|给我|帮你|帮我){{0,5}}"
    rf"(?P<negative>没有|还没|没)?(?:(?P<action>{_PREPARATION})(?:了)?(?:{_AMOUNT})?"
    rf"(?P<item>[\w\u4e00-\u9fff]{{1,18}}?)|(?:{_AMOUNT})?(?P<item_first>[\w\u4e00-\u9fff]{{1,18}}?)"
    rf"(?:(?P<suffix_actor>我|你)(?:刚刚|刚|已经|早就|都|给你|给我){{0,4}})?"
    rf"(?P<action_last>{_PREPARATION}))[了喵呀呢咯啊啦吗么哦喔吧]*$"
)
_FINISH_LEAD = r"(?P<actor>我|你|他|她|哥哥|姐姐)?(?:今天|刚刚|刚才|刚|已经|才|总算|这会儿|刚把|把){0,5}(?P<negative>没有|还没|没)?"
_FINISH = re.compile(
    rf"^{_FINISH_LEAD}(?:(?P<activity>[\w\u4e00-\u9fff]{{1,14}}?)(?P<done>收工|完工|做完|结束|弄完|忙完|录完|拍完|写完)|"
    r"(?P<verb>录制|录|拍|写|剪|画|练|修)完(?:了)?(?P<object>[\w\u4e00-\u9fff]{1,14}?))"
    r"[了喵呀呢咯啊啦吗么吧哦喔]*$"
)
_NOT_FACT = re.compile(r"^(?:我|你|他|她|我们|哥哥|姐姐)?(?:如果|假如|要是|只要|假设|假装|想象|下次|等到|等你|等我|明天|后天|昨天|前天|待会|等会|打算|想要|想吃|想去|提议|刚想到|能不能|要不要)")
_ITEM_META = re.compile(r"^(?:什么|东西|点什么|一点|一些|好吃的|没有|没了|了|呢|啊|呀|吧|喵|咯|吗|么|在|了没|我|你|他|她|它|谁|机会|时间|点困|点饿|点累)$")
_SUPPLY_TAIL = re.compile(r"^(?:(?:那|就|你|我|哥哥|姐姐|来|先|我们|一起|要不|要不要|咱们){0,6}(?:包|拿|端|吃掉|热|煮)(?:来|吧|了|点|一下|那|这个|那个|饺子|还是|不如)|(?:不然|否则|那就|你选一个))")
_WORK_TAIL = re.compile(r"^(?:(?:你|哥哥|姐姐)?(?:听没听|听了没|听了吗|听过没|(?:要不要|想不想|想听|来)听(?:一下|一段|一会|听|吗|么|看)|给你听|(?:要不要|想不想|想)看(?:一下|一段|看)))")

_ARRIVAL_PROMPT = re.compile(
    r"还没到(?:吗|呢|[？?]|$)|走到哪|到(?:家|学校|教室|店里)了没|离店还远|"
    r"到(?:家|学校|教室|店里)?了?.{0,4}(?:说一声|告诉我|报个?平安)"
)
_DESTINATION = re.compile(r"到(家|学校|教室|店里)")
_TRIP_TARGET = re.compile(r"(?:出发去|在去|去|往|赶去)(学校|教室|店里)(?:了|的路上|路上)")
_BATH_CONTEXT = re.compile(r"洗澡|浴缸|浴室|泡澡|出浴|再泡下去|还没洗完澡")
_BATH_EXIT = re.compile(r"还不出来|还没出来|起来吧|快(?:从浴缸)?出来|该出来|再泡.{0,8}晕|出来换|出来吧")
_BATH_NOW = re.compile(r"还没洗完|还在泡|再泡|泡一会儿再出来|出来后.{0,10}(?:擦干|擦头发|换衣)")
_LONG_SOAK = re.compile(r"(?:别|不要)泡(?:得)?(?:太|这么|那么)久|泡(?:了|得)这么久")
_IN_THERE = re.compile(r"还在(?:里头|里面|里边)|还没出来")
_COOL_WATER = re.compile(r"水(?:都|已经|快|有点){0,3}(?:要)?凉")
_OTHER_SOAK = re.compile(r"茶|咖啡|燕麦|泡面|泡脚|泡茶|泡衣服")
_HYPOTHETICAL = re.compile(r"如果|假如|要是|洗澡的话|泡澡的话|洗澡时|泡澡时|下次.{0,8}(?:洗澡|泡澡)")
_BATH_DETAIL = re.compile(r"头发|擦干|毛巾|换衣|水温|别着凉")
_WASH_PROGRESS_QUESTION = re.compile(
    r"^(?:你|哥哥|姐姐)?\s*(?:现在|这会儿|已经|终于)?"
    r"(?:(?:澡)?(?:洗|泡)(?:完(?:澡)?|好澡)(?:了)?\s*(?:没有|没(?!有)|吗|么)(?!必要|关系|问题|理由)|"
    r"还没(?:洗|泡)完(?:澡)?(?:吗|么|吧|呀))"
)
_BOOKING = re.compile(r"(?:票|座位|行程)(?:都|也|刚|已经|早就|总算|是|已经都|都已经){0,3}"
                      r"(?:订好|定好|买好|买了|已订)|"
                      r"(?:订好|定好|买好)了[^，。！？；\n]{0,5}(?:票|座位)")
_PLAN_OBJECT = re.compile(r"演唱会|音乐会|电影票|电影|车票|机票|饭局|聚会|开会|吃饭|门票")
_PLAN_CLAIM = re.compile(r"有个|有一场|说好了|约好了|定好了|已经安排|已经预约|已经订了|已经定了|安排好了")
_PLAN_DIRECTIVE = re.compile(r"(?:到时候|届时|到时|那天).{0,10}(?:要|得|记得)(?:到|去|在).{1,24}")
_PLAN_TAIL = re.compile(r"^(?:到时候|届时|那天|票|座位|后台|到时|记得去后台)")
_QUOTED = re.compile(r'```[\s\S]*?```|`[^`\n]+`|[「“『][^」”』\n]*[」”』]|"[^"\n]*"')
_UNCONFIRMED = re.compile(r"没|不|取消|如果|假如|打算|想去|要不要|[?？]|吗[呀呢喵\s]*$")
_CONFIRMATION = re.compile(r"说好|约好|确认|就定|确实|要去|会去|去看|去参加|有个|有一场")
_RETRACTED = re.compile(r"取消|不去了|不去看|退票|票退了|改期|改到")


def _normalize(value):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())


def _bigrams(value):
    return {value[i:i+2] for i in range(len(value)-1)}


def _near_duplicate(first, second):
    if first == second:
        return True
    if min(len(first), len(second)) < 7:
        return False
    a, b = _bigrams(first), _bigrams(second)
    return len(a & b) / len(a | b) >= .78


def _concrete_plan(sentence):
    return not re.search(r"[?？]|^(?:如果|假如|要是)|吗[喵呀呢\s。]*$", sentence.strip()) and bool(
        _BOOKING.search(sentence) or _PLAN_DIRECTIVE.search(sentence)
        or _PLAN_CLAIM.search(sentence) and _PLAN_OBJECT.search(sentence)
    )


def _matched_plan(sentence, user_turns, appointments, now, anchor=""):
    names = set(_PLAN_OBJECT.findall(sentence)) or set(_PLAN_OBJECT.findall(anchor))
    clock = find_clock(sentence)
    dates = {marker.target for marker in _day_markers(sentence, now)}
    booking = bool(_BOOKING.search(sentence))
    directive = _PLAN_DIRECTIVE.search(sentence)
    if None in dates or len(dates) > 1:
        return False
    if not booking and not directive:
        for item in appointments:
            at = item.get("at")
            if not isinstance(at, (float, int)) or item.get("stage") == "done":
                continue
            target = datetime.fromtimestamp(at)
            if (not any(name in str(item.get("label") or "") for name in names)
                    or target < now or dates and target.date() not in dates):
                continue
            if not clock or resolve_clock(clock, target - timedelta(minutes=1), 24) == target.timestamp():
                return True
    for turn, stamp in user_turns:
        turn = _QUOTED.sub(" ", turn)
        matched_object = names.intersection(_PLAN_OBJECT.findall(turn))
        if matched_object and _RETRACTED.search(turn) and not re.search(r"[?？]|如果|假如", turn):
            return False
        if _UNCONFIRMED.search(turn) or not dates.issubset(
            marker.target for marker in _day_markers(turn, stamp or now)
        ):
            continue
        if directive:
            if _normalize(directive.group()) in _normalize(turn):
                return True
            continue
        if not matched_object:
            continue
        other = find_clock(turn)
        if clock and (not other or any(clock.get(k) != other.get(k) for k in ("h", "m", "period"))):
            continue
        evidence = _BOOKING if booking else _CONFIRMATION
        if evidence.search(turn):
            return True
    return False


def _recent_claims(clause, *, user=False):
    for pattern, kind in ((_RECENT_DEED, "deed"), (_RECENT_SIGHT, "sight")):
        match = pattern.fullmatch(clause.strip())
        if not match or (match["actor"] != "你" if user else match["actor"] == "你"):
            continue
        if kind == "sight":
            if re.search(r"没|未|不", match["lead"]):
                continue
            detail = re.sub(r"^一?[只个位]", "", match["object"])
            yield kind, _normalize(re.sub(r"[喵呀呢啊]+$", "", detail))
        else:
            tail = match["tail"]
            if re.match(r"\s*(?:了)?(?:后|之前|的时候|就|再|告诉|记得)", tail):
                continue
            if re.search(r"刚|已经|才|了|早|晚|咯|喽|啦", clause):
                yield kind, "home" if match["deed"] == "到家" else "off_work"


def _dessert_claims(clause, *, user=False):
    names = _DESSERT.findall(clause)
    if not names or _DESSERT_GONE.search(clause):
        return set()
    actor = _DESSERT_ACTOR.search(clause)
    if actor:
        owner = "bot" if actor[1] == ("你" if user else "我") else "user"
    else:
        owner = ""
    return {(kind, name, owner if kind != "stock" else "")
            for name in names for kind, pattern in _DESSERT_FACTS.items() if pattern.search(clause)}


def _recent_evidence(user_turns):
    personal, desserts, withdrawn = set(), set(), set()
    for body, _ in user_turns:
        body = _QUOTED.sub(" ", body)
        for sentence in reversed(re.split(r"(?<=[。！？!?；;\n])", body)):
            if _QUESTION.search(sentence.strip()) or _IMAGINED.search(sentence):
                continue
            for clause in reversed(_CLAUSE_BREAK.split(sentence)):
                if _SPECULATION.search(clause.strip()):
                    continue
                personal.update(_recent_claims(clause, user=True))
                if _DESSERT_GONE.search(clause):
                    withdrawn.update(_DESSERT.findall(clause))
                desserts.update(claim for claim in _dessert_claims(clause, user=True) if claim[1] not in withdrawn)
    desserts.update((kind, name, "") for kind, name, owner in tuple(desserts) if owner)
    return personal, desserts


def has_topic(scene, user_turns, appointments, now):
    if scene[0] == "away" or scene[1] or any(
        item.get("at", 0) > now.timestamp() and item.get("stage") != "done" for item in appointments
    ):
        return True
    for body, _ in user_turns:
        for clause in _CLAUSE_BREAK.split(_CHAT_MARKER.sub(" ", _QUOTED.sub(" ", body))):
            key = _normalize(clause)
            if key and not _SOCIAL_ONLY.fullmatch(key):
                return True
    return False


def affection_turns(user_turns):
    """Only direct affection from the latest two turns, never quotes or scene facts."""
    turns = []
    for body, _ in user_turns[:2]:
        body = _CHAT_MARKER.sub(" ", _QUOTED.sub(" ", body)).strip()
        key = _normalize(body)
        if len(body) <= 80 and _SOCIAL_ONLY.fullmatch(key) and _AFFECTIVE_SIGNAL.search(key):
            turns.append(body)
    return turns


_GESTURES = (
    (re.compile(r"贴贴|贴一下|贴(?:着|紧)"), re.compile(r"^(?P<gesture>贴贴|贴一下)就(?P=gesture)")),
    (re.compile(r"抱抱|抱一下|抱(?:着|紧)"), re.compile(r"^(?P<gesture>抱抱|抱一下)就(?P=gesture)")),
    (re.compile(r"亲亲|亲一下|亲一口"), re.compile(r"^(?P<gesture>亲亲|亲一下)就(?P=gesture)")),
)
# This continuation depends on the invitation that was just discarded.
_ECHO_TAIL = re.compile(r"^谁先松手谁(?:就)?输[了吧呀呗啦喵哦啊。！？!?]*$")


def trim_answered_echo(text, user, replies):
    """Skip a bare repeat of the latest affectionate exchange, not a fresh follow-up."""
    if not text or not replies or not affection_turns([(user, None)]):
        return text, set()
    def split(value):
        return split_text(value, pattern=r"[。！？!?；;\r\n]+", min_length=0,
                          smart=False, balanced=False, trim=True)

    earlier = {_normalize(sentence) for reply in replies for sentence in split(reply)}
    answered = [echo for gesture, echo in _GESTURES if gesture.search(user)
                and any(gesture.search(_QUOTED.sub(" ", reply)) for reply in replies)]
    kept, removed, removed_invitation = [], False, False
    for sentence in split(text):
        head, *rest = re.split(r"[，,]", sentence, maxsplit=1)
        tail = rest[0].strip() if rest else ""
        bare_echo = any(echo.fullmatch(head.strip().rstrip("了吧呀呗啦喵哦啊。！？!?")) for echo in answered)
        key = _normalize(sentence)
        if (bare_echo and (not tail or _ECHO_TAIL.fullmatch(tail))
                or key and key in earlier
                or removed_invitation and _ECHO_TAIL.fullmatch(sentence.strip())):
            removed = True
            removed_invitation = removed_invitation or bare_echo
            continue
        kept.append(sentence)
    return ("\n".join(kept), {"answered_echo"}) if removed else (text, set())


_IDLE_LOCATION = re.compile(
    r"(?:还|仍|继续|正|现在|这会儿|刚才|刚)(?:在|坐在|躺在|窝在|趴在|是在|从)?"
    r"(?:沙发|被窝|床上|浴室|浴缸)(?:上|里|旁|边)?(?:坐|躺|窝|趴|睡)?"
    r"|(?:沙发|被窝|床上)(?:上|里)?(?:睡醒|醒来|爬起来)"
)


def asks_closed_wash(text):
    visible = _QUOTED.sub(" ", text)
    return any(_WASH_PROGRESS_QUESTION.match(clause.strip()) for clause in _CLAUSE_BREAK.split(visible)
               if not _HYPOTHETICAL.search(clause) and not _OTHER_SOAK.search(clause))


_DEFERRED_TOPIC = re.compile(
    r"^(?:至于|关于)?(?:先聊|聊|讨论|看)?(?P<topic>[\u4e00-\u9fffA-Za-z0-9]{2,16}?)"
    r"(?:这件事|的事|的话题)?(?:先|就)?(?:等会儿|待会儿|晚点|回头|改天|下次|以后)?"
    r"(?:再说|再聊|再谈)[啦咯喵呀哦啊呢吧~～\s]*$"
)
_UNNAMED_TOPIC = re.compile(r"^(?:先|现在|晚点|等会|到时|那个|这个|这事|这件|这话题|这些|它|你|我)(?:\W|的?事|的话题)?$|(?:完|了|之后)$")


def check_idle_scene(text):
    """An older exchange cannot establish where either person is right now."""
    kept, removed = [], False
    for sentence in split_text(text, pattern=r"[。！？!?；;\r\n]+", min_length=0,
                               smart=False, balanced=False, trim=True):
        clause = _QUOTED.sub(" ", sentence)
        if _IDLE_LOCATION.search(clause) and not _IMAGINED.search(clause):
            removed = True
        else:
            kept.append(sentence)
    return ("\n".join(kept) if removed else text), removed


def _item(text):
    key = _normalize(text or "")
    return "" if _ITEM_META.fullmatch(key) else key


def _bot_actor(actor, *, user):
    if actor == "你":
        return "bot" if user else "user"
    if actor == "我" or not actor:
        return "user" if user else "bot"
    return ("user" if not user else "third") if actor in {"哥哥", "姐姐"} else actor


def _premises(clause, *, user=False):
    """Yield bounded factual claims as (kind, actor, action/place, object, polarity)."""
    clause = clause.strip()
    if not clause or _NOT_FACT.search(clause):
        return
    for pattern in (_INVENTORY_TAIL, _INVENTORY_HEAD, _STOCK_HEAD):
        m = pattern.search(clause)
        if not m:
            continue
        item = _item(m["item"])
        place = m.groupdict().get("place") or ""
        negative = bool(re.fullmatch(_GONE, m["verb"]) or re.search(r"(?:并非|不是|并没有)\s*$", clause[:m.start()]))
        if item and (place or pattern is _STOCK_HEAD and m["amount"]
                     or negative and (user or pattern is _INVENTORY_TAIL)):
            yield "stock", "", place, item, not negative
            break

    m = _PREP.fullmatch(clause)
    if m and re.search(r"^.{0,6}(?:刚|已经|早就)|[了咯啦吗么哦喔]$", clause):
        item = _item(m["item"] or m["item_first"])
        action = m["action"] or m["action_last"]
        action = action[0] if action != "准备好" else "备"
        if item:
            yield "prep", _bot_actor(m["actor"] or m["suffix_actor"], user=user), action, item, not m["negative"]

    m = _FINISH.fullmatch(clause)
    if m:
        if m["verb"]:
            detail = _item(m["verb"] + (m["object"] or ""))
        else:
            detail = _item(m["activity"])
        if detail and not re.search(r"(?:不了|就|只要|能不能|要不要|可以|打算|想等|想去|什么时候|怎么|没|不必)", detail):
            yield "finish", _bot_actor(m["actor"], user=user), "completed", detail, not m["negative"]


def _verified_claim(claim, evidence, now):
    kind, actor, action, item, positive = claim
    for other, stamp in evidence:
        if stamp and stamp.date() != now.date() and kind == "finish":
            continue
        other_kind, other_actor, other_action, other_item, affirmed = other
        if other_actor != actor or item != other_item:
            continue
        if kind == other_kind and (action == other_action or kind == "stock" and (not action or not other_action)):
            return positive == affirmed and (not positive or kind != "stock" or not action or action == other_action)
    return False


def _presence_evidence(scene, user_turns):
    for body, _ in user_turns:
        body = _QUOTED.sub(" ", body)
        for clause in reversed(_CLAUSE_BREAK.split(body)):
            clause = clause.strip()
            if not clause or _NOT_FACT.search(clause) or _SPECULATION.search(clause):
                continue
            if _AWAY_NEGATIVE.search(clause) or re.search(r"不是(?:一直|都)?在一起[吗么]", clause):
                return False
            if (_AWAY_POSITIVE.search(clause) or _TRIP_TARGET.search(clause) and clause.startswith(("我", "正在", "出发"))) and not _QUESTION.search(body.strip()):
                return True
    return scene[0] == "away" or scene[3] == "returning" and scene[2] == "self"


def _assumes_away(content):
    for clause in _CLAUSE_BREAK.split(content):
        clause = clause.strip()
        match = _USER_OUTSIDE.search(clause)
        if not match:
            continue
        if _AWAY_TAIL.match(clause):
            return True
        if _NOT_FACT.search(clause) or re.search(r"从外面.{0,4}回|以前|每次|下次|的时候", clause):
            continue
        subject = _OUTSIDE_SUBJECT.search(clause[:match.start()])
        if not subject or not subject.group().startswith(("我", "他", "她")):
            return True
    return False


def _trip_target_matches(content, user_turns):
    targets = set(_DESTINATION.findall(content)) - {"家"}
    if not targets:
        return True
    for body, _ in user_turns:
        body = _QUOTED.sub(" ", body)
        if _QUESTION.search(body.strip()) or _IMAGINED.search(body):
            continue
        for clause in reversed(_CLAUSE_BREAK.split(body)):
            clause = clause.strip()
            if not clause or _NOT_FACT.search(clause) or _SPECULATION.search(clause):
                continue
            if re.search(r"(?:不去|改去|换去|取消行程|已经到了|已经到|我到了)", clause):
                return False
            trip = set(_TRIP_TARGET.findall(clause))
            if trip and not clause.startswith(("你", "他", "她")):
                return targets.issubset(trip)
    return False


def _dependent_on(content, rejected):
    return bool(
        "away" in rejected and _AWAY_TAIL.match(content) or
        "stock" in rejected and _SUPPLY_TAIL.match(content) or
        "finish" in rejected and _WORK_TAIL.match(content)
    )


_WASH_FACE_START = re.compile(r"(?:我)?(?:正在|在|刚开始)洗脸|(?:我)?(?:去|开始)洗脸[了啦咯喵]+")
_WASH_FACE_END = re.compile(r"洗完脸|脸(?:已经|刚刚|刚)?洗好|(?:没在|没有在|不是在)洗脸")
_FOOD = r"晚饭|午饭|早饭|早餐|饭菜|饭|布丁|蛋糕|饺子|面条|甜点|冰淇淋|汉堡"
_FOOD_STATE = re.compile(
    rf"(?P<food>{_FOOD})(?:已经|早就|刚刚|已经都|都|刚|还没|没有|没|还未|还|放|搁|摆|端|快|要|会|得){{0,5}}"
    r"(?P<phase>放凉|凉|冷|上桌|摆好|端好|备好|做好|准备好|煮好|吃完)(?:了|啦|咯|喵|啊|吗|没有|呢|吧|呀|$)"
)
_HEATING = re.compile(r"(?P<object>开水|热水|水|锅)(?:已经|早就|刚刚|刚|都|也|还没|没有|没|还未|还|在){0,4}(?P<phase>烧上|烧开|烧好|烧着|煮上|架上)(?:了|啦|咯|喵|啊|吗|没有|吧|呢|$)")
_SCENE_TAIL = re.compile(r"^(?:(?:不然|否则|那就)\s*)?(?:(?:快来|赶紧|记得|你来|哥哥来)(?:把|趁|吃|喝|端|关火|进来)|(?:趁热|再热一下|去关火|快进厨房|把火关了|先端过来))")
_OLD_SCENE = re.compile(r"以后|下回|刚才.{0,5}(?:说|聊|提)|之前.{0,5}(?:说|聊|提)|以前|每次|平时|(?:要|等).{0,12}的?时候")
_MEAL_OWNERSHIP = re.compile(r"^(?P<actor>我|你|{names})(?:来|自己|负责)?(?:弄|准备|做|煮)(?:今天的?)?(?:这顿|(?:早|午|晚)?饭|早餐|午餐|晚餐)?(?:就好|好了|吧|喵|呀|啊|啦|哦|了)*$")
_MEAL_RETRACT = re.compile(r"(?:吃完|吃过了|取消|不用.{0,6}(?:做|弄)|不吃了|算了|换成外卖)")
_NAMED_MEAL = re.compile(r"(?:早|午|晚)[饭餐]")
_KITCHEN_ENTRY = re.compile(r"进(?:不进|来|厨房)|要不要过来|到底过不过来")
_MEAL_OWNER_NUDGE = re.compile(r"(?:到底|究竟|赶紧|你决定).{0,12}(?:吃什么|做什么|弄什么)|(?:到底|究竟|赶紧).{0,12}(?:吃啥|做啥|弄啥)")


def meal_owner(user_turns, now, nicknames):
    names = "|".join(map(re.escape, nicknames)) or r"(?!)"
    pattern = re.compile(_MEAL_OWNERSHIP.pattern.replace("{names}", names))
    turns = [(body, at) for body, at in user_turns if at and timedelta(0) <= now - at <= timedelta(hours=2)]
    meal_until = None
    owner = ""
    meal = ""
    for body, at in reversed(turns):
        for clause, literal in _scene_clauses(body):
            if not literal:
                continue
            if _MEAL_RETRACT.search(clause):
                owner, meal_until = "", None
                continue
            named = _NAMED_MEAL.search(clause)
            if named and meal != named[0][0]:
                if meal:
                    owner = ""
                meal = named[0][0]
            if _MEAL.search(clause) or re.search(r"饭(?:吃|弄|做)", clause):
                meal_until = at + timedelta(minutes=30)
            match = pattern.fullmatch(clause)
            if match and (owner or meal_until and at <= meal_until):
                owner = "user" if match["actor"] == "我" else "bot"
    return owner


def _scene_clauses(body):
    visible = _QUOTED.sub(lambda m: " " * len(m[0]), body)
    for pos in re.finditer(r"[^，,。！？!?；;\r\n、]+", visible):
        clause = pos[0].strip()
        if not clause:
            continue
        i = pos.start() + len(pos[0]) - len(pos[0].lstrip())
        literal = not (_NOT_FACT.search(clause) or _OLD_SCENE.search(clause)
                       or any(guard(visible, i) for guard in (
                           rules.is_reported_clause, rules.is_conditional_clause, rules.is_dream_clause,
                           rules.is_far_past_clause, rules.is_future_clause, rules.is_third_person_clause)))
        yield clause, literal and not rules.is_question_clause(visible, i, kw_len=len(clause))


def _fact_key(match):
    values = match.groupdict()
    item = values.get("food") or values["object"]
    phase = values["phase"]
    if item in {"饭菜", "晚饭", "午饭", "早饭", "早餐"}:
        item = "饭"
    if item in {"水", "开水", "热水"}:
        item = "水"
    phase = {"放凉": "cool", "凉": "cool", "冷": "cool", "上桌": "ready", "摆好": "ready", "端好": "ready",
             "备好": "ready", "做好": "ready", "准备好": "ready", "煮好": "ready",
             "烧上": "heating", "烧着": "heating", "煮上": "heating", "架上": "heating",
             "烧开": "heated", "烧好": "heated", "吃完": "finished"}[phase]
    return item, phase


def _confirmed_scene_fact(clause, hit, facts):
    item, phase = _fact_key(hit)
    affirmed = not re.search(r"没|不是|还未", clause[:hit.start()] + hit[0])
    return facts.get(item) == (phase, affirmed) or (
        phase == "ready" and affirmed and facts.get(item) == ("cool", True))


@dataclass(frozen=True)
class SceneFacts:
    face_wash: bool | None
    objects: tuple

    def hints(self):
        if self.face_wash is not None:
            yield "用户正在洗脸，尚未确认洗完。" if self.face_wash else "用户已结束洗脸或确认没在洗脸。"
        labels = {"cool": "已放凉", "ready": "已备好", "heating": "正在烧", "heated": "已烧好", "finished": "已吃完"}
        for item, (phase, affirmed) in self.objects:
            yield f"用户确认{item}{labels[phase] if affirmed else '尚未' + labels[phase].removeprefix('已')}；位置和经手人须另外确认。"


def scene_facts(user_turns, now):
    facts = {}
    face = None
    for body, at in user_turns:
        if at is not None and not timedelta(0) <= now - at <= timedelta(minutes=30):
            continue
        for clause, literal in reversed(tuple(_scene_clauses(body))):
            if not literal:
                continue
            if face is None and not clause.startswith(("你", "他", "她")):
                if _WASH_FACE_END.search(clause):
                    face = False
                elif _WASH_FACE_START.fullmatch(clause):
                    face = True
            for pattern in (_FOOD_STATE, _HEATING):
                for match in pattern.finditer(clause):
                    item, phase = _fact_key(match)
                    negative = re.search(r"没|不是|还未", clause[:match.start()] + match[0])
                    facts.setdefault(item, (phase, not bool(negative)))
    return SceneFacts(face, tuple(facts.items()))


def check_scene_facts(text, scene, user_turns, *, owner="", facts=None, now=None):
    """Check concrete current premises at clause boundaries, keeping independent wording."""
    facts = facts or scene_facts(user_turns, now or datetime.now())
    face = facts.face_wash
    objects = dict(facts.objects)
    kept, reasons = [], set()
    rejected = False
    for sentence in _review_sentences(text):
        pieces = []
        visible = _QUOTED.sub(lambda m: " " * len(m[0]), sentence)
        safe = _IMAGINED.search(visible) or _FICTIONAL.search(visible.strip()) or _OLD_SCENE.search(visible)
        missing_heat = (not safe and any(not _confirmed_scene_fact(visible, hit, objects) for hit in _HEATING.finditer(visible)))
        for match in re.finditer(r"[^，,、]+", sentence):
            piece = match[0].strip()
            clause = visible[match.start():match.end()].strip()
            if not piece:
                continue
            reason = None
            if not safe and not _NOT_FACT.search(clause):
                if (asks_closed_wash(clause) or re.match(r"(?:你|哥哥|姐姐)?\s*(?:洗完脸|脸洗完)(?:了)?(?:没|吗|么)", clause)
                        ) and not face and not (scene[1] == "shower" and scene[2] in ("self", "together")):
                    reason = "unconfirmed_wash_progress"
                elif any(not _confirmed_scene_fact(clause, hit, objects)
                         for pattern in (_FOOD_STATE, _HEATING) for hit in pattern.finditer(clause)):
                    reason = "unconfirmed_scene_detail"
                elif missing_heat and _KITCHEN_ENTRY.search(clause):
                    reason = "unsupported_followup"
                elif owner == "bot" and _MEAL_OWNER_NUDGE.search(clause):
                    reason = "meal_already_assigned"
                elif rejected and _SCENE_TAIL.match(clause):
                    reason = "unsupported_followup"
            if reason:
                rejected = True
                reasons.add(reason)
            else:
                pieces.append(piece)
        if not pieces:
            continue
        if len(pieces) == len([part for part in re.split(r"[，,、]", sentence) if part.strip()]):
            kept.append(sentence)
        elif len(pieces) > 1 or not re.fullmatch(r"哥哥|姐姐|宝贝|宝宝|你呀", pieces[0]):
            kept.append("，".join(pieces))
    return ("\n".join(kept), reasons) if reasons else (text, set())


def polish_reply(text, scene, user_turns=(), appointments=(), *, now=None):
    """Filter clear contradictions and repeated actions before QQ segmentation."""
    now = now or datetime.now()
    sentences = split_text(text, pattern=r"[。！？!?；;\r\n]+", min_length=0,
                           balanced=False, max_segments=len(text) + 1, trim=False)
    visible = [_QUOTED.sub(" ", sentence) for sentence in sentences]
    current = [content for content in visible
               if not _HYPOTHETICAL.search(content) and not _OTHER_SOAK.search(content)]
    bath = any(_BATH_CONTEXT.search(content) for content in current)
    implicit_bath = (any(_LONG_SOAK.search(content) for content in current) and
                     any(_IN_THERE.search(content) or _COOL_WATER.search(content) for content in current))
    kept, seen, reasons = [], [], set()
    user_turns = tuple(user_turns)
    personal, desserts = _recent_evidence(user_turns)
    evidence = [
        (claim, stamp) for body, stamp in user_turns
        for part in reversed(re.split(r"(?<=[。！？!?；;\n])", _QUOTED.sub(" ", body)))
        if not _QUESTION.search(part.strip()) and not _IMAGINED.search(part)
        for clause in reversed(_CLAUSE_BREAK.split(part)) if not _SPECULATION.search(clause.strip())
        for claim in _premises(clause, user=True)
    ]
    away = _presence_evidence(scene, user_turns)
    dessert_names = {name for _, name, _ in desserts}
    plan_rejected, plan_anchor = False, ""
    rejected = set()
    for sentence, content in zip(sentences, visible):
        if plan_rejected and _PLAN_TAIL.search(content.strip()):
            reasons.add("unsupported_plan")
            continue
        if _concrete_plan(content):
            plan_rejected = not _matched_plan(content, user_turns, appointments, now, plan_anchor)
            if plan_rejected:
                reasons.add("unsupported_plan")
                continue
            if _PLAN_OBJECT.search(content):
                plan_anchor = content
        bath_exit = _BATH_EXIT.search(content)
        if (scene[1] != "shower" or scene[2] == "her") and content in current and (
            bath and (bath_exit or _BATH_NOW.search(content))
            or implicit_bath and (_LONG_SOAK.search(content) or _IN_THERE.search(content)
                                  or _COOL_WATER.search(content))
        ):
            reasons.add("unconfirmed_bath")
            continue
        claims = [] if _FICTIONAL.search(content.strip()) else [
            clause for clause in _CLAUSE_BREAK.split(content)
            if not _QUESTION.search(clause) and not _IMAGINED.search(clause)
        ]
        missing = [] if _FICTIONAL.search(content.strip()) else [
            claim for clause in _CLAUSE_BREAK.split(content) for claim in _premises(clause)
            if not _verified_claim(claim, evidence, now)
            and not (claim[0] in {"stock", "prep"} and _DESSERT.search(claim[3]))
        ]
        if (not _FICTIONAL.search(content.strip()) and _assumes_away(content)
                and (not away or not _trip_target_matches(content, user_turns))):
            rejected.add("away")
            reasons.add("unconfirmed_presence")
            continue
        if missing:
            for kind, *_ in missing:
                rejected.add("finish" if kind == "finish" else "stock")
                reasons.add("unconfirmed_experience" if kind == "finish" else "unconfirmed_inventory")
            continue
        if _dependent_on(content.strip(), rejected):
            reasons.add("unsupported_followup")
            continue
        if any(set(_recent_claims(clause)) - personal for clause in claims):
            reasons.add("unconfirmed_experience")
            continue
        if any(
            _dessert_claims(clause) - desserts or _DESSERT_PRESSURE.search(clause)
            and any(name not in dessert_names for name in _DESSERT.findall(clause))
            for clause in ([] if _FICTIONAL.search(content.strip()) else
                           [part for part in _CLAUSE_BREAK.split(content) if not _IMAGINED.search(part)
                            and not _NOT_FACT.search(part.strip())])
        ):
            reasons.add("unconfirmed_dessert")
            rejected.add("stock")
            continue
        key = _normalize(sentence)
        arrival = _ARRIVAL_PROMPT.search(content)
        action = "bath_exit" if bath and bath_exit else "arrival_check" if arrival else ""
        details = _BATH_DETAIL.findall(content) if action == "bath_exit" else []
        if action == "arrival_check":
            details = _DESTINATION.findall(content)
            # A destination or a separate substantive clause can add information.
            tail = re.split(r"[，,]", content[arrival.end():], maxsplit=1)
            if len(tail) == 2 and len(_normalize(tail[1])) >= 5 and not _ARRIVAL_PROMPT.search(tail[1]):
                details.append(_normalize(tail[1]))
        details = set(details)
        if key and any(
            _near_duplicate(key, old_key) or
            action and action == old_action and not details.difference(old_details)
            for old_key, old_action, old_details in seen
        ):
            reasons.add("same_reply")
            continue
        seen.append((key, action, details))
        kept.append(sentence)
    return ("\n".join(kept) if reasons else text), reasons


def topics(text, *, from_user=False):
    found = [name for name, pattern in _TOPICS.items() if pattern.search(text)]
    for clause in re.split(r"[。！？!?\n；;]+", _QUOTED.sub(" ", text)):
        if from_user and re.search(r"不想|不吃|不要|算了|取消|没买|没有|如果|假如|假装", clause):
            continue
        if (_DESSERT_STATE.search(clause) or _DESSERT_PRESSURE.search(clause)
                or from_user and _DESSERT_INVITE.search(clause)):
            found.extend("dessert:" + name for name in _DESSERT.findall(clause))
    return sorted(set(found))


_ADDED_PRESSURE = re.compile(r"怎么|一直|根本|都|又|到现在")
_SOCIAL_SILENCE = re.compile(
    r"(?:一上午|一下午|一整天|一晚上|大半天|半天|等了(?:好久|很久|一会儿)|(?:这么|那么)久|三个?小时)"
    r"(?:了|也|还|都|一直|一点|仍然|对方|哥哥|你){0,6}(?:没(?:有)?(?:声音|动静|消息|吭声|说话|回音|回话)|不吭声)"
)
_SILENCE_DEVICE = re.compile(r"耳机|喇叭|音响|扬声器|麦克风|直播|视频|音频|语音|设备|游戏|电视|静音|手机外放")
_WAIT_GUILT = re.compile(r"等(?:着?你)?(?:回(?:复|话|消息))?等?得.{0,12}(?:呆毛.{0,4}蔫|委屈|生气|难过|快哭|快疯|心凉)|等得.{0,7}蔫")
_CALL_FOR_REPLY = re.compile(r"(?:倒是|好歹)(?:也|先|就|来|跟我|给我){0,2}(?:说(?:句|句话|一声)|回(?:我|句话|一句|一声|话)|吭(?:个|一)?声)")
_OPENING = re.compile(
    r"^(?P<greeting>(?:(?:哥哥|姐姐|哥|姐|宝贝|宝宝)[，, \t]*)?"
    r"(?:早上好|晚上好|午安|早安|早[啊呀喵]?|嗨[呀喵]?))[，, \t]+"
    r"|^(?P<question>[^，,。！？?！\n]{0,12}(?:在忙|有空|醒了)(?:吗|嘛|么|没))(?=[，, \t])"
)
_MEAL = re.compile(r"吃饭|饭点|吃(?:午饭|晚饭|午餐|晚餐|午觉后的饭)|(?:早|午|晚)[饭餐]|饭(?:又|不会|都|已经|好了)|做饭|弄(?:点)?吃的")
_MEAL_INVITE = re.compile(r"(?:一起|一块|咱俩|我们).{0,5}吃|吃饭(?:吧|去)|我.{0,6}(?:去|要|会)吃饭")
_MEAL_PRESSURE = re.compile(r"^(?:你|哥哥|姐姐)?(?:不要|不吃|不来吃)(?:的话|也行)[，, ]*(?:那)?我(?:可)?就(?:先)?自己吃")
_ASSUMED_YES = re.compile(r"(?:那)?就当(?:你|哥哥|姐姐|对方)(?:已经|是|也)?(?:应了|答应了|同意了|默认了)")
_PLAN_CANCEL = re.compile(r"取消|不去了|不吃了|不吃饭|改期|改天|不去吃|先不吃|还没定|没说好")
_DIRECT_YES = re.compile(r"^\s*(?:好(?:呀|啊|喵|哦|的)?|行(?:啊|呀|喵)?|同意|说好了|就这么定)(?:[，。！!\s]|$)")
_TIMING = re.compile(r"(?:还(?:只)?差|还(?:只)?有|再(?:等|过))(?P<n>[零〇一二两三四五六七八九十几\d]{1,3})\s*分钟(?P<end>[^。！?？\n]{0,8}(?:就(?:要|该)?到点|就(?:要|该)?开饭|就(?:要|该)?吃饭|就是饭点))?")
_TIME_CONTEXT = re.compile(r"到点|饭点|时间还没到|几点开始|几点吃|几点去|还没到时间")
_TIMING_NEEDS_PROOF = re.compile(r"饭点(?:快到了|差不多到了)|快到(?:饭点|约定的时间)(?:了|啦)?")
_TIME_NUMS = {c:i for i,c in enumerate("零一二三四五六七八九")}
_TIME_NUMS.update({"两":2,"〇":0})
_RELATIVE_MEAL = re.compile(r"(?P<n>[一二两三四五六七八九十\d]{1,3})\s*分钟(?:以?后|再).{0,8}(?:一起吃|去吃|吃饭|开饭)")


def _minutes(value):
    if value.isascii() and value.isdigit():
        return int(value)
    if value in _TIME_NUMS:
        return _TIME_NUMS[value]
    if "十" in value:
        a, b = value.split("十", 1)
        if (not a or a in _TIME_NUMS) and (not b or b in _TIME_NUMS):
            return 10 * _TIME_NUMS.get(a, 1) + _TIME_NUMS.get(b, 0)
    return None


def _user_is_speaking_fact(body):
    return bool(body.strip() and not _IMAGINED.search(body) and not _QUESTION.search(body.strip()))


def _meal_supported(turns, appointments, now):
    for body, _ in turns:
        visible = _QUOTED.sub(" ", body)
        if not _user_is_speaking_fact(visible):
            continue
        if _PLAN_CANCEL.search(visible) and (_MEAL.search(visible) or _MEAL_INVITE.search(visible)):
            return False
        if _MEAL_INVITE.search(visible) and not re.search(r"想吃|不想|还没决定|打算", visible):
            return True
    return any(_MEAL.search(item.get("label", "")) and item.get("stage") != "done"
               and item.get("at", 0) >= now.timestamp() - 60 for item in appointments)


def _confirmed_countdown(m, meal, turns, appointments, now):
    for body, stamp in turns:
        visible = _QUOTED.sub(" ", body)
        if not _user_is_speaking_fact(visible):
            continue
        if _PLAN_CANCEL.search(visible) and ((meal and _MEAL.search(visible)) or _TIME_CONTEXT.search(visible)):
            return False
        if not ((_MEAL.search(visible) or _MEAL_INVITE.search(visible)) if meal else _PLAN_OBJECT.search(visible)):
            continue
        relative = _RELATIVE_MEAL.search(visible) if meal else None
        clock = find_clock(visible)
        if not relative and not clock:
            continue
        dates = {marker.target for marker in _day_markers(visible, now)}
        if dates and dates != {now.date()}:
            return False
        if relative and stamp:
            minutes = _minutes(relative["n"])
            at = stamp.timestamp() + minutes * 60 if minutes is not None else None
        else:
            at = resolve_clock(clock, now, 1) if clock else None
        return _matches_countdown(at, m, now)  # A newer explicit time overrides an old one.
    active = [item for item in appointments if item.get("stage") != "done"
              and (not meal or _MEAL.search(item.get("label", "")))
              and now.timestamp() - 60 <= item.get("at", 0) <= now.timestamp() + 3600]
    return len(active) == 1 and _matches_countdown(active[0]["at"], m, now)


def _matches_countdown(at, match, now):
    if at is None:
        return False
    remaining = (at - now.timestamp()) / 60
    if remaining < 0:
        return False
    value = _minutes(match["n"]) if match else None
    return abs(remaining - value) <= 1 if value is not None else remaining <= 15


def _review_sentences(text):
    return split_text(text, pattern=r"[。！？!?；;\r\n]+", min_length=0,
                      balanced=False, max_segments=len(text) + 1, trim=False)


def needs_timing_check(text):
    return bool(_TIMING.search(text) or _TIMING_NEEDS_PROOF.search(text) or _ASSUMED_YES.search(text))


def check_chat_timing(text, turns, appointments, now):
    """Only police a claimed deadline/assent, not ordinary role play or meal suggestions."""
    if not needs_timing_check(text):
        return text, None
    return _trim_chat_timing(text, turns, appointments, now)


def _trim_chat_timing(text, turns, appointments, now):
    latest = turns[0][0] if turns else ""
    visible_text = _QUOTED.sub(" ", text)
    meal_context = bool(_MEAL.search(visible_text) or _MEAL.search(latest))
    kept, edited = [], False
    for sentence in _review_sentences(text):
        content = _QUOTED.sub(" ", sentence)
        match = _TIMING.search(content)
        implicit = bool(re.match(r"^\s*(?:反正|都|也|这会儿|其实)*\s*(?:还|再)", content))
        meal = bool(_MEAL.search(content) or (meal_context and (implicit or _ASSUMED_YES.search(content))))
        disputed_assent = (meal and _ASSUMED_YES.search(content)
                           and not (_DIRECT_YES.search(latest) and _user_is_speaking_fact(latest) and not _PLAN_CANCEL.search(latest)
                                    or _meal_supported(turns, appointments, now)))
        unsupported_time = ((match is not None and (meal or match["end"] or implicit and _TIME_CONTEXT.search(latest))
                             or _TIMING_NEEDS_PROOF.search(content))
                            and not _confirmed_countdown(match, meal, turns, appointments, now))
        if (disputed_assent or unsupported_time) and _user_is_speaking_fact(content):
            edited = True
        else:
            kept.append(sentence)
    return ("\n".join(kept), "unconfirmed_schedule") if edited else (text, None)


def _no_reply_pattern(nicknames):
    names = "|".join(map(re.escape, sorted({"我", *nicknames}, key=len, reverse=True)))
    return re.compile(
        rf"(?:(?:你|您|哥哥|姐姐)\s*|(?:^|(?<=[，,、\s吗嘛么]))(?:(?:哥|姐)\s*)?)"
        rf"(?P<tone>(?:到现在|这会儿|今天|下午|怎么|还|一直|根本|又|都|也|是不是){{0,6}})"
        rf"\s*(?:没(?:有)?|不)(?:(?P<ignoring>搭理|理)(?:{names})"
        rf"|回(?:复|应)?(?:{names})(?:的?(?:消息|话))?|回(?:复|应|消息|话))"
        rf"(?=\s*(?:[了呢啊吧嘛呀喵咯哦。！？?!，、,；;…～]|$))"
    )


def _safe_opening(sentence):
    match = _OPENING.match(sentence)
    if match:
        if match["greeting"]:
            return match["greeting"] + "。"
        return match["question"] + "？"
    return ""


def check_proactive_assumptions(text, user_turns, appointments, now, replied, *, bot_nicknames=()):
    clean, timing = check_chat_timing(text, user_turns, appointments, now)
    reasons = {timing} if timing else set()
    no_reply = _no_reply_pattern(bot_nicknames)
    kept = []
    for sentence in _review_sentences(clean):
        content = _QUOTED.sub(" ", sentence).strip()
        hypothetical = _IMAGINED.search(content)
        complaint = no_reply.search(content)
        if not hypothetical and (_CALL_FOR_REPLY.search(content) or _WAIT_GUILT.search(content)
                                 or (_SOCIAL_SILENCE.search(content) and not _SILENCE_DEVICE.search(content))):
            reasons.add("reply_pressure")
            if greeting := _safe_opening(sentence):
                kept.append(greeting)
        elif (complaint and not hypothetical
              and (replied is not False or complaint["ignoring"] or _ADDED_PRESSURE.search(complaint["tone"]))):
            reasons.add("reply_received" if replied else "no_reply_unverified" if replied is None else "reply_pressure")
            if greeting := _safe_opening(sentence):
                kept.append(greeting)
        elif _MEAL_PRESSURE.search(content) and not _meal_supported(user_turns, appointments, now):
            reasons.add("unconfirmed_meal_pressure")
        else:
            kept.append(sentence)
    return ("\n".join(kept), reasons) if reasons else (text, set())


def contradictory_clock(text, now):
    """Check clear claims about now, leaving quoted times and plans alone."""
    for match in re.finditer(
        r"(?:^|[，。！？\n])\s*(还没到|还不到|都还没到|已经过了|已经|现在是|现在|都已经)"
        r"\s*((?:(?:凌晨|早上|上午|中午|下午|傍晚|晚上)\s*)?"
        r"(?:\d{1,2}[:：]\d{2}|[零〇一二两三四五六七八九十\d]{1,3}[点時时](?:半|[零〇一二两三四五六七八九十\d]{1,3}分)?))",
        text,
    ):
        tail = text[match.end():]
        if re.match(r"\s*(?:过|半|多|左右|以前|以后|之前|之后|下课|开会|(?:啦|了|喵|呢)*[吗么？?])", tail):
            continue
        clock = find_clock(match[2] + "钟")
        if not clock:
            continue
        hour = clock["h"]
        period = clock.get("period")
        if period in {"night", "pm", "noon"} and hour < 12:
            hours = [hour + 12]
        elif period == "dawn" and hour == 12:
            hours = [0]
        elif period is None and 1 <= hour <= 12:
            hours = [hour % 12, hour % 12 + 12]
        else:
            hours = [hour]
        minute = now.hour * 60 + now.minute
        target = min((h * 60 + clock["m"] for h in hours), key=lambda m: abs(m - minute))
        # Ignore distant unqualified times and midnight rollover instead of
        # correcting the model's intended date by guessing.
        if abs(target - minute) > 180:
            continue
        head = match[1]
        if "不到" in head or "没到" in head:
            if minute >= target:
                return True
        elif head.startswith(("已经", "都已经")):
            if minute < target:
                return True
        else:
            exact = re.match(r"\s*整", tail) or ":" in match[2] or "：" in match[2] or clock["m"]
            if ((exact and minute != target)
                    or (not exact and now.hour != target // 60)):
                return True
    return False


def _hash(text):
    return hashlib.blake2s(text.encode("utf-8"), digest_size=12).hexdigest()


def fingerprint(text, at, scene):
    whole = _normalize(text)
    sentences = [_normalize(s) for s in re.split(r"[\n。？！!?；;]+", text)]
    grams = {
        _hash(s[i:i+2])
        for s in sentences if len(s) >= 6 for i in range(len(s)-1)
    }
    return {"at": at, "text": _hash(whole), "grams": sorted(grams),
            "sentences": sorted({_hash(s) for s in sentences if len(s) >= 9}),
            "topics": topics(text), "scene": scene}


def recent(rows, now):
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("at"), (int, float))
            and 0 <= now - r["at"] < 86400 and {"text", "grams", "topics", "scene"} <= r.keys()][-8:]


def duplicate(candidate, rows, topic_updates):
    for prior in rows:
        if prior["text"] == candidate["text"]:
            return "same_text"
        grams, previous = set(candidate["grams"]), set(prior["grams"])
        if (set(candidate["sentences"]).intersection(prior.get("sentences", ()))
                or grams and previous and len(grams & previous) / len(grams | previous) >= .72):
            return "similar_text"
        if any(
            topic_updates.get(topic, 0) <= prior["at"]
            for topic in set(prior["topics"]) & set(candidate["topics"])
            if prior["scene"] == candidate["scene"] or topic.startswith("dessert:")
        ):
            return "same_topic"
    return None
