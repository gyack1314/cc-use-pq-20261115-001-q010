from collections import defaultdict
from statistics import median

from ..values import safe_ratio


def rolling_summary(values, width):
    result = []
    for index in range(len(values)):
        start = max(0, index - width + 1)
        window = values[start:index + 1]
        usable = [value for value in window if value is not None]
        complete = len(usable) == len(window)
        result.append({"value": sum(usable) / len(usable) if usable and complete else None,
                       "observed_days": len(usable), "expected_days": len(window),
                       "full_window": len(window) == width, "complete": complete})
    return result


def consecutive_runs(rows, predicate):
    runs = []
    current = []
    for row in rows:
        if predicate(row):
            current.append(row)
        else:
            if current:
                runs.append(current)
                current = []
    if current:
        runs.append(current)
    return [{"start": run[0]["day"], "end": run[-1]["day"], "days": len(run),
             "orders": sum(row["orders"] for row in run)} for run in runs]


def coverage_by_city_day(period):
    grouped = defaultdict(list)
    for cell in period["coverage"]:
        grouped[(cell["city"], cell["day"])].append(cell["status"])
    return {key: bool(statuses) and all(value == "complete" for value in statuses)
            for key, statuses in grouped.items()}


def daily_trends(period):
    completeness = coverage_by_city_day(period)
    cities = defaultdict(list)
    for row in period["cube"]["tables"]["daily"]:
        measures = row["measures"]
        complete = completeness.get((row["city"], row["day"]), False)
        cities[row["city"]].append({"day": row["day"], "day_type": row["day_type"],
                                    "holiday": row["holiday"], "orders": measures["orders"],
                                    "net_cents": measures["net_cents"], "complete": complete,
                                    "usable_orders": measures["orders"] if complete else None})
    result = []
    for city, rows in sorted(cities.items()):
        rows.sort(key=lambda row: row["day"])
        values = [row["usable_orders"] for row in rows]
        moving3 = rolling_summary(values, 3)
        moving7 = rolling_summary(values, 7)
        previous = None
        cumulative = 0
        observations = []
        for index, row in enumerate(rows):
            current = row["usable_orders"]
            cumulative += row["orders"]
            change = current - previous if current is not None and previous is not None else None
            relative = safe_ratio(change, previous) if change is not None else None
            observations.append({**row, "previous_day_change": change, "previous_day_relative": relative,
                                 "cumulative_observed_orders": cumulative,
                                 "moving_3": moving3[index], "moving_7": moving7[index]})
            previous = current
        usable = [value for value in values if value is not None]
        result.append({"city": city, "days": observations, "complete_days": len(usable),
                       "observed_median": median(usable) if usable else None,
                       "missing_runs": consecutive_runs(rows, lambda row: not row["complete"]),
                       "zero_runs": consecutive_runs(rows, lambda row: row["complete"] and row["orders"] == 0)})
    return result


def weekday_seasonality(period):
    from datetime import date
    completeness = coverage_by_city_day(period)
    buckets = {index: {"weekday": index, "city_days": 0, "complete_city_days": 0,
                       "orders": 0, "net_cents": 0, "usable_orders": 0} for index in range(7)}
    for row in period["cube"]["tables"]["daily"]:
        index = date.fromisoformat(row["day"]).weekday()
        bucket = buckets[index]
        bucket["city_days"] += 1
        bucket["orders"] += row["measures"]["orders"]
        bucket["net_cents"] += row["measures"]["net_cents"]
        if completeness.get((row["city"], row["day"]), False):
            bucket["complete_city_days"] += 1
            bucket["usable_orders"] += row["measures"]["orders"]
    result = []
    for bucket in buckets.values():
        result.append({**bucket, "observed_orders_per_city_day": safe_ratio(bucket["orders"], bucket["city_days"]),
                       "complete_orders_per_city_day": safe_ratio(bucket["usable_orders"], bucket["complete_city_days"])})
    return result


def robust_flags(period, minimum_orders=3, multiplier=3.0):
    from ..values import integer
    from ..errors import require
    integer(minimum_orders, "minimum_orders", 1, 100000)
    require(type(multiplier) in {int, float} and 1 <= multiplier <= 20,
            "invalid_multiplier", "异常阈值倍数应在1到20之间")
    trends = daily_trends(period)
    result = []
    for city in trends:
        peers = defaultdict(list)
        for row in city["days"]:
            if row["complete"]:
                peers[(row["day_type"], row["holiday"])].append(row)
        for key, rows in peers.items():
            if len(rows) < 3:
                continue
            values = [row["orders"] for row in rows]
            center = median(values)
            mad = median(abs(value - center) for value in values)
            threshold = max(float(minimum_orders), multiplier * 1.4826 * mad)
            for row in rows:
                deviation = row["orders"] - center
                if abs(deviation) > threshold:
                    result.append({"city": city["city"], "day": row["day"], "orders": row["orders"],
                                   "peer_median": center, "mad": mad, "threshold": threshold,
                                   "direction": "high" if deviation > 0 else "low",
                                   "peer_days": len(rows), "day_type": key[0], "holiday": key[1]})
    return {"flags": sorted(result, key=lambda row: (row["city"], row["day"])),
            "method": "在完整覆盖、相同工作日类别与假期归属的日期中，用中位数绝对偏差识别偏离",
            "interpretation": "标记用于核查来源和业务事件，不表示已确认数据错误或预测"}
