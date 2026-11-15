from ..values import safe_ratio


def percent(value):
    if value is None:
        return "无法计算"
    return f"{value * 100:.1f}%"


def money(cents):
    return f"{cents / 100:,.2f}元"


def observation(code, sentence, evidence):
    return {"code": code, "text": sentence, "evidence": evidence}


def summarize_volume(period, period_name):
    total = period["cube"]["totals"]
    coverage = period["coverage_summary"]
    complete = coverage["fully_observed"]
    prefix = "完整覆盖窗口" if complete else "已收到的样本记录"
    result = [observation("volume", f"{period_name}的{prefix}包含{total['orders']}笔有效订单、{total['travelers']}人次，净收入{money(total['net_cents'])}。",
                          {"totals": total, "coverage": coverage})]
    if total["cancelled_orders"]:
        result.append(observation("cancelled", f"另有{total['cancelled_orders']}笔取消订单；其未退金额保留在收入中，出游人数不计。",
                                  {"cancelled_orders": total["cancelled_orders"]}))
    if not complete:
        result.append(observation("coverage_limit", "缺报与部分覆盖不按零需求解释，当前总量不能代表整个选择范围。", coverage))
    return result


def summarize_days(period):
    mix = period["cube"]["day_mix"]
    weekday = mix["weekday"]
    weekend = mix["weekend"]
    ratio = mix["weekday_weekend_ratio"]
    if ratio is None:
        return [observation("day_ratio_unavailable", "缺少工作日或周末暴露，或周末有效订单为零，无法给出工作日与周末热度比。", mix)]
    sentence = f"按城市日期暴露折算，工作日与周末有效订单日均之比为{ratio:.3f}。"
    return [observation("day_ratio", sentence, {"weekday": weekday, "weekend": weekend, "ratio": ratio})]


def summarize_peaks(period, names):
    result = []
    for row in period["cube"]["peak_days"]:
        name = names.get(row["city"], row["city"])
        if not row["days"]:
            result.append(observation("no_peak", f"{name}未观察到有效订单，不指定峰值日期。", row))
            continue
        days = "、".join(row["days"])
        in_holiday = "全部落在已登记假期内" if row["holiday_peak"] else "并非全部位于已登记假期内"
        result.append(observation("peak", f"{name}峰值为{row['orders']}笔，日期为{days}，{in_holiday}。", row))
    return result


def summarize_comparison(comparison):
    if comparison is None:
        return []
    result = []
    for metric, label in (("orders", "有效订单"), ("net_cents", "净收入")):
        change = comparison["totals"][metric]
        if change["baseline_zero"]:
            sentence = f"{label}对比期为零，仅报告绝对变化，不计算增长率。"
        else:
            sentence = f"已观察{label}较对比期变化{percent(change['relative_change'])}。"
        result.append(observation("period_change", sentence, {"metric": metric, **change,
                                                               "fully_comparable": comparison["fully_comparable"]}))
    for warning in comparison["warnings"]:
        result.append(observation("comparison_limit", warning, {}))
    return result


def explain_result(result):
    names = {row["id"]: row["name"] for row in result["references"]["cities"]}
    observations = []
    for name, period in result["periods"].items():
        observations += summarize_volume(period, "当前期" if name == "current" else "对比期")
    current = result["periods"]["current"]
    observations += summarize_days(current)
    observations += summarize_peaks(current, names)
    observations += summarize_comparison(result["comparison"])
    blockers = [issue for issue in result["quality"]["issues"] if issue["severity"] == "error"]
    return {"data_revision": result["data_revision"], "publishable": not blockers,
            "observations": observations, "publication_blockers": blockers,
            "scope_note": "以上只描述所选来源样本，不推断整体游客规模或秋假政策因果效应"}
