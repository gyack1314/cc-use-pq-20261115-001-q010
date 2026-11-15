from collections import defaultdict
from math import log

from ..errors import require
from ..values import safe_ratio
from .cubes import aggregate, empty_measures, totals


DIMENSIONS = {"origin", "destination", "source"}


def concentration(rows, metric):
    amounts = [row["measures"][metric] for row in rows]
    total = sum(amounts)
    if total == 0:
        return {"hhi": None, "entropy": None, "effective_segments": None, "top_share": None}
    shares = [amount / total for amount in amounts if amount > 0]
    hhi = sum(share * share for share in shares)
    entropy = -sum(share * log(share) for share in shares)
    return {"hhi": hhi, "entropy": entropy, "effective_segments": 1 / hhi, "top_share": max(shares)}


def contribution_curve(rows, dimension, metric):
    ordered = sorted(rows, key=lambda row: (-row["measures"][metric], row["dimensions"][dimension]))
    total = sum(row["measures"][metric] for row in rows)
    cumulative = 0
    result = []
    for index, row in enumerate(ordered, 1):
        value = row["measures"][metric]
        cumulative += value
        result.append({"id": row["dimensions"][dimension], "position": index, "value": value,
                       "share": safe_ratio(value, total), "cumulative_value": cumulative,
                       "cumulative_share": safe_ratio(cumulative, total)})
    return result


def segment_profile(orders, dimension, registered_ids):
    require(dimension in DIMENSIONS, "invalid_dimension", "画像维度无效")
    grouped = aggregate(orders, [dimension])
    indexed = {row["dimensions"][dimension]: row["measures"] for row in grouped}
    total = totals(orders)
    result = []
    for identity in sorted(set(registered_ids) | set(indexed)):
        measures = indexed.get(identity, empty_measures())
        active = measures["orders"]
        children = measures["children"]
        travelers = measures["travelers"]
        revenue = measures["net_cents"]
        share = safe_ratio(active, total["orders"])
        revenue_share = safe_ratio(revenue, total["net_cents"])
        share_gap = None if share is None or revenue_share is None else revenue_share - share
        result.append({"id": identity, "observed": identity in indexed,
                       "measures": measures, "order_share": share, "revenue_share": revenue_share,
                       "revenue_share_gap": share_gap, "child_traveler_share": safe_ratio(children, travelers),
                       "net_per_traveler_cents": safe_ratio(revenue, travelers),
                       "travelers_per_order": safe_ratio(travelers, active),
                       "family_share": safe_ratio(measures["family_orders"], active)})
    return {"dimension": dimension, "segments": result,
            "order_concentration": concentration(grouped, "orders"),
            "revenue_concentration": concentration(grouped, "net_cents"),
            "order_curve": contribution_curve(grouped, dimension, "orders"),
            "revenue_curve": contribution_curve(grouped, dimension, "net_cents")}


def cross_shares(orders, left, right):
    require(left in DIMENSIONS and right in DIMENSIONS and left != right,
            "invalid_dimensions", "交叉画像需要两个不同维度")
    grouped = aggregate(orders, [left, right])
    row_totals = defaultdict(int)
    column_totals = defaultdict(int)
    for row in grouped:
        amount = row["measures"]["orders"]
        row_totals[row["dimensions"][left]] += amount
        column_totals[row["dimensions"][right]] += amount
    grand_total = sum(row_totals.values())
    result = []
    for row in grouped:
        left_id = row["dimensions"][left]
        right_id = row["dimensions"][right]
        amount = row["measures"]["orders"]
        expected = row_totals[left_id] * column_totals[right_id] / grand_total if grand_total else None
        lift = safe_ratio(amount, expected) if expected is not None else None
        result.append({"left_id": left_id, "right_id": right_id, "orders": amount,
                       "row_share": safe_ratio(amount, row_totals[left_id]),
                       "column_share": safe_ratio(amount, column_totals[right_id]),
                       "overall_share": safe_ratio(amount, grand_total), "independence_expected": expected,
                       "association_ratio": lift})
    return {"left": left, "right": right, "cells": result,
            "interpretation": "关联比只描述已观察订单构成，不代表偏好因果关系"}


def population_notes(profile, coverage_summary):
    notes = []
    if not coverage_summary["fully_observed"]:
        notes.append("覆盖不完整，分群份额可能由来源缺报导致")
    if any(not row["observed"] for row in profile["segments"]):
        notes.append("未观察到的目录项列为零记录，但不能推断真实需求为零")
    concentration_value = profile["order_concentration"]["top_share"]
    if concentration_value is not None and concentration_value > 0.8:
        notes.append("订单高度集中于单一分群，整体均值容易受构成影响")
    if concentration_value is None:
        notes.append("没有有效订单，不计算构成指数")
    return notes


def profile_period(period, references, dimension):
    key = {"origin": "cities", "destination": "destinations", "source": "sources"}[dimension]
    registered = [item["id"] for item in references[key]]
    profile = segment_profile(period["orders"], dimension, registered)
    profile["notes"] = population_notes(profile, period["coverage_summary"])
    return profile
