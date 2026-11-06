"""Saved-report actions. Only an exact deletion preview may pause for confirmation."""

from __future__ import annotations

import re

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from ..checks import find_pii, redact_pii, run_output_check
from ..context import RuntimeContext
from ..schemas import AgentState
from ..store import report_content

YES = re.compile(r"^\s*(y|yes|yep|yeah|confirm|confirmed|delete|do it|go ahead|please delete)\s*[.!]?\s*$", re.I)
REPORT_NUMBER = re.compile(r"(?:report\s+)?([1-9]\d*)", re.I)


def _route_entry(state: AgentState) -> str:
    return "save_last" if (state.get("report_action") or {}).get("action") == "save_last" else "find_reports"


def _resolve_report_id(ref: str, displayed: list[str]) -> str:
    number = REPORT_NUMBER.fullmatch(ref)
    if not number:
        return ref  # An unknown exact ID must never become a numbered reference.
    index = int(number[1]) - 1
    return displayed[index] if index < len(displayed) else ""


def _candidates(state: AgentState, ctx: RuntimeContext) -> list[dict]:
    action = state.get("report_action") or {}
    scope = action.get("scope", "all")
    text = (action.get("text") or "").strip()
    ref = (action.get("report_ref") or "").strip()
    store, user = ctx.store, ctx.user
    if scope == "previous_step":
        # The preceding result is the authority for this reference; never search again.
        found = [store.get_report(rid, user) for rid in state.get("previous_report_ids", [])]
        return [dict(report, match_reason="returned by the previous action") for report in found if report]
    if scope == "id":
        report_id = _resolve_report_id(ref, state.get("displayed_report_ids") or [])
        found = store.get_report(report_id, user) if report_id else None
        return [dict(found, match_reason=f"report {ref}")] if found else []
    if scope == "latest":
        return [dict(r, match_reason="your most recent report") for r in store.list_reports(user, limit=1)]
    if scope == "conversation":
        return store.find_reports(user, text=text, conversation_id=ctx.conversation_id)
    if scope == "text":
        return store.find_reports(user, text=text) if text else []
    return store.find_reports(user)


def _format_list(reports: list[dict]) -> str:
    """Redact display labels without changing the IDs that identify each report."""
    lines = []
    for i, report in enumerate(reports, 1):
        title = redact_pii(report["title"])
        reason = redact_pii(report.get("match_reason", ""))
        suffix = f" — {reason}" if reason else ""
        lines.append(f"{i}. **{title}** (`{report['id']}`, {report['created_at'][:10]}){suffix}")
    if any(report.get("restricted") for report in reports):
        lines.append("\nRestricted reports are yours, but their contents are outside your current brand permissions.")
    return "\n".join(lines)


def deletion_preview(reports: list[dict]) -> str:
    return (f"This will permanently delete {len(reports)} report{'s' if len(reports) != 1 else ''}:\n\n"
            f"{_format_list(reports)}\n\nOnly the reports listed here will be deleted. "
            "Reply **yes** to delete them or **no** to keep them.")


def find_reports(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict:
    ctx = runtime.context
    action = (state.get("report_action") or {}).get("action", "list")
    with ctx.tracer.span("find_reports", action=action) as span:
        candidates = _candidates(state, ctx)
        span["candidates"] = len(candidates)
    return {"report_candidates": candidates}


def _route_action(state: AgentState) -> str:
    action = (state.get("report_action") or {}).get("action", "list")
    if (state.get("report_action") or {}).get("scope") == "previous_step" and not state.get("report_candidates"):
        return "report_outcome"
    if action in ("delete", "show"):
        if not state.get("report_candidates"):
            return "report_outcome"
        return "prepare_delete" if action == "delete" else "show_report"
    return "list_reports"


def report_list_update(candidates: list[dict]) -> dict:
    """List text and numbered selection state for report actions."""
    answer = (f"Your saved reports ({len(candidates)}):\n\n{_format_list(candidates)}\n\n"
              'Say "show report 1" to read one.' if candidates else "No saved reports match this request.")
    return {"answer": answer, "outcome": "success", "displayed_report_ids": [r["id"] for r in candidates],
            "report_outcome": {"action": "list", "count": len(candidates)}}


def list_reports(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict:
    return report_list_update(state.get("report_candidates") or [])


def show_report(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict:
    candidates = state.get("report_candidates") or []
    if len(candidates) > 1:
        return {"answer": "Which one?\n\n" + _format_list(candidates), "outcome": "clarification",
                "displayed_report_ids": [r["id"] for r in candidates],
                "report_outcome": {"action": "show", "count": len(candidates)}}
    report = candidates[0]
    content = report.get("content", {})
    if not runtime.context.user.allows_scope(content.get("brand_scope")):
        return {"answer": "I can't show this report under your current brand permissions. Ask for a new analysis.",
                "outcome": "denied", "report_outcome": {"action": "show", "status": "denied"}}
    cutoff = content.get("data_cutoff") or "not recorded"
    answer = f"{content.get('answer_md', '')}\n\n_Report `{report['id']}` · Analysis cutoff: {cutoff}._"
    return {"answer": answer, "outcome": "success", "report_outcome": {"action": "show", "report_id": report["id"]}}


def save_last(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict:
    """Save the latest checked analysis, including a completed action in this plan."""
    ctx = runtime.context
    history = [action for turn in state.get("history", []) for action in (turn.get("actions") or [turn])]
    turn = next((t for t in reversed(history) if t.get("intent") == "analysis"), None)
    queries = [p for p in turn.get("parts", []) if p.get("type") == "sql"] if turn else []
    if not (turn and turn.get("outcome") == "success" and turn.get("check_ok") and turn.get("answer") and queries
            and turn.get("turn_id") and "step_index" in turn and turn.get("data_cutoff")):
        return {"answer": "There is no successful, checked analysis in this conversation to save. Ask a data question first.",
                "outcome": "clarification", "report_outcome": {"action": "save_last", "status": "nothing"}}
    source_scope = turn.get("brand_scope")
    if not ctx.user.allows_scope(source_scope):
        return {"answer": "That analysis is outside your current brand scope, so I haven't saved it.",
                "outcome": "denied", "report_outcome": {"action": "save_last", "status": "denied"}}
    check = run_output_check(turn["answer"])
    if not check.ok:
        return {"answer": "That previous analysis no longer passes the safety check, so I haven't saved it.",
                "outcome": "denied", "report_outcome": {"action": "save_last", "status": "denied"}}
    title, content = _saved_analysis_payload(turn, queries)
    if ctx.expired():
        return {"answer": "The request timed out before the report could be saved.",
                "outcome": "failed", "stop_reason": "timeout",
                "report_outcome": {"action": "save_last", "status": "timeout"}}
    with ctx.tracer.span("save_report") as span:
        rid = ctx.store.save_report(ctx.user.id, ctx.conversation_id, title, content)
        span["report_id"] = rid
    ctx.tracer.event("report_saved", report_id=rid, source="save_last")
    return {"answer": f"Saved the analysis as report `{rid}`: **{title}**.", "outcome": "success",
            "report_outcome": {"action": "save_last", "report_id": rid}, "saved_report_id": rid}


def _saved_analysis_payload(turn: dict, queries: list[dict]) -> tuple[str, dict]:
    """Keep the original analysis provenance while deriving a safe display title."""
    answer = turn["answer"]
    first = answer.strip().splitlines()[0]
    title = first.lstrip("# ").strip() if first.startswith("#") else turn.get("question", "Saved analysis")
    title = " ".join(redact_pii(title).split())[:80] or "Saved analysis"
    content = report_content(
        turn_id=turn["turn_id"], step_index=turn["step_index"],
        question=turn.get("question", ""), answer=answer, queries=queries,
        brand_scope=turn.get("brand_scope"), data_cutoff=turn["data_cutoff"],
        assumptions=turn.get("assumptions", ""))
    return title, content


def prepare_delete(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict:
    candidates = state.get("report_candidates") or []
    ids = [r["id"] for r in candidates]
    preview = deletion_preview(candidates)
    # Interrupts are displayed before the parent's output check; check this text here.
    if find_pii(preview):
        runtime.context.tracer.event("delete_preview_denied", reason="personal data")
        return {"answer": "I couldn't safely display the deletion preview. Nothing was deleted.",
                "outcome": "denied", "pending_delete_ids": [],
                "report_outcome": {"action": "delete", "status": "denied"}}
    runtime.context.tracer.event("delete_preview", count=len(ids), report_ids=ids)
    return {"pending_delete_ids": ids, "displayed_report_ids": ids, "answer": preview}


def _after_preview(state: AgentState) -> str:
    return "await_confirmation" if state.get("pending_delete_ids") else END


def await_confirmation(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict:
    """Resume the exact preview. This node itself has no deletion side effects."""
    user = runtime.context.user
    reply = interrupt({"kind": "delete_confirmation", "preview": state.get("answer", ""),
                       "report_ids": state.get("pending_delete_ids", []),
                       "brand_scope": ["all"] if user.is_ceo else list(user.brands)})
    confirmed = bool(YES.fullmatch(str(reply or "")))
    return {"report_outcome": {"action": "delete", "confirmed": confirmed}}


def _after_confirmation(state: AgentState) -> str:
    return "commit_delete" if (state.get("report_outcome") or {}).get("confirmed") else "report_outcome"


def commit_delete(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict:
    ctx = runtime.context
    ids = list(state.get("pending_delete_ids") or [])
    with ctx.tracer.span("commit_delete", requested=len(ids)) as span:
        deleted = ctx.store.delete_reports(ctx.user.id, ids)
        span["deleted"] = deleted
    ctx.tracer.event("reports_deleted", count=deleted, report_ids=ids)
    answer = f"Deleted {deleted} report{'s' if deleted != 1 else ''}."
    if deleted < len(ids):
        answer += f" {len(ids) - deleted} had already been removed."
    return {"answer": answer, "outcome": "success", "pending_delete_ids": [],
            "report_outcome": {"action": "delete", "confirmed": True, "deleted": deleted}}


def report_outcome(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict:
    action = (state.get("report_action") or {}).get("action", "list")
    if action == "delete" and state.get("pending_delete_ids"):
        runtime.context.tracer.event("delete_cancelled", report_ids=state["pending_delete_ids"])
        return {"answer": "No reports were deleted.", "outcome": "cancelled", "pending_delete_ids": [],
                "report_outcome": {"action": "delete", "confirmed": False, "deleted": 0}}
    return {"answer": "I couldn't find any of your reports matching that. Nothing was changed.",
            "outcome": "clarification", "report_outcome": {"action": action, "count": 0}}


def build_reports_subgraph():
    g = StateGraph(AgentState, context_schema=RuntimeContext)
    for name, fn in (("find_reports", find_reports), ("list_reports", list_reports), ("show_report", show_report),
                     ("save_last", save_last), ("prepare_delete", prepare_delete),
                     ("await_confirmation", await_confirmation), ("commit_delete", commit_delete),
                     ("report_outcome", report_outcome)):
        g.add_node(name, fn)
    g.add_conditional_edges(START, _route_entry, ["save_last", "find_reports"])
    g.add_conditional_edges("find_reports", _route_action,
                            ["list_reports", "show_report", "prepare_delete", "report_outcome"])
    for node in ("list_reports", "show_report", "save_last", "commit_delete", "report_outcome"):
        g.add_edge(node, END)
    g.add_conditional_edges("prepare_delete", _after_preview, ["await_confirmation", END])
    g.add_conditional_edges("await_confirmation", _after_confirmation, ["commit_delete", "report_outcome"])
    return g.compile()
