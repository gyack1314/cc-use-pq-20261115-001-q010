from math import isfinite

from .cubes import MEASURES


def check(name, condition, expected=None, actual=None):
    return {"name": name, "ok": bool(condition), "expected": expected, "actual": actual}


def check_totals(cube):
    total = cube["totals"]
    result = []
    for metric in MEASURES:
        value = total[metric]
        result.append(check("nonnegative:" + metric, type(value) is int and value >= 0, ">=0 integer", value))
    result.append(check("money_identity", total["gross_cents"] - total["refund_cents"] == total["net_cents"],
                        total["gross_cents"] - total["refund_cents"], total["net_cents"]))
    result.append(check("family_subset", total["family_orders"] <= total["orders"], total["orders"], total["family_orders"]))
    result.append(check("children_subset", total["children"] <= total["travelers"], total["travelers"], total["children"]))
    for name, rows in cube["tables"].items():
        for metric in MEASURES:
            actual = sum(row["measures"][metric] for row in rows)
            result.append(check(f"partition:{name}:{metric}", actual == total[metric], total[metric], actual))
    return result


def check_distributions(cube):
    result = []
    for metric in MEASURES:
        actual = sum(row["measures"][metric] for row in cube["spending_distribution"])
        expected = cube["totals"][metric]
        result.append(check("spending_buckets:" + metric, actual == expected, expected, actual))
    for name in ("origin_ranking", "destination_ranking"):
        ranks = cube[name]
        values = [row["measures"]["orders"] for row in ranks]
        result.append(check(name + ":sorted", values == sorted(values, reverse=True)))
        shares = [row["share"] for row in ranks if row["share"] is not None]
        if cube["totals"]["orders"]:
            result.append(check(name + ":share", abs(sum(shares) - 1) < 1e-10, 1, sum(shares)))
        else:
            result.append(check(name + ":undefined_share", not shares))
    return result


def check_daily(period):
    rows = period["cube"]["tables"]["daily"]
    identities = [(row["city"], row["day"]) for row in rows]
    result = [check("daily_unique", len(identities) == len(set(identities)), len(set(identities)), len(identities)),
              check("daily_exposure", len(rows) == period["exposure_summary"]["city_days"],
                    period["exposure_summary"]["city_days"], len(rows))]
    by_type = period["cube"]["day_mix"]
    by_holiday = period["cube"]["holiday_mix"]
    for metric in MEASURES:
        actual = by_type["weekday"]["measures"][metric] + by_type["weekend"]["measures"][metric]
        expected = period["cube"]["totals"][metric]
        result.append(check("day_type:" + metric, actual == expected, expected, actual))
        actual = by_holiday["holiday"]["measures"][metric] + by_holiday["outside_holiday"]["measures"][metric]
        result.append(check("holiday:" + metric, actual == expected, expected, actual))
    return result


def check_coverage(period):
    rows = period["coverage"]
    summary = period["coverage_summary"]
    result = []
    keys = [(row["source"], row["city"], row["day"]) for row in rows]
    result.append(check("coverage_unique", len(keys) == len(set(keys))))
    result.append(check("coverage_expected", len(rows) == summary["expected_cells"], summary["expected_cells"], len(rows)))
    for status in ("complete", "partial", "missing"):
        count = sum(row["status"] == status for row in rows)
        result.append(check("coverage:" + status, count == summary[status + "_cells"], summary[status + "_cells"], count))
    return result


def check_lineage(result):
    expected = []
    for name, period in result["periods"].items():
        for order in period["orders"]:
            expected.append((name, order["source"], order["order_id"], order["sequence"], order["event_id"]))
    actual = [(row["period"], row["source"], row["order_id"], row["sequence"], row["event_id"]) for row in result["lineage"]]
    return [check("lineage_unique", len(actual) == len(set(actual))),
            check("lineage_heads", sorted(expected) == sorted(actual), len(expected), len(actual))]


def check_finite(value, path="result"):
    failures = []
    if isinstance(value, float) and not isfinite(value):
        failures.append(path)
    elif isinstance(value, dict):
        for key, item in value.items():
            failures += check_finite(item, path + "." + str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            failures += check_finite(item, path + f"[{index}]")
    return failures


def inspect_result(result):
    checks = check_lineage(result)
    for name, period in result["periods"].items():
        period_checks = check_totals(period["cube"]) + check_distributions(period["cube"])
        period_checks += check_daily(period) + check_coverage(period)
        checks += [{**item, "period": name} for item in period_checks]
    nonfinite = check_finite(result)
    checks.append(check("finite_numbers", not nonfinite, [], nonfinite))
    failed = [item for item in checks if not item["ok"]]
    return {"ok": not failed, "checks": checks, "failures": failed, "check_count": len(checks)}
