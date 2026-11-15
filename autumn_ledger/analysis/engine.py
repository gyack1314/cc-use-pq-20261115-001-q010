from ..errors import require
from ..values import fingerprint, utc_now
from .comparison import comparable_coverage, compare_periods
from .cubes import build_cubes
from .quality import quality_report, validate_result
from .windows import daily_grid, exposure_summary, match_order, normalize_request, request_windows
from .segments import profile_period, cross_shares
from .trends import daily_trends, weekday_seasonality
from .diagnostics import diagnostics
from .calendar_view import calendar_analysis
from .invariants import inspect_result


class AnalysisEngine:
    def __init__(self, database, catalog, calendars, coverage, intake):
        self.database = database
        self.catalog = catalog
        self.calendars = calendars
        self.coverage = coverage
        self.intake = intake

    def run(self, value, connection=None):
        request = normalize_request(value)
        if connection is None:
            with self.database.transaction() as opened:
                return self.run(request, opened)
        revision = self.database.snapshot_revision(connection, request.get("revision"))
        references = self.validate_references(request, revision, connection)
        heads = self.intake.heads(revision, request["sources"], connection)
        windows = request_windows(request)
        periods = {}
        lineage = []
        for name, window in windows.items():
            selected = [order for order in heads if match_order(order, request, window)]
            exposure = self.calendars.exposure(request["cities"], window.start, window.end, revision, connection)
            grid = daily_grid(window, request["cities"], exposure)
            matrix = self.coverage.matrix(request["sources"], request["cities"],
                                          window.start, window.end, revision, connection)
            cube = build_cubes(selected, window, request["cities"], grid)
            periods[name] = {"window": window.as_dict(), "orders": selected, "exposure": grid,
                             "exposure_summary": exposure_summary(grid), "coverage": matrix,
                             "coverage_summary": self.coverage.summary(matrix),
                             "coverage_gaps": self.coverage.missing_groups(matrix), "cube": cube}
            period = periods[name]
            period["profiles"] = {dimension: profile_period(period, references, dimension)
                                  for dimension in ("origin", "destination", "source")}
            period["route_association"] = cross_shares(selected, "origin", "destination")
            period["daily_trends"] = daily_trends(period)
            period["weekday_seasonality"] = weekday_seasonality(period)
            period["diagnostics"] = diagnostics(period)
            period["calendar_analysis"] = calendar_analysis(period, references)
            for order in selected:
                lineage.append({"period": name, "source": order["source"], "order_id": order["order_id"],
                                "event_id": order["event_id"], "sequence": order["sequence"],
                                "revision": order["ingested_revision"], "digest": fingerprint(order)})
        comparison = None
        if "baseline" in periods:
            pairs = comparable_coverage(periods["current"]["coverage"], periods["baseline"]["coverage"],
                                        request["sources"], request["cities"])
            comparison = compare_periods(periods["current"]["cube"], periods["baseline"]["cube"],
                                         windows["current"], windows["baseline"], pairs)
        quality = quality_report(periods, request, comparison)
        result = {"format_version": 1, "data_revision": revision, "request": request,
                  "references": references, "periods": periods, "comparison": comparison,
                  "quality": quality, "lineage": lineage, "lineage_digest": fingerprint(lineage),
                  "calculated_at": utc_now(), "semantics": self.semantics()}
        result["integrity"] = validate_result(result)
        result["statistical_checks"] = inspect_result(result)
        require(result["statistical_checks"]["ok"], "statistical_integrity", "统计口径一致性失败",
                details={"failures": result["statistical_checks"]["failures"]})
        require(result["integrity"]["ok"], "analysis_integrity", "分析内部一致性失败",
                details=result["integrity"])
        return result

    def validate_references(self, request, revision, connection):
        references = {"cities": [], "sources": [], "destinations": []}
        for city in request["cities"]:
            references["cities"].append(self.catalog.get("city", city, revision, connection))
        for source in request["sources"]:
            references["sources"].append(self.catalog.get("source", source, revision, connection))
        for destination in request["destinations"]:
            references["destinations"].append(self.catalog.get("destination", destination, revision, connection))
        if not request["destinations"]:
            references["destinations"] = self.catalog.list("destination", revision, connection)
        references["calendars"] = [item for item in self.calendars.list(revision=revision, connection=connection)
                                    if item["city"] in request["cities"]]
        return references

    def semantics(self):
        return {"order_identity": "source+order_id", "head": "不超过数据版本的最高sequence",
                "amount_unit": "人民币分", "travel_day": "供应商给出的目的地本地出游日期",
                "cancelled": "取消订单不计出游量；未退金额仍计入净收入",
                "refund": "refund_cents为订单累计退款，不是本次退款增量",
                "coverage": "complete表示来源确认该城市日期数据已齐，未登记不能当零",
                "denominator": "日均按选定城市日期暴露计，不按有订单的日期计",
                "scope": "仅描述所选来源订单，不推断总体或因果效应"}

    def preview(self, value):
        result = self.run(value)
        return {"data_revision": result["data_revision"], "request": result["request"],
                "current": result["periods"]["current"]["cube"]["totals"],
                "baseline": result["periods"].get("baseline", {}).get("cube", {}).get("totals"),
                "quality": result["quality"], "lineage_digest": result["lineage_digest"],
                "integrity": result["integrity"]}

    def reconcile_revision(self, value, expected_digest):
        result = self.run(value)
        require(result["lineage_digest"] == expected_digest, "lineage_changed", "订单证据与预期不同", 409)
        return result
