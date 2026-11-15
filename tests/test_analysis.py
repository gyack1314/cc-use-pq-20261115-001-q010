from copy import deepcopy

from autumn_ledger.demo import request_data, synthetic_event
from autumn_ledger.analysis.invariants import inspect_result
from autumn_ledger.analysis.narrative import explain_result
from autumn_ledger.analysis.trends import rolling_summary, robust_flags
from autumn_ledger.analysis.windows import normalize_request
from tests.support import LedgerCase


class AnalysisTests(LedgerCase):
    def result(self, **changes):
        return self.ledger.analysis.run(request_data(**changes))

    def test_total_and_partition_reconciliation(self):
        result = self.result()
        current = result["periods"]["current"]
        self.assertEqual(current["cube"]["totals"]["orders"], 3)
        self.assertEqual(current["cube"]["totals"]["travelers"], 9)
        self.assertEqual(current["cube"]["totals"]["children"], 3)
        self.assertEqual(current["cube"]["totals"]["net_cents"], 120000)
        self.assertTrue(all(row["ok"] for row in current["cube"]["reconciliation"]))
        self.assertTrue(result["statistical_checks"]["ok"])
        self.assertTrue(result["quality"]["publishable"])
        self.assertEqual(len(result["lineage"]), 5)

    def test_daily_grid_contains_zero_days(self):
        current = self.result()["periods"]["current"]
        rows = current["cube"]["tables"]["daily"]
        self.assertEqual(len(rows), 14)
        zeros = [row for row in rows if row["measures"]["orders"] == 0]
        self.assertEqual(len(zeros), 11)
        self.assertEqual(current["exposure_summary"]["city_days"], 14)
        self.assertEqual(current["cube"]["day_mix"]["weekday"]["city_days"], 10)
        self.assertEqual(current["cube"]["day_mix"]["weekend"]["city_days"], 4)
        self.assertAlmostEqual(current["cube"]["day_mix"]["weekday_weekend_ratio"], 0.8)

    def test_calendar_is_origin_based_not_destination_based(self):
        current = self.result()["periods"]["current"]
        mix = current["cube"]["holiday_mix"]
        self.assertEqual(mix["holiday"]["measures"]["orders"], 3)
        self.assertEqual(mix["outside_holiday"]["measures"]["orders"], 0)
        self.assertEqual(mix["holiday"]["city_days"], 9)
        routes = current["calendar_analysis"]["destination_city_routes"]
        sz_route = next(row for row in routes if row["origin"] == "sz")
        self.assertEqual(sz_route["destination_city"], "sh")
        self.assertTrue(sz_route["holiday"])
        self.assertTrue(sz_route["cross_city"])

    def test_peak_ties_and_unknown_denominator(self):
        result = self.result(cities=["sz", "nj", "sh"])
        peaks = result["periods"]["current"]["cube"]["peak_days"]
        sz = next(row for row in peaks if row["city"] == "sz")
        sh = next(row for row in peaks if row["city"] == "sh")
        self.assertEqual(sz["days"], ["2026-11-16", "2026-11-17"])
        self.assertTrue(sz["holiday_peak"])
        self.assertEqual(sh["days"], [])
        self.assertIsNone(sh["holiday_peak"])
        alignment = result["periods"]["current"]["calendar_analysis"]["peak_alignment"]
        self.assertEqual(alignment["eligible_cities"], 2)
        self.assertEqual(alignment["unknown_cities"], ["sh"])

    def test_cumulative_refund_is_not_added_across_events(self):
        self.ledger.intake.ingest("alpha", "refund1", [synthetic_event("a1", sequence=2, refund=10000)])
        self.ledger.intake.ingest("alpha", "refund2", [synthetic_event("a1", sequence=3, refund=15000)])
        total = self.result()["periods"]["current"]["cube"]["totals"]
        self.assertEqual(total["orders"], 3)
        self.assertEqual(total["gross_cents"], 120000)
        self.assertEqual(total["refund_cents"], 15000)
        self.assertEqual(total["net_cents"], 105000)

    def test_cancellation_keeps_retained_revenue_separate(self):
        event = synthetic_event("a1", sequence=2, state="cancelled", refund=40000)
        self.ledger.intake.ingest("alpha", "cancel", [event])
        current = self.result()["periods"]["current"]
        total = current["cube"]["totals"]
        self.assertEqual(total["orders"], 2)
        self.assertEqual(total["cancelled_orders"], 1)
        self.assertEqual(total["travelers"], 6)
        self.assertEqual(total["net_cents"], 80000)
        retained = current["diagnostics"]["cancellation_retention"][0]
        self.assertEqual(retained["retained_cents"], 10000)
        self.assertAlmostEqual(retained["retention_fraction"], 0.2)

    def test_correction_moves_order_between_groups_and_periods(self):
        before = self.ledger.database.revision()
        event = synthetic_event("a1", day="2026-11-10", origin="nj", destination="museum", sequence=2)
        self.ledger.intake.ingest("alpha", "move", [event])
        result = self.result()
        self.assertEqual(result["periods"]["current"]["cube"]["totals"]["orders"], 2)
        self.assertEqual(result["periods"]["baseline"]["cube"]["totals"]["orders"], 3)
        old = self.result(revision=before)
        self.assertEqual(old["periods"]["current"]["cube"]["totals"]["orders"], 3)
        self.assertEqual(old["periods"]["baseline"]["cube"]["totals"]["orders"], 2)
        self.assertNotEqual(result["lineage_digest"], old["lineage_digest"])

    def test_missing_coverage_blocks_release_and_preserves_observation(self):
        result = self.result(sources=["alpha", "beta"])
        self.assertFalse(result["quality"]["publishable"])
        self.assertEqual(result["periods"]["current"]["cube"]["totals"]["orders"], 3)
        self.assertEqual(result["periods"]["current"]["coverage_summary"]["missing_cells"], 14)
        self.assertFalse(result["comparison"]["fully_comparable"])
        codes = {row["code"] for row in result["quality"]["issues"]}
        self.assertIn("missing_coverage", codes)
        self.assertIn("incomparable_coverage", codes)

    def test_partial_coverage_propagates_to_trends(self):
        self.ledger.coverage.put({"source": "alpha", "city": "sz", "day": "2026-11-17",
                                  "status": "partial", "note": "未齐"}, 1)
        result = self.result()
        current = result["periods"]["current"]
        self.assertFalse(result["quality"]["publishable"])
        city = next(row for row in current["daily_trends"] if row["city"] == "sz")
        day = next(row for row in city["days"] if row["day"] == "2026-11-17")
        self.assertEqual(day["orders"], 1)
        self.assertIsNone(day["usable_orders"])
        self.assertIsNone(day["moving_3"]["value"])
        self.assertEqual(city["missing_runs"][0]["days"], 1)

    def test_no_baseline_omits_comparison(self):
        request = request_data()
        del request["baseline_start"]
        del request["baseline_end"]
        result = self.ledger.analysis.run(request)
        self.assertIsNone(result["comparison"])
        self.assertEqual(set(result["periods"]), {"current"})
        self.assertEqual(len(result["lineage"]), 3)
        self.assertTrue(result["quality"]["publishable"])

    def test_zero_baseline_has_null_growth_not_infinity(self):
        result = self.result(baseline_start="2026-11-01", baseline_end="2026-11-07")
        change = result["comparison"]["totals"]["orders"]
        self.assertTrue(change["baseline_zero"])
        self.assertIsNone(change["relative_change"])
        self.assertEqual(change["absolute_change"], 3)
        self.assertFalse(result["quality"]["publishable"])
        self.assertTrue(result["statistical_checks"]["ok"])

    def test_destination_and_family_filters_use_same_lineage(self):
        event = synthetic_event("adult")
        event.update(family=False, children=0, adults=1)
        self.ledger.intake.ingest("alpha", "adults", [event])
        family = self.result(destinations=["park"])
        all_orders = self.result(destinations=["park"], family_only=False)
        self.assertEqual(family["periods"]["current"]["cube"]["totals"]["orders"], 2)
        self.assertEqual(all_orders["periods"]["current"]["cube"]["totals"]["orders"], 3)
        self.assertEqual(all_orders["periods"]["current"]["cube"]["totals"]["family_orders"], 2)
        self.assertEqual(len(all_orders["lineage"]), 4)

    def test_request_validation_boundaries(self):
        for changes, code in (({"end": "2026-11-15"}, "invalid_range"),
                              ({"baseline_start": "2026-11-20"}, "invalid_range"),
                              ({"baseline_start": "2026-11-15", "baseline_end": "2026-11-17"}, "overlapping_windows"),
                              ({"cities": ["sz", "sz"]}, "duplicate_id"),
                              ({"sources": []}, "invalid_list"),
                              ({"family_only": 1}, "invalid_boolean"),
                              ({"minimum_orders": 0}, "out_of_range")):
            with self.subTest(changes=changes):
                self.assertError(code, normalize_request, request_data(**changes))

    def test_unequal_window_lengths_include_per_day_interpretation(self):
        result = self.result(baseline_end="2026-11-14")
        comparison = result["comparison"]
        self.assertEqual(comparison["totals"]["orders"]["absolute_change"], 1)
        self.assertAlmostEqual(comparison["per_day"]["orders"]["current"], 3 / 7)
        self.assertAlmostEqual(comparison["per_day"]["orders"]["baseline"], 2 / 6)
        self.assertTrue(any("天数不同" in warning for warning in comparison["warnings"]))
        self.assertTrue(comparison["fully_comparable"])

    def test_profile_concentration_and_rank_ties(self):
        current = self.result()["periods"]["current"]
        profile = current["profiles"]["origin"]
        self.assertAlmostEqual(profile["order_concentration"]["hhi"], 5 / 9)
        self.assertAlmostEqual(profile["order_curve"][-1]["cumulative_share"], 1)
        self.assertEqual(profile["order_curve"][0]["id"], "sz")
        ranks = current["cube"]["origin_ranking"]
        self.assertEqual(ranks[0]["rank"], 1)
        self.assertEqual(ranks[1]["rank"], 2)
        self.assertAlmostEqual(sum(row["share"] for row in ranks), 1)

    def test_diagnostic_receipts_and_source_balance(self):
        current = self.result()["periods"]["current"]
        diagnostic = current["diagnostics"]
        source = diagnostic["source_reliability"][0]
        self.assertEqual(source["active_orders"], 3)
        self.assertEqual(source["complete_cells"], 14)
        self.assertEqual(source["coverage_fraction"], 1)
        self.assertEqual(source["revised_heads"], 0)
        self.assertEqual(sum(row["orders"] for row in diagnostic["source_city_balance"]), 3)
        self.assertEqual(diagnostic["missing_intervals"], [])

    def test_invariant_checker_catches_tampered_total(self):
        result = self.result()
        result["periods"]["current"]["cube"]["totals"]["orders"] += 1
        check = inspect_result(result)
        self.assertFalse(check["ok"])
        names = {row["name"] for row in check["failures"]}
        self.assertIn("partition:origins:orders", names)
        self.assertIn("day_type:orders", names)

    def test_narrative_preserves_sample_and_zero_limits(self):
        result = self.result(sources=["alpha", "beta"])
        narrative = explain_result(result)
        self.assertFalse(narrative["publishable"])
        words = "".join(item["text"] for item in narrative["observations"])
        self.assertIn("样本", words)
        self.assertIn("缺报", words)
        self.assertIn("因果", narrative["scope_note"])
        self.assertTrue(narrative["publication_blockers"])

    def test_rolling_windows_refuse_missing_day_interpolation(self):
        result = rolling_summary([1, None, 3, 5, 7], 3)
        self.assertEqual(result[0]["value"], 1)
        self.assertFalse(result[0]["full_window"])
        self.assertIsNone(result[2]["value"])
        self.assertEqual(result[2]["observed_days"], 2)
        self.assertEqual(result[4]["value"], 5)
        self.assertTrue(result[4]["complete"])

    def test_anomaly_flags_are_observations_not_error_claims(self):
        current = self.result()["periods"]["current"]
        result = robust_flags(current)
        self.assertEqual(result["flags"], [])
        self.assertIn("不表示", result["interpretation"])
        self.assertError("invalid_multiplier", robust_flags, current, multiplier=100)
        self.assertError("out_of_range", robust_flags, current, minimum_orders=0)
