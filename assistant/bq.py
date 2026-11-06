"""BigQuery execution with retries, full result fetching and a timeout."""

from __future__ import annotations

import datetime as dt
import decimal
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .llm import is_transient


class SqlError(Exception):
    """BigQuery rejected the SQL (syntax, unknown name, type error). Repairable by the model."""


class BigQueryUnavailable(Exception):
    """BigQuery could not be reached within the retry policy."""


@dataclass
class QueryResult:
    sql: str
    columns: list[str]
    rows: list[dict[str, Any]]
    job_id: str = ""
    bytes_processed: int = 0
    bytes_billed: int = 0
    truncated: bool = False
    duration_ms: int = 0
    purpose: str = ""

    def to_state(self) -> dict[str, Any]:
        return {
            "sql": self.sql, "columns": self.columns, "rows": self.rows, "job_id": self.job_id,
            "bytes_processed": self.bytes_processed, "bytes_billed": self.bytes_billed,
            "truncated": self.truncated, "duration_ms": self.duration_ms, "purpose": self.purpose,
        }


def _plain(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _sanitize_message(message: str) -> str:
    """Keep the useful part of a BigQuery error and drop project and job details."""
    text = message.replace("\n", " ")
    for marker in ("; reason:", " Location:", " jobReference", "Job ID"):
        if marker in text:
            text = text.split(marker, 1)[0]
    return text.strip()[:300]


class QueryExecutor:
    def __init__(self, project: str | None, *, location: str = "US", tracer: Any = None,
                 backoff: tuple[float, ...] = (2.0, 5.0), timeout: float = 30.0) -> None:
        from google.cloud import bigquery

        self._bigquery = bigquery
        self.client = bigquery.Client(project=project)
        self.location, self.tracer = location, tracer
        self.backoff, self.timeout = backoff, timeout

    def _remaining(self, deadline: float | None) -> float:
        remaining = self.timeout if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            raise BigQueryUnavailable("the turn deadline was reached")
        return min(self.timeout, remaining)

    def run(self, sql: str, parameters: list, *, deadline: float | None = None, purpose: str = "analysis") -> QueryResult:
        started = time.monotonic()
        # Bound a direct call too, even when no graph deadline was supplied.
        deadline = deadline if deadline is not None else started + 120.0
        job = self._submit_query(sql, parameters, deadline, purpose)
        # Single-request polling recalculates the time left for each RPC.
        # QueryJob.result's built-in polling reuses its original timeout.
        while not self._retry(lambda timeout: job.done(retry=None, timeout=timeout), deadline):
            time.sleep(min(0.25, self._remaining(deadline)))

        fields, raw = self._fetch_results(job, deadline)
        return QueryResult(
            sql=sql, columns=[f.name for f in fields],
            rows=[{k: _plain(v) for k, v in row.items()} for row in raw],
            job_id=job.job_id or "", bytes_processed=int(job.total_bytes_processed or 0),
            bytes_billed=int(job.total_bytes_billed or 0),
            duration_ms=round((time.monotonic() - started) * 1000), purpose=purpose,
        )

    def _submit_query(self, sql: str, parameters: list, deadline: float, purpose: str):
        from google.api_core import exceptions as gexc

        # Keep the job id across submission retries: a lost HTTP response must
        # never create a second billable query.
        job_id = f"assistant_{uuid.uuid4().hex}"

        def submit(timeout: float):
            try:
                return self.client.query(
                    sql, location=self.location, job_id=job_id, timeout=timeout, retry=None, job_retry=None,
                    job_config=self._bigquery.QueryJobConfig(
                        query_parameters=parameters,
                        job_timeout_ms=max(1, int((deadline - time.monotonic()) * 1000)),
                        labels={"app": "retail-assistant", "purpose": purpose}),
                )
            except gexc.Conflict:
                return self.client.get_job(job_id, location=self.location, retry=None,
                                           timeout=self._remaining(deadline))

        return self._retry(submit, deadline)

    def _fetch_results(self, job, deadline: float):
        fields, rows = [], []
        token, total_rows = None, None
        while True:
            page = self._retry(lambda timeout: self._fetch_page(job, token, timeout), deadline)
            if "schema" in page:
                fields = [self._bigquery.SchemaField.from_api_repr(f) for f in page["schema"]["fields"]]
            if "totalRows" in page:
                total_rows = int(page["totalRows"])
            iterator = self._bigquery.table.RowIterator(
                client=self.client, api_request=None, path="", schema=fields, first_page_response=page)
            rows.extend(dict(row) for row in next(iter(iterator.pages)))
            self._remaining(deadline)
            token = page.get("pageToken")
            if not token:
                break
        if total_rows is not None and len(rows) != total_rows:
            raise BigQueryUnavailable("query results were incomplete")
        return fields, rows

    def _fetch_page(self, job, page_token: str | None, timeout: float):
        from google.api_core import exceptions as gexc

        failure = job.exception(timeout=0)  # job.done() populated the completed future
        if failure is not None:
            raise failure
        # Fetch completed results without the SDK's longer default polling timeout.
        # Pagination reads the same job and does not run or bill the query again.
        # This is a page size, not a total row limit. _fetch_results follows every page.
        params = {"location": self.location, "maxResults": 200,
                  "formatOptions.useInt64Timestamp": True}
        if page_token:
            params["pageToken"] = page_token
        response = self.client._http.get(
            f"https://bigquery.googleapis.com/bigquery/v2/projects/{job.project}/queries/{job.job_id}",
            params=params, timeout=timeout, max_allowed_time=timeout,
        )
        if response.status_code >= 400:
            raise gexc.from_http_response(response)
        page = response.json()
        if not page.get("jobComplete"):
            raise gexc.ServiceUnavailable("completed query results are not available yet")
        return page

    def _retry(self, fn, deadline: float | None):
        from google.api_core import exceptions as gexc

        last: Exception | None = None
        for attempt in range(len(self.backoff) + 1):
            timeout = self._remaining(deadline)
            try:
                return fn(timeout)
            except gexc.BadRequest as exc:
                raise SqlError(_sanitize_message(getattr(exc, "message", str(exc)))) from exc
            except Exception as exc:
                last = exc
                if not (isinstance(exc, TimeoutError) or is_transient(exc)) or attempt == len(self.backoff):
                    break
                wait = self.backoff[attempt]
                if deadline is not None and time.monotonic() + wait >= deadline:
                    break
                if self.tracer is not None:
                    self.tracer.event("bigquery_retry", attempt=attempt + 1, wait_s=wait)
                time.sleep(wait)
        raise BigQueryUnavailable(f"BigQuery unavailable: {type(last).__name__}: {str(last)[:160]}") from last
