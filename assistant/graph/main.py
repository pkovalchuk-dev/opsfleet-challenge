"""Main graph: validate a plan, then execute and check each ordered action."""

from __future__ import annotations

import datetime as dt
import re

from pydantic import ValidationError

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from ..checks import redact_pii, run_output_check
from ..context import RuntimeContext
from ..llm import ProviderError
from ..prompts import route_messages, schema_messages
from ..schemas import AgentState, PlanStep, RouteDecision
from .analysis import build_analysis_subgraph
from .reports import build_reports_subgraph

SAFE_TEXT = {
    "personal data": "I can't show that answer because it would include personal information about shoppers.",
    "empty answer": "I couldn't put an answer together this time. Please try again.",
}


def _safe_plan(reply: str, *, intent: str = "clarify", outcome: str = "", reason: str = "") -> dict:
    return {"plan_steps": [PlanStep(intent=intent, reply=reply).model_dump()],
            "outcome": outcome, "stop_reason": reason}


def _validate_plan(decision: RouteDecision, question: str, ctx: RuntimeContext) -> list[dict]:
    """Validate the whole plan before any analysis or report mutation is attempted."""
    steps = [step.model_dump() for step in RouteDecision.model_validate(decision.model_dump()).steps]
    for index, step in enumerate(steps):
        if step["intent"] in ("clarify", "refuse"):
            if not step["reply"].strip():
                step["reply"] = "I can help with retail analysis and your saved reports."
                if step["intent"] == "clarify":
                    step["reply"] = step["question"].strip() or (
                        "Please clarify which analysis or saved-report actions you want me to perform.")
            return [step]
        if not step["question"].strip():
            if len(steps) > 1:
                raise ValueError("Each action needs a clear question. Please specify the actions in order.")
            step["question"] = question
        if len(steps) == 1 and step["intent"] in ("analysis", "schema"):
            step["question"] = question  # A single action needs no lossy paraphrase.
        action = step["report_action"]
        if step["intent"] == "analysis" and not ctx.user.has_scope:
            return [PlanStep(intent="refuse", reply="You don't have any brands assigned, so there is no data you can analyze.").model_dump()]
        if step["wants_report"] and step["intent"] != "analysis":
            raise ValueError("Please clarify which analysis you want written in report format.")
        if step["intent"] != "reports":
            if action["action"] != "none":
                raise ValueError("Please separate the analysis and saved-report actions.")
            continue
        _validate_report_action(step, steps[index - 1] if index else None)
    return steps


def _validate_report_action(step: dict, prior: dict | None) -> None:
    """Reject ambiguous report selections before any action in the plan runs."""
    action = step["report_action"]
    if action["action"] == "none":
        raise ValueError("What would you like to do with your saved reports: list, show, save or delete?")
    if action["scope"] == "text" and not action["text"].strip():
        raise ValueError("Which text should I search for in your reports?")
    if action["scope"] == "id" and not action["report_ref"].strip():
        raise ValueError("Which report should I use? Please provide its ID or displayed number.")
    if action["action"] in ("list", "show", "delete") and action["scope"] in ("all", "conversation") and re.search(
            r"\b(old|older|previous|earlier)\b", step["question"], re.I):
        raise ValueError("Which reports do you mean by old or previous? Please specify their IDs or search text.")
    if action["scope"] != "previous_step":
        return
    produces_ids = prior and prior["intent"] == "reports" and prior["report_action"]["action"] in (
        "list", "show", "save_last")
    if action["action"] not in ("show", "delete") or not produces_ids:
        raise ValueError("That report reference is unclear. Please list or save the reports immediately before using them.")


def plan_and_route(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict:
    ctx = runtime.context
    base = {"plan_id": ctx.turn_id}
    if ctx.expired():
        return {**base, **_safe_plan("This request exceeded its time limit. Please try again.",
                                    intent="refuse", outcome="failed", reason="timeout")}
    with ctx.tracer.span("plan_and_route") as span:
        try:
            decision = ctx.llm.structured(route_messages(state["question"], state.get("history", []), ctx.user),
                                          RouteDecision, deadline=ctx.deadline)
            steps = _validate_plan(decision, state["question"], ctx)
        except ValidationError:
            return {**base, **_safe_plan("I couldn't form a clear supported plan. Please specify the actions in order, with exact report criteria.")}
        except ValueError as exc:
            return {**base, **_safe_plan(str(exc))}
        except ProviderError as exc:
            span["status"] = "provider_error"
            span["detail"] = redact_pii(str(exc))[:200]
            if isinstance(exc.__cause__, ValidationError):
                return {**base, **_safe_plan("I couldn't form a clear supported plan. Please specify the actions in order, with exact report criteria.")}
            return {**base, **_safe_plan("The model service is unavailable right now. Please try again in a minute.",
                                        intent="refuse", outcome="failed", reason="provider")}
        span["step_count"] = len(steps)
    return {**base, "plan_steps": steps}


def start_step(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict:
    ctx = runtime.context
    index = len(state.get("completed_steps", []))
    step = state["plan_steps"][index]
    # Keep plan counters and completed work; clear all per-action results and flags.
    update = {**step, "step_index": index,
              "data_cutoff": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "sql": "", "assumptions": "", "sql_error": "", "empty_checks": 0,
              "follow_up": "", "results": [], "next_step": "", "stop_detail": "",
              "report_candidates": [], "pending_delete_ids": [], "report_outcome": {},
              "answer": "", "check_ok": False, "saved_report_id": "",
              "history": [turn for turn in state.get("history", []) if ctx.user.allows_scope(turn.get("brand_scope"))]}
    if state.get("stop_reason") and state.get("outcome") == "failed":
        return update  # planner failure carries its trusted outcome/reason into safe_reply
    update.update(outcome="clarification" if step["intent"] == "clarify" else "denied" if step["intent"] == "refuse" else "",
                  stop_reason="")
    if ctx.expired():
        return {**update, "intent": "refuse", "outcome": "failed", "stop_reason": "timeout",
                "reply": "This request exceeded its time limit. Remaining actions were not run."}
    if step["intent"] in ("analysis", "schema"):
        try:
            ctx.ensure_clients()
        except Exception as exc:
            ctx.tracer.event("turn_failed", error_type=type(exc).__name__)
            return {**update, "intent": "refuse", "outcome": "failed", "stop_reason": "provider_setup",
                    "reply": "I couldn't connect to the analysis services. Please check the configuration and try again."}
    ctx.tracer.event("start_step", plan_id=state["plan_id"], step_index=index, step_count=len(state["plan_steps"]))
    return update


def _route_intent(state: AgentState) -> str:
    return {"analysis": "analyze", "schema": "read_context", "reports": "manage_reports"}.get(
        state.get("intent", ""), "safe_reply")


def read_context(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict:
    """Schema questions never run SQL: answer from the data description."""
    ctx = runtime.context
    if ctx.expired():
        return {"answer": "This request exceeded its time limit. Please try again.",
                "outcome": "failed", "stop_reason": "timeout"}
    with ctx.tracer.span("read_context") as span:
        try:
            answer = ctx.llm.text(schema_messages(state["question"], ctx.schema_doc, ctx.user), deadline=ctx.deadline)
        except ProviderError as exc:
            span.update(status="provider_error", detail=redact_pii(str(exc))[:200])
            answer = (f"Here is what you can analyze (scope: {ctx.user.scope_label}): orders, order items, products "
                      f"and shoppers of the retailer, with sales, returns, customers and product performance. "
                      "The model service is unavailable, so I couldn't answer your specific question.")
            return {"answer": answer, "outcome": "failed", "stop_reason": "provider"}
    return {"answer": answer, "outcome": "success"}


def safe_reply(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict:
    reply = (state.get("reply") or "").strip()
    if not reply:
        reply = ("I can help with questions about sales, orders, products and customers for your brands, "
                 "and with saved reports. What would you like to look at?")
    runtime.context.tracer.event("safe_reply", intent=state.get("intent", ""))
    outcome = state.get("outcome") or ("clarification" if state.get("intent") == "clarify" else "denied")
    return {"answer": reply, "outcome": outcome}


def output_check(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict:
    ctx = runtime.context
    answer = state.get("answer", "") or ""
    results = state.get("results", []) or []
    if answer.strip() and state.get("intent") == "analysis" and any(r.get("truncated") for r in results):
        answer += "\n\n_Data note: only part of a query result was fetched; this answer uses a partial result._"
    with ctx.tracer.span("output_check") as span:
        check = run_output_check(answer)
        span.update({"status": "ok" if check.ok else "blocked", "ok": check.ok,
                     "blocked": check.blocked_reason, "findings": check.findings})
    if not check.ok:
        text = SAFE_TEXT.get(check.blocked_reason, "I've withheld that answer because it failed a safety check.")
        outcome = "failed" if check.blocked_reason == "empty answer" else "denied"
        return {"answer": text, "check_ok": False, "outcome": outcome,
                "displayed_report_ids": []}
    return {"answer": answer, "check_ok": True}


def completed_answer(steps: list[dict], user=None) -> str:
    """Only checked outputs and fixed headings enter the combined user-visible answer."""
    labels = {"analysis": "Analysis", "reports": "Reports", "schema": "Available data",
              "clarify": "Clarification", "refuse": "Request stopped"}
    blocks = []
    for step in steps:
        answer = step["answer"]
        if user is not None and step.get("outcome") == "success" and not user.allows_scope(step.get("brand_scope")):
            answer = "This earlier output is unavailable under your current brand permissions."
        blocks.append(f"**Step {step['step_index'] + 1} — {labels.get(step['intent'], 'Result')}**\n\n{answer}")
    return "\n\n".join(blocks)


def _step_record(state: AgentState, ctx: RuntimeContext, plan_id: str) -> dict:
    """Record the checked answer and evidence used by history and later saves."""
    return {"turn_id": plan_id, "step_index": state.get("step_index", 0),
              "question": state["question"], "answer": state["answer"], "intent": state["intent"],
              "outcome": state["outcome"], "check_ok": state["check_ok"],
              "report_ids": _result_report_ids(state) if state["check_ok"] and state["outcome"] == "success" else [],
              "brand_scope": ["all"] if ctx.user.is_ceo else list(ctx.user.brands),
              "data_cutoff": next((r["data_cutoff"] for r in state.get("results", []) if r.get("data_cutoff")), ""),
              "assumptions": state.get("assumptions", ""), "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "parts": [{"type": "sql", "sql": r.get("sql"), "bq_job_id": r.get("job_id"),
                         "bytes_billed": r.get("bytes_billed")} for r in state.get("results", [])]}


def _result_report_ids(state: AgentState) -> list[str]:
    """The next action may refer only to reports returned by this action."""
    if state.get("saved_report_id"):
        return [state["saved_report_id"]]
    if state["intent"] != "reports":
        return []
    action = state.get("report_outcome", {})
    if action.get("action") == "list":
        return list(state.get("displayed_report_ids", []))
    if action.get("action") == "show" and action.get("report_id"):
        return [action["report_id"]]
    return []


def complete_step(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict:
    ctx = runtime.context
    record = _step_record(state, ctx, state["plan_id"])
    success = state.get("outcome") == "success" and state.get("check_ok")
    results = list(state.get("all_results", [])) + (state.get("results", []) if success else [])
    return {"step_index": record["step_index"],
            "completed_steps": state.get("completed_steps", []) + [record], "all_results": results,
            "previous_report_ids": record["report_ids"],
            "history": state.get("history", []) + ([record] if success else [])}


def _after_step(state: AgentState) -> str:
    return "start_step" if (state.get("outcome") == "success" and state.get("check_ok") and
                             len(state.get("completed_steps", [])) < len(state.get("plan_steps", []))) else "finish_plan"


def finish_plan(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict:
    steps = state.get("completed_steps", [])
    visible = steps[runtime.context.displayed_steps:]
    answer = completed_answer(visible, runtime.context.user) if len(state["plan_steps"]) > 1 else state["answer"]
    remaining = len(state["plan_steps"]) - len(steps)
    if remaining:
        answer += f"\n\nI stopped here; {remaining} remaining action{'s were' if remaining != 1 else ' was'} not run."
    authorized = all(runtime.context.user.allows_scope(s.get("brand_scope")) for s in steps if s.get("outcome") == "success")
    return {"answer": answer, "results": state.get("all_results", []) if authorized else [],
            "intent": "multi_step" if len(state["plan_steps"]) > 1 else state["intent"]}


def build_graph(checkpointer=None):
    g = StateGraph(AgentState, context_schema=RuntimeContext)
    g.add_node("plan_and_route", plan_and_route)
    g.add_node("start_step", start_step)
    g.add_node("analyze", build_analysis_subgraph())
    g.add_node("read_context", read_context)
    g.add_node("manage_reports", build_reports_subgraph())
    g.add_node("safe_reply", safe_reply)
    g.add_node("output_check", output_check)
    g.add_node("complete_step", complete_step)
    g.add_node("finish_plan", finish_plan)

    g.add_edge(START, "plan_and_route")
    g.add_edge("plan_and_route", "start_step")
    g.add_conditional_edges("start_step", _route_intent, ["analyze", "read_context", "manage_reports", "safe_reply"])
    for node in ("analyze", "read_context", "manage_reports", "safe_reply"):
        g.add_edge(node, "output_check")
    g.add_edge("output_check", "complete_step")
    g.add_conditional_edges("complete_step", _after_step, ["start_step", "finish_plan"])
    g.add_edge("finish_plan", END)
    return g.compile(checkpointer=checkpointer)
