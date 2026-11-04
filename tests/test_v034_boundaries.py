import copy
import pytest


@pytest.mark.parametrize('text',[
 '我原计划明天下午三点开会，但是取消了',
 '我本来明天下午三点开会，现在不用去了',
])
def test_same_sentence_cancel_also_removes_existing_target(plugin,umo,clock,text):
    r=plugin.runner
    r.update_from_message(umo,'明天下午三点开会')
    r.update_from_message(umo,'后天下午三点开会')
    before=copy.deepcopy(r._appointments(umo))
    r.update_from_message(umo,text)
    assert [a['at'] for a in r._appointments(umo)]==[before[1]['at']]
    rec=next(x for x in r.decisions.items(umo) if x['rule']=='CANCEL' and x['action']=='applied')
    r.undo_decision(rec['id'])
    assert r._appointments(umo)==before


@pytest.mark.parametrize('suffix',[
 '但是取消了吗','但是取消了？','但是不要取消','但是取消失败了',
 '他说取消了','如果取消了','但是取消了后天的会议',
])
def test_following_clause_cannot_cancel_unrelated_or_unconfirmed_target(plugin,umo,clock,suffix):
    r=plugin.runner;r.update_from_message(umo,'明天下午三点开会')
    before=copy.deepcopy(r._appointments(umo))
    r.update_from_message(umo,'我明天下午三点开会，'+suffix)
    assert any(a['id']==before[0]['id'] for a in r._appointments(umo))


@pytest.mark.parametrize('text',[
 '不要取消明天下午三点的会议',
 '如果明天下午三点的会议不用提醒我了',
 '明天下午三点的会议不用提醒我了吗',
 '他说明天下午三点的会议不用提醒我了',
 '明天下午三点的牙医预约不用提醒我了',
])
def test_stop_notice_protections(plugin,umo,clock,text):
    r=plugin.runner;r.update_from_message(umo,'明天下午三点开会')
    before=copy.deepcopy(r._appointments(umo))
    r.update_from_message(umo,text)
    assert r._appointments(umo)==before


@pytest.mark.parametrize('text',['没事？','慢慢来，我不去了','没关系但不是我','我在忙','不是我','取消','明天吧'])
def test_neutral_grammar_cannot_admit_real_intent(plugin,text):
    assert not plugin.runner.is_confirmation(text)


@pytest.mark.parametrize('text',[
 '我明天下午三点可能去开会',
 '我明天下午三点也许要去开会',
 '我还没有决定要去明天下午三点开会',
 '我不确定明天下午三点是否要去开会',
])
def test_uncertain_plan_with_go_between_modality_and_event(plugin,umo,clock,text):
    plugin.runner.update_from_message(umo,text)
    assert not plugin.runner._appointments(umo)


@pytest.mark.parametrize('text',[
 '我确定明天下午三点开会','我已经决定明天下午三点去开会','我明天下午三点去开会',
])
def test_confirmed_plans_remain_usable(plugin,umo,clock,text):
    plugin.runner.update_from_message(umo,text)
    assert len(plugin.runner._appointments(umo))==1


@pytest.mark.parametrize('text',[
 '我明天下午三点开会，但是取消了，后天下午四点下课',
 '我本来明天下午三点开会，现在不用去了，后天下午四点下课',
])
def test_followup_cancel_keeps_independent_later_appointment(plugin,umo,clock,text):
    from datetime import datetime
    plugin.runner.update_from_message(umo,text)
    assert [a['at'] for a in plugin.runner._appointments(umo)]==[datetime(2026,11,4,16).timestamp()]

@pytest.mark.parametrize('text',['明天下午三点的会议不要再提醒我了','明天下午三点的会议别再提醒我了'])
def test_stop_notice_with_again_modifier(plugin,umo,clock,text):
    r=plugin.runner
    r.update_from_message(umo,'明天下午三点开会')
    r.update_from_message(umo,'后天下午三点开会')
    original=copy.deepcopy(r._appointments(umo))
    r.update_from_message(umo,text)
    assert [a['id'] for a in r._appointments(umo)]==[original[1]['id']]
    rec=next(x for x in r.decisions.items(umo) if x['rule']=='CANCEL' and x['action']=='applied')
    r.undo_decision(rec['id'])
    assert r._appointments(umo)==original
