import os

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

from ..application import Ledger
from ..errors import LedgerError
from ..analysis.narrative import explain_result
from ..analysis.trends import robust_flags
from .models import (CatalogWrite, CalendarWrite, CoverageWrite, EventBatch, FileBatch, FileInspect,
                     AnalysisRequest, EditionRequest, DecisionRequest, PublishRequest, RetireRequest,
                     EditionComparison, ProfileRequest, AnomalyRequest)


def request_value(value):
    return value.model_dump(exclude_none=True)


def create_app(path=None):
    ledger = Ledger(path or os.environ.get("AUTUMN_LEDGER_DB", "autumn-ledger.sqlite3"))
    app = FastAPI(title="秋窗客流分析账本", version="1.0.0", docs_url="/docs", redoc_url=None)
    app.state.ledger = ledger

    @app.exception_handler(LedgerError)
    async def ledger_error(request: Request, exc: LedgerError):
        return JSONResponse(status_code=exc.status, content={"error": exc.as_dict()})

    @app.get("/health")
    def health():
        return ledger.status()

    @app.get("/references")
    def references(revision: int | None = None):
        return ledger.references(revision)

    @app.put("/catalog/{kind}/{identity}")
    def put_catalog(kind: str, identity: str, body: CatalogWrite):
        return ledger.catalog.put(kind, identity, body.value, body.expected_version, body.actor)

    @app.get("/catalog/{kind}")
    def list_catalog(kind: str, revision: int | None = None, active_only: bool = False):
        return ledger.catalog.list(kind, revision, active_only=active_only)

    @app.get("/catalog/{kind}/{identity}")
    def get_catalog(kind: str, identity: str, revision: int | None = None):
        return ledger.catalog.get(kind, identity, revision)

    @app.put("/calendars/{identity}")
    def put_calendar(identity: str, body: CalendarWrite):
        return ledger.calendars.put(identity, body.value, body.expected_version, body.actor)

    @app.get("/calendars")
    def list_calendars(city: str | None = None, revision: int | None = None):
        return ledger.calendars.list(city, revision)

    @app.get("/calendars/{identity}")
    def get_calendar(identity: str, revision: int | None = None):
        return ledger.calendars.get(identity, revision)

    @app.get("/calendar-classification")
    def classify(city: str, day: str, revision: int | None = None):
        ledger.catalog.get("city", city, revision)
        return ledger.calendars.classify(city, day, revision)

    @app.put("/coverage")
    def put_coverage(body: CoverageWrite):
        return ledger.coverage.put(body.value, body.expected_version, body.actor)

    @app.get("/coverage")
    def get_coverage(source: str, city: str, day: str, revision: int | None = None):
        return ledger.coverage.get(source, city, day, revision)

    @app.get("/coverage-matrix")
    def coverage_matrix(start: str, end: str, source: list[str] = Query(), city: list[str] = Query(),
                        revision: int | None = None):
        matrix = ledger.coverage.matrix(source, city, start, end, revision)
        return {"summary": ledger.coverage.summary(matrix), "gaps": ledger.coverage.missing_groups(matrix), "cells": matrix}

    @app.post("/sources/{source}/batches")
    def ingest(source: str, body: EventBatch):
        return ledger.intake.ingest(source, body.batch_id, body.events, body.actor)

    @app.post("/sources/{source}/csv")
    def ingest_csv(source: str, body: FileBatch):
        return ledger.files.ingest_csv(source, body.batch_id, body.content, body.actor)

    @app.post("/sources/{source}/jsonl")
    def ingest_jsonl(source: str, body: FileBatch):
        return ledger.files.ingest_jsonl(source, body.batch_id, body.content, body.actor)

    @app.post("/csv-inspection")
    def inspect_csv(body: FileInspect):
        return ledger.files.inspect(body.content)

    @app.get("/batches")
    def list_batches(source: str | None = None, limit: int = 100, offset: int = 0):
        return ledger.intake.list_batches(source, limit, offset)

    @app.get("/sources/{source}/batches/{batch_id}")
    def batch(source: str, batch_id: str):
        return ledger.intake.batch(source, batch_id)

    @app.get("/sources/{source}/orders/{order_id}")
    def order_history(source: str, order_id: str):
        return ledger.intake.history(source, order_id)

    @app.post("/analysis/preview")
    def preview(body: AnalysisRequest):
        return ledger.analysis.preview(request_value(body))

    @app.post("/analysis/calculate")
    def calculate(body: AnalysisRequest):
        return ledger.analysis.run(request_value(body))

    @app.post("/editions")
    def create_edition(body: EditionRequest):
        return ledger.editions.create(body.request_key, request_value(body.request))

    @app.get("/editions")
    def list_editions(state: str | None = None, limit: int = 100, offset: int = 0):
        return ledger.editions.list(state, limit, offset)

    @app.get("/editions/{identity}")
    def get_edition(identity: str):
        return ledger.editions.get(identity)

    @app.get("/editions/{identity}/lineage")
    def lineage(identity: str, source: str | None = None, limit: int = 100, offset: int = 0):
        return ledger.editions.lineage(identity, source, limit, offset)

    @app.get("/editions/{identity}/explanation")
    def explanation(identity: str):
        return ledger.editions.explain(identity)

    @app.post("/editions/{identity}/verify")
    def verify(identity: str):
        return ledger.editions.verify(identity)

    @app.post("/edition-comparison")
    def compare(body: EditionComparison):
        return ledger.editions.compare_editions(body.left, body.right)

    @app.post("/editions/{identity}/decision")
    def decide(identity: str, body: DecisionRequest):
        return ledger.review.decide(identity, body.action, body.expected_version, body.actor, body.note)

    @app.post("/editions/{identity}/publish")
    def publish(identity: str, body: PublishRequest):
        return ledger.review.publish(identity, body.channel, body.expected_version, body.expected_generation, body.actor)

    @app.post("/editions/{identity}/retire")
    def retire(identity: str, body: RetireRequest):
        return ledger.review.retire(identity, body.expected_version, body.actor, body.note)

    @app.get("/editions/{identity}/decisions")
    def decisions(identity: str):
        return ledger.review.history(identity)

    @app.get("/channels")
    def channels():
        return ledger.review.channels()

    @app.get("/channels/{channel}")
    def channel(channel: str):
        return ledger.review.channel(channel)

    @app.get("/channels/{channel}/history")
    def channel_history(channel: str):
        return ledger.review.publication_history(channel)

    @app.get("/public/{channel}")
    def public_channel(channel: str):
        return ledger.export.public_channel(channel)

    @app.get("/editions/{identity}/export.json")
    def json_export(identity: str):
        return ledger.export.document(identity)

    @app.get("/editions/{identity}/tables/{table}.csv")
    def csv_export(identity: str, table: str, period: str = "current"):
        content = ledger.export.table(identity, table, period)
        return Response(content, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{identity}-{table}.csv"'})

    @app.get("/editions/{identity}/export.zip")
    def zip_export(identity: str):
        content = ledger.export.bundle(identity)
        return Response(content, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{identity}.zip"'})

    @app.get("/audit")
    def audit(after: int = 0, limit: int = 100):
        return ledger.database.audit(after, limit)

    @app.post("/analysis/profile")
    def profile(body: ProfileRequest):
        result = ledger.analysis.run(request_value(body.request))
        return {"data_revision": result["data_revision"],
                "profiles": {name: period["profiles"][body.dimension] for name, period in result["periods"].items()}}

    @app.post("/analysis/anomalies")
    def anomalies(body: AnomalyRequest):
        result = ledger.analysis.run(request_value(body.request))
        return {"data_revision": result["data_revision"],
                **robust_flags(result["periods"]["current"], body.minimum_orders, body.multiplier)}

    @app.get("/editions/{identity}/narrative")
    def narrative(identity: str):
        return explain_result(ledger.editions.get(identity)["result"])

    @app.get("/editions/{identity}/drilldown")
    def drilldown(identity: str, period: str = "current", origin: str | None = None,
                  destination: str | None = None, source: str | None = None, day: str | None = None,
                  state: str | None = None, limit: int = 100, offset: int = 0):
        return ledger.investigation.drilldown(identity, period, origin, destination, source, day, state, limit, offset)

    @app.get("/editions/{identity}/orders/{source}/{order_id}")
    def trace_order(identity: str, source: str, order_id: str):
        return ledger.investigation.order_trace(identity, source, order_id)

    @app.get("/editions/{identity}/reference-drift")
    def reference_drift(identity: str):
        return ledger.investigation.reference_drift(identity)

    @app.get("/editions/{identity}/event-reconciliation")
    def event_reconciliation(identity: str):
        return ledger.investigation.event_reconciliation(identity)

    @app.get("/editions/{identity}/coverage-disputes")
    def coverage_disputes(identity: str, period: str = "current"):
        return ledger.investigation.coverage_disputes(identity, period)

    @app.get("/publication-impact")
    def publication_impact(after_revision: int):
        return ledger.investigation.publication_impact(after_revision)

    @app.get("/sources/{source}/receipts")
    def receipts(source: str, revision: int | None = None):
        return ledger.receipts.inspect(source, revision)

    @app.get("/sources/{source}/orders/{order_id}/contributions")
    def contributions(source: str, order_id: str):
        return ledger.receipts.contributions(source, order_id)

    @app.get("/sources/{source}/activity")
    def source_activity(source: str, after: int = 0, through: int | None = None):
        return ledger.receipts.activity(source, after, through)

    @app.get("/sources/{source}/snapshot-difference")
    def source_difference(source: str, before: int, after: int):
        return ledger.receipts.between_snapshots(source, before, after)

    @app.get("/analysis-readiness")
    def readiness(start: str, end: str, city: list[str] = Query(), source: list[str] = Query(),
                  revision: int | None = None):
        return ledger.planning.readiness(city, source, start, end, revision)

    @app.get("/city-calendar/{city}")
    def city_calendar(city: str, start: str, end: str, source: list[str] = Query(), revision: int | None = None):
        return ledger.planning.city_calendar(city, start, end, source, revision)

    @app.get("/calendars/{identity}/history")
    def calendar_history(identity: str):
        return ledger.planning.calendar_changes(identity)

    @app.get("/editions/{identity}/overview")
    def overview(identity: str):
        return ledger.query.overview(identity)

    @app.get("/editions/{identity}/table-query/{table}")
    def table_query(identity: str, table: str, period: str = "current", sort_by: str = "orders",
                    descending: bool = True, minimum_orders: int = 0, limit: int = 100, offset: int = 0):
        return ledger.query.table(identity, table, period, None, sort_by, descending, minimum_orders, limit, offset)

    @app.get("/editions/{identity}/coverage-query")
    def coverage_query(identity: str, period: str = "current", status: str | None = None,
                       source: str | None = None, city: str | None = None, limit: int = 100, offset: int = 0):
        return ledger.query.coverage_filter(identity, period, status, source, city, limit, offset)

    @app.get("/editions/{identity}/issues")
    def issues(identity: str, severity: str | None = None, period: str | None = None):
        return ledger.query.issue_filter(identity, severity, period)

    @app.get("/editions/{identity}/segment-changes/{dimension}")
    def segment_changes(identity: str, dimension: str, metric: str = "orders", minimum_absolute: int = 0):
        return ledger.query.compare_segments(identity, dimension, metric, minimum_absolute)

    @app.get("/editions/{identity}/publication-readiness")
    def publication_readiness(identity: str, channel: str | None = None):
        return ledger.publication_readiness.inspect(identity, channel)

    @app.get("/review-queue")
    def review_queue():
        return ledger.publication_readiness.queue()

    @app.get("/publication-consistency")
    def publication_consistency():
        return ledger.publication_readiness.published_consistency()

    return app
