"""Agent 工作时间区间（services/agent_working_schedule.py）评估器回归测试。

纯函数测试，不需要 DB。所有 at 参数均为 naive UTC。
"""

from datetime import datetime

import pytest

from services.agent_working_schedule import (
    evaluate_working_window,
    is_in_working_window,
    next_working_window,
    normalize_working_schedule,
    validate_working_schedule,
)


def dt(*args):
    return datetime(*args)


SH = 'Asia/Shanghai'  # UTC+8，无夏令时


def _daily(start, end, tz=SH, **kw):
    window = {'type': 'daily', 'start_time': start, 'end_time': end}
    window.update(kw)
    return {'enabled': True, 'timezone': tz, 'includes': [window]}


# ────────────────────────── 基础语义 ──────────────────────────

def test_empty_or_disabled_schedule_always_in_window():
    assert is_in_working_window(None) is True
    assert is_in_working_window({}) is True
    assert is_in_working_window({'enabled': False, 'includes': []}, at=dt(2026, 9, 9, 3, 0)) is True
    # enabled + 空 includes/excludes = 全天候允许
    assert is_in_working_window({'enabled': True}, at=dt(2026, 9, 9, 3, 0)) is True


def test_fail_open_on_corrupt_schedule():
    """脏数据放行（fail-open），不阻断派发。"""
    assert is_in_working_window({'enabled': True, 'timezone': 'Not/Real', 'includes': 'oops'}) is True
    # start_time 解不开 → 求值异常 → 放行
    assert is_in_working_window({'enabled': True, 'timezone': 'UTC',
                                 'includes': [{'type': 'daily', 'start_time': 'bogus'}]}) is True
    assert next_working_window({'enabled': True, 'includes': [{'type': 'daily', 'start_time': 'bogus'}]}) is None


def test_daily_window_basic():
    schedule = _daily('22:00', '06:00')
    # Shanghai 22:30（UTC 14:30）在窗口内
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 14, 30)) is True
    # Shanghai 18:00（UTC 10:00）不在
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 10, 0)) is False
    # Shanghai 05:59（UTC 前一日 21:59）在（跨午夜前段）
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 21, 59)) is True
    # Shanghai 06:00（UTC 22:00）恰好出窗（左闭右开）
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 22, 0)) is False
    # Shanghai 22:00 整入窗
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 14, 0)) is True


def test_daily_full_day_window():
    schedule = _daily('00:00', '24:00')
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 0, 0)) is True
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 23, 59)) is True


def test_daily_without_times_means_whole_day():
    schedule = {'enabled': True, 'timezone': 'UTC', 'includes': [{'type': 'daily'}]}
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 13, 0)) is True
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 2, 0)) is True


def test_utc_default_timezone():
    schedule = _daily('09:00', '18:00', tz=None)
    schedule['timezone'] = 'UTC'
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 8, 59)) is False
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 9, 0)) is True
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 17, 59)) is True
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 18, 0)) is False


def test_timezone_conversion():
    # 同一时刻，UTC 窗口与 Shanghai 窗口结论不同
    utc_window = _daily('09:00', '18:00', tz='UTC')
    sh_window = _daily('09:00', '18:00', tz=SH)
    at = dt(2026, 9, 9, 2, 0)  # UTC 02:00 = Shanghai 10:00
    assert is_in_working_window(utc_window, at=at) is False
    assert is_in_working_window(sh_window, at=at) is True


# ────────────────────────── weekly ──────────────────────────

def test_weekly_window():
    # 周一 22:00 – 周二 06:00（Shanghai）
    schedule = {
        'enabled': True, 'timezone': SH,
        'includes': [{'type': 'weekly', 'days_of_week': [1], 'start_time': '22:00', 'end_time': '06:00'}],
    }
    # 2026-09-07 是周一。Shanghai 周一 23:00 = UTC 15:00 → 在
    assert is_in_working_window(schedule, at=dt(2026, 9, 7, 15, 0)) is True
    # Shanghai 周二 01:00 = UTC 周一 17:00 → 跨午夜凌晨归开始日（周一）→ 在
    assert is_in_working_window(schedule, at=dt(2026, 9, 7, 17, 0)) is True
    # Shanghai 周二 23:00 = UTC 15:00 → 不在（周二不在 days_of_week）
    assert is_in_working_window(schedule, at=dt(2026, 9, 8, 15, 0)) is False
    # Shanghai 周三 05:00 = UTC 周二 21:00 → 周二窗口未开启 → 不在
    assert is_in_working_window(schedule, at=dt(2026, 9, 8, 21, 0)) is False
    # Shanghai 周日 12:00 → 不在
    assert is_in_working_window(schedule, at=dt(2026, 9, 6, 4, 0)) is False


def test_weekly_workdays_nine_to_six():
    schedule = {
        'enabled': True, 'timezone': 'UTC',
        'includes': [{'type': 'weekly', 'days_of_week': [1, 2, 3, 4, 5], 'start_time': '09:00', 'end_time': '18:00'}],
    }
    # 2026-09-09 周三
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 10, 0)) is True
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 8, 0)) is False
    # 2026-09-12 周六
    assert is_in_working_window(schedule, at=dt(2026, 9, 12, 10, 0)) is False


# ────────────────────────── monthly ──────────────────────────

def test_monthly_window_positive_days():
    schedule = {
        'enabled': True, 'timezone': 'UTC',
        'includes': [{'type': 'monthly', 'days_of_month': [1, 15], 'start_time': '00:00', 'end_time': '24:00'}],
    }
    assert is_in_working_window(schedule, at=dt(2026, 9, 1, 10, 0)) is True
    assert is_in_working_window(schedule, at=dt(2026, 9, 15, 23, 59)) is True
    assert is_in_working_window(schedule, at=dt(2026, 9, 2, 10, 0)) is False


def test_monthly_negative_days_count_from_month_end():
    schedule = {
        'enabled': True, 'timezone': 'UTC',
        'includes': [{'type': 'monthly', 'days_of_month': [-1], 'start_time': '00:00', 'end_time': '24:00'}],
    }
    # 9 月 30 天：-1 = 30 号
    assert is_in_working_window(schedule, at=dt(2026, 9, 30, 12, 0)) is True
    assert is_in_working_window(schedule, at=dt(2026, 9, 29, 12, 0)) is False
    # 平年 2 月 28 天：-1 = 28 号；闰年 -1 = 29 号
    assert is_in_working_window(schedule, at=dt(2027, 2, 28, 12, 0)) is True
    assert is_in_working_window(schedule, at=dt(2028, 2, 29, 12, 0)) is True
    # -2 = 月末倒数第二天
    schedule2 = {
        'enabled': True, 'timezone': 'UTC',
        'includes': [{'type': 'monthly', 'days_of_month': [-2], 'start_time': '00:00', 'end_time': '24:00'}],
    }
    assert is_in_working_window(schedule2, at=dt(2026, 9, 29, 12, 0)) is True


def test_monthly_day_31_only_matches_31_day_months():
    schedule = {
        'enabled': True, 'timezone': 'UTC',
        'includes': [{'type': 'monthly', 'days_of_month': [31], 'start_time': '00:00', 'end_time': '24:00'}],
    }
    assert is_in_working_window(schedule, at=dt(2026, 10, 31, 12, 0)) is True   # 10 月有 31 号
    assert is_in_working_window(schedule, at=dt(2026, 9, 30, 12, 0)) is False   # 9 月没有（不 clamp）


# ────────────────────────── dates / months / 界限 ──────────────────────────

def test_dates_window_with_time_range():
    schedule = {
        'enabled': True, 'timezone': 'UTC',
        'includes': [{
            'type': 'dates', 'start_date': '2026-09-01', 'end_date': '2026-09-10',
            'start_time': '09:00', 'end_time': '18:00',
        }],
    }
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 10, 0)) is True
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 20, 0)) is False
    assert is_in_working_window(schedule, at=dt(2026, 9, 11, 10, 0)) is False
    assert is_in_working_window(schedule, at=dt(2026, 9, 1, 9, 0)) is True   # 起始日含
    assert is_in_working_window(schedule, at=dt(2026, 9, 10, 17, 59)) is True  # 结束日含


def test_dates_window_cross_midnight():
    schedule = {
        'enabled': True, 'timezone': 'UTC',
        'includes': [{
            'type': 'dates', 'start_date': '2026-09-09', 'end_date': '2026-09-09',
            'start_time': '22:00', 'end_time': '06:00',
        }],
    }
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 23, 0)) is True
    assert is_in_working_window(schedule, at=dt(2026, 9, 10, 5, 0)) is True   # 次日凌晨属开始日
    assert is_in_working_window(schedule, at=dt(2026, 9, 10, 6, 0)) is False
    assert is_in_working_window(schedule, at=dt(2026, 9, 10, 22, 0)) is False  # 结束日已过


def test_months_filter():
    schedule = {
        'enabled': True, 'timezone': 'UTC',
        'includes': [{'type': 'daily', 'start_time': '00:00', 'end_time': '24:00', 'months': [12, 1]}],
    }
    assert is_in_working_window(schedule, at=dt(2026, 12, 25, 10, 0)) is True
    assert is_in_working_window(schedule, at=dt(2027, 1, 1, 10, 0)) is True
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 10, 0)) is False


def test_date_bounds_on_recurring_window():
    schedule = {
        'enabled': True, 'timezone': 'UTC',
        'includes': [{
            'type': 'daily', 'start_time': '00:00', 'end_time': '24:00',
            'start_date': '2026-09-05', 'end_date': '2026-09-15',
        }],
    }
    assert is_in_working_window(schedule, at=dt(2026, 9, 4, 23, 0)) is False
    assert is_in_working_window(schedule, at=dt(2026, 9, 5, 0, 0)) is True
    assert is_in_working_window(schedule, at=dt(2026, 9, 15, 23, 0)) is True
    assert is_in_working_window(schedule, at=dt(2026, 9, 16, 0, 0)) is False


# ────────────────────────── includes / excludes 组合 ──────────────────────────

def test_exclude_carves_out_of_include():
    schedule = {
        'enabled': True, 'timezone': 'UTC',
        'includes': [{'type': 'daily', 'start_time': '00:00', 'end_time': '24:00'}],
        'excludes': [{'type': 'daily', 'start_time': '12:00', 'end_time': '14:00', 'label': '会议时间'}],
    }
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 11, 59)) is True
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 12, 0)) is False
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 13, 59)) is False
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 14, 0)) is True


def test_exclude_without_includes_blocks_from_always_allowed():
    schedule = {
        'enabled': True, 'timezone': SH,
        'excludes': [{'type': 'daily', 'start_time': '22:00', 'end_time': '06:00'}],
    }
    # Shanghai 23:00 = UTC 15:00 → 排除中
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 15, 0)) is False
    # Shanghai 12:00 = UTC 04:00 → 允许
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 4, 0)) is True


def test_disabled_window_is_ignored():
    schedule = {
        'enabled': True, 'timezone': 'UTC',
        'includes': [{'type': 'daily', 'start_time': '00:00', 'end_time': '01:00', 'enabled': False}],
    }
    # 唯一 include 被禁用 → 等价无 includes → 全天候
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 12, 0)) is True


def test_multiple_includes_union():
    schedule = {
        'enabled': True, 'timezone': 'UTC',
        'includes': [
            {'type': 'daily', 'start_time': '08:00', 'end_time': '10:00'},
            {'type': 'daily', 'start_time': '16:00', 'end_time': '18:00'},
        ],
    }
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 9, 0)) is True
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 12, 0)) is False
    assert is_in_working_window(schedule, at=dt(2026, 9, 9, 17, 0)) is True


# ────────────────────────── next_working_window ──────────────────────────

def test_next_window_daily():
    schedule = _daily('22:00', '06:00')
    # UTC 10:00 = Shanghai 18:00，窗口未开 → 下一个开始 = Shanghai 22:00 = UTC 14:00
    nxt = next_working_window(schedule, after=dt(2026, 9, 9, 10, 0))
    assert nxt == datetime(2026, 9, 9, 14, 0)
    # 已在窗口内 → 下一次开窗 = 明日 Shanghai 22:00
    nxt2 = next_working_window(schedule, after=dt(2026, 9, 9, 15, 0))
    assert nxt2 == datetime(2026, 9, 10, 14, 0)


def test_next_window_after_exclude_ends():
    schedule = {
        'enabled': True, 'timezone': 'UTC',
        'includes': [{'type': 'daily', 'start_time': '00:00', 'end_time': '24:00'}],
        'excludes': [{'type': 'daily', 'start_time': '12:00', 'end_time': '14:00'}],
    }
    # 13:00 被 exclude 挡住 → exclude 结束的 14:00 立刻恢复
    assert next_working_window(schedule, after=dt(2026, 9, 9, 13, 0)) == datetime(2026, 9, 9, 14, 0)


def test_next_window_none_when_never():
    schedule = {
        'enabled': True, 'timezone': 'UTC',
        'includes': [{'type': 'dates', 'start_date': '2020-01-01', 'end_date': '2020-01-02'}],
    }
    assert next_working_window(schedule, after=dt(2026, 9, 9, 10, 0)) is None


def test_next_window_none_when_disabled():
    assert next_working_window({'enabled': False}, after=dt(2026, 9, 9, 10, 0)) is None


def test_next_window_weekly_boundary():
    schedule = {
        'enabled': True, 'timezone': 'UTC',
        'includes': [{'type': 'weekly', 'days_of_week': [1], 'start_time': '09:00', 'end_time': '18:00'}],
    }
    # 2026-09-09 周三 → 下一个周一是 2026-09-14 09:00 UTC
    assert next_working_window(schedule, after=dt(2026, 9, 9, 10, 0)) == datetime(2026, 9, 14, 9, 0)


# ────────────────────────── 校验 / 规范化 ──────────────────────────

def test_validate_ok_schedule():
    schedule = {
        'enabled': True, 'timezone': SH,
        'includes': [{'type': 'weekly', 'days_of_week': [1, 2], 'start_time': '09:00', 'end_time': '18:00'}],
        'excludes': [{'type': 'monthly', 'days_of_month': [-1], 'label': '月末结算'}],
    }
    assert validate_working_schedule(schedule) == []


@pytest.mark.parametrize('bad', [
    {'enabled': True, 'includes': [{'type': 'hourly'}]},
    {'enabled': True, 'includes': [{'type': 'weekly'}]},                                  # 缺 days_of_week
    {'enabled': True, 'includes': [{'type': 'monthly', 'days_of_month': [0]}]},           # 0 非法
    {'enabled': True, 'includes': [{'type': 'monthly'}]},                                 # 缺 days_of_month
    {'enabled': True, 'includes': [{'type': 'daily', 'start_time': '25:00'}]},            # 小时越界
    {'enabled': True, 'includes': [{'type': 'daily', 'start_time': 'x'}]},                # 格式错
    {'enabled': True, 'includes': [{'type': 'daily', 'start_time': '10:00', 'end_time': '10:00'}]},  # 零长度
    {'enabled': True, 'includes': [{'type': 'dates', 'start_date': '2026-01-01'}]},       # 缺 end_date
    {'enabled': True, 'includes': [{'type': 'dates', 'start_date': '2026-02-01', 'end_date': '2026-01-01'}]},  # 起晚于止
    {'enabled': True, 'includes': [{'type': 'daily', 'months': [13]}]},                   # 月份越界
    {'enabled': True, 'includes': [{'type': 'weekly', 'days_of_week': [1, 1]}]},          # 重复
    {'enabled': True, 'includes': [{'type': 'daily', 'label': 'x' * 101}]},               # label 过长
    {'enabled': True, 'timezone': 'Mars/Olympus'},                                        # 未知时区
    {'enabled': True, 'includes': 'not-a-list'},
    'not-a-dict',
])
def test_validate_rejects_bad_schedules(bad):
    assert validate_working_schedule(bad) != []


def test_normalize_fills_defaults():
    normalized = normalize_working_schedule({'enabled': True, 'includes': [{'type': 'daily'}]})
    assert normalized['timezone'] == 'UTC'
    assert normalized['includes'][0]['enabled'] is True
    assert 'excludes' in normalized
    # disabled 开关缺省 = 关闭（不限制）
    assert normalize_working_schedule({})['enabled'] is False


def test_evaluate_working_window_shape():
    result = evaluate_working_window(_daily('22:00', '06:00'), at=dt(2026, 9, 9, 10, 0))
    assert result['enabled'] is True
    assert result['in_window'] is False
    assert result['next_window_at'] == '2026-09-09T14:00:00'
    assert result['timezone'] == SH

    result2 = evaluate_working_window(_daily('22:00', '06:00'), at=dt(2026, 9, 9, 15, 0))
    assert result2['in_window'] is True
    assert result2['next_window_at'] is None
