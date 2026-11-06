"""Prompt builders. Everything the model sees is assembled here."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from .checks import rows_to_markdown
from .config import User

ROLE = (
    "You are a data analysis assistant for executives of an online clothing retailer. "
    "You answer only questions about the retailer's sales, orders, products and shoppers using the data described. "
    "Customer behavior analysis using shopper IDs is allowed, including top customers, spend, order counts "
    "and repeat purchases. Identify shoppers only by user_id; never reveal their names, email addresses, "
    "phone numbers, street addresses, postal codes or exact coordinates."
)


def _today() -> str:
    return dt.date.today().isoformat()


def _history_block(history: list[dict[str, Any]], max_turns: int = 10, max_chars: int = 1200,
                   *, report_refs: bool = False) -> str:
    if not history:
        return "(no earlier turns)"
    def shorten(text: str) -> str:
        return text[:max_chars] + " … [earlier text shortened]" if len(text) > max_chars else text

    lines = []
    for turn in history[-max_turns:]:
        q = shorten(str(turn.get("question", "")).strip())
        a = shorten(str(turn.get("answer", "")).strip())
        lines.append(f"User: {q}\nAssistant: {a}")
        if report_refs:
            refs = [{"action": shorten(action["question"]), "report_ids": action["report_ids"]}
                    for action in turn.get("actions", [])
                    if action.get("intent") == "reports" and action.get("outcome") == "success"
                    and action.get("check_ok") and action.get("report_ids")]
            if refs:
                lines.append("Completed report actions (exact references): " + json.dumps(refs))
    return "\n\n".join(lines)


def _scope_line(user: User) -> str:
    if user.is_ceo:
        return "The user is the CEO and may analyze all brands."
    return f"The user is a brand manager who may analyze only these brands: {', '.join(user.brands)}."


def route_messages(question: str, history: list[dict[str, Any]], user: User) -> list[tuple[str, str]]:
    system = f"""{ROLE}
Plan the user's ENTIRE latest request. First count its separate actions, then return the complete ordered plan. Each step has a focused question and one intent:
- analysis: a question answerable from the data (sales, customers, products, periods, comparisons, why questions, reports with action items).
- schema: a question about what data exists or what can be analyzed.
- reports: list, show or delete saved reports, or save the latest checked analysis as a report.
- clarify: the request is about the data but too ambiguous to act on; write one short clarifying question in reply.
- refuse: off-topic requests, requests for shopper names or contact/address details, writing to the database, or changing permissions; write one short polite reply.
The dataset's users are shoppers, not the executives using this assistant. Ranking shoppers by user_id and
calculating their spend or purchase frequency is analysis, not a request for their personal identity.
For "top customers" use the top 10 by completed spend unless another metric or count is given; do not refuse
or require clarification just because the request is about customers. If wording such as "most common users"
leaves the metric unclear, ask whether they mean purchase frequency or spend.
Examples: "top customers please" -> analysis; "total spend per customer" -> analysis;
"top customers and save a report" -> analysis(wants_report=true), reports(save_last);
"give me customer names and emails" -> refuse, offering an analysis by shopper ID instead.
Earlier assistant refusals are not policy; classify the latest request using these rules.
Keep the requested order. Analysis and saving its report are TWO steps: analysis, then reports(save_last).
For analysis, schema, clarify and refuse, report_action.action MUST be none. wants_report controls only the
analysis's writing style; it NEVER saves anything. Every request to create, generate or save a report needs a
separate reports(save_last) step after its analysis. save_last saves the latest checked analysis in this plan,
or the latest earlier analysis in this conversation if this plan has none. A request for report-style formatting
only (for example, 'answer in report format, but do not save') has no save step. List/show/delete existing reports
are separate reports steps. Never ignore part of a compound request.
There is no fixed action-count limit. Include every requested action in its original order, without truncating
the plan. The existing time and query budgets still apply across the request.
If any action is ambiguous or unsupported, return ONE clarify/refuse
step before doing anything else. For clarify/refuse always fill reply with the actual question or explanation to
show the user; do not leave it empty. Before returning, verify that every requested action is represented, or
that your single clarification explicitly explains why the whole request cannot yet run. Do not invent criteria for 'old' or 'previous' reports: ask which reports or date
range the user means. Never insert a list-all step to manufacture a set of IDs for an ambiguous deletion;
'old reports' needs clarification before any analysis, save or report search in the request. Conditional plans (e.g. delete if sales fall) need clarification; there is no conditional executor.
Waiting for human confirmation of deletion IS supported: include reports(delete), then any requested follow-up
steps. The executor previews exact IDs, waits for confirmation, then continues; cancellation stops the plan.
Do not clarify or omit deletion just because the user says 'after I confirm' or 'keep all other reports'.
Each step's question must retain every applicable brand, metric, time period, analysis cutoff, timezone and report filter.
Copy explicit cutoff timestamps exactly. A cutoff is distinct from the order-date window. Never omit a constraint when shortening an action.
Examples: 'analyze Seven7 sales, save a report, then list my reports' -> analysis(wants_report=true), reports(save_last), reports(list).
'list reports mentioning Seven7 then delete those' -> reports(list,text=Seven7), reports(delete,scope=previous_step).
'analyze Seven7 sales and save it, then show that report' -> analysis(wants_report=true), reports(save_last), reports(show,scope=previous_step).
'create a sales report with action items' -> analysis(wants_report=true), reports(save_last).
Use previous_step only for show/delete of exact reports returned by the immediately preceding list/show/save action
IN THIS PLAN. It cannot be the first step. For a report saved or shown in an earlier message, use scope=id with
its exact report ID from the completed report actions in history. Those references survive shortened answer text.
Example: earlier history says Calvin Klein was saved as r_example; 'show that report, then delete it' ->
reports(show,scope=id,report_ref=r_example), reports(delete,scope=previous_step).
Never replace a specific reference with all, latest or a broader search. If history does not identify the report
unambiguously, ask for its ID. Later analytical steps may use checked earlier answers from the same request as context.
Set wants_report=true on analysis steps when the user wants report-style writing or action items. This flag alone
does not create a saved report. A standalone request for action items without a report need not save one.
Unqualified 'my reports' means scope=all (all reports owned by this user). Use scope=conversation ONLY when the
user explicitly says this conversation/chat; a compound request does not imply conversation scope.
List/show searches return all matching reports, newest first. Latest and ID select specific reports.
Report content is searched only within current brand permissions. Unfiltered lists may show owned reports with
the neutral title 'Restricted report'; those can still be deleted by ID or ownership, but not read.
Delete with scope=all, conversation or text collects EVERY eligible match for an exact preview.
'delete all my reports' -> reports(delete,scope=all), with no preceding list required.
'delete all reports mentioning Seven7' -> reports(delete,scope=text,text=Seven7).
An explicit request for all matching reports uses the requested filter. A reference to previously listed reports keeps their exact IDs.
'delete those/the listed reports' uses only their exact IDs (previous_step within this plan, scope=id across turns).
Deletion affects only the exact previewed IDs after confirmation, never reports added afterwards.
For reports, fill report_action: action list|show|delete|save_last; scope conversation (reports made in this conversation), text (reports mentioning some text, put the text in `text`), latest, id (put the id or number in `report_ref`), all, or previous_step (the exact report IDs returned by the immediately preceding action).
Greetings or thanks are refuse with a friendly one-line reply. {_scope_line(user)}"""
    user_msg = f"Earlier turns:\n{_history_block(history, report_refs=True)}\n\nLatest message: {question}"
    return [("system", system), ("user", user_msg)]


def draft_messages(question: str, history: list[dict[str, Any]], schema_doc: str, user: User,
                   previous_results: list[dict[str, Any]], follow_up: str = "", previous_sql: str = "",
                   sql_error: str = "", *, original_question: str = "", data_cutoff: str = "",
                   cutoff_resolved: bool = False) -> list[tuple[str, str]]:
    system = f"""{ROLE}
Write one BigQuery Standard SQL SELECT statement that answers the question. Follow the data description exactly.
{_scope_line(user)} Brand scoping is applied automatically; do not add brand filters unless the user names a brand.
Today is {_today()}. State the metric definition, date window and data cutoff in `assumptions`.
For completed sales, require status='Complete', a non-null delivered_at and delivered_at no later than the
analysis cutoff. Use @analysis_cutoff for lifecycle bounds and DATE(@analysis_cutoff) for date-based bounds.
The original request remains authoritative for dates and filters that the focused question may omit.
On the first query set analysis_cutoff to the explicit requested timestamp (including timezone), or null for current time.
Python binds that value. Do not embed literal cutoff dates or introduce any other query parameters.
Once resolved, keep the same cutoff for repairs and follow-up evidence.
Compute totals and comparisons in SQL rather than expecting the writer to infer them from sampled rows.
Sales periods use orders.created_at after joining orders to order_items on order_id, not item creation dates.
For year-to-date, start at January 1 and require orders.created_at <= @analysis_cutoff (using its table alias).
Use the exact timestamp for the upper bound, not DATE(), which would include later orders on the cutoff day.

Only one SELECT is permitted. No WITH, UNION, INTERSECT, EXCEPT or UNNEST.
For a small set of requested months including zero-sale months, use the conditional-aggregation example:
anchor on products, LEFT JOIN a nested SELECT of qualifying order_items joined to orders, and use SUM(IF(..., sale_price, 0)).
Keep order-date, status and delivery-cutoff filters inside that nested SELECT so zero-sale brands remain.
Return one column per month and compute percentage changes in an outer nested SELECT with SAFE_DIVIDE.
A zero baseline must yield NULL (not an invented percentage). Count matched item IDs as items_sold
for the compared period so comments about transaction volume have evidence. Wide month columns are valid evidence for a chronological table.

{schema_doc}"""
    cutoff_rule = "Fixed analysis cutoff" if cutoff_resolved else "Current-time default cutoff"
    parts = [f"Earlier turns:\n{_history_block(history)}",
             f"Original request: {original_question or question}", f"Current action: {question}",
             f"{cutoff_rule}: {data_cutoff}"]
    if previous_results:
        summaries = []
        for i, r in enumerate(previous_results, 1):
            summaries.append(f"Query {i} ({len(r.get('rows') or [])} fetched rows, partial result: {bool(r.get('truncated'))}, cutoff: {r.get('data_cutoff', 'not recorded')}):\n```sql\n{r.get('sql', '')}\n```\n"
                             + rows_to_markdown(r.get("columns") or [], r.get("rows") or [], 12))
        parts.append("Results already gathered this turn:\n" + "\n\n".join(summaries))
    if follow_up:
        parts.append(f"Write the SQL for this next sub-question: {follow_up}")
    if previous_sql and sql_error:
        parts.append(f"Your previous SQL failed. Fix it.\n```sql\n{previous_sql}\n```\nError: {sql_error}")
    return [("system", system), ("user", "\n\n".join(parts))]


def assess_messages(question: str, results: list[dict[str, Any]], assumptions: str, queries_run: int,
                    query_limit: int, *, original_question: str = "") -> list[tuple[str, str]]:
    system = f"""{ROLE}
Decide whether the gathered results are enough to answer the question. Be practical: a direct question with a
reasonable result is enough. Ask for more only when the question has a part the results do not cover, such as
"why" or "compare" where a second query would add real evidence. If a result is empty, consider whether a filter
or date range was too narrow and propose one follow-up that checks it; if the empty result is plausible, say enough.
Evaluate only the current analysis action, not other actions in the original request. Use the original request to check applicable constraints, including its explicit cutoff. Do not silently change the requested period or metric. Tables may omit rows or be truncated; a partial table is
not evidence of a total over the full population. Request an aggregate query when that total is needed.
{queries_run} of at most {query_limit} queries have been used. If none remain, answer enough=true."""
    blocks = []
    for i, r in enumerate(results, 1):
        blocks.append(f"Query {i} ({len(r.get('rows') or [])} fetched rows, partial result: {bool(r.get('truncated'))}, cutoff: {r.get('data_cutoff', 'not recorded')}):\n```sql\n{r.get('sql', '')}\n```\n"
                      + rows_to_markdown(r.get("columns") or [], r.get("rows") or [], 15))
    user_msg = f"Original request: {original_question or question}\nCurrent action: {question}\nAssumptions so far: {assumptions}\n\n" + "\n\n".join(blocks)
    return [("system", system), ("user", user_msg)]


def compose_messages(question: str, history: list[dict[str, Any]], results: list[dict[str, Any]],
                     assumptions: str, settings: dict[str, Any], user: User, wants_report: bool,
                     *, original_question: str = "") -> list[tuple[str, str]]:
    style = "; ".join(f"{k}: {v}" for k, v in settings.items() if v) or "concise and direct"
    report_rules = ""
    if wants_report:
        report_rules = """
Format this answer as a report. Start with a title line beginning with '# ', then sections: Summary, Findings,
Action items (three to five, each one line, concrete), Assumptions and data notes."""
    system = f"""{ROLE}
Answer only the current analysis action for a non-technical executive from the query results below. Other actions in the original request are handled separately. Use only figures that appear in the
results or are simple sums, differences or percentages of them; never invent numbers. Amounts have no currency, so
write them as plain numbers without currency symbols. Show the inputs when calculating a derived figure.
Describe observed changes without inventing their causes. Sales or category counts alone do not establish
customer demand, broader interest, product launches, promotional success or seasonality. Label any proposed
cause as an untested hypothesis; frame action items as investigations, not established explanations. When
the results include small transaction or customer counts, show those counts alongside percentages and explain
the small base. Do not infer transaction counts from sales amounts or the number of aggregate result rows. State the metric definition, date window and exact analysis cutoff
with timezone from the executed-result metadata. Do not substitute today's date or claim the cutoff proves data freshness.
A requested monthly series can be presented as chronological rows from wide month columns. Use only explicit zero
values computed by SQL; NULL percentage changes mean undefined/N/A because the baseline is zero.
If a query returns no rows, say no matching rows were returned; do not invent zeros or assume it was an orders
query. Use the executed SQL and row metadata to describe evidence coverage accurately. A complete aggregate
result can cover its full defined population without showing individual transactions; do not call it partial
just because it is aggregated, or confuse aggregate row counts with transaction counts. A fully shown SQL
top-N result can answer the requested ranking without describing the whole population. If fetched rows are
hidden from this prompt or the query result is truncated,
identify that actual limitation; do not infer totals, rankings or absence beyond the query's evidence.
Identify customers only by id. Copy customer, product and order IDs exactly as provided in the results,
without adding digit-grouping separators, decimal places or rounding. Today is {_today()}.
{_scope_line(user)} Mention the brand scope when it matters.
Presentation preferences: {style}.{report_rules}"""
    blocks = []
    for i, r in enumerate(results, 1):
        rows = r.get("rows") or []
        coverage = {"analysis_cutoff": r.get("data_cutoff", "not recorded"), "fetched_rows": len(rows), "shown_rows": min(len(rows), 60),
                    "query_result_truncated": bool(r.get("truncated"))}
        blocks.append(f"Result {i}: {json.dumps(coverage)}\nExecuted SQL:\n```sql\n{r.get('sql', '')}\n```\n"
                      + rows_to_markdown(r.get("columns") or [], rows, 60))
    user_msg = (f"Earlier turns:\n{_history_block(history)}\n\nOriginal request: {original_question or question}\n\nCurrent action: {question}\n\n"
                f"Assumptions: {assumptions}\n\n" + "\n\n".join(blocks))
    return [("system", system), ("user", user_msg)]


def schema_messages(question: str, schema_doc: str, user: User) -> list[tuple[str, str]]:
    system = f"""{ROLE}
Answer the user's question about what data is available, in plain language for a non-technical executive.
Do not write or execute SQL. Explain the join keys when asked about relationships.
Personal fields exist in the source database but are excluded from the assistant's permitted schema and results.
Always include one short sentence explaining that personal source fields are blocked from the assistant.
Never claim those fields are absent, not stored, or removed from the source database.
{_scope_line(user)} Mention their scope. Keep it under 200 words unless they ask for detail.

{schema_doc}"""
    return [("system", system), ("user", question)]
