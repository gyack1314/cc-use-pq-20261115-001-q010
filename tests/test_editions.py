import csv
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor

from autumn_ledger.application import Ledger
from autumn_ledger.demo import synthetic_event, request_data
from autumn_ledger.errors import LedgerError
from autumn_ledger.editions.export import csv_value
from tests.support import LedgerCase


class EditionTests(LedgerCase):
    def test_idempotent_request_and_conflicting_key(self):
        edition = self.edition()
        replay = self.edition()
        self.assertEqual(edition["id"], replay["id"])
        self.assertEqual(edition["result_digest"], replay["result_digest"])
        self.assertEqual(len(self.ledger.editions.list()), 1)
        self.assertError("conflict", self.edition, start="2026-11-17")
        self.assertEqual(len(self.ledger.editions.list()), 1)

    def test_draft_review_publish_and_channel_pointer(self):
        edition = self.edition()
        approved = self.approve(edition)
        self.assertEqual(approved["state"], "approved")
        self.assertEqual(approved["version"], 2)
        published = self.ledger.review.publish(edition["id"], "weekly", 2, 0, "publisher")
        self.assertEqual(published["generation"], 1)
        self.assertEqual(published["version"], 3)
        self.assertEqual(self.ledger.review.channel("weekly")["edition_id"], edition["id"])
        self.assertEqual(self.ledger.review.history(edition["id"])[-1]["action"], "publish")
        self.assertEqual(self.ledger.export.public_channel("weekly")["result"]["periods"]["current"]["cube"]["totals"]["orders"], 3)

    def test_reject_and_reopen_require_explicit_state(self):
        edition = self.edition()
        rejected = self.ledger.review.decide(edition["id"], "reject", 1, "reviewer", "等待来源说明")
        self.assertEqual(rejected["state"], "rejected")
        self.assertError("invalid_transition", self.ledger.review.decide, edition["id"], "approve", 2, "reviewer", "说明")
        reopened = self.ledger.review.decide(edition["id"], "reopen", 2, "reviewer", "来源说明已补充")
        self.assertEqual(reopened["state"], "draft")
        self.assertEqual(reopened["version"], 3)
        self.assertEqual([row["action"] for row in self.ledger.review.history(edition["id"])], ["reject", "reopen"])

    def test_quality_error_blocks_approval(self):
        edition = self.edition(sources=["alpha", "beta"])
        self.assertFalse(edition["quality"]["publishable"])
        error = self.assertError("quality_blocked", self.approve, edition)
        self.assertEqual(error.status, 409)
        self.assertEqual(self.ledger.editions.get(edition["id"])["state"], "draft")
        self.assertEqual(self.ledger.review.history(edition["id"]), [])

    def test_source_correction_blocks_stale_review(self):
        edition = self.edition()
        self.ledger.intake.ingest("alpha", "correction", [synthetic_event("a1", sequence=2, refund=10000)])
        self.assertError("stale_edition", self.approve, edition)
        stored = self.ledger.editions.get(edition["id"])
        self.assertTrue(stored["stale"])
        self.assertEqual(stored["result"]["periods"]["current"]["cube"]["totals"]["net_cents"], 120000)
        self.assertTrue(self.ledger.editions.verify(edition["id"])["ok"])

    def test_correction_after_approval_blocks_publication(self):
        edition = self.edition()
        approved = self.approve(edition)
        self.ledger.intake.ingest("alpha", "correction", [synthetic_event("a1", sequence=2, refund=10000)])
        self.assertError("stale_edition", self.ledger.review.publish, edition["id"], "weekly", approved["version"], 0, "publisher")
        self.assertEqual(self.ledger.review.channels(), [])
        self.assertEqual(self.ledger.editions.get(edition["id"])["state"], "approved")

    def test_reference_change_also_invalidates_approval(self):
        edition = self.edition()
        self.approve(edition)
        self.ledger.catalog.put("city", "sz", {"name": "修订名称"}, 1)
        self.assertError("stale_edition", self.ledger.review.publish, edition["id"], "weekly", 2, 0, "publisher")
        self.assertTrue(self.ledger.editions.verify(edition["id"])["ok"])
        drift = self.ledger.investigation.reference_drift(edition["id"])
        self.assertEqual(len(drift["changes"]), 1)
        self.assertIn("name", drift["changes"][0]["changed_fields"])

    def test_stale_expected_version_cannot_override_reviewer(self):
        edition = self.edition()
        self.approve(edition)
        self.assertError("conflict", self.ledger.review.decide, edition["id"], "reject", 1, "other", "过期状态")
        stored = self.ledger.editions.get(edition["id"])
        self.assertEqual(stored["state"], "approved")
        self.assertEqual(stored["version"], 2)
        self.assertEqual(len(self.ledger.review.history(edition["id"])), 1)

    def test_channel_compare_and_swap_prevents_lost_publication(self):
        first = self.edition("one")
        second = self.edition("two")
        self.publish(first)
        approved = self.approve(second)
        self.assertError("conflict", self.ledger.review.publish, second["id"], "weekly", 2, 0, "publisher")
        self.assertEqual(self.ledger.review.channel("weekly")["edition_id"], first["id"])
        replacement = self.ledger.review.publish(second["id"], "weekly", 2, 1, "publisher")
        self.assertEqual(replacement["generation"], 2)
        self.assertEqual(self.ledger.review.channel("weekly")["edition_id"], second["id"])
        self.assertEqual(len(self.ledger.review.publication_history("weekly")), 2)

    def test_publication_retry_is_idempotent_with_current_versions(self):
        edition = self.edition()
        self.publish(edition)
        retry = self.ledger.review.publish(edition["id"], "weekly", 3, 1, "publisher")
        self.assertTrue(retry["replayed"])
        self.assertEqual(retry["generation"], 1)
        self.assertEqual(retry["version"], 3)
        self.assertEqual(len(self.ledger.review.publication_history("weekly")), 1)

    def test_active_channel_prevents_retirement(self):
        edition = self.edition()
        self.publish(edition)
        self.assertError("edition_in_use", self.ledger.review.retire, edition["id"], 3, "publisher", "撤下")
        self.assertEqual(self.ledger.editions.get(edition["id"])["state"], "published")
        replacement = self.edition("replacement")
        self.publish(replacement, generation=1)
        retired = self.ledger.review.retire(edition["id"], 3, "publisher", "已有替代分析")
        self.assertEqual(retired["state"], "retired")
        self.assertError("retired", self.ledger.export.document, edition["id"])

    def test_parallel_reviews_have_one_winner(self):
        edition = self.edition()
        def approve(_):
            try:
                return Ledger(self.path).review.decide(edition["id"], "approve", 1, "reviewer", "核对")
            except LedgerError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(approve, range(3)))
        self.assertEqual(sum(isinstance(row, dict) for row in results), 1)
        self.assertEqual(results.count("conflict"), 2)
        self.assertEqual(len(self.ledger.review.history(edition["id"])), 1)

    def test_parallel_channel_publications_have_one_winner(self):
        first = self.approve(self.edition("one"))
        second = self.approve(self.edition("two"))
        def publish(edition):
            try:
                return Ledger(self.path).review.publish(edition["id"], "weekly", 2, 0, "publisher")
            except LedgerError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(publish, [first, second]))
        self.assertEqual(sum(isinstance(row, dict) for row in results), 1)
        self.assertEqual(results.count("conflict"), 1)
        self.assertEqual(len(self.ledger.review.publication_history("weekly")), 1)

    def test_export_bundle_is_deterministic_and_verifiable(self):
        edition = self.edition()
        first = self.ledger.export.bundle(edition["id"])
        second = self.ledger.export.bundle(edition["id"])
        self.assertEqual(first, second)
        verified = self.ledger.export.verify_bundle(first)
        self.assertTrue(verified["ok"])
        self.assertEqual(verified["edition_id"], edition["id"])
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            self.assertIn("current/daily.csv", archive.namelist())
            self.assertIn("baseline/coverage.csv", archive.namelist())
            self.assertIn("lineage.csv", archive.namelist())
            document = json.loads(archive.read("edition.json"))
            self.assertEqual(document["result_digest"], edition["result_digest"])

    def test_bundle_content_tampering_fails(self):
        edition = self.edition()
        content = self.ledger.export.bundle(edition["id"])
        replacement = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(content)) as original, zipfile.ZipFile(replacement, "w") as altered:
            for name in original.namelist():
                value = original.read(name)
                altered.writestr(name, value + b"changed" if name == "current/daily.csv" else value)
        self.assertError("invalid_bundle", self.ledger.export.verify_bundle, replacement.getvalue())
        self.assertError("invalid_bundle", self.ledger.export.verify_bundle, b"not a zip")

    def test_csv_exports_reconcile_to_fixed_totals(self):
        edition = self.edition()
        content = self.ledger.export.table(edition["id"], "origins")
        rows = list(csv.DictReader(io.StringIO(content)))
        self.assertEqual(sum(int(row["orders"]) for row in rows), 3)
        self.assertEqual(sum(int(row["net_cents"]) for row in rows), 120000)
        self.assertEqual({row["edition_id"] for row in rows}, {edition["id"]})
        self.assertError("unknown_table", self.ledger.export.table, edition["id"], "invented")
        self.assertError("unknown_period", self.ledger.export.table, edition["id"], "origins", "future")

    def test_csv_formula_escape_keeps_numbers_numeric(self):
        self.assertEqual(csv_value("=1+1"), "'=1+1")
        self.assertEqual(csv_value("@formula"), "'@formula")
        self.assertEqual(csv_value("-text"), "'-text")
        self.assertEqual(csv_value(-10), "-10")
        self.assertEqual(csv_value(None), "")
        self.assertEqual(csv_value(False), "false")
        self.assertEqual(csv_value("普通名称"), "普通名称")

    def test_fixed_snapshot_verification_after_many_changes(self):
        edition = self.edition()
        saved = self.ledger.export.document(edition["id"])
        self.ledger.intake.ingest("alpha", "change", [synthetic_event("a1", sequence=2, refund=20000)])
        self.ledger.coverage.put({"source": "alpha", "city": "nj", "day": "2026-11-22", "status": "partial", "note": "待核查"}, 1)
        old = self.ledger.calendars.get("sz-autumn")
        self.ledger.calendars.put("sz-autumn", {"city": "sz", "start": "2026-11-17", "end": "2026-11-20", "label": "修订"}, 1)
        self.assertEqual(self.ledger.export.document(edition["id"]), saved)
        self.assertTrue(self.ledger.editions.verify(edition["id"])["ok"])
        self.assertTrue(self.ledger.editions.get(edition["id"])["stale"])

    def test_investigation_traces_changed_order_without_rewriting_snapshot(self):
        edition = self.edition()
        self.ledger.intake.ingest("alpha", "move", [synthetic_event("a1", origin="nj", sequence=2)])
        trace = self.ledger.investigation.order_trace(edition["id"], "alpha", "a1")
        self.assertEqual(trace["selected_at_snapshot"]["sequence"], 1)
        self.assertTrue(trace["has_later_changes"])
        self.assertEqual(trace["included_in_periods"][0]["sequence"], 1)
        self.assertTrue(trace["history"][0]["visible_at_snapshot"])
        self.assertFalse(trace["history"][1]["visible_at_snapshot"])
        self.assertTrue(self.ledger.investigation.event_reconciliation(edition["id"])["ok"])

    def test_drilldown_and_table_query_preserve_filtered_totals(self):
        edition = self.edition()
        selected = self.ledger.investigation.drilldown(edition["id"], origin="sz", limit=1)
        self.assertEqual(selected["total"], 2)
        self.assertEqual(len(selected["items"]), 1)
        self.assertEqual(selected["totals"]["net_cents"], 100000)
        table = self.ledger.query.table(edition["id"], "origins", filters={"origin": "sz"})
        self.assertEqual(table["filtered_totals"]["orders"], 2)
        self.assertEqual(table["total_rows"], 1)
        self.assertError("invalid_filters", self.ledger.query.table, edition["id"], "origins", filters={"source": "alpha"})
        self.assertError("invalid_sort", self.ledger.query.table, edition["id"], "origins", sort_by="unknown")

    def test_publication_impact_distinguishes_known_order_corrections(self):
        edition = self.edition()
        self.publish(edition)
        before = self.ledger.database.revision()
        self.ledger.intake.ingest("alpha", "correction", [synthetic_event("a1", sequence=2), synthetic_event("new")])
        impact = self.ledger.investigation.publication_impact(before)
        self.assertEqual(len(impact["changed_orders"]), 2)
        self.assertTrue(impact["channels"][0]["stale"])
        self.assertEqual(impact["channels"][0]["changed_existing_orders"], [["alpha", "a1"]])

    def test_edition_comparison_reconciles_head_replacements(self):
        first = self.edition("one")
        self.ledger.intake.ingest("alpha", "refund", [synthetic_event("a1", sequence=2, refund=10000)])
        second = self.edition("two")
        difference = self.ledger.editions.compare_editions(first["id"], second["id"])
        self.assertEqual(difference["metrics"]["net_cents"]["change"], -10000)
        self.assertEqual(difference["metrics"]["orders"]["change"], 0)
        self.assertEqual(difference["added_heads"], [["alpha", "a1", 2]])
        self.assertEqual(difference["removed_heads"], [["alpha", "a1", 1]])
        self.assertTrue(difference["same_request"])

    def test_corrupt_persisted_result_is_detected(self):
        edition = self.edition()
        with self.ledger.database.transaction(write=True) as connection:
            stored = self.ledger.editions.get(edition["id"], connection)
            altered = stored["result"]
            altered["data_revision"] += 1
            connection.execute("UPDATE editions SET result=? WHERE id=?", (json.dumps(altered), edition["id"]))
        self.assertError("edition_corrupt", self.ledger.editions.get, edition["id"])
        self.assertError("edition_corrupt", self.ledger.export.document, edition["id"])
