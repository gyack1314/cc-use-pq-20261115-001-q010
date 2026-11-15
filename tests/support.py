import tempfile
import unittest
from pathlib import Path

from autumn_ledger.application import Ledger
from autumn_ledger.demo import seed, synthetic_event, request_data
from autumn_ledger.errors import LedgerError


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="ledger-test-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "ledger.sqlite3"
        self.ledger = Ledger(self.path)
        self.events = seed(self.ledger)

    def assertError(self, code, function, *args, **kwargs):
        with self.assertRaises(LedgerError) as context:
            function(*args, **kwargs)
        self.assertEqual(context.exception.code, code)
        return context.exception

    def edition(self, key="edition", **changes):
        return self.ledger.editions.create(key, request_data(**changes))

    def approve(self, edition):
        return self.ledger.review.decide(edition["id"], "approve", edition["version"], "analyst", "核查通过")

    def publish(self, edition, channel="weekly", generation=0):
        approved = self.approve(edition)
        return self.ledger.review.publish(edition["id"], channel, approved["version"], generation, "publisher")
