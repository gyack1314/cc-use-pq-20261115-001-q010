from collections import defaultdict

from ..values import safe_ratio


def relative_calendar_days(period):
    grouped = defaultdict(lambda: {"city_days": 0, "complete_city_days": 0, "orders": 0,
                                   "travelers": 0, "net_cents": 0, "cities": set()})
    coverage = defaultdict(list)
    for cell in period["coverage"]:
        coverage[(cell["city"], cell["day"])].append(cell["status"])
    for row in period["cube"]["tables"]["daily"]:
        if row["calendar_id"] is None:
            continue
        key = (row["offset"], row["holiday"])
        item = grouped[key]
        item["city_days"] += 1
        statuses = coverage[(row["city"], row["day"])]
        item["complete_city_days"] += int(bool(statuses) and all(value == "complete" for value in statuses))
        item["orders"] += row["measures"]["orders"]
        item["travelers"] += row["measures"]["travelers"]
        item["net_cents"] += row["measures"]["net_cents"]
        item["cities"].add(row["city"])
    result = []
    for key, item in sorted(grouped.items()):
        cities = sorted(item.pop("cities"))
        result.append({"offset": key[0], "holiday": key[1], **item, "cities": cities,
                       "orders_per_city_day": safe_ratio(item["orders"], item["city_days"]),
                       "fully_observed": item["city_days"] == item["complete_city_days"]})
    return result


def calendar_balances(period, references):
    calendars = {row["id"]: row for row in references["calendars"]}
    grouped = defaultdict(lambda: {"observed_city_days": 0, "holiday_city_days": 0,
                                   "orders": 0, "holiday_orders": 0, "school_day_orders": 0, "net_cents": 0})
    for row in period["cube"]["tables"]["daily"]:
        identity = row["calendar_id"]
        if identity is None:
            continue
        item = grouped[identity]
        item["observed_city_days"] += 1
        item["holiday_city_days"] += int(row["holiday"])
        item["orders"] += row["measures"]["orders"]
        item["holiday_orders" if row["holiday"] else "school_day_orders"] += row["measures"]["orders"]
        item["net_cents"] += row["measures"]["net_cents"]
    result = []
    for identity, values in sorted(grouped.items()):
        reference = calendars[identity]
        result.append({"calendar_id": identity, "version": reference["version"], "city": reference["city"],
                       "label": reference["label"], **values,
                       "holiday_order_share": safe_ratio(values["holiday_orders"], values["orders"]),
                       "holiday_orders_per_city_day": safe_ratio(values["holiday_orders"], values["holiday_city_days"])})
    return result


def peak_alignment(period):
    peaks = period["cube"]["peak_days"]
    calendar_cities = {row["city"] for row in period["exposure"] if row["calendar_id"]}
    eligible = [row for row in peaks if row["days"] and row["city"] in calendar_cities]
    inside = [row for row in eligible if row["holiday_peak"]]
    outside = [row for row in eligible if not row["holiday_peak"]]
    unknown = [row["city"] for row in peaks if row["city"] not in calendar_cities or not row["days"]]
    return {"eligible_cities": len(eligible), "all_peaks_in_holiday": len(inside),
            "not_all_peaks_in_holiday": len(outside), "fraction": safe_ratio(len(inside), len(eligible)),
            "unknown_cities": unknown,
            "tie_rule": "并列峰值全部位于假期内才计入；无有效订单或无校历不进入分母"}


def destination_calendar_routes(period, references):
    destination_city = {row["id"]: row["city"] for row in references["destinations"]}
    calendar = {(row["city"], row["day"]): row for row in period["exposure"]}
    groups = defaultdict(lambda: {"orders": 0, "net_cents": 0, "travelers": 0, "destinations": set()})
    for order in period["orders"]:
        if order["state"] != "booked":
            continue
        classification = calendar[(order["origin"], order["travel_day"])]
        target_city = destination_city[order["destination"]]
        key = (order["origin"], target_city, classification["holiday"])
        item = groups[key]
        item["orders"] += 1
        item["net_cents"] += order["gross_cents"] - order["refund_cents"]
        item["travelers"] += order["adults"] + order["children"]
        item["destinations"].add(order["destination"])
    result = []
    for key, values in sorted(groups.items()):
        destinations = sorted(values.pop("destinations"))
        result.append({"origin": key[0], "destination_city": key[1], "holiday": key[2],
                       "cross_city": key[0] != key[1], **values, "destinations": destinations})
    return result


def calendar_analysis(period, references):
    return {"relative_days": relative_calendar_days(period), "calendar_balances": calendar_balances(period, references),
            "peak_alignment": peak_alignment(period), "destination_city_routes": destination_calendar_routes(period, references),
            "scope": "校历归属按客源城市；分析不将目的地假期当作客源假期"}
