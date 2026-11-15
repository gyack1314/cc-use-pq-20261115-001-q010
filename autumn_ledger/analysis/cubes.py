from collections import Counter, defaultdict

from ..errors import require
from ..records.contracts import contribution
from ..values import safe_ratio
from .windows import day_type


MEASURES = ("orders", "cancelled_orders", "travelers", "children", "gross_cents",
            "refund_cents", "net_cents", "family_orders")


def empty_measures():
    return {name: 0 for name in MEASURES}


def add_measures(target, values):
    for name in MEASURES:
        target[name] += values[name]


def enrich(values):
    return {**values, "average_net_cents": safe_ratio(values["net_cents"], values["orders"]),
            "family_share": safe_ratio(values["family_orders"], values["orders"]),
            "refund_fraction": safe_ratio(values["refund_cents"], values["gross_cents"]),
            "cancellation_fraction": safe_ratio(values["cancelled_orders"], values["orders"] + values["cancelled_orders"])}


def totals(orders):
    result = empty_measures()
    for order in orders:
        add_measures(result, contribution(order))
    return enrich(result)


def aggregate(orders, dimensions, classifier=None):
    groups = {}
    for order in orders:
        derived = classifier(order) if classifier else {}
        dimensions_value = {key: derived[key] if key in derived else order[key] for key in dimensions}
        identity = tuple(dimensions_value[key] for key in dimensions)
        if identity not in groups:
            groups[identity] = {"dimensions": dimensions_value, "measures": empty_measures(), "order_ids": []}
        group = groups[identity]
        add_measures(group["measures"], contribution(order))
        group["order_ids"].append(f"{order['source']}:{order['order_id']}:{order['sequence']}")
    result = []
    for key in sorted(groups, key=lambda v: tuple(str(x) for x in v)):
        group = groups[key]
        result.append({"dimensions": group["dimensions"], "measures": enrich(group["measures"]),
                       "lineage_count": len(group["order_ids"])})
    return result


def daily(orders, window, cities, grid):
    grouped = {(item["dimensions"]["origin"], item["dimensions"]["travel_day"]): item["measures"]
               for item in aggregate(orders, ["origin", "travel_day"])}
    result = []
    for row in grid:
        measures = grouped.get((row["city"], row["day"]), enrich(empty_measures()))
        result.append({**row, "measures": measures})
    return result


def rank(orders, dimension, metric="orders"):
    require(metric in MEASURES, "invalid_metric", "未知指标")
    grouped = aggregate(orders, [dimension])
    grouped.sort(key=lambda item: (-item["measures"][metric], str(item["dimensions"][dimension])))
    total = sum(item["measures"][metric] for item in grouped)
    previous = None
    position = 0
    result = []
    for index, item in enumerate(grouped, 1):
        value = item["measures"][metric]
        if previous != value:
            position = index
        result.append({"rank": position, "id": item["dimensions"][dimension],
                       "measures": item["measures"], "share": safe_ratio(value, total)})
        previous = value
    return result


def peak_days(daily_rows):
    by_city = defaultdict(list)
    for row in daily_rows:
        by_city[row["city"]].append(row)
    result = []
    for city, rows in sorted(by_city.items()):
        maximum = max(row["measures"]["orders"] for row in rows)
        if maximum == 0:
            result.append({"city": city, "orders": 0, "days": [], "holiday_peak": None})
            continue
        peaks = [row for row in rows if row["measures"]["orders"] == maximum]
        result.append({"city": city, "orders": maximum, "days": [row["day"] for row in peaks],
                       "holiday_peak": all(row["holiday"] for row in peaks)})
    return result


def distribution(orders):
    buckets = {"under_100": empty_measures(), "100_to_499": empty_measures(),
               "500_to_1999": empty_measures(), "2000_plus": empty_measures()}
    for order in orders:
        net = order["gross_cents"] - order["refund_cents"]
        if net < 10000:
            key = "under_100"
        elif net < 50000:
            key = "100_to_499"
        elif net < 200000:
            key = "500_to_1999"
        else:
            key = "2000_plus"
        add_measures(buckets[key], contribution(order))
    return [{"bucket": key, "measures": enrich(value)} for key, value in buckets.items()]


def day_mix(orders, grid):
    exposures = Counter(row["day_type"] for row in grid)
    groups = {item["dimensions"]["day_type"]: item["measures"]
              for item in aggregate(orders, ["day_type"], lambda o: {"day_type": day_type(o["travel_day"])})}
    result = {}
    for kind in ("weekday", "weekend"):
        measures = groups.get(kind, enrich(empty_measures()))
        result[kind] = {"measures": measures, "city_days": exposures[kind],
                        "orders_per_city_day": safe_ratio(measures["orders"], exposures[kind]),
                        "net_cents_per_city_day": safe_ratio(measures["net_cents"], exposures[kind])}
    result["weekday_weekend_ratio"] = None
    weekday = result["weekday"]["orders_per_city_day"]
    weekend = result["weekend"]["orders_per_city_day"]
    if weekday is not None and weekend is not None:
        result["weekday_weekend_ratio"] = safe_ratio(weekday, weekend)
    return result


def holiday_mix(orders, grid):
    lookup = {(row["city"], row["day"]): row["holiday"] for row in grid}
    grouped = {True: [], False: []}
    for order in orders:
        grouped[lookup[(order["origin"], order["travel_day"])]] .append(order)
    exposures = Counter(row["holiday"] for row in grid)
    result = {}
    for holiday in (True, False):
        measures = totals(grouped[holiday])
        result["holiday" if holiday else "outside_holiday"] = {
            "measures": measures, "city_days": exposures[holiday],
            "orders_per_city_day": safe_ratio(measures["orders"], exposures[holiday])}
    return result


def reconcile(orders, tables):
    expected = totals(orders)
    result = []
    for name, rows in tables.items():
        actual = empty_measures()
        for row in rows:
            add_measures(actual, row["measures"])
        differences = {key: actual[key] - expected[key] for key in MEASURES}
        result.append({"table": name, "ok": all(value == 0 for value in differences.values()),
                       "differences": differences})
    return result


def build_cubes(orders, window, cities, grid):
    tables = {"origins": aggregate(orders, ["origin"]),
              "destinations": aggregate(orders, ["destination"]),
              "sources": aggregate(orders, ["source"]),
              "routes": aggregate(orders, ["origin", "destination"]),
              "daily": daily(orders, window, cities, grid)}
    checks = reconcile(orders, tables)
    require(all(item["ok"] for item in checks), "reconciliation_failed", "分析分组与总量未对账")
    return {"totals": totals(orders), "tables": tables, "reconciliation": checks,
            "origin_ranking": rank(orders, "origin"), "destination_ranking": rank(orders, "destination"),
            "peak_days": peak_days(tables["daily"]), "spending_distribution": distribution(orders),
            "day_mix": day_mix(orders, grid), "holiday_mix": holiday_mix(orders, grid)}
