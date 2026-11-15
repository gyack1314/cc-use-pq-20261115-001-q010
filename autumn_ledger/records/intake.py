import json

from ..errors import conflict, missing, require
from ..values import canonical, fields, fingerprint, identifier, integer, page_size, utc_now
from .contracts import normalize_event, validate_transition


class Intake:
    def __init__(self, database, catalog):
        self.database = database
        self.catalog = catalog

    def ingest(self, source, batch_id, events, actor="operator"):
        identifier(source, "source")
        identifier(batch_id, "batch_id")
        require(isinstance(events, list) and 0 < len(events) <= 5000,
                "invalid_batch", "每批应包含1至5000条事件")
        normalized = [normalize_event(event) for event in events]
        digest = fingerprint(normalized)
        with self.database.transaction(write=True) as connection:
            existing = connection.execute("SELECT * FROM batches WHERE source=? AND id=?", (source, batch_id)).fetchone()
            if existing:
                if existing["digest"] != digest:
                    raise conflict("批次号已用于不同内容")
                return {**dict(existing), "replayed": True}
            self.catalog.require_active("source", source, connection)
            checked = set()
            accepted = []
            duplicates = 0
            heads = {}
            local_events = {}
            local_sequences = {}
            for event in normalized:
                event_id = event["event_id"]
                event_digest = fingerprint(event)
                if event_id in local_events:
                    if local_events[event_id] != event_digest:
                        raise conflict("批次内事件号对应不同内容", {"event_id": event_id})
                    duplicates += 1
                    continue
                local_events[event_id] = event_digest
                stored = connection.execute("SELECT digest FROM events WHERE source=? AND event_id=?", (source, event_id)).fetchone()
                if stored:
                    if stored["digest"] != event_digest:
                        raise conflict("事件号已用于不同内容", {"event_id": event_id})
                    duplicates += 1
                    continue
                order_id = event["order_id"]
                sequence_key = (order_id, event["sequence"])
                if sequence_key in local_sequences:
                    raise conflict("同一订单修订号对应多条事件")
                local_sequences[sequence_key] = event_id
                used = connection.execute(
                    "SELECT event_id FROM events WHERE source=? AND order_id=? AND sequence=?",
                    (source, order_id, event["sequence"]),
                ).fetchone()
                if used:
                    raise conflict("订单修订号已被使用", {"event_id": used["event_id"]})
                if order_id not in heads:
                    row = connection.execute(
                        "SELECT payload FROM events WHERE source=? AND order_id=? ORDER BY sequence DESC LIMIT 1",
                        (source, order_id),
                    ).fetchone()
                    heads[order_id] = json.loads(row["payload"]) if row else None
                validate_transition(heads[order_id], event)
                for kind, identity in (("city", event["origin"]), ("destination", event["destination"])):
                    key = (kind, identity)
                    if key not in checked:
                        self.catalog.require_active(kind, identity, connection)
                        checked.add(key)
                heads[order_id] = event
                accepted.append((event, event_digest))
            revision = self.database.advance(connection, "batch.ingest", f"{source}:{batch_id}", actor,
                                              {"source": source, "batch_id": batch_id, "digest": digest,
                                               "accepted": len(accepted), "duplicates": duplicates})
            for event, event_digest in accepted:
                connection.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)",
                                   (source, event["event_id"], event["order_id"], event["sequence"],
                                    revision, event_digest, canonical(event)))
            created = utc_now()
            connection.execute("INSERT INTO batches VALUES (?,?,?,?,?,?,?)",
                               (source, batch_id, digest, revision, len(accepted), duplicates, created))
            return {"source": source, "id": batch_id, "digest": digest, "revision": revision,
                    "accepted": len(accepted), "duplicates": duplicates, "created_at": created, "replayed": False}

    def heads(self, revision=None, sources=None, connection=None):
        if connection is None:
            with self.database.transaction() as opened:
                return self.heads(revision, sources, opened)
        revision = self.database.snapshot_revision(connection, revision)
        parameters = [revision]
        source_clause = ""
        if sources is not None:
            require(isinstance(sources, list), "invalid_sources", "来源必须是数组")
            if not sources:
                return []
            for source in sources:
                identifier(source)
            source_clause = " AND source IN (" + ",".join("?" for _ in sources) + ")"
            parameters += sources
        rows = connection.execute(
            "SELECT e.* FROM events e JOIN (SELECT source,order_id,MAX(sequence) seq FROM events WHERE revision<=?"
            + source_clause + " GROUP BY source,order_id) h ON e.source=h.source AND e.order_id=h.order_id AND e.sequence=h.seq ORDER BY e.source,e.order_id",
            parameters,
        ).fetchall()
        return [{**json.loads(row["payload"]), "source": row["source"], "ingested_revision": row["revision"]} for row in rows]

    def history(self, source, order_id):
        identifier(source)
        identifier(order_id)
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE source=? AND order_id=? ORDER BY sequence", (source, order_id)
            ).fetchall()
        if not rows:
            raise missing("订单", f"{source}:{order_id}")
        return [{**json.loads(row["payload"]), "revision": row["revision"], "digest": row["digest"]} for row in rows]

    def batch(self, source, batch_id):
        identifier(source)
        identifier(batch_id)
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM batches WHERE source=? AND id=?", (source, batch_id)).fetchone()
        if not row:
            raise missing("批次", f"{source}:{batch_id}")
        return dict(row)

    def list_batches(self, source=None, limit=100, offset=0):
        page_size(limit, offset)
        parameters = []
        clause = ""
        if source:
            identifier(source)
            clause = " WHERE source=?"
            parameters.append(source)
        with self.database.transaction() as connection:
            rows = connection.execute("SELECT * FROM batches" + clause + " ORDER BY revision DESC LIMIT ? OFFSET ?",
                                      parameters + [limit, offset]).fetchall()
        return [dict(row) for row in rows]
