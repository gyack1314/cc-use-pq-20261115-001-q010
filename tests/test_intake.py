from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

from autumn_ledger.application import Ledger
from autumn_ledger.demo import synthetic_event
from autumn_ledger.records.contracts import normalize_event, contribution
from tests.support import LedgerCase


class IntakeTests(LedgerCase):
    def test_idempotent_batch_does_not_advance_revision(self):
        before = self.ledger.database.revision()
        result = self.ledger.intake.ingest("alpha", "synthetic-initial", self.events)
        self.assertTrue(result["replayed"])
        self.assertEqual(result["accepted"], 5)
        self.assertEqual(self.ledger.database.revision(), before)
        self.assertEqual(len(self.ledger.intake.heads()), 5)

    def test_same_batch_different_payload_conflicts(self):
        before = self.ledger.database.revision()
        changed = deepcopy(self.events)
        changed[0]["gross_cents"] = 999
        self.assertError("conflict", self.ledger.intake.ingest, "alpha", "synthetic-initial", changed)
        self.assertEqual(self.ledger.database.revision(), before)
        self.assertEqual(self.ledger.intake.history("alpha", "a1")[0]["gross_cents"], 50000)

    def test_identical_event_in_new_batch_is_duplicate(self):
        result = self.ledger.intake.ingest("alpha", "retry", [self.events[0]])
        self.assertEqual(result["accepted"], 0)
        self.assertEqual(result["duplicates"], 1)
        self.assertFalse(result["replayed"])
        self.assertEqual(len(self.ledger.intake.history("alpha", "a1")), 1)

    def test_duplicate_inside_batch_counts_once(self):
        event = synthetic_event("fresh")
        result = self.ledger.intake.ingest("alpha", "same-row", [event, event])
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(len(self.ledger.intake.history("alpha", "fresh")), 1)
        self.assertTrue(self.ledger.receipts.inspect("alpha")["ok"])

    def test_event_identity_conflict_rolls_back_entire_batch(self):
        before = self.ledger.database.revision()
        altered = dict(self.events[0], gross_cents=80000)
        self.assertError("conflict", self.ledger.intake.ingest, "alpha", "atomic", [synthetic_event("new"), altered])
        self.assertEqual(self.ledger.database.revision(), before)
        self.assertError("not_found", self.ledger.intake.history, "alpha", "new")
        self.assertError("not_found", self.ledger.intake.batch, "alpha", "atomic")

    def test_in_batch_conflicting_event_identity(self):
        event = synthetic_event("fresh")
        other = dict(event, gross_cents=90000)
        self.assertError("conflict", self.ledger.intake.ingest, "alpha", "bad", [event, other])
        self.assertError("not_found", self.ledger.intake.history, "alpha", "fresh")
        self.assertTrue(self.ledger.database.integrity()["ok"])

    def test_existing_sequence_cannot_be_renamed(self):
        other = dict(self.events[0], event_id="renamed")
        error = self.assertError("conflict", self.ledger.intake.ingest, "alpha", "sequence-collision", [other])
        self.assertEqual(error.status, 409)
        self.assertEqual(error.details["event_id"], "a1-1")

    def test_in_batch_sequence_collision_is_atomic(self):
        event = synthetic_event("fresh")
        other = dict(event, event_id="other")
        self.assertError("conflict", self.ledger.intake.ingest, "alpha", "collision", [event, other])
        self.assertError("not_found", self.ledger.intake.history, "alpha", "fresh")
        self.assertTrue(self.ledger.receipts.inspect("alpha")["ok"])

    def test_revision_head_and_historical_head(self):
        old = self.ledger.database.revision()
        update = synthetic_event("a1", sequence=2, refund=17000)
        receipt = self.ledger.intake.ingest("alpha", "refund", [update])
        self.assertGreater(receipt["revision"], old)
        old_head = next(row for row in self.ledger.intake.heads(old) if row["order_id"] == "a1")
        new_head = next(row for row in self.ledger.intake.heads() if row["order_id"] == "a1")
        self.assertEqual(old_head["sequence"], 1)
        self.assertEqual(old_head["refund_cents"], 0)
        self.assertEqual(new_head["sequence"], 2)
        self.assertEqual(new_head["refund_cents"], 17000)

    def test_lower_sequence_rejected_without_new_head(self):
        self.ledger.intake.ingest("alpha", "jump", [synthetic_event("a1", sequence=4)])
        before = self.ledger.database.revision()
        self.assertError("stale_event", self.ledger.intake.ingest, "alpha", "late", [synthetic_event("a1", sequence=3)])
        self.assertEqual(self.ledger.database.revision(), before)
        self.assertEqual(self.ledger.intake.history("alpha", "a1")[-1]["sequence"], 4)

    def test_sequence_gap_is_allowed_and_disclosed(self):
        self.ledger.intake.ingest("alpha", "jump", [synthetic_event("a1", sequence=4)])
        receipts = self.ledger.receipts.inspect("alpha")
        item = next(row for row in receipts["orders"] if row["order_id"] == "a1")
        self.assertEqual(item["sequence_gaps"], [{"after": 1, "before": 4}])
        self.assertTrue(receipts["ok"])
        self.assertEqual(item["events"], 2)

    def test_event_timestamp_cannot_go_backwards(self):
        event = synthetic_event("a1", sequence=2)
        event["occurred_at"] = "2026-10-01T00:00:00Z"
        self.assertError("event_time_reversed", self.ledger.intake.ingest, "alpha", "time", [event])
        self.assertEqual(len(self.ledger.intake.history("alpha", "a1")), 1)

    def test_cancelled_order_cannot_reopen(self):
        cancel = synthetic_event("a1", sequence=2, state="cancelled", refund=40000)
        self.ledger.intake.ingest("alpha", "cancel", [cancel])
        self.assertError("cancelled_terminal", self.ledger.intake.ingest, "alpha", "reopen", [synthetic_event("a1", sequence=3)])
        history = self.ledger.intake.history("alpha", "a1")
        self.assertEqual(history[-1]["state"], "cancelled")
        self.assertEqual(contribution(history[-1])["net_cents"], 10000)

    def test_cancelled_refund_can_be_corrected(self):
        self.ledger.intake.ingest("alpha", "cancel", [synthetic_event("a1", sequence=2, state="cancelled", refund=40000)])
        self.ledger.intake.ingest("alpha", "refund", [synthetic_event("a1", sequence=3, state="cancelled", refund=50000)])
        head = self.ledger.intake.history("alpha", "a1")[-1]
        self.assertEqual(contribution(head)["orders"], 0)
        self.assertEqual(contribution(head)["travelers"], 0)
        self.assertEqual(contribution(head)["net_cents"], 0)

    def test_source_identity_partitions_same_order_ids(self):
        result = self.ledger.intake.ingest("beta", "independent", [self.events[0]])
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(len(self.ledger.intake.heads()), 6)
        self.assertEqual(len(self.ledger.intake.heads(sources=["beta"])), 1)
        self.assertEqual(len(self.ledger.intake.history("alpha", "a1")), 1)

    def test_unknown_reference_rolls_back_earlier_event(self):
        invalid = synthetic_event("unknown", origin="elsewhere")
        before = self.ledger.database.revision()
        self.assertError("not_found", self.ledger.intake.ingest, "alpha", "unknown", [synthetic_event("valid"), invalid])
        self.assertEqual(self.ledger.database.revision(), before)
        self.assertError("not_found", self.ledger.intake.history, "alpha", "valid")

    def test_disabled_source_blocks_new_batch_but_allows_replay(self):
        self.ledger.catalog.put("source", "alpha", {"name": "来源甲", "active": False}, 1)
        self.assertError("inactive_reference", self.ledger.intake.ingest, "alpha", "new", [synthetic_event("new")])
        replay = self.ledger.intake.ingest("alpha", "synthetic-initial", self.events)
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(self.ledger.intake.heads()), 5)

    def test_parallel_same_batch_is_exactly_once(self):
        def submit(_):
            return Ledger(self.path).intake.ingest("alpha", "parallel", [synthetic_event("parallel")])
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(submit, range(4)))
        self.assertEqual(sum(not row["replayed"] for row in results), 1)
        self.assertEqual(len({row["revision"] for row in results}), 1)
        self.assertEqual(len(self.ledger.intake.history("alpha", "parallel")), 1)
        self.assertTrue(self.ledger.database.integrity()["ok"])

    def test_validation_rejects_boolean_money_and_excess_refund(self):
        for changes, code in (({"gross_cents": True}, "invalid_integer"),
                              ({"refund_cents": 50001}, "excess_refund"),
                              ({"children": 0}, "invalid_family"),
                              ({"family": False}, "invalid_family"),
                              ({"occurred_at": "2026-11-01T01:00:00"}, "invalid_timestamp")):
            with self.subTest(changes=changes):
                self.assertError(code, normalize_event, dict(self.events[0], **changes))
        self.assertTrue(self.ledger.database.integrity()["ok"])

    def test_batch_listing_and_pagination(self):
        self.ledger.intake.ingest("alpha", "second", [synthetic_event("second")])
        self.ledger.intake.ingest("beta", "third", [synthetic_event("third")])
        self.assertEqual(self.ledger.intake.list_batches(limit=1)[0]["id"], "third")
        self.assertEqual(self.ledger.intake.list_batches(source="alpha", limit=1)[0]["id"], "second")
        self.assertEqual(self.ledger.intake.list_batches(source="alpha", offset=1)[0]["id"], "synthetic-initial")
        self.assertError("out_of_range", self.ledger.intake.list_batches, limit=0)
