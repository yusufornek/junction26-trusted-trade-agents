"""Opt-in, per-gateway OTel. Only the explicit business allowlist leaves the process."""
import os
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from .runtime import ENVIRONMENT, SUPPLIER, TRANSPORT, CLOCK

CURRENT = ContextVar("telemetry", default=None)
ALLOWLIST = frozenset({"transport", "supplier", "environment", "outcome", "cache.hit",
                      "candidates.screened", "candidates.priced", "rule.codes", "correlation.id",
                      "order.ref", "quote.ref"})
STACK_VERSIONS = {"collector": "0.139.0", "jaeger": "1.76.0", "prometheus": "v3.7.3", "grafana": "12.2.1"}


def attributes(**extra):
    from .foundation import CORRELATION
    values = {"transport": TRANSPORT.get(), "supplier": SUPPLIER.get(), "environment": ENVIRONMENT.get(),
              "correlation.id": CORRELATION.get(), **extra}
    if values["supplier"] not in {"valio", "nordfood"}:
        values["supplier"] = "unknown"
    return {k: v for k, v in values.items() if k in ALLOWLIST and v is not None}


class Telemetry:
    def __init__(self, environment="live", *, exporter=None, metric_reader=None):
        self.enabled = exporter is not None or metric_reader is not None or bool(
            os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv("OTEL_CONSOLE") == "1")
        self.environment = environment
        self.db = None
        self.provider = self.meter_provider = None
        self.instruments = {}
        if not self.enabled:
            return
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor, BatchSpanProcessor, ConsoleSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.view import View
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import ReadableSpan
        from opentelemetry.trace import Status
        from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

        class SafeExporter(SpanExporter):
            def __init__(self, delegate):
                self.delegate = delegate

            def export(self, spans):
                try:
                    clean = [ReadableSpan(name=s.name, context=s.context, parent=s.parent, kind=s.kind,
                        resource=Resource({}), attributes={k: v for k, v in s.attributes.items() if k in ALLOWLIST},
                        events=(), links=s.links, status=Status(s.status.status_code),
                        start_time=s.start_time, end_time=s.end_time,
                        instrumentation_scope=s.instrumentation_scope) for s in spans]
                    return self.delegate.export(clean)
                except Exception:
                    return SpanExportResult.FAILURE

            def shutdown(self):
                try:
                    self.delegate.shutdown()
                except Exception:
                    pass

        self.provider = TracerProvider(resource=Resource({}), shutdown_on_exit=False)
        if exporter is not None:
            self.provider.add_span_processor(SimpleSpanProcessor(SafeExporter(exporter)))
        else:
            if endpoint := os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                self.provider.add_span_processor(BatchSpanProcessor(SafeExporter(
                    OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces", timeout=1))))
            if os.getenv("OTEL_CONSOLE") == "1":
                self.provider.add_span_processor(SimpleSpanProcessor(SafeExporter(ConsoleSpanExporter())))
        readers = [metric_reader] if metric_reader else []
        if metric_reader is None and os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            readers.append(PeriodicExportingMetricReader(OTLPMetricExporter(
                endpoint=os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"].rstrip("/") + "/v1/metrics", timeout=1)))
        if metric_reader is None and os.getenv("OTEL_CONSOLE") == "1":
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader, ConsoleMetricExporter
            readers.append(PeriodicExportingMetricReader(ConsoleMetricExporter()))
        self.meter_provider = MeterProvider(resource=Resource({}), metric_readers=readers,
                                           views=[View(instrument_name="*", attribute_keys=ALLOWLIST)], shutdown_on_exit=False)
        self.tracer = self.provider.get_tracer("trusted-trade")
        meter = self.meter_provider.get_meter("trusted-trade")
        for name in ("request.latency", "upstream.latency", "approval.wait"):
            self.instruments[name] = meter.create_histogram(name, unit="s")
        for name in ("upstream.calls", "cache.hits", "decisions", "candidates.screened", "candidates.priced",
                     "throttles", "delivery.attempts", "expiries"):
            self.instruments[name] = meter.create_counter(name)
        def backlog(options):
            from opentelemetry.metrics import Observation
            if self.db is not None:
                try:
                    with self.db.connect() as conn:
                        count = conn.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0]
                    return [Observation(count, {"environment": self.environment})]
                except Exception:
                    pass
            return []
        meter.create_observable_gauge("outbox.backlog", callbacks=[backlog])

    def metric(self, name, value=1, **attrs):
        if not self.enabled:
            return
        try:
            instrument = self.instruments[name]
            method = getattr(instrument, "add", None) or getattr(instrument, "record", None) or instrument.set
            method(value, attributes(**attrs))
        except Exception:
            pass

    @contextmanager
    def activate(self):
        token = CURRENT.set(self)
        try:
            yield
        finally:
            CURRENT.reset(token)

    @contextmanager
    def span(self, name, *, links=(), **attrs):
        if not self.enabled:
            yield None
            return
        # Do not record exception messages or stack traces (they can include input).
        from opentelemetry.context import Context
        with self.tracer.start_as_current_span(name, attributes=attributes(**attrs), links=links,
                context=Context() if name == "webhook.deliver" and links else None,
                record_exception=False, set_status_on_exception=False) as span:
            try:
                yield span
            except Exception as error:
                span.set_attribute("outcome", getattr(error, "code", "error"))
                raise

    def instrument_app(self, app):
        if self.enabled:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            FastAPIInstrumentor.instrument_app(app, tracer_provider=self.provider, meter_provider=self.meter_provider)

    def instrument_client(self, client):
        if self.enabled:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
            HTTPXClientInstrumentor.instrument_client(client, tracer_provider=self.provider, meter_provider=self.meter_provider)

    def close(self):
        for provider in (self.provider, self.meter_provider):
            if provider:
                try:
                    provider.shutdown()
                except Exception:
                    pass


@contextmanager
def span(name, *, links=(), **attrs):
    telemetry = CURRENT.get()
    if telemetry:
        with telemetry.span(name, links=links, **attrs) as current:
            yield current
    else:
        yield None


def metric(name, value=1, **attrs):
    if telemetry := CURRENT.get():
        telemetry.metric(name, value, **attrs)


def traced(name):
    def decorate(fn):
        @wraps(fn)
        def run(*args, **kwargs):
            telemetry = CURRENT.get() or (getattr(args[0], "telemetry", None) if args else None)
            if telemetry is None:
                return fn(*args, **kwargs)
            saved_links = []
            if name == "webhook.deliver" and len(args) > 3:
                with args[0].db.connect() as conn:
                    event = conn.execute("SELECT payload FROM events WHERE id=?", (args[3],)).fetchone()
                if event:
                    import json
                    saved_links = links(json.loads(event[0]).get("links", {}).get("placement_span"))
            from .runtime import scope
            with scope(telemetry.environment, CLOCK.get()), telemetry.activate(), telemetry.span(name, links=saved_links) as current:
                result = fn(*args, **kwargs)
                if name == "policy.evaluate":
                    telemetry.metric("decisions", outcome=result.outcome)
                    if current is not None:
                        current.set_attribute("outcome", result.outcome)
                        current.set_attribute("rule.codes", [h.rule for h in result.hits])
                return result
        return run
    return decorate


def placement_context():
    from opentelemetry.trace import get_current_span
    context = get_current_span().get_span_context()
    return {"trace_id": format(context.trace_id, "032x"), "span_id": format(context.span_id, "016x"), "flags": int(context.trace_flags)} if context.is_valid else None


def links(saved):
    if not saved:
        return []
    from opentelemetry.trace import SpanContext, TraceFlags, Link
    context = SpanContext(int(saved["trace_id"], 16), int(saved["span_id"], 16), is_remote=True, trace_flags=TraceFlags(saved.get("flags", 1)))
    return [Link(context)]
