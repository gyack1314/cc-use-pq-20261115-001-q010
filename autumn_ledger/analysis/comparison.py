from ..values import safe_ratio
from .cubes import MEASURES, empty_measures
from .windows import aligned_day_pairs


def difference(current, baseline):
    return {"current": current, "baseline": baseline, "absolute_change": current - baseline,
            "relative_change": (current - baseline) / baseline if baseline else None,
            "baseline_zero": baseline == 0}


def compare_measures(current, baseline):
    return {metric: difference(current[metric], baseline[metric]) for metric in MEASURES}


def comparable_coverage(current_matrix, baseline_matrix, sources, cities):
    def statuses(matrix):
        result = {}
        for row in matrix:
            result.setdefault((row["source"], row["city"]), []).append(row["status"])
        return result
    current = statuses(current_matrix)
    baseline = statuses(baseline_matrix)
    pairs = []
    for source in sources:
        for city in cities:
            key = (source, city)
            left = current.get(key, [])
            right = baseline.get(key, [])
            complete = bool(left and right) and all(x == "complete" for x in left + right)
            pairs.append({"source": source, "city": city, "comparable": complete,
                          "current_complete": sum(x == "complete" for x in left),
                          "baseline_complete": sum(x == "complete" for x in right),
                          "current_expected": len(left), "baseline_expected": len(right)})
    return pairs


def segment_changes(current_rows, baseline_rows, dimension):
    left = {row["dimensions"][dimension]: row["measures"] for row in current_rows}
    right = {row["dimensions"][dimension]: row["measures"] for row in baseline_rows}
    result = []
    for identity in sorted(set(left) | set(right)):
        current = left.get(identity, empty_measures())
        baseline = right.get(identity, empty_measures())
        result.append({"id": identity, "changes": compare_measures(current, baseline),
                       "present_current": identity in left, "present_baseline": identity in right})
    return result


def compare_periods(current, baseline, current_window, baseline_window, coverage_pairs):
    complete = all(pair["comparable"] for pair in coverage_pairs) and bool(coverage_pairs)
    current_count = len(current_window.days)
    baseline_count = len(baseline_window.days)
    normalized = {}
    for metric in MEASURES:
        left = current["totals"][metric] / current_count
        right = baseline["totals"][metric] / baseline_count
        normalized[metric] = difference(left, right)
    pairs = aligned_day_pairs(current_window, baseline_window)
    warnings = []
    if current_count != baseline_count:
        warnings.append("窗口天数不同，应结合日均指标阅读总量变化")
    if any(not pair["same_weekday"] for pair in pairs):
        warnings.append("按序号对齐的日期并非相同星期，周末构成可能影响变化")
    if not complete:
        warnings.append("至少一个来源城市组合存在缺报，变化只描述已收到记录")
    warnings.append("平台订单为观察样本，变化不代表总体增量或校历政策因果效应")
    return {"totals": compare_measures(current["totals"], baseline["totals"]),
            "per_day": normalized, "coverage_pairs": coverage_pairs,
            "fully_comparable": complete, "day_alignment": pairs, "warnings": warnings,
            "origins": segment_changes(current["tables"]["origins"], baseline["tables"]["origins"], "origin"),
            "destinations": segment_changes(current["tables"]["destinations"], baseline["tables"]["destinations"], "destination"),
            "sources": segment_changes(current["tables"]["sources"], baseline["tables"]["sources"], "source")}
