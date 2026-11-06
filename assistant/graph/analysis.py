"""Subgraph A: draft SQL, validate and execute, assess, compose. Bounded repair and evidence loops."""

from __future__ import annotations

import datetime as dt

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from ..bq import BigQueryUnavailable, SqlError
from ..checks import redact_pii
from ..context import RuntimeContext
from ..llm import ProviderError
from ..prompts import assess_messages, compose_messages, draft_messages
from ..schemas import AgentState, Assessment, SqlProposal
from ..sqlpolicy import PolicyViolation, RepairableSqlError, ScopedQuery, validate_and_scope

STOP_TEXT = {
    "denied": "I can't run that query: {detail}. I can analyze completed sales, orders, products and shoppers within your brands.",
    "provider": "The model service is unavailable right now. Please try again in a minute.",
    "bigquery": "BigQuery is unavailable right now. Please try again in a minute.",
    "timeout": "I couldn't finish this analysis within the time limit. Try a narrower question.",
    "repairs_exhausted": "I couldn't produce a valid query within the repair limit. Try rephrasing or narrowing the question.",
    "queries_exhausted": "I couldn't retrieve results within the query limit. Try rephrasing or narrowing the question.",
    "empty_answer": "The model returned no answer for this analysis. Please try again.",
    "no_scope": "You don't have any brands assigned, so there is no data you can analyze.",
}


def _stop(reason: str, detail: str = "") -> dict:
    return {"stop_reason": reason, "stop_detail": detail, "next_step": "stop"}


def _propose_sql(state: AgentState, runtime: Runtime[RuntimeContext], *, repair: bool) -> dict:
    ctx = runtime.context
    if ctx.expired():
        return _stop("timeout")
    if state.get("queries_run", 0) >= ctx.limits.queries:
        return _stop("queries_exhausted")
    if not repair and not ctx.user.has_scope:
        return _stop("no_scope")
    name = "repair_sql" if repair else "draft_sql"
    attrs = {"attempt": state.get("repairs", 0)} if repair else {"follow_up": bool(state.get("follow_up"))}
    with ctx.tracer.span(name, **attrs) as span:
        messages = draft_messages(state["question"], state.get("history", []), ctx.schema_doc, ctx.user,
                                  state.get("results", []), follow_up=state.get("follow_up", ""),
                                  previous_sql=state.get("sql", "") if repair else "",
                                  sql_error=state.get("sql_error", "") if repair else "",
                                  original_question=state.get("original_question", state["question"]),
                                  data_cutoff=state["data_cutoff"], cutoff_resolved=repair or bool(state.get("sql")))
        try:
            proposal = ctx.llm.structured(messages, SqlProposal, deadline=ctx.deadline)
        except ProviderError as exc:
            span.update(status="provider_error", detail=redact_pii(str(exc))[:200])
            return _stop("provider", str(exc))
    cutoff = dt.datetime.fromisoformat(state["data_cutoff"])
    if not repair and not state.get("sql") and proposal.analysis_cutoff is not None:
        cutoff = proposal.analysis_cutoff
    return {"data_cutoff": cutoff.astimezone(dt.timezone.utc).isoformat(),
            "sql": proposal.sql.strip(), "assumptions": proposal.assumptions.strip() or state.get("assumptions", ""),
            "sql_error": "", "next_step": "query"}


def draft_sql(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict:
    return _propose_sql(state, runtime, repair=False)


def repair_sql(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict:
    return _propose_sql(state, runtime, repair=True)


def validate_and_execute_query(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict:
    """Python only. Policy first, then BigQuery with a deadline and full result fetching."""
    ctx = runtime.context
    if ctx.expired():
        return _stop("timeout")
    if state.get("queries_run", 0) >= ctx.limits.queries:
        return {"next_step": "assess"}
    with ctx.tracer.span("validate_and_execute_query") as span:
        try:
            scoped = validate_and_scope(state["sql"], role=ctx.user.role, brands=ctx.user.brands,
                                        analysis_cutoff=dt.datetime.fromisoformat(state["data_cutoff"]))
        except PolicyViolation as exc:
            span["status"] = "denied"
            span["detail"] = redact_pii(str(exc))
            ctx.tracer.event("policy_denied", detail=redact_pii(str(exc)))
            return _stop("denied", str(exc))
        except RepairableSqlError as exc:
            span["status"] = "sql_error"
            span["detail"] = redact_pii(str(exc))
            return _repair_or_stop(state, ctx, str(exc))
        span["tables"] = scoped.tables
        return _execute_scoped_query(state, ctx, scoped, span)


def _repair_or_stop(state: AgentState, ctx: RuntimeContext, error: str) -> dict:
    repairs = state.get("repairs", 0)
    if repairs >= ctx.limits.repairs:
        return _stop("repairs_exhausted", error)
    return {"sql_error": error, "repairs": repairs + 1, "next_step": "repair"}


def _execute_scoped_query(state: AgentState, ctx: RuntimeContext, scoped: ScopedQuery, span: dict) -> dict:
    """Count every BigQuery attempt, including failures, against the shared budget."""
    queries_run = state.get("queries_run", 0) + 1
    span["attempt"] = queries_run
    try:
        result = ctx.bq.run(scoped.sql, scoped.parameters, deadline=ctx.deadline, purpose="analysis")
    except SqlError as exc:
        span.update(status="sql_error", detail=redact_pii(str(exc)))
        if queries_run >= ctx.limits.queries:
            return {"queries_run": queries_run, "sql_error": str(exc), "next_step": "assess"}
        return {"queries_run": queries_run, **_repair_or_stop(state, ctx, str(exc))}
    except BigQueryUnavailable as exc:
        span["status"] = "unavailable"
        return {"queries_run": queries_run, **_stop("bigquery", str(exc))}
    span.update({"rows": len(result.rows), "bytes_billed": result.bytes_billed, "job_id": result.job_id,
                 "truncated": result.truncated, "ms": result.duration_ms})
    result_record = {**result.to_state(), "data_cutoff": scoped.data_cutoff.isoformat()}
    results = list(state.get("results", [])) + [result_record]
    return {"results": results, "queries_run": queries_run, "sql_error": "", "next_step": "assess"}


def assess_results(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict:
    ctx = runtime.context
    results = state.get("results", [])
    if not results:
        reason = "queries_exhausted" if state.get("queries_run", 0) >= ctx.limits.queries else "repairs_exhausted"
        return _stop(reason, state.get("sql_error", "no query could be executed"))
    if ctx.expired():
        return _stop("timeout")
    queries_run = state.get("queries_run", 0)
    if queries_run >= ctx.limits.queries:
        return {"next_step": "compose", "follow_up": ""}
    last_empty = not results[-1].get("rows")
    if last_empty and state.get("empty_checks", 0) >= 1:
        return {"next_step": "compose", "follow_up": ""}  # one check of filters is enough
    with ctx.tracer.span("assess_results", queries_run=queries_run) as span:
        try:
            verdict = ctx.llm.structured(
                assess_messages(state["question"], results, state.get("assumptions", ""), queries_run, ctx.limits.queries,
                                original_question=state.get("original_question", state["question"])),
                Assessment, deadline=ctx.deadline)
        except ProviderError as exc:
            span.update(status="provider_error", detail=redact_pii(str(exc))[:200])
            return {"next_step": "compose", "follow_up": ""}  # compose from what we have
    if verdict.enough or not verdict.follow_up.strip():
        return {"next_step": "compose", "follow_up": ""}
    update = {"next_step": "draft", "follow_up": verdict.follow_up.strip()}
    if last_empty:
        update["empty_checks"] = state.get("empty_checks", 0) + 1
    return update


def compose_analysis(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict:
    ctx = runtime.context
    if ctx.expired():
        return _stop("timeout")
    with ctx.tracer.span("compose_analysis", wants_report=state.get("wants_report", False)) as span:
        try:
            answer = ctx.llm.text(
                compose_messages(state["question"], state.get("history", []), state.get("results", []),
                                 state.get("assumptions", ""), state.get("settings", {}), ctx.user,
                                 state.get("wants_report", False),
                                 original_question=state.get("original_question", state["question"])),
                deadline=ctx.deadline)
        except ProviderError as exc:
            span.update(status="provider_error", detail=redact_pii(str(exc))[:200])
            return _stop("provider", str(exc))
        if not answer.strip():
            span["status"] = "empty_answer"
            return _stop("empty_answer")
        if ctx.expired():
            span["status"] = "timeout"
            return _stop("timeout")
    return {"answer": answer, "outcome": "success", "next_step": "done"}


def set_analysis_stop(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict:
    reason = state.get("stop_reason", "timeout")
    detail = state.get("stop_detail", "")
    text = STOP_TEXT.get(reason, STOP_TEXT["timeout"]).format(detail=detail or "no further detail")
    runtime.context.tracer.event("analysis_stopped", reason=reason, detail=redact_pii(detail)[:200])
    outcome = "denied" if reason in ("denied", "no_scope") else "failed"
    return {"answer": text, "outcome": outcome, "results": [], "next_step": "done"}


def _route(state: AgentState) -> str:
    step = state.get("next_step", "stop")
    if step == "done":
        return END
    return {"query": "validate_and_execute_query", "repair": "repair_sql", "assess": "assess_results",
            "draft": "draft_sql", "compose": "compose_analysis", "stop": "set_analysis_stop"}.get(step, "set_analysis_stop")


def build_analysis_subgraph():
    g = StateGraph(AgentState, context_schema=RuntimeContext)
    for name, fn in (("draft_sql", draft_sql), ("repair_sql", repair_sql),
                     ("validate_and_execute_query", validate_and_execute_query), ("assess_results", assess_results),
                     ("compose_analysis", compose_analysis), ("set_analysis_stop", set_analysis_stop)):
        g.add_node(name, fn)
    g.add_edge(START, "draft_sql")
    targets = ["validate_and_execute_query", "repair_sql", "assess_results", "draft_sql", "compose_analysis", "set_analysis_stop"]
    g.add_conditional_edges("draft_sql", _route, targets)
    g.add_conditional_edges("repair_sql", _route, targets)
    g.add_conditional_edges("validate_and_execute_query", _route, targets)
    g.add_conditional_edges("assess_results", _route, targets)
    g.add_conditional_edges("compose_analysis", _route, targets + [END])  # failure goes through the stop node
    g.add_edge("set_analysis_stop", END)
    return g.compile()
