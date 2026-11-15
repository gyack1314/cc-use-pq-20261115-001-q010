from copy import deepcopy

from autumn_ledger.values import date_range
from tests.support import LedgerCase


class ReferenceTests(LedgerCase):
    def test_catalog_compare_and_swap_preserves_history(self):
        before = self.ledger.database.revision()
        original = self.ledger.catalog.get("city", "sz")
        updated = self.ledger.catalog.put("city", "sz", {"name": "新名称", "region": "新区域"}, 1)
        self.assertEqual(updated["version"], 2)
        self.assertGreater(updated["revision"], before)
        self.assertEqual(self.ledger.catalog.get("city", "sz", before), original)
        self.assertEqual(self.ledger.catalog.get("city", "sz")["name"], "新名称")
        self.assertError("conflict", self.ledger.catalog.put, "city", "sz", {"name": "过期写入"}, 1)

    def test_identical_catalog_value_is_noop(self):
        before = self.ledger.database.revision()
        original = self.ledger.catalog.get("city", "sz")
        value = {key: original[key] for key in ("name", "region", "timezone", "active")}
        result = self.ledger.catalog.put("city", "sz", value, 1)
        self.assertEqual(result, original)
        self.assertEqual(self.ledger.database.revision(), before)

    def test_destination_requires_city(self):
        before = self.ledger.database.revision()
        self.assertError("not_found", self.ledger.catalog.put, "destination", "unknown", {"name": "未知", "city": "none"})
        self.assertEqual(self.ledger.database.revision(), before)
        self.assertError("not_found", self.ledger.catalog.get, "destination", "unknown")

    def test_reference_input_boundaries(self):
        cases = [("city", "../bad", {"name": "名称"}, "invalid_id"),
                 ("city", "new", {"name": "名称", "timezone": "No/SuchZone"}, "invalid_timezone"),
                 ("source", "new", {"name": "名称", "unknown": 1}, "unknown_fields"),
                 ("city", "new", {"name": " "}, "invalid_text"),
                 ("source", "new", {"name": "名称", "active": 1}, "invalid_boolean")]
        for kind, identity, value, code in cases:
            with self.subTest(identity=identity, value=value):
                self.assertError(code, self.ledger.catalog.put, kind, identity, value)
        self.assertTrue(self.ledger.database.integrity()["ok"])

    def test_calendar_inclusive_endpoints(self):
        first = self.ledger.calendars.classify("sz", "2026-11-16")
        last = self.ledger.calendars.classify("sz", "2026-11-20")
        before = self.ledger.calendars.classify("sz", "2026-11-15")
        after = self.ledger.calendars.classify("sz", "2026-11-21")
        self.assertTrue(first["holiday"])
        self.assertTrue(last["holiday"])
        self.assertEqual(first["offset"], 0)
        self.assertEqual(last["offset"], 4)
        self.assertFalse(before["holiday"])
        self.assertFalse(after["holiday"])

    def test_school_day_override_is_not_holiday(self):
        original = self.ledger.calendars.get("sz-autumn")
        old_revision = self.ledger.database.revision()
        value = {key: original[key] for key in ("city", "start", "end", "label")}
        value["school_days"] = ["2026-11-17"]
        updated = self.ledger.calendars.put("sz-autumn", value, 1)
        classification = self.ledger.calendars.classify("sz", "2026-11-17")
        self.assertFalse(classification["holiday"])
        self.assertTrue(classification["school_day"])
        self.assertEqual(classification["calendar_version"], 2)
        self.assertTrue(self.ledger.calendars.classify("sz", "2026-11-17", old_revision)["holiday"])
        self.assertGreater(updated["revision"], old_revision)

    def test_overlapping_calendar_rejected_atomically(self):
        before = self.ledger.database.revision()
        value = {"city": "sz", "start": "2026-11-20", "end": "2026-11-22", "label": "重叠"}
        self.assertError("calendar_overlap", self.ledger.calendars.put, "overlap", value)
        self.assertEqual(self.ledger.database.revision(), before)
        self.assertError("not_found", self.ledger.calendars.get, "overlap")

    def test_adjacent_calendar_allowed(self):
        value = {"city": "sz", "start": "2026-11-21", "end": "2026-11-22", "label": "相邻"}
        new = self.ledger.calendars.put("adjacent", value)
        self.assertEqual(new["version"], 1)
        self.assertEqual(self.ledger.calendars.classify("sz", "2026-11-21")["calendar_id"], "adjacent")
        self.assertEqual(self.ledger.calendars.classify("sz", "2026-11-20")["calendar_id"], "sz-autumn")

    def test_withdrawal_removes_classification_not_history(self):
        before = self.ledger.database.revision()
        old = self.ledger.calendars.get("sz-autumn")
        value = {key: old[key] for key in ("city", "start", "end", "label")}
        value["status"] = "withdrawn"
        self.ledger.calendars.put("sz-autumn", value, 1)
        self.assertFalse(self.ledger.calendars.classify("sz", "2026-11-16")["holiday"])
        self.assertTrue(self.ledger.calendars.classify("sz", "2026-11-16", before)["holiday"])
        history = self.ledger.planning.calendar_changes("sz-autumn")["history"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[1]["changed_fields"], ["status"])

    def test_calendar_cannot_move_city(self):
        old = self.ledger.calendars.get("sz-autumn")
        value = {key: old[key] for key in ("city", "start", "end", "label")}
        value["city"] = "sh"
        self.assertError("calendar_city_change", self.ledger.calendars.put, "sz-autumn", value, 1)
        self.assertEqual(self.ledger.calendars.get("sz-autumn")["city"], "sz")

    def test_calendar_invalid_ranges_and_overrides(self):
        base = {"city": "sz", "start": "2026-12-01", "end": "2026-12-03", "label": "候选"}
        for changes, code in (({"end": "2026-11-01"}, "invalid_range"),
                              ({"school_days": ["2026-12-04"]}, "invalid_school_days"),
                              ({"school_days": ["2026-12-02", "2026-12-02"]}, "duplicate_day"),
                              ({"status": "tentative"}, "invalid_status")):
            with self.subTest(changes=changes):
                self.assertError(code, self.ledger.calendars.put, "invalid", dict(base, **changes))

    def test_coverage_missing_is_distinct_from_complete_zero(self):
        missing = self.ledger.coverage.get("beta", "sz", "2026-11-16")
        complete = self.ledger.coverage.get("alpha", "sz", "2026-11-16")
        self.assertEqual(missing["status"], "missing")
        self.assertEqual(missing["version"], 0)
        self.assertEqual(complete["status"], "complete")
        self.assertEqual(complete["version"], 1)
        summary = self.ledger.coverage.summary([missing, complete])
        self.assertEqual(summary["complete_fraction"], 0.5)
        self.assertFalse(summary["fully_observed"])

    def test_coverage_version_correction(self):
        before = self.ledger.database.revision()
        value = {"source": "alpha", "city": "sz", "day": "2026-11-16", "status": "partial", "note": "上游待补齐"}
        new = self.ledger.coverage.put(value, 1)
        self.assertEqual(new["version"], 2)
        self.assertEqual(self.ledger.coverage.get("alpha", "sz", "2026-11-16", before)["status"], "complete")
        self.assertEqual(self.ledger.coverage.get("alpha", "sz", "2026-11-16")["status"], "partial")
        self.assertError("conflict", self.ledger.coverage.put, value, 1)

    def test_incomplete_coverage_requires_note(self):
        value = {"source": "beta", "city": "sz", "day": "2026-11-16", "status": "partial"}
        self.assertError("missing_note", self.ledger.coverage.put, value)
        self.assertEqual(self.ledger.coverage.get("beta", "sz", "2026-11-16")["version"], 0)
        self.assertError("invalid_status", self.ledger.coverage.put, dict(value, status="zero"))

    def test_readiness_combines_reference_and_coverage(self):
        result = self.ledger.planning.readiness(["sz", "nj"], ["alpha"], "2026-11-16", "2026-11-22")
        self.assertTrue(result["ready"])
        self.assertEqual(result["coverage"]["expected_cells"], 14)
        self.assertEqual(len(result["cities"]), 2)
        incomplete = self.ledger.planning.readiness(["sz"], ["alpha", "beta"], "2026-11-16", "2026-11-22")
        self.assertFalse(incomplete["ready"])
        self.assertEqual(incomplete["coverage"]["missing_cells"], 7)
        self.assertEqual(incomplete["gaps"][0]["source"], "beta")

    def test_city_calendar_shows_unobserved_holidays(self):
        result = self.ledger.planning.city_calendar("sz", "2026-11-16", "2026-11-22", ["beta"])
        self.assertEqual(result["holiday_days"], 5)
        self.assertEqual(result["complete_days"], 0)
        self.assertEqual(len(result["days"]), 7)
        self.assertTrue(result["days"][0]["holiday"])
        self.assertFalse(result["days"][0]["fully_observed"])

    def test_reference_snapshot_excludes_future_entities(self):
        before = self.ledger.database.revision()
        self.ledger.catalog.put("city", "new", {"name": "新增"})
        self.assertNotIn("new", [row["id"] for row in self.ledger.catalog.list("city", before)])
        self.assertIn("new", [row["id"] for row in self.ledger.catalog.list("city")])
        self.assertError("future_revision", self.ledger.catalog.list, "city", before + 100)
