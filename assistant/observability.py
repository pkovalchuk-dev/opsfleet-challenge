"""Private LangSmith callbacks and policy annotations; native runs supply timing."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from langchain_core.tracers.langchain import LangChainTracer
from langsmith import Client, get_current_run_tree, tracing_context
from langsmith.address import address_from_env

# Explicit allowlists: SDK metadata and exception messages can contain user data.
METADATA = frozenset({
    "thread_id", "turn_id", "plan_id", "outcome", "intent",
    "ls_model_name", "ls_model_type", "ls_provider", "langgraph_node",
    "langgraph_step", "status", "error_type", "attempt", "rows", "bytes_billed",
    "step_index", "step_count", "truncated", "queries_run", "candidates", "requested", "deleted", "ok",
    "wants_report", "model", "from_model", "to_model", "label",
    "reason", "wait_s", "count", "source",
})
OPERATIONS = frozenset({
    "id", "trace_id", "parent_run_id", "dotted_order", "name", "run_type", "start_time",
    "end_time", "first_token_time", "session_name", "session_id", "project_name",
    "reference_example_id", "address",
})
EVENTS = frozenset({
    "start_step", "model_fallback", "bigquery_retry", "policy_denied",
    "safe_reply", "report_saved", "delete_preview_denied", "delete_preview", "reports_deleted",
    "delete_cancelled", "analysis_stopped", "turn_failed", "history_write_failed",
})


def safe_metadata(values: dict) -> dict:
    clean = {k: v for k, v in values.items() if k in METADATA
             and isinstance(v, (str, int, float, bool))}
    usage = values.get("usage_metadata")
    if isinstance(usage, dict):
        clean["usage_metadata"] = {k: v for k, v in usage.items()
                                   if k in {"input_tokens", "output_tokens", "total_tokens"}
                                   and isinstance(v, int)}
    return clean


def sanitize_runs(operations):
    """Last step before SDK serialization, including errors and nested model runs."""
    result = []
    for operation in operations:
        clean = {k: v for k, v in operation.items() if k in OPERATIONS}
        clean.update(inputs={}, outputs={}, serialized={}, tags=["prototype"])
        clean["extra"] = {"metadata": safe_metadata((operation.get("extra") or {}).get("metadata") or {})}
        clean["error"] = "Execution failed; sensitive error details omitted." if operation.get("error") else None
        clean["events"] = [
            {"name": e["name"], "time": e.get("time"), "kwargs": safe_metadata(e.get("kwargs") or {})}
            for e in operation.get("events") or [] if e.get("name") in EVENTS
        ]
        result.append(clean)
    return result


class PrivateClient(Client):
    """Sanitize before the SDK's normal async queue, preserving operation order."""

    def create_run(self, name, inputs, run_type, **kwargs):
        clean = sanitize_runs([{**kwargs, "name": name, "inputs": inputs, "run_type": run_type}])[0]
        return super().create_run(**clean)

    def update_run(self, run_id, **kwargs):
        return super().update_run(run_id, **sanitize_runs([kwargs])[0])


class TurnCallback(LangChainTracer):
    """Child callbacks are native; finish() emits the root's one final outcome."""

    def _on_chain_end(self, run) -> None:
        if run.parent_run_id is not None:
            super()._on_chain_end(run)

    def _on_chain_error(self, run) -> None:
        if run.parent_run_id is not None:
            super()._on_chain_error(run)


class Observability:
    def __init__(self) -> None:
        self.project = os.getenv("LANGSMITH_PROJECT", "opsfleet-challenge")
        self.address = None
        self.client: Client | None = None
        self.callback: LangChainTracer | None = None
        self.error_type: str | None = None
        enabled = os.getenv("LANGSMITH_TRACING", os.getenv("LANGCHAIN_TRACING_V2", "false"))
        self.requested = enabled.lower() == "true"
        self.missing_key = self.requested and not (os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY"))
        if self.requested and not self.missing_key:
            try:
                self.address = address_from_env()
                self.client = PrivateClient(
                    hide_inputs=True, hide_outputs=True, hide_metadata=safe_metadata,
                    omit_traced_runtime_info=True, timeout_ms=1500,
                    tracing_error_callback=self._failed,
                )
            except Exception as exc:
                self._failed(exc)

    def _failed(self, exc: Exception) -> None:
        self.error_type = type(exc).__name__

    def context(self, *, graph: bool = False):
        # Explicitly disable automatic environment-driven tracing if unconfigured.
        return tracing_context(enabled=graph and self.callback is not None,
                               client=self.client, parent=False)

    @contextmanager
    def span(self, name: str, **attrs):
        """Annotate the existing native node without creating or timing another run."""
        record = dict(attrs)
        try:
            yield record
            record.setdefault("status", "ok")
        except Exception as exc:
            record.update(status="error", error_type=type(exc).__name__)
            raise
        finally:
            try:
                run = get_current_run_tree()
                if run is not None and self.client is not None:
                    run.add_metadata(safe_metadata(record))
            except Exception:
                pass  # Telemetry must not affect the answer or confirmation flow.

    def event(self, name: str, **attrs) -> None:
        if self.client is None or self.callback is None or name not in EVENTS:
            return
        event = {"name": name, "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                 "kwargs": safe_metadata(attrs)}
        try:
            # Persistence failures happen after the graph, before its final update.
            run = get_current_run_tree() or self.callback.latest_run
            if run is not None:
                run.add_event(event)
        except Exception:
            pass

    def graph_config(self, config: dict, turn_id: str) -> dict:
        self.callback = None
        if self.client is None:
            return config
        try:
            self.callback = TurnCallback(client=self.client, address=self.address,
                                         project_name=None if self.address else self.project)
            return {**config, "callbacks": [self.callback], "run_id": uuid.uuid4(),
                    "run_name": "retail_assistant_turn",
                    "metadata": {"turn_id": turn_id,
                                 "thread_id": config["configurable"]["thread_id"]}}
        except Exception as exc:
            self._failed(exc)
            return config

    def finish(self, turn: dict) -> None:
        try:
            if not self.client or not self.callback or not self.callback.latest_run:
                return
            run = self.callback.latest_run
            # Graph errors are often handled and returned as state, not raised.
            metadata = safe_metadata({**run.metadata, **turn, "outcome": turn["status"]})
            self.client.update_run(
                run.id, name="retail_assistant_turn", trace_id=run.trace_id, dotted_order=run.dotted_order,
                address=self.address, end_time=run.end_time,
                extra={"metadata": metadata},
                error="Turn failed." if turn["status"] == "failed" else None,
                events=run.events,
            )
        except Exception as exc:
            self._failed(exc)
        finally:
            self.callback = None

    def close(self) -> None:
        if self.client:
            try:
                self.client.flush(timeout=2.0)
            except Exception as exc:
                self._failed(exc)
