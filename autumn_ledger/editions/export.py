import csv
import io
import json
import zipfile

from ..errors import require
from ..values import canonical, fingerprint, identifier
from ..analysis.cubes import MEASURES
from ..metrics.dsl import explain


TABLE_DIMENSIONS = {"origins": ["origin"], "destinations": ["destination"], "sources": ["source"],
                    "routes": ["origin", "destination"], "daily": ["city", "day", "day_type", "holiday"]}


def csv_value(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    result = str(value)
    if result.startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        result = "'" + result
    return result


def encode_csv(headers, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow([csv_value(row.get(header)) for header in headers])
    return stream.getvalue()


class Export:
    def __init__(self, editions, review):
        self.editions = editions
        self.review = review

    def edition(self, identity, published_only=False):
        edition = self.editions.get(identity)
        if published_only:
            require(edition["state"] == "published", "not_published", "该版本未发布", 409)
        require(edition["state"] != "retired", "retired", "该版本已撤下", 410)
        return edition

    def document(self, identity, published_only=False):
        edition = self.edition(identity, published_only)
        freeze = edition["result"].get("metric_freeze")
        document = {"format": "autumn-ledger-edition-v1", "edition_id": identity,
                    "data_revision": edition["data_revision"], "result_digest": edition["result_digest"],
                    "result": edition["result"]}
        if freeze:
            document["metric_definition_evidence"] = self.metric_evidence(identity)
        return document

    def metric_evidence(self, identity):
        """可读的指标定义证据：冻结摘要 + 每个指标的人类可读算术展开，无任何可执行代码。"""
        edition = self.edition(identity)
        freeze = edition["result"].get("metric_freeze")
        require(freeze, "no_metric_freeze", "该报告没有冻结自定义指标口径", 404)
        definitions = []
        for entry in freeze["entries"]:
            definitions.append({"key": entry["key"], "version": entry["version"], "name": entry["name"],
                                "state": entry["state"], "definition_digest": entry["definition_digest"],
                                "unit": entry["expression"].get("unit"),
                                "readable_definition": explain(entry["expression"])})
        return {"schema": freeze["schema"], "freeze_digest": freeze["digest"],
                "frozen_at": freeze.get("frozen_at"), "base_catalog": freeze.get("base_catalog"),
                "definitions": definitions,
                "replay_note": "证据按定义版本冻结；重命名或停用口径不会改写本文件，重放始终引用这些版本"}

    def metric_values_csv(self, identity, period="current", published_only=False):
        """导出各分组上的自定义指标三态值（含状态与零分母/不适用原因）。"""
        edition = self.edition(identity, published_only)
        result = edition["result"]
        require(period in result["periods"], "unknown_period", "分析没有该期间")
        freeze = result.get("metric_freeze")
        require(freeze and freeze["entries"], "no_metric_freeze", "该报告没有可导出的自定义指标", 404)
        headers = ["table", "dimensions", "metric_key", "metric_version", "status", "value", "reason", "unit"]
        rows = []
        cube = result["periods"][period]["cube"]
        # 总计行
        totals = cube["totals"].get("custom_metrics", {})
        for key in sorted(totals):
            item = totals[key]
            rows.append({"table": "__totals__", "dimensions": "{}", "metric_key": key,
                         "metric_version": self._frozen_version(freeze, key), "status": item["status"],
                         "value": item["value"], "reason": item.get("reason") or "",
                         "unit": canonical(item.get("unit")) if item.get("unit") else ""})
        for table_name, table_rows in cube["tables"].items():
            for row in table_rows:
                dims = canonical(row.get("dimensions", {}))
                for key, item in row["measures"].get("custom_metrics", {}).items():
                    rows.append({"table": table_name, "dimensions": dims, "metric_key": key,
                                 "metric_version": self._frozen_version(freeze, key), "status": item["status"],
                                 "value": item["value"], "reason": item.get("reason") or "",
                                 "unit": canonical(item.get("unit")) if item.get("unit") else ""})
        return encode_csv(headers, rows)

    @staticmethod
    def _frozen_version(freeze, key):
        for entry in freeze["entries"]:
            if entry["key"] == key:
                return entry["version"]
        return ""

    def table(self, identity, table, period="current", published_only=False):
        require(table in TABLE_DIMENSIONS, "unknown_table", "未知统计表")
        edition = self.edition(identity, published_only)
        result = edition["result"]
        require(period in result["periods"], "unknown_period", "分析没有该期间")
        rows = result["periods"][period]["cube"]["tables"][table]
        flat = []
        dimensions = TABLE_DIMENSIONS[table]
        for row in rows:
            values = row["dimensions"] if "dimensions" in row else row
            flat.append({"edition_id": identity, "data_revision": edition["data_revision"], "period": period,
                         **{key: values.get(key) for key in dimensions},
                         **{key: row["measures"][key] for key in MEASURES}})
        headers = ["edition_id", "data_revision", "period"] + dimensions + list(MEASURES)
        return encode_csv(headers, flat)

    def coverage(self, identity, period="current"):
        edition = self.edition(identity)
        require(period in edition["result"]["periods"], "unknown_period", "分析没有该期间")
        rows = edition["result"]["periods"][period]["coverage"]
        return encode_csv(["source", "city", "day", "status", "version", "revision", "note"], rows)

    def lineage(self, identity):
        edition = self.edition(identity)
        return encode_csv(["period", "source", "order_id", "event_id", "sequence", "revision", "digest"],
                          edition["result"]["lineage"])

    def bundle(self, identity, published_only=False):
        document = self.document(identity, published_only)
        result = document["result"]
        files = {"edition.json": canonical(document).encode("utf-8"),
                 "lineage.csv": self.lineage(identity).encode("utf-8")}
        if result.get("metric_freeze") and result["metric_freeze"].get("entries"):
            files["metric-definitions.json"] = canonical(
                document["metric_definition_evidence"]).encode("utf-8")
            for period in result["periods"]:
                files[f"{period}/metric-values.csv"] = self.metric_values_csv(
                    identity, period, published_only).encode("utf-8")
        for period in result["periods"]:
            for table in TABLE_DIMENSIONS:
                files[f"{period}/{table}.csv"] = self.table(identity, table, period, published_only).encode("utf-8")
            files[f"{period}/coverage.csv"] = self.coverage(identity, period).encode("utf-8")
        manifest = {"format": "autumn-ledger-bundle-v1", "edition_id": identity,
                    "result_digest": document["result_digest"],
                    "files": {name: {"bytes": len(content), "sha256": self.bytes_digest(content)}
                              for name, content in sorted(files.items())}}
        files["manifest.json"] = canonical(manifest).encode("utf-8")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in sorted(files.items()):
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, content)
        return output.getvalue()

    @staticmethod
    def bytes_digest(content):
        import hashlib
        return hashlib.sha256(content).hexdigest()

    def public_channel(self, channel):
        current = self.review.channel(channel)
        document = self.document(current["edition_id"], published_only=True)
        return {"channel": channel, "generation": current["generation"],
                "stale": current["edition"]["stale"], **document}

    def verify_bundle(self, content):
        require(isinstance(content, bytes), "invalid_bundle", "归档必须是字节串")
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = archive.namelist()
                require(len(names) == len(set(names)), "invalid_bundle", "归档中存在重复文件")
                require("manifest.json" in names and len(names) <= 30,
                        "invalid_bundle", "归档清单缺失或条目过多")
                require(sum(item.file_size for item in archive.infolist()) <= 50000000,
                        "invalid_bundle", "归档解压大小超过限制")
                manifest = json.loads(archive.read("manifest.json"))
                require(manifest.get("format") == "autumn-ledger-bundle-v1", "invalid_bundle", "归档格式无效")
                expected = set(manifest["files"]) | {"manifest.json"}
                require(expected == set(names), "invalid_bundle", "归档文件与清单不一致")
                for name, details in manifest["files"].items():
                    require(not name.startswith(("/", "\\")) and ".." not in name.split("/"),
                            "invalid_bundle", "归档路径无效")
                    data = archive.read(name)
                    require(len(data) == details["bytes"] and self.bytes_digest(data) == details["sha256"],
                            "invalid_bundle", "归档内容校验失败")
                document = json.loads(archive.read("edition.json"))
                require(fingerprint(document["result"]) == manifest["result_digest"] == document["result_digest"],
                        "invalid_bundle", "分析内容摘要不一致")
                return {"ok": True, "edition_id": manifest["edition_id"], "file_count": len(names),
                        "result_digest": manifest["result_digest"]}
        except (zipfile.BadZipFile, KeyError, ValueError, TypeError):
            require(False, "invalid_bundle", "无法读取分析归档")
