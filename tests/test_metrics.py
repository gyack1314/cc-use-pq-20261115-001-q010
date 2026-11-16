import json

from autumn_ledger.demo import request_data, synthetic_event
from autumn_ledger.errors import LedgerError
from autumn_ledger.metrics.dsl import (normalize_definition, detect_cycle, resolve_units,
                                       evaluate, explain, ok, undefined)
from autumn_ledger.metrics.base import BASE_MEASURES
from autumn_ledger.metrics.runtime import FrozenPlan, MetricContext, apply_metrics
from tests.support import LedgerCase


# 两个业务口径：每个有效出游人的净收入（分/人），以及每城市日期订单数
NET_PER_TRAVELER = {
    "key": "net_per_traveler", "name": "每有效出游人净收入",
    "description": "净收入除以有效出游人数",
    "expression": {"type": "ratio",
                   "numerator": {"type": "measure", "name": "net_cents"},
                   "denominator": {"type": "exposure", "name": "travelers"}}}

ORDERS_PER_CITY_DAY = {
    "key": "orders_per_city_day", "name": "每城市日期订单数",
    "expression": {"type": "ratio",
                   "numerator": {"type": "measure", "name": "orders"},
                   "denominator": {"type": "exposure", "name": "city_days"}}}

# 各城市组的城市日期分母相同，订单不同：组比例的平均 != 汇总分子/汇总分母
REFUND_RATE = {
    "key": "refund_rate", "name": "退款率",
    "expression": {"type": "ratio",
                   "numerator": {"type": "measure", "name": "refund_cents"},
                   "denominator": {"type": "measure", "name": "gross_cents"}}}


def activate(ledger, definition, note="审核启用"):
    draft = ledger.metrics.register(definition)
    return ledger.metrics.activate(draft["key"], draft["version"], "reviewer", note)["metric"]


class DslTests(LedgerCase):
    def test_only_whitelisted_node_types_are_accepted(self):
        for bad in ({"type": "eval", "left": {"type": "measure", "name": "orders"},
                     "right": {"type": "const", "value": 1}},
                    {"type": "lambda", "value": 1},
                    {"type": "import", "name": "os"}):
            with self.subTest(bad=bad):
                self.assertError("invalid_node", normalize_definition,
                                 {"key": "x", "name": "X", "expression": bad})

    def test_unknown_fields_in_node_are_rejected(self):
        self.assertError("unknown_fields", normalize_definition,
                         {"key": "x", "name": "X",
                          "expression": {"type": "measure", "name": "orders", "__import__": "os"}})

    def test_unknown_measure_and_exposure_rejected(self):
        self.assertError("unknown_measure", normalize_definition,
                         {"key": "x", "name": "X", "expression": {"type": "measure", "name": "ghost"}})
        self.assertError("unknown_exposure", normalize_definition,
                         {"key": "y", "name": "Y", "expression": {"type": "exposure", "name": "ghost"}})

    def test_unit_conflict_on_add_reports_dimensions(self):
        expression = {"type": "add",
                      "left": {"type": "measure", "name": "net_cents"},
                      "right": {"type": "measure", "name": "travelers"}}
        normalized = normalize_definition({"key": "bad", "name": "冲突", "expression": expression})
        with self.assertRaises(LedgerError) as context:
            resolve_units(normalized["expression"], lambda k, v: None)
        self.assertEqual(context.exception.code, "unit_conflict")
        self.assertEqual(context.exception.details["left"]["kind"], "currency")

    def test_cycle_detection_returns_closed_path(self):
        def ref(key):
            return {"type": "metric", "key": key, "version": 1}
        definitions = {
            "a": normalize_definition({"key": "a", "name": "A",
                                       "expression": ref("b")})["expression"],
            "b": normalize_definition({"key": "b", "name": "B",
                                       "expression": ref("c")})["expression"],
            "c": normalize_definition({"key": "c", "name": "C",
                                       "expression": ref("a")})["expression"]}
        path = detect_cycle(definitions)
        self.assertEqual(path[0], path[-1])
        self.assertIn("a", path)

    def test_ratio_returns_independent_zero_denominator_state(self):
        node = normalize_definition(NET_PER_TRAVELER)["expression"]
        env = {"measure": lambda name: 0 if name == "travelers" else 120000,
               "exposure": lambda name: ok(0, BASE_MEASURES["travelers"]["unit"]),
               "metric": lambda k, v: None}
        result = evaluate(node, env)
        self.assertEqual(result["status"], "zero_denominator")
        self.assertIsNone(result.get("value"))

    def test_missing_propagates_and_differs_from_zero(self):
        node = normalize_definition(ORDERS_PER_CITY_DAY)["expression"]
        env = {"measure": lambda name: 5,
               "exposure": lambda name: undefined("missing", "缺报"),
               "metric": lambda k, v: None}
        result = evaluate(node, env)
        self.assertEqual(result["status"], "missing")
        env2 = dict(env, exposure=lambda name: ok(0, {"kind": "city_day", "dimension": "city_day"}))
        zero = evaluate(node, env2)
        self.assertEqual(zero["status"], "zero_denominator")
        self.assertNotEqual(zero["status"], result["status"])

    def test_explain_is_readable_and_contains_no_code(self):
        text = explain(normalize_definition(NET_PER_TRAVELER)["expression"])
        self.assertIn("比例", text)
        self.assertIn("净收入", text)
        self.assertNotIn("__import__", text)


class RegistryTests(LedgerCase):
    def test_register_versioning_and_activation_lifecycle(self):
        first = self.ledger.metrics.register(NET_PER_TRAVELER)
        self.assertEqual(first["version"], 1)
        self.assertEqual(first["state"], "draft")
        activated = self.ledger.metrics.activate(first["key"], 1, "reviewer", "ok")
        self.assertEqual(activated["metric"]["state"], "active")
        # 登记同内容返回既有版本（幂等）
        again = self.ledger.metrics.register(NET_PER_TRAVELER)
        self.assertEqual(again["version"], 1)
        # 修改定义产生新版本草稿，不影响已启用的 v1
        changed = {**NET_PER_TRAVELER, "expression": {"type": "ratio",
                                                      "numerator": {"type": "measure", "name": "gross_cents"},
                                                      "denominator": {"type": "exposure", "name": "travelers"}}}
        second = self.ledger.metrics.register(changed)
        self.assertEqual(second["version"], 2)
        self.assertEqual(self.ledger.metrics.get("net_per_traveler", 1)["state"], "active")

    def test_activation_rejects_missing_referenced_version(self):
        dangling = {"key": "dangling", "name": "悬空",
                    "expression": {"type": "metric", "key": "ghost", "version": 3}}
        draft = self.ledger.metrics.register(dangling)
        error = self.assertError("missing_metric_version", self.ledger.metrics.activate,
                                 "dangling", draft["version"], "r", "n")
        self.assertEqual(error.details["references"], [{"key": "ghost", "version": 3}])

    def test_activation_rejects_reference_to_inactive_draft(self):
        dependency = self.ledger.metrics.register(NET_PER_TRAVELER)  # 保持 draft
        child = {"key": "child", "name": "子口径",
                 "expression": {"type": "mul",
                                "left": {"type": "metric", "key": "net_per_traveler",
                                         "version": dependency["version"]},
                                "right": {"type": "const", "value": 2}}}
        child_draft = self.ledger.metrics.register(child)
        self.assertError("inactive_reference", self.ledger.metrics.activate,
                         "child", child_draft["version"], "r", "n")

    def test_cyclic_definitions_cannot_activate(self):
        activate(self.ledger, NET_PER_TRAVELER)
        a = {"key": "loop_a", "name": "环A",
             "expression": {"type": "measure", "name": "orders"}}
        activate(self.ledger, a)
        # loop_b 引用 loop_a，再把 loop_a 改成引用 loop_b 形成环
        b_draft = self.ledger.metrics.register(
            {"key": "loop_b", "name": "环B",
             "expression": {"type": "metric", "key": "loop_a", "version": 1}})
        self.ledger.metrics.activate("loop_b", b_draft["version"], "r", "n")
        cyclic_a = {"key": "loop_a", "name": "环A改",
                    "expression": {"type": "metric", "key": "loop_b", "version": 1}}
        new_a = self.ledger.metrics.register(cyclic_a)
        error = self.assertError("cyclic_dependency", self.ledger.metrics.activate,
                                 "loop_a", new_a["version"], "r", "n")
        self.assertEqual(error.details["path"][0], error.details["path"][-1])

    def test_modifying_active_metric_reports_downstream_dependents(self):
        activate(self.ledger, NET_PER_TRAVELER)
        child_def = {"key": "double_net", "name": "两倍人均净额",
                     "expression": {"type": "mul",
                                    "left": {"type": "metric", "key": "net_per_traveler", "version": 1},
                                    "right": {"type": "const", "value": 2}}}
        activate(self.ledger, child_def)
        dependents = self.ledger.metrics.dependents("net_per_traveler")
        self.assertEqual(dependents, ["double_net"])
        # 被下游引用时不能直接停用
        self.assertError("metric_in_use", self.ledger.metrics.disable,
                         "net_per_traveler", 1, "op", "停用")

    def test_rename_creates_draft_but_keeps_frozen_history(self):
        activated = activate(self.ledger, NET_PER_TRAVELER)
        renamed = self.ledger.metrics.rename("net_per_traveler", "人均净收入(修订名)", "op")
        self.assertEqual(renamed["version"], activated["version"] + 1)
        self.assertEqual(renamed["state"], "draft")
        self.assertEqual(self.ledger.metrics.get("net_per_traveler", activated["version"])["name"],
                         "每有效出游人净收入")


class RuntimeAndEditionTests(LedgerCase):
    def setUp(self):
        super().setUp()
        activate(self.ledger, NET_PER_TRAVELER)
        activate(self.ledger, ORDERS_PER_CITY_DAY)
        # 制造组分母不同的数据：a1 退款 10000，使深圳组退款率非零
        self.ledger.intake.ingest("alpha", "refund-a1", [synthetic_event("a1", sequence=2, refund=10000)])
        activate(self.ledger, REFUND_RATE)

    def test_edition_freezes_metric_versions_and_computes_totals(self):
        edition = self.ledger.editions.create("e1", request_data())
        result = self.ledger.editions.get(edition["id"])["result"]
        freeze = result["metric_freeze"]
        self.assertEqual(freeze["schema"], "autumn-metric-freeze-v1")
        self.assertEqual(len(freeze["entries"]), 3)
        totals = result["periods"]["current"]["cube"]["totals"]["custom_metrics"]
        # 3 个有效订单、9 名旅客；setUp 中 a1 退款 10000，净收入 110000 分
        npt = totals["net_per_traveler"]
        self.assertEqual(npt["status"], "ok")
        self.assertAlmostEqual(npt["value"], 110000 / 9)
        # 14 个城市日期暴露（2 城市 x 7 天）
        opd = totals["orders_per_city_day"]
        self.assertAlmostEqual(opd["value"], 3 / 14)
        self.assertEqual(len(freeze["entries"]), 3)

    def test_ratio_total_is_recomputed_not_average_of_group_ratios(self):
        edition = self.ledger.editions.create("e2", request_data())
        cube = self.ledger.editions.get(edition["id"])["result"]["periods"]["current"]["cube"]
        group_rows = cube["tables"]["origins"]
        group_ratios = [row["measures"]["custom_metrics"]["refund_rate"]["value"]
                        for row in group_rows]
        # 总计必须等于 汇总退款/汇总总额，而不是各组退款率的算术平均
        total_metric = cube["totals"]["custom_metrics"]["refund_rate"]
        self.assertEqual(total_metric["status"], "ok")
        total_refund = sum(row["measures"]["refund_cents"] for row in group_rows)
        total_gross = sum(row["measures"]["gross_cents"] for row in group_rows)
        self.assertAlmostEqual(total_metric["value"], total_refund / total_gross)
        average_of_groups = sum(group_ratios) / len(group_ratios)
        self.assertNotAlmostEqual(total_metric["value"], average_of_groups)
        # 同时验证 orders_per_city_day 总计与城市日期暴露口径一致
        self.assertAlmostEqual(
            cube["totals"]["custom_metrics"]["orders_per_city_day"]["value"],
            cube["totals"]["orders"] / 14)

    def test_daily_rows_carry_metric_states_and_missing_differs_from_zero(self):
        # 增加一个依赖完整覆盖暴露的口径，用于区分 missing 与零分母
        complete_rate_def = {"key": "orders_per_complete_day", "name": "每完整城市日期订单数",
                             "expression": {"type": "ratio",
                                            "numerator": {"type": "measure", "name": "orders"},
                                            "denominator": {"type": "exposure", "name": "complete_city_days"}}}
        activate(self.ledger, complete_rate_def)
        # 引入缺报：beta 来源无任何覆盖登记
        edition = self.ledger.editions.create("e3", request_data(sources=["alpha", "beta"]))
        result = self.ledger.editions.get(edition["id"])["result"]
        daily = result["periods"]["current"]["cube"]["tables"]["daily"]
        complete_statuses = {row["measures"]["custom_metrics"]["orders_per_complete_day"]["status"]
                             for row in daily}
        # 缺报格点上完整暴露率为 missing；而城市日期暴露率仍可计算为 ok
        self.assertIn("missing", complete_statuses)
        city_day_statuses = {row["measures"]["custom_metrics"]["orders_per_city_day"]["status"]
                             for row in daily}
        self.assertEqual(city_day_statuses, {"ok"})

    def test_zero_denominator_state_preserved_when_no_travelers(self):
        # 只保留取消订单：有效出游人数为 0（a1 已到 sequence 2，这里用 sequence 3）
        self.ledger.intake.ingest("alpha", "cancel-all", [
            synthetic_event("a1", sequence=3, state="cancelled", refund=40000),
            synthetic_event("a2", sequence=2, state="cancelled"),
            synthetic_event("a3", sequence=2, state="cancelled")])
        edition = self.ledger.editions.create("e4", request_data())
        totals = self.ledger.editions.get(edition["id"])["result"]["periods"]["current"]["cube"]["totals"]
        npt = totals["custom_metrics"]["net_per_traveler"]
        self.assertEqual(npt["status"], "zero_denominator")
        self.assertIsNone(npt["value"])

    def test_not_applicable_state_when_group_cannot_locate_city_exposure(self):
        edition = self.ledger.editions.create("e-na", request_data())
        result = self.ledger.editions.get(edition["id"])["result"]
        # 目的地维度与出发城市没有一一对应，城市日期分母不适用
        destination_rows = result["periods"]["current"]["cube"]["tables"]["destinations"]
        for row in destination_rows:
            metric = row["measures"]["custom_metrics"]["orders_per_city_day"]
            self.assertEqual(metric["status"], "not_applicable")
            self.assertTrue(metric["reason"])
        # 而 origin（出发城市）维度可以定位城市日期，仍为可计算状态
        origin_rows = result["periods"]["current"]["cube"]["tables"]["origins"]
        self.assertTrue(all(row["measures"]["custom_metrics"]["orders_per_city_day"]["status"] == "ok"
                            for row in origin_rows))

    def test_frozen_replay_survives_disable_and_new_activation(self):
        edition = self.ledger.editions.create("e5", request_data())
        stored = self.ledger.editions.get(edition["id"])["result"]
        # 停用后再启用一个修改版口径
        self.ledger.metrics.disable("orders_per_city_day", 1, "op", "停用旧暴露率")
        changed = {"key": "orders_per_city_day", "name": "每完整城市日期订单数",
                   "expression": {"type": "ratio",
                                  "numerator": {"type": "measure", "name": "orders"},
                                  "denominator": {"type": "exposure", "name": "complete_city_days"}}}
        draft = self.ledger.metrics.register(changed)
        self.ledger.metrics.activate("orders_per_city_day", draft["version"], "r", "新口径")
        # 历史重放仍按冻结的 v1，结果字节级一致
        verify = self.ledger.editions.verify(edition["id"])
        self.assertTrue(verify["ok"])
        self.assertEqual(self.ledger.editions.get(edition["id"])["result"], stored)

    def test_ratio_of_metrics_recomputes_total_from_aggregated_metric_values(self):
        # 先有一个“人均净收入”指标，再定义引用它的衍生比例（人均净额相对常量阈值的比率无意义，
        # 这里改用两个指标相除：人均净额 / 人均毛额），验证总计仍按汇总指标值重算
        gross_per_traveler = {
            "key": "gross_per_traveler", "name": "每出游人毛额",
            "expression": {"type": "ratio",
                           "numerator": {"type": "measure", "name": "gross_cents"},
                           "denominator": {"type": "exposure", "name": "travelers"}}}
        gpt = activate(self.ledger, gross_per_traveler)
        derived = {"key": "net_to_gross_per_traveler", "name": "人均净额比人均毛额",
                   "expression": {"type": "ratio",
                                  "numerator": {"type": "metric", "key": "net_per_traveler", "version": 1},
                                  "denominator": {"type": "metric", "key": "gross_per_traveler",
                                                  "version": gpt["version"]}}}
        activate(self.ledger, derived)
        edition = self.ledger.editions.create("e-derived", request_data())
        totals = self.ledger.editions.get(edition["id"])["result"]["periods"]["current"]["cube"]["totals"]
        derived_total = totals["custom_metrics"]["net_to_gross_per_traveler"]
        # 必须等于 汇总净/汇总人 除以 汇总毛/汇总人 = 汇总净/汇总毛
        self.assertEqual(derived_total["status"], "ok")
        self.assertAlmostEqual(derived_total["value"],
                               totals["net_cents"] / totals["gross_cents"])

    def test_comparison_includes_custom_metrics_with_states(self):
        edition = self.ledger.editions.create("e6", request_data())
        comparison = self.ledger.editions.get(edition["id"])["result"]["comparison"]
        self.assertIn("net_per_traveler", comparison["custom_metrics"])
        entry = comparison["custom_metrics"]["net_per_traveler"]
        self.assertTrue(entry["comparable"])
        self.assertIsNotNone(entry["absolute_change"])

    def test_tampered_freeze_set_is_detected_on_replay(self):
        edition = self.ledger.editions.create("e9", request_data())
        identity = edition["id"]
        with self.ledger.database.transaction(write=True) as connection:
            row = connection.execute("SELECT result FROM editions WHERE id=?", (identity,)).fetchone()
            stored = json.loads(row["result"])
            # 篡改冻结指标的分母（城市日期 -> 订单数）
            for entry in stored["metric_freeze"]["entries"]:
                if entry["key"] == "orders_per_city_day":
                    entry["expression"]["denominator"]["name"] = "orders"
            connection.execute("UPDATE editions SET result=? WHERE id=?",
                               (json.dumps(stored), identity))
        # 结果摘要首先检出篡改；即便绕过摘要，verify 的 periods 比对也会失败
        self.assertError("edition_corrupt", self.ledger.editions.verify, identity)

    def test_export_evidence_and_bundle_contain_definitions(self):
        edition = self.ledger.editions.create("e7", request_data())
        identity = edition["id"]
        evidence = self.ledger.export.metric_evidence(identity)
        self.assertEqual(evidence["freeze_digest"],
                         self.ledger.editions.get(identity)["result"]["metric_freeze"]["digest"])
        self.assertTrue(all(item["readable_definition"] for item in evidence["definitions"]))
        csv_content = self.ledger.export.metric_values_csv(identity)
        self.assertIn("zero_denominator", csv_content + "ok")  # 状态列存在
        bundle = self.ledger.export.bundle(identity)
        import io, zipfile
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            self.assertIn("metric-definitions.json", archive.namelist())
            self.assertIn("current/metric-values.csv", archive.namelist())
        self.assertTrue(self.ledger.export.verify_bundle(bundle)["ok"])


class MetricTrialTests(LedgerCase):
    def test_parallel_trial_on_same_data_revision(self):
        activate(self.ledger, NET_PER_TRAVELER)
        candidate_def = {"key": "gross_per_traveler", "name": "每出游人总额",
                         "expression": {"type": "ratio",
                                        "numerator": {"type": "measure", "name": "gross_cents"},
                                        "denominator": {"type": "exposure", "name": "travelers"}}}
        draft = self.ledger.metrics.register(candidate_def)
        trial = self.ledger.metric_trials.run_trial(request_data(), "gross_per_traveler",
                                                     draft["version"], "analyst", "trial-1")
        self.assertEqual(trial["candidate"], {"key": "gross_per_traveler", "version": draft["version"]})
        current = trial["comparison"]["periods"]["current"]["totals"]
        keys = {row["metric"] for row in current}
        self.assertIn("gross_per_traveler", keys)
        # 试算不改变启用状态
        self.assertEqual(self.ledger.metrics.get("gross_per_traveler")["state"], "draft")
        fetched = self.ledger.metric_trials.get("trial-1")
        self.assertEqual(fetched["result_digest"], trial["result_digest"])

    def test_trial_rejects_non_draft_candidate(self):
        activate(self.ledger, NET_PER_TRAVELER)
        self.assertError("invalid_transition", self.ledger.metric_trials.run_trial,
                         request_data(), "net_per_traveler", 1, "analyst")

    def test_only_approved_definitions_reach_publication(self):
        # 草稿口径不会进入任何报告的冻结集
        self.ledger.metrics.register(NET_PER_TRAVELER)
        edition = self.ledger.editions.create("e8", request_data())
        result = self.ledger.editions.get(edition["id"])["result"]
        self.assertEqual(result["metric_freeze"]["entries"], [])
