from ..errors import require
from ..values import page_size, safe_ratio
from ..analysis.cubes import MEASURES, empty_measures


class EditionQuery:
    def __init__(self, editions):
        self.editions = editions

    def table(self, identity, table, period="current", filters=None, sort_by="orders", descending=True,
              minimum_orders=0, limit=100, offset=0):
        from ..values import integer
        page_size(limit, offset)
        integer(minimum_orders, "minimum_orders", 0)
        require(type(descending) is bool, "invalid_sort", "排序方向必须是布尔值")
        edition = self.editions.get(identity)
        require(period in edition["result"]["periods"], "unknown_period", "分析没有该期间")
        tables = edition["result"]["periods"][period]["cube"]["tables"]
        require(table in tables, "unknown_table", "未知统计表")
        dimensions = {"origins": {"origin"}, "destinations": {"destination"}, "sources": {"source"},
                      "routes": {"origin", "destination"}, "daily": {"city", "day", "day_type", "holiday"}}[table]
        filters = {} if filters is None else filters
        require(isinstance(filters, dict), "invalid_filters", "筛选必须是对象")
        require(not set(filters) - dimensions, "invalid_filters", "筛选字段不属于统计维度")
        allowed_sort = dimensions | set(MEASURES)
        require(sort_by in allowed_sort, "invalid_sort", "排序字段无效")
        rows = []
        for row in tables[table]:
            values = row.get("dimensions", row)
            if any(values.get(key) != value for key, value in filters.items()):
                continue
            if row["measures"]["orders"] < minimum_orders:
                continue
            rows.append({"dimensions": {key: values[key] for key in sorted(dimensions)},
                         "measures": row["measures"]})
        rows.sort(key=lambda row: tuple(str(row["dimensions"][key]) for key in sorted(dimensions)))
        rows.sort(key=lambda row: row["measures"][sort_by] if sort_by in MEASURES else row["dimensions"][sort_by],
                  reverse=descending)
        totals = empty_measures()
        for row in rows:
            for metric in MEASURES:
                totals[metric] += row["measures"][metric]
        return {"edition_id": identity, "data_revision": edition["data_revision"], "period": period,
                "table": table, "filters": filters, "sort_by": sort_by, "descending": descending,
                "minimum_orders": minimum_orders, "total_rows": len(rows), "filtered_totals": totals,
                "offset": offset, "limit": limit, "rows": rows[offset:offset + limit]}

    def overview(self, identity):
        edition = self.editions.get(identity)
        periods = {}
        for name, content in edition["result"]["periods"].items():
            cube = content["cube"]
            periods[name] = {"window": content["window"], "totals": cube["totals"],
                             "coverage": content["coverage_summary"], "exposure": content["exposure_summary"],
                             "weekday_weekend_ratio": cube["day_mix"]["weekday_weekend_ratio"],
                             "peak_alignment": content["calendar_analysis"]["peak_alignment"],
                             "top_origins": cube["origin_ranking"][:5],
                             "top_destinations": cube["destination_ranking"][:5]}
        return {"edition_id": identity, "name": edition["name"], "state": edition["state"],
                "version": edition["version"], "data_revision": edition["data_revision"],
                "stale": edition["stale"], "periods": periods, "quality": edition["result"]["quality"],
                "comparison": edition["result"]["comparison"], "result_digest": edition["result_digest"]}

    def coverage_filter(self, identity, period="current", status=None, source=None, city=None, limit=100, offset=0):
        page_size(limit, offset)
        require(status is None or status in {"complete", "partial", "missing"}, "invalid_status", "覆盖状态无效")
        edition = self.editions.get(identity)
        require(period in edition["result"]["periods"], "unknown_period", "分析没有该期间")
        result = []
        for row in edition["result"]["periods"][period]["coverage"]:
            if status is not None and row["status"] != status:
                continue
            if source is not None and row["source"] != source:
                continue
            if city is not None and row["city"] != city:
                continue
            result.append(row)
        return {"edition_id": identity, "period": period, "total_rows": len(result),
                "offset": offset, "cells": result[offset:offset + limit]}

    def issue_filter(self, identity, severity=None, period=None):
        require(severity is None or severity in {"error", "warning", "info"}, "invalid_severity", "问题级别无效")
        edition = self.editions.get(identity)
        if period is not None:
            require(period in edition["result"]["periods"], "unknown_period", "分析没有该期间")
        issues = edition["result"]["quality"]["issues"]
        selected = []
        for issue in issues:
            if severity is not None and issue["severity"] != severity:
                continue
            if period is not None and issue["evidence"].get("period") != period:
                continue
            selected.append(issue)
        return {"edition_id": identity, "publishable": edition["result"]["quality"]["publishable"],
                "issues": selected, "count": len(selected)}

    def compare_segments(self, identity, dimension, metric="orders", minimum_absolute=0):
        from ..values import integer
        require(dimension in {"origins", "destinations", "sources"}, "invalid_dimension", "对比维度无效")
        require(metric in MEASURES, "invalid_metric", "指标无效")
        integer(minimum_absolute, "minimum_absolute", 0)
        edition = self.editions.get(identity)
        comparison = edition["result"]["comparison"]
        require(comparison is not None, "no_comparison", "分析没有对比窗口")
        result = []
        total_change = comparison["totals"][metric]["absolute_change"]
        for row in comparison[dimension]:
            change = row["changes"][metric]
            if abs(change["absolute_change"]) < minimum_absolute:
                continue
            result.append({"id": row["id"], **change, "contribution_to_total_change": safe_ratio(change["absolute_change"], total_change),
                           "present_current": row["present_current"], "present_baseline": row["present_baseline"]})
        result.sort(key=lambda row: (-abs(row["absolute_change"]), row["id"]))
        return {"edition_id": identity, "dimension": dimension, "metric": metric,
                "total_change": total_change, "fully_comparable": comparison["fully_comparable"], "segments": result,
                "note": "贡献只是原始分组差额对账，不是固定构成分解或因果归因"}
