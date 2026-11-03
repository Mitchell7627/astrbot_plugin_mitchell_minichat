from datetime import datetime
from unittest.mock import AsyncMock
import pytest

@pytest.mark.parametrize('text',[
 '明早八点叫醒我','明早八点叫我起床','明早八点喊我起床','明早八点提醒我起床',
 '明早八点把我叫醒','明早八点叫醒我一下','明早八点叫醒一下我',
 '明早八点记得叫醒我','明早八点叫醒我哦','明早八点叫醒我就行',
 '明早八点再起来','明天早上八点起床',
])
def test_wake_variants(plugin,umo,clock,text):
 clock.now=datetime(2026,11,3,22).timestamp()
 plugin.runner.update_from_message(umo,text)
 aps=plugin.runner._appointments(umo)
 assert len(aps)==1 and aps[0]['kind']=='wake' and aps[0]['at']==datetime(2026,11,4,8).timestamp(),{'text':text,'actual':aps}

@pytest.mark.parametrize('text',[
 '明早八点不要叫醒我','明早八点不用叫醒我','明早八点别喊我起床',
 '明早八点叫醒他','明早八点叫我妈起床','明早八点让室友起来',
 '他说让我明早八点叫醒他','如果明早八点起床就来得及','明早八点起床吗',
 '明早八点站起来','明早八点想起来','明早八点好起来',
 '明早八点叫醒我或者九点叫醒我',
 '明早八点把我妈叫醒','明早八点把他叫醒',
])
def test_wake_negative(plugin,umo,clock,text):
 clock.now=datetime(2026,11,3,22).timestamp()
 plugin.runner.update_from_message(umo,text)
 assert not plugin.runner._appointments(umo),{'text':text,'actual':plugin.runner._appointments(umo)}

@pytest.mark.parametrize('now,expected',[(datetime(2026,11,3,22),datetime(2026,11,4,6)),(datetime(2026,11,3,5),datetime(2026,11,3,6)),(datetime(2026,11,3,6),datetime(2026,11,3,18))])
def test_nearest_wake(plugin,umo,clock,now,expected):
 clock.now=now.timestamp();plugin.runner.update_from_message(umo,'六点再起来')
 assert plugin.runner._appointments(umo)[0]['at']==expected.timestamp()

@pytest.mark.parametrize('cancel',['明早八点的叫醒取消','不用明早八点叫醒我了','明早八点不要叫醒我了','明早八点起床取消'])
def test_cancel_wake_preserves_meeting(plugin,umo,clock,cancel):
 clock.now=datetime(2026,11,3,22).timestamp();r=plugin.runner
 r.update_from_message(umo,'明早八点叫醒我');r.update_from_message(umo,'明天下午三点开会')
 assert len(r._appointments(umo))==2
 r.update_from_message(umo,cancel)
 assert [a['kind'] for a in r._appointments(umo)]==['start'],{'cancel':cancel,'actual':r._appointments(umo)}

@pytest.mark.parametrize('failure',['empty','send_false','generate_error'])
async def test_failed_wake_does_not_end_sleep(plugin,umo,clock,monkeypatch,failure):
 clock.now=datetime(2026,11,3,16,37).timestamp();r=plugin.runner
 plugin.engine_cfg['sleep_quiet_hours']=4
 r.update_from_message(umo,'我去睡觉了');r.update_from_message(umo,'晚上六点叫醒我')
 assert r.is_sleeping(umo)
 clock.now=datetime(2026,11,3,18).timestamp()
 model=AsyncMock(return_value='' if failure=='empty' else '该起床啦')
 if failure=='generate_error':model.side_effect=RuntimeError('synthetic model outage')
 monkeypatch.setattr(r,'_generate',model)
 if failure=='send_false':plugin.context.send_message.return_value=False
 result=await r._fire(umo)
 assert not result['sent']
 assert r.is_sleeping(umo),{'failure':failure,'result':result,'sleep':r._sleep_state(umo)}

async def test_wake_one_shot_with_follow_enabled(plugin,umo,clock,monkeypatch):
 clock.now=datetime(2026,11,3,16).timestamp();r=plugin.runner
 r.update_from_message(umo,'晚上六点叫醒我')
 monkeypatch.setattr(r,'_generate',AsyncMock(return_value='起床啦'))
 clock.now=datetime(2026,11,3,18).timestamp();result=await r._fire(umo)
 assert result['sent']
 clock.now+=600
 assert r.appointment_shot_item(umo) is None

@pytest.mark.parametrize('text',[
 '三天后下午三点开会','15天后下午三点开会','明年11月4日下午三点开会',
 '明天下午三点开会，或者后天下午四点开会',
])
def test_date_safety(plugin,umo,clock,text):
 clock.now=datetime(2026,11,3,10).timestamp();r=plugin.runner;r.update_from_message(umo,text)
 aps=r._appointments(umo)
 if text.startswith('三天后'):assert len(aps)==1 and aps[0]['at']==datetime(2026,11,6,15).timestamp(),aps
 else:assert not aps,{'text':text,'actual':aps}
@pytest.mark.parametrize('text',[
 '那个会改到四点','那个会改到下午四点','那个会改到明天下午四点',
 '改到明天下午四点吧','改成明天下午四点','推迟到明天下午四点',
])
def test_cross_turn_reschedule(plugin,umo,clock,text):
 clock.now=datetime(2026,11,3,10).timestamp()
 r=plugin.runner;r.update_from_message(umo,'明天下午三点开会')
 assert len(r._appointments(umo))==1
 r.update_from_message(umo,text)
 actual=[datetime.fromtimestamp(x['at']).isoformat() for x in r._appointments(umo)]
 assert actual==['2026-11-04T16:00:00'],{'text':text,'actual':actual}
