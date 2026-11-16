"""新口径试算：在同一数据版本上与现行启用口径并行计算，供审核比对。

试算不改变任何启用状态，结果持久化到 metric_trials 作为审核证据；
候选必须是已登记的 draft，且其依赖的指标版本均已启用。
"""

import json
import uuid

from ..errors import missing, require
from ..values import canonical, fingerprint, identifier, integer, text, utc_now
from ..analysis.windows import normalize_request
from .registry import DRAFT


class MetricTrials:
    def __init__(self, database, registry, engine):
        self.database = database
        self.registry = registry
        self.engine = engine

    def _build_candidate_freeze(self, connection, candidate_key, candidate_version):
        """候选草稿 + 其全部已启用依赖，组成试算冻结集；复用启用前同款校验。"""
        row = self.registry._row(connection, candidate_key, candidate_version)
        require(row["state"] == DRAFT, "invalid_transition", "只有草稿版本可以发起试算", 409)
        import json
        candidate_expression = json.loads(row["expression"])

        # 复用注册校验逻辑：引用存在、依赖已启用、无环、单位一致
        self.registry.validate_activation(connection, candidate_key, candidate_version)

        base_freeze = self.registry.freeze_set(connection)
        entries = {entry["key"]: entry for entry in base_freeze["entries"]}
        candidate_entry = {"key": candidate_key, "version": candidate_version,
                           "state": DRAFT, "name": row["name"], "description": row["description"],
                           "expression": candidate_expression,
                           "definition_digest": row["definition_digest"]}
        entries[candidate_key] = candidate_entry
        ordered = [entries[key] for key in sorted(entries)]
        return base_freeze, {"schema": "autumn-metric-freeze-v1", "entries": ordered,
                             "digest": fingerprint(ordered), "frozen_at": utc_now(),
                             "base_catalog": base_freeze["base_catalog"]}

    def run_trial(self, request, candidate_key, candidate_version, actor, trial_key=None):
        identifier(candidate_key, "candidate_key")
        integer(candidate_version, "candidate_version", 1)
        actor = text(actor, "试算人", 100)
        normalized = normalize_request(request)
        with self.database.transaction(write=True) as connection:
            baseline_freeze, candidate_freeze = self._build_candidate_freeze(
                connection, candidate_key, candidate_version)
            revision = self.database.snapshot_revision(connection)
            # 同一事务内用同一份订单快照分别按两套冻结集计算
            baseline_result = self.engine.run(normalized, connection, metric_freeze=baseline_freeze)
            candidate_result = self.engine.run(normalized, connection, metric_freeze=candidate_freeze)
            comparison = self._compare(baseline_result, candidate_result, baseline_freeze,
                                       candidate_freeze, candidate_key)
            record = {"schema": "autumn-metric-trial-v1", "request": normalized,
                      "data_revision": revision, "candidate": {"key": candidate_key,
                                                                "version": candidate_version},
                      "baseline_freeze_digest": baseline_freeze["digest"],
                      "candidate_freeze_digest": candidate_freeze["digest"],
                      "comparison": comparison, "calculated_at": utc_now()}
            digest = fingerprint(record)
            trial_id = trial_key or ("mt-" + uuid.uuid4().hex)
            identifier(trial_id, "trial_id")
            existing = connection.execute("SELECT id FROM metric_trials WHERE id=?", (trial_id,)).fetchone()
            require(existing is None, "conflict", "试算号已存在", 409)
            connection.execute(
                "INSERT INTO metric_trials VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (trial_id, trial_id, revision, candidate_key, candidate_version,
                 canonical(baseline_freeze), canonical(candidate_freeze), canonical(record),
                 digest, actor, utc_now()))
            return {"trial_id": trial_id, "result_digest": digest, **record}

    def _view(self, value):
        return {"status": value["status"],
                "value": value["value"] if value["status"] == "ok" else None,
                "reason": value.get("reason")}

    def _compare(self, baseline_result, candidate_result, baseline_freeze, candidate_freeze, candidate_key):
        periods = {}
        for name in candidate_result["periods"]:
            base_total = baseline_result["periods"][name]["cube"]["totals"].get("custom_metrics", {})
            cand_total = candidate_result["periods"][name]["cube"]["totals"].get("custom_metrics", {})
            keys = sorted(set(base_total) | set(cand_total))
            totals = []
            for key in keys:
                before = base_total.get(key)
                after = cand_total.get(key)
                row = {"metric": key, "baseline": self._view(before) if before else None,
                       "candidate": self._view(after) if after else None}
                if before and after and before["status"] == "ok" and after["status"] == "ok":
                    row["absolute_change"] = after["value"] - before["value"]
                else:
                    row["absolute_change"] = None
                totals.append(row)
            # 分组级并行结果（以 origins 表示例，保留全部表）
            tables = {}
            for table_name, rows in candidate_result["periods"][name]["cube"]["tables"].items():
                base_rows = baseline_result["periods"][name]["cube"]["tables"][table_name]
                paired = []
                for index, row in enumerate(rows):
                    base_metrics = base_rows[index]["measures"].get("custom_metrics", {})
                    cand_metrics = row["measures"].get("custom_metrics", {})
                    paired.append({"dimensions": row.get("dimensions", row),
                                   "baseline": {k: self._view(v) for k, v in base_metrics.items()},
                                   "candidate": {k: self._view(v) for k, v in cand_metrics.items()}})
                tables[table_name] = paired
            periods[name] = {"totals": totals, "tables": tables}
        return {"periods": periods,
                "candidate_added": sorted({e["key"] for e in candidate_freeze["entries"]}
                                          - {e["key"] for e in baseline_freeze["entries"]}),
                "note": "试算仅用于审核比对，不改变任何启用口径；比例总计按汇总分子分母重算"}

    def get(self, trial_id):
        identifier(trial_id)
        import json
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM metric_trials WHERE id=?", (trial_id,)).fetchone()
            if row is None:
                raise missing("试算记录", trial_id)
            record = json.loads(row["result"])
            require(fingerprint(record) == row["result_digest"], "trial_corrupt", "试算内容校验失败", 500)
            return {"trial_id": trial_id, "actor": row["actor"], "created_at": row["created_at"],
                    "result_digest": row["result_digest"], **record}

    def list(self, limit=100, offset=0):
        from ..values import page_size
        page_size(limit, offset)
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT id,data_revision,candidate_key,candidate_version,actor,created_at,result_digest"
                " FROM metric_trials ORDER BY created_at,id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return [dict(row) for row in rows]
