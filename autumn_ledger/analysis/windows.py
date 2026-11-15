from dataclasses import dataclass
from datetime import timedelta

from ..errors import require
from ..values import date_range, fields, integer, iso_day, text, unique_ids


@dataclass(frozen=True)
class Window:
    start: str
    end: str

    @property
    def days(self):
        return date_range(self.start, self.end)

    def contains(self, day):
        return self.start <= day <= self.end

    def as_dict(self):
        return {"start": self.start, "end": self.end, "day_count": len(self.days)}


def normalize_request(value):
    allowed = {"name", "start", "end", "baseline_start", "baseline_end", "cities", "sources",
               "destinations", "family_only", "revision", "minimum_orders"}
    fields(value, allowed, {"name", "start", "end", "cities", "sources"})
    dates = date_range(value["start"], value["end"], 93)
    name = text(value["name"], "分析名称", 120)
    cities = unique_ids(value["cities"], "cities", maximum=50)
    sources = unique_ids(value["sources"], "sources", maximum=30)
    destinations = unique_ids(value.get("destinations", []), "destinations", allow_empty=True, maximum=100)
    require(len(cities) * len(sources) * len(dates) <= 50000,
            "analysis_too_large", "覆盖检查范围过大，请缩小城市、来源或日期")
    family = value.get("family_only", True)
    require(type(family) is bool, "invalid_boolean", "family_only必须是布尔值")
    result = {"name": name, "start": dates[0].isoformat(), "end": dates[-1].isoformat(),
              "cities": cities, "sources": sources, "destinations": destinations,
              "family_only": family, "minimum_orders": integer(value.get("minimum_orders", 1), "minimum_orders", 1, 10000)}
    baseline_start = value.get("baseline_start")
    baseline_end = value.get("baseline_end")
    require((baseline_start is None) == (baseline_end is None), "incomplete_baseline", "对比窗口需要同时提供起止日期")
    if baseline_start is not None:
        baseline = date_range(baseline_start, baseline_end, 93)
        require(baseline[-1] < dates[0] or baseline[0] > dates[-1],
                "overlapping_windows", "当前窗口和对比窗口不能重叠")
        result["baseline_start"] = baseline[0].isoformat()
        result["baseline_end"] = baseline[-1].isoformat()
    if "revision" in value and value["revision"] is not None:
        result["revision"] = integer(value["revision"], "revision")
    return result


def request_windows(request):
    result = {"current": Window(request["start"], request["end"])}
    if "baseline_start" in request:
        result["baseline"] = Window(request["baseline_start"], request["baseline_end"])
    return result


def day_type(day):
    return "weekend" if iso_day(day).weekday() >= 5 else "weekday"


def match_order(order, request, window):
    if order["origin"] not in request["cities"]:
        return False
    if order["source"] not in request["sources"]:
        return False
    if request["destinations"] and order["destination"] not in request["destinations"]:
        return False
    if request["family_only"] and not order["family"]:
        return False
    return window.contains(order["travel_day"])


def daily_grid(window, cities, calendar_rows):
    indexed = {(row["city"], row["day"]): row for row in calendar_rows}
    result = []
    for city in cities:
        for parsed in window.days:
            day = parsed.isoformat()
            row = indexed[(city, day)]
            result.append({"city": city, "day": day, "day_type": day_type(day),
                           "holiday": row["holiday"], "calendar_id": row["calendar_id"],
                           "calendar_version": row["calendar_version"], "offset": row["offset"],
                           "school_day": row["school_day"]})
    return result


def exposure_summary(grid):
    result = {"city_days": len(grid), "weekday_city_days": 0, "weekend_city_days": 0,
              "holiday_city_days": 0, "nonholiday_city_days": 0, "calendar_cities": 0}
    calendar_cities = set()
    for row in grid:
        result[row["day_type"] + "_city_days"] += 1
        result["holiday_city_days" if row["holiday"] else "nonholiday_city_days"] += 1
        if row["calendar_id"]:
            calendar_cities.add(row["city"])
    result["calendar_cities"] = len(calendar_cities)
    return result


def aligned_day_pairs(current, baseline):
    length = min(len(current.days), len(baseline.days))
    return [{"offset": i, "current": current.days[i].isoformat(),
             "baseline": baseline.days[i].isoformat(),
             "same_weekday": current.days[i].weekday() == baseline.days[i].weekday()}
            for i in range(length)]
