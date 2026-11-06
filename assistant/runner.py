"""Run one conversation: graph execution, confirmation, persistence and turn outcomes."""

from __future__ import annotations

import datetime as dt
import re
import sys
import time
import uuid
from typing import Any, Callable

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from .checks import redact_pii
from .config import AppConfig, resolve_settings
from .context import RuntimeContext
from .graph import build_graph
from .graph.main import completed_answer
from .graph.reports import deletion_preview
from .observability import Observability
from .schemas import fresh_turn_state
from .store import Store

# Only a standalone acknowledgement resumes a deletion. Other text cancels first.
NO = re.compile(r"\s*(n|no|nope|cancel|stop|keep|keep them|don't|do not|never mind)[.!]?\s*", re.I)


class Runner:
    def __init__(self, cfg: AppConfig, user_id: str, *, llm: Any = None, bq: Any = None,
                 db_path: str | None = None) -> None:
        self.cfg, self.llm, self.bq = cfg, llm, bq
        self.observability = Observability()
        self.tracer = self.observability
        path = db_path or str(cfg.db_path)
        self.store = Store(path)
        self.checkpointer = InMemorySaver()
        self.graph = build_graph(self.checkpointer)
        self.switch_user(user_id)

    @property
    def graph_config(self) -> dict:
        # The finite plan and existing execution budgets bound work, not its action count.
        return {"configurable": {"thread_id": self.conversation_id}, "recursion_limit": sys.maxsize}

    def close(self) -> None:
        self.observability.close()
        self.store.close()

    def switch_user(self, user_id: str) -> None:
        if user_id not in self.cfg.users:
            raise KeyError(f"Unknown user '{user_id}'. Known: {', '.join(self.cfg.users)}")
        self.user = self.cfg.users[user_id]
        self.new_conversation()

    def new_conversation(self) -> str:
        self.conversation_id = self.store.new_conversation(self.user.id)
        self.pending_confirmation = False
        self._displayed_steps = 0
        self.last_state: dict[str, Any] = {}
        return self.conversation_id

    def _plan_authorized(self, state: dict) -> bool:
        return all(self.user.allows_scope(step.get("brand_scope")) for step in state.get("completed_steps", [])
                   if step.get("outcome") == "success")

    def confirmation_preview(self) -> str:
        snapshot = self.graph.get_state(self.graph_config)
        if not snapshot.interrupts:
            return ""
        steps = (snapshot.values or {}).get("completed_steps", [])
        prefix = completed_answer(steps[self._displayed_steps:], self.user)
        if not self._plan_authorized(snapshot.values or {}):
            return "Your brand permissions changed. This pending plan will be cancelled when you reply."
        pending = snapshot.interrupts[0].value
        preview = pending["preview"]
        if not self.user.allows_scope(pending.get("brand_scope")):
            # Refresh labels under current access without changing the confirmed target set.
            reports = [self.store.get_report(rid, self.user) or
                       {"id": rid, "title": "Unavailable report", "created_at": ""}
                       for rid in pending["report_ids"]]
            preview = deletion_preview(reports)
        preview = redact_pii(preview)
        return (prefix + "\n\n" if prefix else "") + preview

    def _clients(self):
        self._ensure_clients()
        return self.llm, self.bq

    def _ensure_clients(self) -> None:
        if self.llm is None:
            from .llm import GeminiLLM
            self.llm = GeminiLLM(self.cfg.model, self.cfg.api_key,
                                 thinking_budget=self.cfg.thinking_budget, tracer=self.tracer,
                                 fallback_model=self.cfg.fallback_model)
        if self.bq is None:
            from .bq import QueryExecutor
            self.bq = QueryExecutor(self.cfg.project, tracer=self.tracer)

    def run_turn(self, text: str, on_progress: Callable[[str], None] | None = None) -> str:
        with self.observability.context():
            return self._run_turn(text, on_progress)

    def _run_turn(self, text: str, on_progress: Callable[[str], None] | None = None) -> str:
        if self.pending_confirmation:
            answer = self._execute(text, resume=True, on_progress=on_progress)
            if self.last_state.get("outcome") == "cancelled" and not NO.fullmatch(text):
                return answer + "\n\n" + self._execute(text, on_progress=on_progress)
            return answer
        return self._execute(text, on_progress=on_progress)

    def _execute(self, text: str, *, resume: bool = False,
                 on_progress: Callable[[str], None] | None = None) -> str:
        started = time.monotonic()
        turn_id = f"t_{uuid.uuid4().hex}"
        traced_config = self.observability.graph_config(self.graph_config, turn_id)
        trace_id = str(traced_config["run_id"]) if "run_id" in traced_config else None
        state: dict[str, Any] = {}
        if not resume:
            self._displayed_steps = 0
        try:
            if self.store.get_conversation(self.conversation_id, self.user.id) is None:
                raise PermissionError("Conversation is not owned by the current user")
            if not resume:  # confirmation uses the active-session preview and SQLite reports
                self._ensure_clients()
            settings = resolve_settings(self.cfg.settings, self.user.id)
            history = [turn for turn in self.store.recent_turns(self.conversation_id, self.user.id, limit=10)
                       if self.user.allows_scope(turn.get("brand_scope"))]
            snapshot = self.graph.get_state(self.graph_config)
            if "metadata" in traced_config:
                traced_config["metadata"]["plan_id"] = (snapshot.values or {}).get("plan_id", turn_id) if resume else turn_id
            state = (dict(snapshot.values or {}) if resume else
                     fresh_turn_state(text, history, settings,
                                      (snapshot.values or {}).get("displayed_report_ids", [])))
            ctx = RuntimeContext(user=self.user, conversation_id=self.conversation_id, turn_id=turn_id,
                                 limits=self.cfg.limits, llm=self.llm, bq=self.bq, store=self.store,
                                 tracer=self.tracer, schema_doc=self.cfg.schema_doc, settings=settings,
                                 started_at=started, client_factory=self._clients,
                                 displayed_steps=self._displayed_steps)
            if resume and not snapshot.interrupts:
                raise ValueError("No deletion is waiting for confirmation")
            scope_changed = resume and not self._plan_authorized(snapshot.values or {})
            payload = Command(resume="no" if scope_changed else text) if resume else state
            answer, pending = self._stream_graph(payload, state, ctx, traced_config, scope_changed, on_progress)
        except Exception as exc:  # the execution boundary preserves the interactive session
            state, pending = self._failed_turn(state, exc, turn_id=turn_id, resume=resume)
            answer = state["answer"]

        self.pending_confirmation, self.last_state = pending, state
        answer = self._finish_turn(text, answer, turn_id=turn_id, trace_id=trace_id)
        self._displayed_steps = max(self._displayed_steps, len(state.get("completed_steps", [])))
        return answer

    def _stream_graph(self, payload, state: dict, ctx: RuntimeContext, config: dict,
                      scope_changed: bool, on_progress: Callable[[str], None] | None) -> tuple[str, bool]:
        # Keep completed work in the caller's state even if a later node raises.
        with self.observability.context(graph=True):
            for _, update in self.graph.stream(payload, config, context=ctx,
                                              stream_mode="updates", subgraphs=True):
                for node, value in update.items():
                    if node != "__interrupt__" and isinstance(value, dict):
                        state.update(value)
                    if on_progress:
                        on_progress(node)

        snapshot = self.graph.get_state(self.graph_config)
        state.clear()
        state.update(snapshot.values or {})
        if snapshot.interrupts:
            answer = self.confirmation_preview()
            state["results"] = state.get("all_results", []) if self._plan_authorized(state) else []
            return answer, True
        if not state.get("answer") or not state.get("outcome"):
            raise RuntimeError("Graph returned no answer or outcome")
        if scope_changed:
            state.update(answer="Your brand permissions changed, so I cancelled the pending deletion and remaining actions.",
                         results=[], outcome="denied")
        return state["answer"], False

    def _failed_turn(self, state: dict, exc: Exception, *, turn_id: str, resume: bool) -> tuple[dict, bool]:
        self.tracer.event("turn_failed", error_type=type(exc).__name__)
        answer = "I couldn't finish this turn. Please try again."
        if resume:
            answer += " Ask me to list your reports before retrying a deletion; the change may have completed."
        completed = state.get("completed_steps", [])
        if completed:
            prefix = completed_answer(completed[self._displayed_steps:], self.user)
            answer = (prefix + "\n\n" if prefix else "") + answer + " Remaining actions were not run."
        failed = {"answer": answer, "outcome": "failed", "stop_reason": type(exc).__name__,
                  "completed_steps": completed, "plan_id": state.get("plan_id", turn_id),
                  "plan_steps": state.get("plan_steps", []), "step_index": state.get("step_index", 0),
                  "check_ok": False, "queries_run": state.get("queries_run", 0),
                  "displayed_report_ids": self.last_state.get("displayed_report_ids", [])}
        # Preserve a real pending interrupt; a failed ordinary node is never a confirmation.
        try:
            pending = bool(self.graph.get_state(self.graph_config).interrupts)
        except Exception:
            pending = False
        return failed, pending

    def _finish_turn(self, text: str, answer: str, *, turn_id: str, trace_id: str | None) -> str:
        state = self.last_state
        status = state.get("outcome", "failed")
        if self.pending_confirmation and status != "failed":
            status = "pending_confirmation"
        try:
            self.store.append_turn(self.conversation_id, self.user.id,
                                   self._turn_record(turn_id, text, answer, state, trace_id, status))
        except Exception as exc:
            self.tracer.event("history_write_failed", error_type=type(exc).__name__)
            status = "failed"
            answer += "\n\nI couldn't save this turn to conversation history."
            self.last_state.update(answer=answer, outcome="failed")
        self.observability.finish({"status": status, "plan_id": state.get("plan_id", turn_id)})
        return answer

    def _turn_record(self, turn_id: str, question: str, answer: str, state: dict,
                     trace_id: str | None, outcome: str) -> dict:
        actions = [step for step in state.get("completed_steps", []) if self.user.allows_scope(step.get("brand_scope"))]
        parts = [{"type": "sql", "sql": r.get("sql"), "bq_job_id": r.get("job_id"),
                  "bytes_billed": r.get("bytes_billed")} for r in state.get("results", [])]
        return {"turn_id": turn_id, "question": question, "answer": answer, "parts": parts, "actions": actions,
                "intent": state.get("intent"), "outcome": outcome, "check_ok": state.get("check_ok", False),
                "brand_scope": ["all"] if self.user.is_ceo else list(self.user.brands),
                "trace_id": trace_id, "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
