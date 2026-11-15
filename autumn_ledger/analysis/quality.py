from collections import Counter

from .cubes import MEASURES


def issue(code, severity, message, evidence=None):
    return {"code": code, "severity": severity, "message": message, "evidence": evidence or {}}


def coverage_issues(summary, missing_groups, period):
    result = []
    if summary["missing_cells"]:
        result.append(issue("missing_coverage", "error", "缺报日期不能被解释为零需求",
                            {"period": period, "cells": summary["missing_cells"], "groups": missing_groups}))
    if summary["partial_cells"]:
        result.append(issue("partial_coverage", "error", "部分覆盖日期不足以支持完整期间比较",
                            {"period": period, "cells": summary["partial_cells"]}))
    if not summary["expected_cells"]:
        result.append(issue("empty_coverage", "error", "没有可检查的来源城市日期组合", {"period": period}))
    return result


def volume_issues(cube, minimum_orders, period):
    result = []
    total = cube["totals"]
    if total["orders"] < minimum_orders:
        result.append(issue("insufficient_orders", "error", "有效订单数量不足，不能发布本次分析",
                            {"period": period, "orders": total["orders"], "minimum": minimum_orders}))
    if total["orders"] == 0 and total["cancelled_orders"]:
        result.append(issue("only_cancelled", "warning", "仅收到取消订单，旅客量不代表有出行",
                            {"period": period, "cancelled_orders": total["cancelled_orders"]}))
    if total["net_cents"] and total["orders"] == 0:
        result.append(issue("retained_cancellation_revenue", "warning", "取消订单仍有未退款金额，金额与出行人数需分别理解",
                            {"period": period, "net_cents": total["net_cents"]}))
    empty_origins = [row["city"] for row in cube["peak_days"] if row["orders"] == 0]
    if empty_origins:
        result.append(issue("empty_origins", "warning", "部分城市没有有效订单，不应凭空补造峰值",
                            {"period": period, "cities": empty_origins}))
    return result


def calendar_issues(exposure, cities, period):
    result = []
    known = {row["city"] for row in exposure if row["calendar_id"]}
    absent = sorted(set(cities) - known)
    if absent:
        result.append(issue("no_calendar", "warning", "部分城市在窗口内没有已确认的校历，仍按非假期分类",
                            {"period": period, "cities": absent}))
    school_days = [row for row in exposure if row["school_day"] and row["calendar_id"]]
    if school_days:
        result.append(issue("school_day_overrides", "info", "校历中的上课日已排除在假期暴露之外",
                            {"period": period, "city_days": len(school_days)}))
    return result


def record_issues(orders, matrix, period):
    indexed = {(row["source"], row["city"], row["day"]): row["status"] for row in matrix}
    inconsistent = []
    for order in orders:
        key = (order["source"], order["origin"], order["travel_day"])
        if indexed.get(key) == "missing":
            inconsistent.append({"source": order["source"], "order_id": order["order_id"],
                                 "day": order["travel_day"]})
    result = []
    if inconsistent:
        result.append(issue("records_on_missing_days", "warning", "缺报日期仍有少量记录，不能据此推定当日完整",
                            {"period": period, "count": len(inconsistent), "examples": inconsistent[:20]}))
    return result


def comparison_issues(comparison):
    if comparison is None:
        return []
    result = []
    for metric in ("orders", "net_cents"):
        change = comparison["totals"][metric]
        if change["baseline_zero"]:
            result.append(issue("zero_baseline", "warning", "对比期为零，不显示无限增长率",
                                {"metric": metric, "current": change["current"]}))
    if not comparison["fully_comparable"]:
        result.append(issue("incomparable_coverage", "error", "两个期间未具备相同且完整的来源覆盖"))
    return result


def summarize(issues):
    counts = Counter(item["severity"] for item in issues)
    return {"publishable": counts["error"] == 0, "errors": counts["error"],
            "warnings": counts["warning"], "information": counts["info"], "issues": issues}


def quality_report(periods, request, comparison):
    issues = []
    for name, period in periods.items():
        issues += coverage_issues(period["coverage_summary"], period["coverage_gaps"], name)
        issues += volume_issues(period["cube"], request["minimum_orders"], name)
        issues += calendar_issues(period["exposure"], request["cities"], name)
        issues += record_issues(period["orders"], period["coverage"], name)
    issues += comparison_issues(comparison)
    issues.append(issue("observational_sample", "info", "结果仅描述所选来源已收到的订单，不作总体或政策因果推断"))
    return summarize(issues)


def validate_result(result):
    failures = []
    for name, period in result["periods"].items():
        cube = period["cube"]
        total = cube["totals"]
        if any(total[metric] < 0 for metric in MEASURES):
            failures.append(f"{name}:negative_measure")
        if total["net_cents"] != total["gross_cents"] - total["refund_cents"]:
            failures.append(f"{name}:money_balance")
        if total["family_orders"] > total["orders"]:
            failures.append(f"{name}:family_count")
        if total["children"] > total["travelers"]:
            failures.append(f"{name}:party_count")
        for check in cube["reconciliation"]:
            if not check["ok"]:
                failures.append(f"{name}:{check['table']}")
        coverage = period["coverage_summary"]
        if coverage["expected_cells"] != coverage["complete_cells"] + coverage["partial_cells"] + coverage["missing_cells"]:
            failures.append(f"{name}:coverage_balance")
    return {"ok": not failures, "failures": failures}
