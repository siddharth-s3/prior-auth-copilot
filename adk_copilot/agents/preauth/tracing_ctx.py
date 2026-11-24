"""Lets the evaluation runner make a REAL agent run the item of a Langfuse experiment.

The runner sends W3C `traceparent` + `baggage` headers with the AG-UI request. `TraceContextMiddleware`
attaches that context, so every span of the run (supervisor, specialists, MCP tools, model calls) becomes a
child of the runner's root span, in the runner's trace. `ExperimentAttributes` copies `langfuse.*` baggage
entries (experiment id/name, dataset id, item id) onto each span, which is how Langfuse links the trace to the
experiment. Requests without these headers are untouched."""
from opentelemetry import baggage, context, propagate
from opentelemetry.sdk.trace import SpanProcessor

_HEADERS = (b"traceparent", b"tracestate", b"baggage")


class ExperimentAttributes(SpanProcessor):
    """Copy `langfuse.*` baggage onto every span as it starts."""

    def on_start(self, span, parent_context=None):
        for key, value in (baggage.get_all(parent_context) or {}).items():
            if str(key).startswith("langfuse."):
                span.set_attribute(str(key), str(value))

    def on_end(self, span):
        pass

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


class TraceContextMiddleware:
    """Pure-ASGI: attach the caller's trace context (traceparent/baggage) for POST /agui."""

    def __init__(self, app, path="/agui"):
        self.app = app
        self.path = path

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != self.path:
            return await self.app(scope, receive, send)
        carrier = {k.decode(): v.decode() for k, v in scope["headers"] if k in _HEADERS}
        if "traceparent" not in carrier:
            return await self.app(scope, receive, send)
        token = context.attach(propagate.extract(carrier))
        try:
            await self.app(scope, receive, send)
        finally:
            context.detach(token)
