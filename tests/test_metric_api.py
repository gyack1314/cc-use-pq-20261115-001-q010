from fastapi.testclient import TestClient

from autumn_ledger.api.server import create_app
from autumn_ledger.demo import request_data
from tests.support import LedgerCase


NET_PER_TRAVELER = {
    "key": "net_per_traveler", "name": "每有效出游人净收入",
    "expression": {"type": "ratio",
                   "numerator": {"type": "measure", "name": "net_cents"},
                   "denominator": {"type": "exposure", "name": "travelers"}}}


class MetricApiTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.path)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def test_register_trial_activate_publish_lifecycle(self):
        # 1. 登记草稿
        created = self.client.put("/metrics", json=NET_PER_TRAVELER)
        self.assertEqual(created.status_code, 200, created.text)
        version = created.json()["version"]
        self.assertEqual(created.json()["state"], "draft")

        # 2. 同数据版本并行试算（草稿未启用也可试算）
        trial = self.client.post("/metric-trials",
                                 json={"request": request_data(), "candidate_key": "net_per_traveler",
                                       "candidate_version": version, "actor": "analyst",
                                       "trial_id": "mt-http-1"})
        self.assertEqual(trial.status_code, 200, trial.text)
        self.assertEqual(trial.json()["candidate"], {"key": "net_per_traveler", "version": version})
        self.assertEqual(self.client.get("/metric-trials/mt-http-1").status_code, 200)

        # 3. 启用前草稿不会进入报告
        edition_before = self.client.post("/editions", json={"request_key": "before",
                                                              "request": request_data()}).json()
        self.assertEqual(
            self.client.get(f"/editions/{edition_before['id']}").json()["result"]["metric_freeze"]["entries"],
            [])

        # 4. 审核启用
        activated = self.client.post("/metrics/net_per_traveler/activate",
                                     json={"version": version, "actor": "reviewer", "note": "口径核对通过"})
        self.assertEqual(activated.status_code, 200, activated.text)
        self.assertEqual(activated.json()["metric"]["state"], "active")

        # 5. 启用后创建报告，冻结集含该指标，总计可解释
        edition = self.client.post("/editions", json={"request_key": "after",
                                                       "request": request_data()}).json()
        result = self.client.get(f"/editions/{edition['id']}").json()["result"]
        metric = result["periods"]["current"]["cube"]["totals"]["custom_metrics"]["net_per_traveler"]
        self.assertEqual(metric["status"], "ok")
        self.assertTrue(metric["value"] > 0)
        self.assertIsNotNone(result["metric_freeze"]["digest"])

        # 6. 审核通过并发布，公开频道带定义证据
        approve = self.client.post(f"/editions/{edition['id']}/decision",
                                   json={"action": "approve", "expected_version": 1,
                                         "actor": "analyst", "note": "ok"})
        self.assertEqual(approve.status_code, 200, approve.text)
        publish = self.client.post(f"/editions/{edition['id']}/publish",
                                   json={"channel": "weekly", "expected_version": 2,
                                         "expected_generation": 0, "actor": "publisher"})
        self.assertEqual(publish.status_code, 200, publish.text)
        evidence = self.client.get(f"/editions/{edition['id']}/metric-evidence")
        self.assertEqual(evidence.status_code, 200, evidence.text)
        self.assertTrue(evidence.json()["definitions"][0]["readable_definition"])
        csv_resp = self.client.get(f"/editions/{edition['id']}/metric-values.csv")
        self.assertEqual(csv_resp.status_code, 200)
        self.assertIn("net_per_traveler", csv_resp.text)

    def test_activation_gate_reports_cycle_and_unit_conflict(self):
        # 单位冲突：货币 + 人数
        bad_unit = {"key": "bad_unit", "name": "单位冲突",
                    "expression": {"type": "add",
                                   "left": {"type": "measure", "name": "net_cents"},
                                   "right": {"type": "measure", "name": "travelers"}}}
        draft = self.client.put("/metrics", json=bad_unit).json()
        resp = self.client.post("/metrics/bad_unit/activate",
                                json={"version": draft["version"], "actor": "r", "note": "n"})
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"]["code"], "unit_conflict")

        # 悬空引用
        dangling = {"key": "dangling", "name": "悬空",
                    "expression": {"type": "metric", "key": "ghost", "version": 9}}
        draft = self.client.put("/metrics", json=dangling).json()
        resp = self.client.post("/metrics/dangling/activate",
                                json={"version": draft["version"], "actor": "r", "note": "n"})
        self.assertEqual(resp.json()["error"]["code"], "missing_metric_version")

    def test_arbitrary_expression_node_is_refused_over_http(self):
        malicious = {"key": "evil", "name": "越权",
                     "expression": {"type": "eval", "code": "__import__('os').system('id')"}}
        resp = self.client.put("/metrics", json=malicious)
        self.assertEqual(resp.status_code, 422)
        self.assertIn(resp.json()["error"]["code"], {"invalid_node", "unknown_fields"})

    def test_history_and_decisions_are_listed(self):
        draft = self.client.put("/metrics", json=NET_PER_TRAVELER).json()
        self.client.post("/metrics/net_per_traveler/activate",
                         json={"version": draft["version"], "actor": "reviewer", "note": "ok"})
        history = self.client.get("/metrics/net_per_traveler/history").json()
        self.assertEqual(len(history), 1)
        decisions = self.client.get("/metric-decisions?key=net_per_traveler").json()
        self.assertEqual(decisions[0]["action"], "activate")
        self.assertIn("/metrics", self.client.get("/openapi.json").json()["paths"])
