"""Structured model outputs and the graph state."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import AwareDatetime, BaseModel, Field


class ReportAction(BaseModel):
    """What the user wants to do with saved reports."""

    action: Literal["none", "list", "show", "delete", "save_last"] = "none"
    scope: Literal["all", "conversation", "text", "latest", "id", "previous_step"] = "all"
    text: str = Field(default="", description="Search text when scope is 'text', e.g. 'Client X'")
    report_ref: str = Field(default="", description="Report id or number when scope is 'id'")


class PlanStep(BaseModel):
    question: str = Field(default="", description="The focused action, retaining applicable dates, cutoff timestamp, timezone and filters")
    intent: Literal["analysis", "schema", "reports", "clarify", "refuse"]
    wants_report: bool = Field(default=False, description="Format an analysis as a report; this does not save it. "
                              "Creating or saving a report also needs a separate reports/save_last step.")
    reply: str = Field(default="", description="For clarify or refuse: the short message to send to the user")
    report_action: ReportAction = Field(
        default_factory=ReportAction,
        description="Only for intent=reports. For analysis, schema, clarify or refuse, action must be none. "
                    "Use save_last as a separate step to save the latest checked analysis, including one earlier in this plan.")


class RouteDecision(BaseModel):
    # No maxItems: represent the entire request; execution budgets bound the work.
    steps: list[PlanStep] = Field(min_length=1, description="The complete ordered request; never drop a requested action")


class SqlProposal(BaseModel):
    analysis_cutoff: AwareDatetime | None = Field(
        default=None, description="The user's explicit analysis cutoff as an ISO 8601 timestamp with timezone. "
        "Use null for current time or when no cutoff is requested. SQL must use @analysis_cutoff, never a literal cutoff.")
    sql: str = Field(description="One BigQuery Standard SQL SELECT statement")
    assumptions: str = Field(default="", description="Metric definition, date window and any assumption made")


class Assessment(BaseModel):
    enough: bool = Field(description="True if the results answer the question well enough")
    follow_up: str = Field(default="", description="If not enough: the single next sub-question to query")
    reason: str = Field(default="")


TurnOutcome = Literal["success", "clarification", "denied", "cancelled", "failed"]


class AgentState(TypedDict, total=False):
    # model-visible inputs
    question: str
    original_question: str
    data_cutoff: str
    history: list[dict[str, Any]]
    settings: dict[str, Any]
    # ordered plan (checkpointed across a deletion confirmation)
    plan_id: str
    plan_steps: list[dict[str, Any]]
    step_index: int
    completed_steps: list[dict[str, Any]]
    previous_report_ids: list[str]
    all_results: list[dict[str, Any]]
    # routing
    intent: str
    outcome: TurnOutcome | Literal[""]
    wants_report: bool
    reply: str
    report_action: dict[str, Any]
    # analysis
    sql: str
    assumptions: str
    sql_error: str
    repairs: int
    queries_run: int
    empty_checks: int
    follow_up: str
    results: list[dict[str, Any]]
    next_step: str
    stop_reason: str
    stop_detail: str
    # reports
    report_candidates: list[dict[str, Any]]
    displayed_report_ids: list[str]
    pending_delete_ids: list[str]
    report_outcome: dict[str, Any]
    # answer
    answer: str
    check_ok: bool
    saved_report_id: str


def fresh_turn_state(question: str, history: list[dict[str, Any]], settings: dict[str, Any],
                     displayed_report_ids: list[str] | None = None) -> AgentState:
    """Input for a new turn. Every per-turn field is reset explicitly because the
    checkpoint keeps the previous turn's values otherwise."""
    return AgentState(
        question=question,
        original_question=question,
        data_cutoff="",
        history=history,
        settings=settings,
        plan_id="",
        plan_steps=[],
        step_index=0,
        completed_steps=[],
        previous_report_ids=[],
        all_results=[],
        intent="",
        outcome="",
        wants_report=False,
        reply="",
        report_action={},
        sql="",
        assumptions="",
        sql_error="",
        repairs=0,
        queries_run=0,
        empty_checks=0,
        follow_up="",
        results=[],
        next_step="",
        stop_reason="",
        stop_detail="",
        report_candidates=[],
        displayed_report_ids=list(displayed_report_ids or []),
        pending_delete_ids=[],
        report_outcome={},
        answer="",
        check_ok=False,
        saved_report_id="",
    )
