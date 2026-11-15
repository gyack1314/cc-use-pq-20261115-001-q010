import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from ..errors import LedgerError, require
from ..values import canonical, integer, page_size, text, utc_now
from .schema import initialize


class Database:
    def __init__(self, path):
        self.path = str(Path(path).absolute())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                initialize(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        try:
            yield connection
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise LedgerError("busy", "数据正在写入，请重试", 503) from exc
            raise
        finally:
            connection.close()

    @contextmanager
    def transaction(self, write=False):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def revision(self, connection=None):
        if connection is None:
            with self.transaction() as opened:
                return self.revision(opened)
        return connection.execute("SELECT revision FROM ledger_meta WHERE singleton=1").fetchone()[0]

    def snapshot_revision(self, connection, requested=None):
        current = self.revision(connection)
        if requested is None:
            return current
        integer(requested, "revision")
        require(requested <= current, "future_revision", "指定数据版本尚不存在")
        return requested

    def advance(self, connection, action, identity, actor, payload):
        actor = text(actor, "操作人", 100)
        connection.execute("UPDATE ledger_meta SET revision=revision+1 WHERE singleton=1")
        revision = self.revision(connection)
        connection.execute(
            "INSERT INTO audit VALUES (?,?,?,?,?,?)",
            (revision, action, identity, actor, canonical(payload), utc_now()),
        )
        return revision

    def audit(self, after=0, limit=100):
        integer(after, "after")
        page_size(limit, 0)
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM audit WHERE revision>? ORDER BY revision LIMIT ?", (after, limit)
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def integrity(self):
        with self.transaction() as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
            revision = self.revision(connection)
            count = connection.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
            event_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            edition_count = connection.execute("SELECT COUNT(*) FROM editions").fetchone()[0]
        ok = [row[0] for row in integrity] == ["ok"] and not foreign and count == revision
        return {"ok": ok, "revision": revision, "audit_entries": count,
                "events": event_count, "editions": edition_count}
