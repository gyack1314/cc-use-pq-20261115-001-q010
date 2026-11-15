import hashlib
import json
import re
from datetime import date, datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import require


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def identifier(value, field="id"):
    require(isinstance(value, str), "invalid_id", f"{field}必须是字符串")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", value) is not None,
            "invalid_id", f"{field}格式无效")
    return value


def text(value, field="名称", maximum=200):
    require(isinstance(value, str), "invalid_text", f"{field}必须是字符串")
    value = value.strip()
    require(0 < len(value) <= maximum, "invalid_text", f"{field}长度无效")
    require(not any(ord(ch) < 32 for ch in value), "invalid_text", f"{field}不能含控制字符")
    return value


def integer(value, field, minimum=0, maximum=10**12):
    require(type(value) is int, "invalid_integer", f"{field}必须是整数")
    require(minimum <= value <= maximum, "out_of_range", f"{field}超出范围")
    return value


def boolean(value, field):
    require(type(value) is bool, "invalid_boolean", f"{field}必须是布尔值")
    return value


def iso_day(value, field="日期"):
    require(isinstance(value, str), "invalid_date", f"{field}必须使用YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        require(False, "invalid_date", f"{field}无效")
    require(parsed.isoformat() == value, "invalid_date", f"{field}必须使用YYYY-MM-DD")
    return parsed


def instant(value):
    require(isinstance(value, str), "invalid_timestamp", "事件时间必须是带时区的ISO时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        require(False, "invalid_timestamp", "事件时间格式无效")
    require(parsed.tzinfo is not None and parsed.utcoffset() is not None,
            "invalid_timestamp", "事件时间缺少时区")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def timezone_name(value):
    require(isinstance(value, str), "invalid_timezone", "时区必须是字符串")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        require(False, "invalid_timezone", "未知时区")
    return value


def date_range(start, end, limit=366):
    first = iso_day(start, "开始日期")
    last = iso_day(end, "结束日期")
    require(first <= last, "invalid_range", "开始日期不能晚于结束日期")
    require((last - first).days < limit, "range_too_large", "日期跨度超过支持范围")
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def fields(value, allowed, required=()):
    require(isinstance(value, dict), "invalid_object", "输入必须是对象")
    unknown = sorted(set(value) - set(allowed))
    absent = sorted(set(required) - set(value))
    require(not unknown, "unknown_fields", "存在未知字段", details={"fields": unknown})
    require(not absent, "missing_fields", "缺少必填字段", details={"fields": absent})
    return value


def unique_ids(values, field, allow_empty=False, maximum=100):
    require(isinstance(values, list), "invalid_list", f"{field}必须是数组")
    require(len(values) <= maximum and (allow_empty or len(values) > 0),
            "invalid_list", f"{field}数量无效")
    result = [identifier(value, field) for value in values]
    require(len(result) == len(set(result)), "duplicate_id", f"{field}存在重复项")
    return sorted(result)


def page_size(limit, offset):
    return integer(limit, "limit", 1, 500), integer(offset, "offset", 0, 10**7)


def safe_ratio(numerator, denominator):
    if denominator == 0:
        return None
    return numerator / denominator


def decode(value):
    return json.loads(value)
