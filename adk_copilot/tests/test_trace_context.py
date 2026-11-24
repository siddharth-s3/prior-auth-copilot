"""The evaluation runner's trace context must become the parent of the agent's spans."""
import asyncio

from opentelemetry import baggage, trace
from opentelemetry.sdk.trace import TracerProvider

from agents.preauth.tracing_ctx import ExperimentAttributes, TraceContextMiddleware

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
TP = f"00-{TRACE_ID}-b7ad6b7169203331-01"


def run(headers, method="POST", path="/agui"):
    seen = {}

    async def app(scope, receive, send):
        span = trace.get_current_span().get_span_context()
        seen["trace_id"] = format(span.trace_id, "032x")
        seen["baggage"] = dict(baggage.get_all())

    scope = {"type": "http", "method": method, "path": path, "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}
    asyncio.run(TraceContextMiddleware(app)(scope, None, None))
    return seen


def test_traceparent_and_baggage_are_attached():
    seen = run({"traceparent": TP, "baggage": "langfuse.experiment.name=demo,langfuse.experiment.item.id=PA-001"})
    assert seen["trace_id"] == TRACE_ID
    assert seen["baggage"]["langfuse.experiment.item.id"] == "PA-001"


def test_requests_without_traceparent_are_untouched():
    assert run({})["trace_id"] == "0" * 32


def test_other_paths_and_methods_pass_through():
    assert run({"traceparent": TP}, path="/ui/cases")["trace_id"] == "0" * 32
    assert run({"traceparent": TP}, method="GET")["trace_id"] == "0" * 32


def test_spans_inherit_the_runner_trace_and_experiment_attributes():
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    exp = InMemorySpanExporter()
    prov = TracerProvider()
    prov.add_span_processor(ExperimentAttributes())
    prov.add_span_processor(SimpleSpanProcessor(exp))
    tracer = prov.get_tracer("t")

    async def app(scope, receive, send):
        with tracer.start_as_current_span("invocation"):
            with tracer.start_as_current_span("tool"):
                pass

    scope = {"type": "http", "method": "POST", "path": "/agui", "headers": [
        (b"traceparent", TP.encode()), (b"baggage", b"langfuse.experiment.name=demo,other=1")]}
    asyncio.run(TraceContextMiddleware(app)(scope, None, None))
    spans = exp.get_finished_spans()
    assert {format(s.context.trace_id, "032x") for s in spans} == {TRACE_ID}
    assert all(s.attributes.get("langfuse.experiment.name") == "demo" for s in spans)
    assert all("other" not in s.attributes for s in spans)          # only langfuse.* baggage is copied
    assert next(s for s in spans if s.name == "invocation").parent.span_id == int("b7ad6b7169203331", 16)
