import csv
import io
import json

from ..errors import LedgerError, require
from ..values import fingerprint
from .contracts import EVENT_FIELDS, normalize_event


INTEGER_FIELDS = {"sequence", "adults", "children", "gross_cents", "refund_cents"}
CSV_HEADERS = ["event_id", "order_id", "sequence", "occurred_at", "state", "origin", "destination",
               "travel_day", "family", "adults", "children", "gross_cents", "refund_cents"]


def parse_csv(content):
    require(isinstance(content, str), "invalid_csv", "CSV内容必须是字符串")
    require(len(content.encode("utf-8")) <= 10000000, "csv_too_large", "CSV超过大小限制")
    require("\x00" not in content, "invalid_csv", "CSV含空字符")
    content = content.removeprefix("\ufeff")
    stream = io.StringIO(content, newline="")
    try:
        reader = csv.DictReader(stream, strict=True)
        headers = reader.fieldnames
        require(headers is not None and len(headers) == len(set(headers)), "invalid_csv", "CSV表头缺失或重复")
        require(set(headers) == EVENT_FIELDS, "invalid_csv", "CSV表头与订单契约不一致",
                details={"expected": CSV_HEADERS})
        result = []
        for index, row in enumerate(reader, 2):
            require(index <= 5001, "csv_too_large", "CSV超过5000条记录")
            require(None not in row and all(value is not None for value in row.values()),
                    "invalid_csv", "CSV列数不一致", details={"line": index})
            converted = dict(row)
            for field in INTEGER_FIELDS:
                raw = converted[field]
                require(raw.isascii() and raw.isdecimal(), "invalid_csv_integer", "CSV整数必须为非负十进制",
                        details={"line": index, "field": field})
                converted[field] = int(raw)
            require(converted["family"] in {"true", "false"}, "invalid_csv_boolean", "CSV布尔值必须是true/false",
                    details={"line": index, "field": "family"})
            converted["family"] = converted["family"] == "true"
            try:
                result.append(normalize_event(converted))
            except LedgerError as exc:
                exc.details = {**exc.details, "line": index}
                raise
    except csv.Error as exc:
        raise LedgerError("invalid_csv", "CSV引号或分隔符格式无效", details={"reason": str(exc)}) from exc
    require(bool(result), "empty_csv", "CSV没有订单记录")
    return result


class FileIntake:
    def __init__(self, intake):
        self.intake = intake

    def inspect(self, content):
        events = parse_csv(content)
        identities = set()
        sequences = set()
        duplicate_ids = []
        sequence_conflicts = []
        states = {"booked": 0, "cancelled": 0}
        origins = set()
        dates = []
        for event in events:
            if event["event_id"] in identities:
                duplicate_ids.append(event["event_id"])
            identities.add(event["event_id"])
            key = (event["order_id"], event["sequence"])
            if key in sequences:
                sequence_conflicts.append(list(key))
            sequences.add(key)
            states[event["state"]] += 1
            origins.add(event["origin"])
            dates.append(event["travel_day"])
        return {"rows": len(events), "orders": len({e["order_id"] for e in events}),
                "digest": fingerprint(events), "duplicate_event_ids": sorted(set(duplicate_ids)),
                "duplicate_sequences": sequence_conflicts, "states": states, "origins": sorted(origins),
                "first_day": min(dates), "last_day": max(dates)}

    def ingest_csv(self, source, batch_id, content, actor="operator"):
        return self.intake.ingest(source, batch_id, parse_csv(content), actor)

    def ingest_jsonl(self, source, batch_id, content, actor="operator"):
        require(isinstance(content, str) and len(content.encode()) <= 10000000,
                "invalid_jsonl", "JSONL内容超出限制")
        events = []
        for index, line in enumerate(content.splitlines(), 1):
            if not line.strip():
                continue
            require(len(events) < 5000, "invalid_jsonl", "JSONL超过5000条记录")
            try:
                value = json.loads(line)
            except ValueError as exc:
                raise LedgerError("invalid_jsonl", "JSONL行不是有效JSON", details={"line": index}) from exc
            events.append(value)
        return self.intake.ingest(source, batch_id, events, actor)
