"""Extended appointment audit: malformed clocks and exact-target cancellation."""
from datetime import datetime

import pytest

from astrbot_plugin_mitchell_minichat.core.appointment import find_clock


@pytest.mark.parametrize('text', [
    '差一刻二十五点开会', '差五分二十六点开会', '差一刻二十八点开会',
    '明天下午差一刻二十五点开会', '明天差十五分二十七点到家',
    '明天差九十九分五点开会', '明天差六十分五点开会',
])
def test_impossible_reverse_clock_cannot_wrap_into_valid_hour(plugin, umo, clock, text):
    plugin.runner.update_from_message(umo, text)
    assert not plugin.runner._appointments(umo), {
        'text': text, 'clock': find_clock(text), 'actual': plugin.runner._appointments(umo),
    }
    assert plugin.store.get(umo, 'active_event') is None


@pytest.mark.parametrize('text,expected', [
    ('明天下午差一刻五点开会', datetime(2026, 11, 3, 16, 45)),
    ('明天上午差五分十一点到家', datetime(2026, 11, 3, 10, 55)),
    ('今晚差一刻十二点到家', datetime(2026, 11, 2, 23, 45)),
    ('今晚差一刻零点到家', datetime(2026, 11, 2, 23, 45)),
    ('今晚差五分零点到家', datetime(2026, 11, 2, 23, 55)),
    ('明晚差五分零点到家', datetime(2026, 11, 3, 23, 55)),
    ('明天凌晨差一刻零点到家', datetime(2026, 11, 2, 23, 45)),
    ('明天下午差一刻一点到家', datetime(2026, 11, 3, 12, 45)),
    ('明天晚上差一刻六点到家', datetime(2026, 11, 3, 17, 45)),
])
def test_valid_reverse_clock_keeps_expected_date(plugin, umo, clock, text, expected):
    plugin.runner.update_from_message(umo, text)
    assert [a['at'] for a in plugin.runner._appointments(umo)] == [expected.timestamp()]


@pytest.mark.parametrize('text', [
    '11月3日（周三）下午三点开会',
    '明天（周三）下午三点开会',
    '明天11月4日下午三点开会',
    '2027年11月3日周二下午三点开会',
    '明天是周三下午三点开会',
])
def test_conflicting_date_descriptions_do_not_guess(plugin, umo, clock, text):
    plugin.runner.update_from_message(umo, text)
    assert not plugin.runner._appointments(umo), (text, plugin.runner._appointments(umo))


@pytest.mark.parametrize('text', [
    '明天下午三点开会取消失败了',
    '明天下午三点开会取消不了',
    '我妈明天下午三点开会取消了',
    '他说“明天下午三点开会取消了”',
    '明天下午三点的音乐会取消了',
    '后天下午三点的会议取消了',
    '明天上午三点的会议取消了',
])
def test_cancellation_boundary_preserves_real_appointment(plugin, umo, clock, text):
    r = plugin.runner
    r.update_from_message(umo, '明天下午三点开会')
    before = r._appointments(umo)
    r.update_from_message(umo, text)
    assert r._appointments(umo) == before, (text, r._appointments(umo))


@pytest.mark.parametrize('text', [
    '我明天不打算下午三点开会', '我没说我明天下午三点开会',
    '我从来没说过明天下午三点开会', '我明天下午三点开会不成了',
])
def test_negated_plans_and_disclaimed_claims_are_not_appointments(plugin, umo, clock, text):
    plugin.runner.update_from_message(umo, text)
    assert not plugin.runner._appointments(umo)


@pytest.mark.parametrize('text', [
    '我明天不吃午饭，下午三点开会', '我不想迟到，明天下午三点开会',
])
def test_negation_in_another_clause_does_not_block_valid_plan(plugin, umo, clock, text):
    plugin.runner.update_from_message(umo, text)
    assert len(plugin.runner._appointments(umo)) == 1


def test_invalid_reverse_clock_does_not_block_later_valid_plan(plugin, umo, clock):
    plugin.runner.update_from_message(umo, '明天下午差一刻二十五点开会，后天下午三点到家')
    items = plugin.runner._appointments(umo)
    assert len(items) == 1 and items[0]['at'] == datetime(2026, 11, 4, 15).timestamp()


@pytest.mark.parametrize('text', ['今晚差一刻零点到家', '明天凌晨差一刻零点到家'])
def test_reverse_midnight_cancellation_matches_registered_timestamp(plugin, umo, clock, text):
    runner = plugin.runner
    runner.update_from_message(umo, text)
    assert len(runner._appointments(umo)) == 1
    runner.update_from_message(umo, text + '的安排取消了')
    assert not runner._appointments(umo)
