"""
Agent 工作时间区间（Working Windows）

让 Agent 只在被允许的时间段内接收新任务。典型场景：所依赖的 LLM API 在夜间
免费/折扣时段才值得跑，或 Agent 只应在工时内工作。

数据形态（Agent.working_schedule JSON 列）::

    {
        "enabled": true,                 # false 或缺省 = 不限制（全天候工作）
        "timezone": "Asia/Shanghai",     # IANA 时区名，缺省 UTC
        "includes": [ <时间窗>... ],     # 允许时段的并集；为空 = 全天候允许（再由 excludes 削减）
        "excludes": [ <时间窗>... ]      # 从允许时段中扣除，优先级高于 includes
    }

时间窗（window）::

    {
        "label": "夜间免费时段",          # 可选说明
        "enabled": true,                 # 单窗开关，缺省 true
        "type": "daily" | "weekly" | "monthly" | "dates",
        "start_time": "22:00",           # 当地墙钟 HH:MM（dates 类型可省略 = 整天）
        "end_time": "06:00",             # 允许 "24:00"；<= start_time 视为跨午夜
        "days_of_week": [1, 2, 3, 4, 5], # weekly 必填：1=周一 … 7=周日
        "days_of_month": [1, 15, -1],    # monthly 必填：1-31；负数从月末倒数（-1=最后一天）
        "months": [1, 2, 12],            # 可选：限定月份 1-12
        "start_date": "2026-09-01",      # dates 必填；循环类型可选的生效起始日（含）
        "end_date": "2026-12-31"         # dates 必填；循环类型可选的失效日（含）
    }

语义：
- 求值时刻先换算到 schedule.timezone 的当地墙钟时间再匹配；
- in_window = (includes 为空 ? True : 任一 include 命中) and 非任一 exclude 命中；
- 跨午夜窗口的凌晨部分归属于「开始日」的星期/月日/月份约束；
- 门禁只拦新任务派发，正在执行中的任务不受影响（由各调用点保证）。
"""

import calendar
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

WINDOW_TYPES = ('daily', 'weekly', 'monthly', 'dates')
MAX_WINDOWS = 50
# next_working_window 向前扫描的天数上限
HORIZON_DAYS = 370

DAY_NAMES = {1: '周一', 2: '周二', 3: '周三', 4: '周四', 5: '周五', 6: '周六', 7: '周日'}


# ────────────────────────── 校验 / 规范化 ──────────────────────────

def _err(window_index, message):
    return f"窗口#{window_index + 1}: {message}" if window_index is not None else message


def _parse_hhmm(value, field, window_index=None, allow_24=False):
    if not isinstance(value, str):
        raise ValueError(_err(window_index, f"{field} 必须是 HH:MM 字符串"))
    parts = value.split(':')
    if len(parts) != 2:
        raise ValueError(_err(window_index, f"{field} 必须是 HH:MM 格式，收到 {value!r}"))
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(_err(window_index, f"{field} 必须是 HH:MM 格式，收到 {value!r}"))
    max_hour = 24 if allow_24 else 23
    if not (0 <= hour <= max_hour and 0 <= minute <= 59):
        raise ValueError(_err(window_index, f"{field} 超出范围：{value!r}"))
    if hour == 24 and (minute != 0 or not allow_24):
        raise ValueError(_err(window_index, f"{field} 只允许 24:00 表示当日结束，收到 {value!r}"))
    return hour * 60 + minute


def _parse_date(value, field, window_index=None):
    if not isinstance(value, str):
        raise ValueError(_err(window_index, f"{field} 必须是 YYYY-MM-DD 字符串"))
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(_err(window_index, f"{field} 不是合法日期：{value!r}"))


def _parse_int_list(value, field, window_index=None, lo=None, hi=None,
                    allow_negative=False):
    if not isinstance(value, list) or not value:
        raise ValueError(_err(window_index, f"{field} 必须是非空数组"))
    out = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError(_err(window_index, f"{field} 必须是整数数组，收到 {item!r}"))
        if allow_negative and item < 0:
            if item < -31:
                raise ValueError(_err(window_index, f"{field} 负数只允许 -31..-1，收到 {item}"))
            out.append(item)
            continue
        if lo is not None and item < lo or hi is not None and item > hi:
            raise ValueError(_err(window_index, f"{field} 允许范围 {lo}..{hi}，收到 {item}"))
        out.append(item)
    if len(set(out)) != len(out):
        raise ValueError(_err(window_index, f"{field} 存在重复值"))
    return sorted(out)


def _normalize_window(raw, index):
    if not isinstance(raw, dict):
        raise ValueError(_err(index, "时间窗必须是对象"))
    wtype = raw.get('type')
    if wtype not in WINDOW_TYPES:
        raise ValueError(_err(index, f"type 必须是 {'/'.join(WINDOW_TYPES)}，收到 {wtype!r}"))

    window = {
        'type': wtype,
        'enabled': raw.get('enabled', True) is not False,
    }
    label = raw.get('label')
    if label is not None:
        if not isinstance(label, str) or len(label) > 100:
            raise ValueError(_err(index, "label 必须是 ≤100 字符的字符串"))
        window['label'] = label

    has_time = 'start_time' in raw or 'end_time' in raw
    if wtype == 'dates' or has_time:
        start_md = _parse_hhmm(raw.get('start_time', '00:00'), 'start_time', index)
        end_md = _parse_hhmm(raw.get('end_time', '24:00'), 'end_time', index, allow_24=True)
        if start_md == 1440:
            raise ValueError(_err(index, "start_time 不能是 24:00"))
        if start_md == end_md:
            raise ValueError(_err(index, "start_time 与 end_time 相同，时间窗长度为 0"))
        window['start_time'] = raw.get('start_time', '00:00')
        window['end_time'] = raw.get('end_time', '24:00')

    if wtype == 'weekly':
        window['days_of_week'] = _parse_int_list(
            raw.get('days_of_week'), 'days_of_week', index, lo=1, hi=7)
    elif wtype == 'monthly':
        window['days_of_month'] = _parse_int_list(
            raw.get('days_of_month'), 'days_of_month', index, lo=1, hi=31, allow_negative=True)

    if 'months' in raw and raw['months'] is not None:
        window['months'] = _parse_int_list(raw['months'], 'months', index, lo=1, hi=12)

    start_date_raw, end_date_raw = raw.get('start_date'), raw.get('end_date')
    if wtype == 'dates' and (start_date_raw is None or end_date_raw is None):
        raise ValueError(_err(index, "dates 类型必须提供 start_date 与 end_date"))
    if start_date_raw is not None:
        window['start_date'] = start_date_raw
    if end_date_raw is not None:
        window['end_date'] = end_date_raw
    if start_date_raw is not None and end_date_raw is not None:
        if _parse_date(start_date_raw, 'start_date', index) > _parse_date(end_date_raw, 'end_date', index):
            raise ValueError(_err(index, "start_date 晚于 end_date"))

    return window


def validate_working_schedule(raw):
    """校验 working_schedule，返回错误列表（空列表 = 合法）。"""
    try:
        normalize_working_schedule(raw)
        return []
    except ValueError as e:
        return [str(e)]


def normalize_working_schedule(raw):
    """校验并规范化 working_schedule；不合法抛 ValueError。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("working_schedule 必须是对象")

    schedule = {
        'enabled': raw.get('enabled') is True,
        'timezone': raw.get('timezone') or 'UTC',
    }
    zone = schedule['timezone']
    try:
        ZoneInfo(zone)
    except Exception:
        raise ValueError(f"未知时区：{zone!r}（需 IANA 名称，如 Asia/Shanghai）")

    for key in ('includes', 'excludes'):
        windows = raw.get(key) or []
        if not isinstance(windows, list):
            raise ValueError(f"{key} 必须是数组")
        if len(windows) > MAX_WINDOWS:
            raise ValueError(f"{key} 最多 {MAX_WINDOWS} 个时间窗")
        schedule[key] = [_normalize_window(w, i) for i, w in enumerate(windows)]
    return schedule


# ────────────────────────── 求值 ──────────────────────────

def _get_zone(tz_name):
    try:
        return ZoneInfo(tz_name or 'UTC')
    except Exception:
        return ZoneInfo('UTC')


def _utc_to_local(naive_utc, zone):
    return naive_utc.replace(tzinfo=timezone.utc).astimezone(zone).replace(tzinfo=None)


def _local_to_utc(naive_local, zone):
    return naive_local.replace(tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)


def _minutes(value):
    """已规范化窗口的 HH:MM 字符串 → 当日分钟数；'24:00' → 1440。"""
    hour, minute = value.split(':')
    return int(hour) * 60 + int(minute)


def _days_in_month(year, month):
    return calendar.monthrange(year, month)[1]


def _day_matches_list(day, values):
    """day（当月第几天）是否命中 days_of_month（支持负数从月末倒数）。"""
    days_in_month = _days_in_month(day.year, day.month)
    for v in values:
        if v > 0:
            if day.day == v:
                return True
        else:
            if day.day == days_in_month + v + 1:
                return True
    return False


def _window_day_matches(window, day):
    """窗口的「日级」约束（星期/月日/月份/日期界限）是否命中某个当地日期。"""
    if window.get('enabled') is False:
        return False

    # 日期有效性界限（对循环类型是可选的生效区间）
    start_date = window.get('start_date')
    end_date = window.get('end_date')
    if start_date and day < date.fromisoformat(start_date):
        return False
    if end_date and day > date.fromisoformat(end_date):
        return False

    months = window.get('months')
    if months and day.month not in months:
        return False

    wtype = window['type']
    if wtype == 'weekly':
        return day.isoweekday() in window['days_of_week']
    if wtype == 'monthly':
        return _day_matches_list(day, window['days_of_month'])
    if wtype == 'dates':
        return True  # 界限已在上面判断
    return True  # daily


def _window_time_range(window):
    """返回 (start_md, end_md)；dates 类型未配置时间时返回 (0, 1440) 整天。"""
    if window['type'] == 'dates' and 'start_time' not in window:
        return 0, 1440
    start_md = _minutes(window.get('start_time', '00:00'))
    end_md = _minutes(window.get('end_time', '24:00'))
    if start_md == 0 and end_md == 1440:
        return start_md, end_md
    return start_md, end_md


def _window_matches_at(window, local_dt):
    """某个时间窗在当地墙钟时刻 local_dt 是否命中。"""
    if window.get('enabled') is False:
        return False
    start_md, end_md = _window_time_range(window)
    minute_of_day = local_dt.hour * 60 + local_dt.minute

    # 跨午夜窗口的凌晨部分归属于开始日
    day = local_dt.date()
    if end_md <= start_md and not (start_md == 0 and end_md == 1440):
        if minute_of_day < end_md:
            day = day - timedelta(days=1)

    if not _window_day_matches(window, day):
        return False

    if start_md == 0 and end_md == 1440:
        return True
    if end_md <= start_md:  # 跨午夜
        return minute_of_day >= start_md or minute_of_day < end_md
    return start_md <= minute_of_day < end_md


def _enabled_windows(schedule, key):
    return [w for w in (schedule.get(key) or []) if w.get('enabled') is not False]


def is_in_working_window(schedule, at=None):
    """时刻 at（naive UTC）是否在该 Agent 的工作时间区间内。

    schedule 为空 / 未启用 / 数据异常时一律放行（fail-open，避免脏数据
    中断正常派发；写入路径已有严格校验）。
    """
    try:
        return _is_in_working_window(schedule or {}, at)
    except Exception:
        return True


def _is_in_working_window(schedule, at):
    if not schedule.get('enabled'):
        return True
    zone = _get_zone(schedule.get('timezone'))
    local_dt = _utc_to_local(at or datetime.utcnow(), zone)

    excludes = _enabled_windows(schedule, 'excludes')
    for window in excludes:
        if _window_matches_at(window, local_dt):
            return False

    includes = _enabled_windows(schedule, 'includes')
    if not includes:
        return True
    return any(_window_matches_at(w, local_dt) for w in includes)


def _iter_occurrences(window, first_day, days):
    """生成某时间窗在 [first_day, first_day+days) 内的
    (当地开始日, 当地开始时间, 当地结束时间) 出现序列（结束可能跨到次日）。"""
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        if not _window_day_matches(window, day):
            continue
        start_md, end_md = _window_time_range(window)
        start_dt = datetime.combine(day, datetime.min.time()) + timedelta(minutes=start_md)
        if start_md == 0 and end_md == 1440:
            end_dt = start_dt + timedelta(days=1)
        elif end_md <= start_md:
            end_dt = datetime.combine(day + timedelta(days=1), datetime.min.time()) + timedelta(minutes=end_md)
        else:
            end_dt = datetime.combine(day, datetime.min.time()) + timedelta(minutes=end_md)
        yield day, start_dt, end_dt


def next_working_window(schedule, after=None):
    """下一个进入工作区间的时刻（naive UTC）；已在区间内或永远不会进入则返回 None。

    只扫描未来 HORIZON_DAYS 天；in-window 状态只会在「include 开始」或
    「exclude 结束」两个边界翻转，因此逐边界检查即得精确解。
    """
    try:
        return _next_working_window(schedule or {}, after)
    except Exception:
        return None


def _next_working_window(schedule, after):
    if not schedule.get('enabled'):
        return None
    zone = _get_zone(schedule.get('timezone'))
    after = after or datetime.utcnow()
    after_local = _utc_to_local(after, zone)
    first_day = after_local.date() - timedelta(days=1)  # 回溯一天，接住跨午夜窗口

    includes = _enabled_windows(schedule, 'includes')
    excludes = _enabled_windows(schedule, 'excludes')

    candidates = set()
    for window in includes:
        for _, start_local, _ in _iter_occurrences(window, first_day, HORIZON_DAYS):
            candidates.add(_local_to_utc(start_local, zone))
    # exclude 结束点同样是 in-window 可能翻转的边界（include 仍在覆盖时恢复放行）
    for window in excludes:
        for _, _, end_local in _iter_occurrences(window, first_day, HORIZON_DAYS):
            candidates.add(_local_to_utc(end_local, zone))

    for start_utc in sorted(candidates):
        if start_utc > after and _is_in_working_window(schedule, start_utc):
            return start_utc
    return None


def evaluate_working_window(schedule, at=None):
    """综合求值，供 API / runtime 使用。返回::

        {
            "enabled": bool,
            "in_window": bool,
            "next_window_at": "2026-09-09T22:00:00" | None,  # naive UTC ISO
            "timezone": "Asia/Shanghai",
        }
    """
    schedule = schedule or {}
    at = at or datetime.utcnow()
    enabled = bool(schedule.get('enabled'))
    in_window = is_in_working_window(schedule, at)
    next_at = None
    if enabled and not in_window:
        next_at = next_working_window(schedule, at)
    return {
        'enabled': enabled,
        'in_window': in_window,
        'next_window_at': next_at.isoformat() if next_at else None,
        'timezone': schedule.get('timezone') or 'UTC',
    }
