"""Reproduce state conflicts visible in the supplied 2026-11-02 log."""
import asyncio
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

import pytest

from test_minichat import runner, UMO
from test_semantics_v028 import generation


@contextmanager
def log_time(hour, minute=0, second=0):
    moment = datetime(2026, 11, 2, hour, minute, second)
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz is None else moment.astimezone(tz)
    with patch('core.runner.datetime', Frozen), patch('core.runner.time.time', return_value=moment.timestamp()):
        yield moment.timestamp()


def prepare_class(runner):
    with log_time(16, 36, 10):
        runner.update_from_message(UMO, '可是还要上课')
        assert runner.store.get(UMO, 'active_event', {}).get('name') == 'study'
        runner.update_from_message(UMO, '得到晚上6点下课呢')
        assert runner._appointments(UMO)[0]['label'] == '下课'


def test_explicit_class_end_and_current_event_share_end_time(runner):
    with log_time(15, 10, 15):
        runner.update_from_message(UMO, '上课啦')
    with log_time(16, 20, 40):
        runner.update_from_message(UMO, '得到晚上6点下课呢')
        expected = datetime(2026, 11, 2, 18).timestamp()
        event = runner.store.get(UMO, 'active_event')
        assert event['expires_at'] == expected, event


def test_sending_class_end_appointment_finishes_matching_activity(runner):
    async def scenario():
        prepare_class(runner)
        provider = generation(runner)
        with log_time(18):
            await runner._fire(UMO)
            provider.text_chat.assert_awaited_once()
            assert runner._appointments(UMO)[0]['due_sent_at']
            assert runner.store.get(UMO, 'active_event') is None, runner.store.get(UMO, 'active_event')
    asyncio.run(scenario())


def test_class_end_prompt_does_not_also_claim_current_study(runner):
    prepare_class(runner)
    with log_time(18):
        hint = runner.appointment_hint(UMO, 'due')
        assert '在学习' not in hint, hint


@pytest.mark.parametrize('begin,end', [
    ('我去上课了','都已经下课了'),
    ('我去上班了','下班了'),
    ('我出门去开会了','散会了'),
])
def test_period_end_does_not_invent_physical_return(runner, begin, end):
    with log_time(17, 30):
        runner.update_from_message(UMO, begin)
        assert runner.store.get(UMO, 'away', {}).get('active')
    with log_time(18):
        runner.update_from_message(UMO, end)
        hint = runner.event_hint(UMO)
        assert '回到你这边' not in hint and '刚到家' not in hint, hint


def test_explicit_home_arrival_can_claim_return(runner):
    with log_time(17, 30):
        runner.update_from_message(UMO, '我去上课了')
    with log_time(18):
        runner.update_from_message(UMO, '我到家了')
        assert '回到你这边' in runner.event_hint(UMO)
