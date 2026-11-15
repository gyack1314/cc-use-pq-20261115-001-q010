from autumn_ledger.demo import synthetic_event
from tests.support import LedgerCase


class QueryTests(LedgerCase):
    def test_published_history_remains_consistent_when_data_is_stale(self):
        edition = self.edition()
        self.publish(edition)
        self.ledger.intake.ingest("alpha", "late", [synthetic_event("a1", sequence=2, refund=3000)])
        consistency = self.ledger.publication_readiness.published_consistency()
        self.assertTrue(consistency["ok"])
        self.assertTrue(consistency["channels"][0]["stale"])
        self.assertTrue(consistency["channels"][0]["history_matches"])
        readiness = self.ledger.publication_readiness.inspect(edition["id"], "weekly")
        self.assertFalse(readiness["can_publish"])
        self.assertIn("current_data", readiness["blockers"])

    def test_gate_explanation_matches_review_and_publish_states(self):
        edition = self.edition()
        draft = self.ledger.publication_readiness.inspect(edition["id"], "weekly")
        self.assertFalse(draft["can_publish"])
        self.assertEqual(draft["blockers"], ["approval"])
        self.assertEqual(draft["channel"]["generation"], 0)
        self.approve(edition)
        ready = self.ledger.publication_readiness.inspect(edition["id"], "weekly")
        self.assertTrue(ready["can_publish"])
        self.assertEqual(ready["expected_version"], 2)
        self.ledger.review.publish(edition["id"], "weekly", 2, 0, "publisher")
        self.assertTrue(self.ledger.publication_readiness.published_consistency()["ok"])

    def test_queue_classifies_quality_and_version_without_changing_state(self):
        first = self.edition("first")
        blocked = self.edition("blocked", sources=["alpha", "beta"])
        queue = self.ledger.publication_readiness.queue()
        self.assertEqual(queue["counts"]["awaiting_review"], 1)
        self.assertEqual(queue["counts"]["quality_blocked"], 1)
        self.approve(first)
        self.assertEqual(self.ledger.publication_readiness.queue()["counts"]["ready_to_publish"], 1)
        self.ledger.intake.ingest("alpha", "change", [synthetic_event("a1", sequence=2)])
        queue = self.ledger.publication_readiness.queue()
        self.assertEqual(queue["counts"]["stale"], 2)
        self.assertEqual(self.ledger.editions.get(first["id"])["state"], "approved")
        self.assertEqual(self.ledger.editions.get(blocked["id"])["state"], "draft")

    def test_issue_and_coverage_filters_preserve_absent_information(self):
        edition = self.edition(sources=["alpha", "beta"])
        errors = self.ledger.query.issue_filter(edition["id"], "error", "current")
        self.assertTrue(errors["issues"])
        self.assertTrue(all(item["severity"] == "error" for item in errors["issues"]))
        missing = self.ledger.query.coverage_filter(edition["id"], status="missing", source="beta", city="sz", limit=2)
        self.assertEqual(missing["total_rows"], 7)
        self.assertEqual(len(missing["cells"]), 2)
        self.assertTrue(all(row["status"] == "missing" for row in missing["cells"]))
        self.assertError("invalid_severity", self.ledger.query.issue_filter, edition["id"], "critical")

    def test_segment_changes_add_up_to_total(self):
        edition = self.edition()
        changes = self.ledger.query.compare_segments(edition["id"], "origins")
        self.assertEqual(changes["total_change"], 1)
        self.assertEqual(sum(row["absolute_change"] for row in changes["segments"]), 1)
        self.assertTrue(changes["fully_comparable"])
        overview = self.ledger.query.overview(edition["id"])
        self.assertEqual(overview["periods"]["current"]["totals"]["orders"], 3)
        self.assertEqual(overview["result_digest"], edition["result_digest"])

    def test_snapshot_differences_and_activity_reconcile(self):
        before = self.ledger.database.revision()
        self.ledger.intake.ingest("alpha", "change", [synthetic_event("a1", sequence=2, refund=5000), synthetic_event("new")])
        after = self.ledger.database.revision()
        activity = self.ledger.receipts.activity("alpha", before, after)
        self.assertEqual(len(activity["activity"]), 1)
        self.assertEqual(activity["activity"][0]["events"], 2)
        self.assertEqual(activity["activity"][0]["orders"], 2)
        difference = self.ledger.receipts.between_snapshots("alpha", before, after)
        self.assertEqual(difference["added"], [{"order_id": "new", "sequence": 1}])
        self.assertEqual(difference["changed"][0]["changed_fields"], ["refund_cents"])
        self.assertTrue(self.ledger.receipts.inspect("alpha")["ok"])
