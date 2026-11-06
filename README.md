# Retail Analysis Assistant

A Python CLI assistant built with LangGraph, Gemini and BigQuery for brand scoped retail analysis and private saved reports, with deletion confirmation and LangSmith tracing.

I spent about 12 hours on this exercise. I used a combination of Codex, Cursor and Claude Code to write the code and help draft and organize the technical report.

## Implemented prototype scope

- **Analysis:** Natural language BigQuery queries, follow-up and multi step requests, customer spending, product performance, time-based metrics, schema questions, and reports with findings and action items.
- **Safety and PII:** Read-only SQL validation, blocked personal fields and output checks. Two predefined managers have different brand permissions, including one with multiple brands. The CEO can analyze all brands.
- **Deletion confirmation:** Private reports stored in SQLite. Listing and searching return all matching reports. “Delete all” previews every matching report and requires confirmation of the exact IDs. Reports outside current brand access have neutral labels and cannot be searched by their hidden content, but owners can still delete them.
- **Resilience:** Bounded SQL repair, follow up checks for empty results, a fallback model, query and time limits and clear error responses. Queries have no automatic SQL row limit. The backend fetches and parses results in pages of up to 200 rows, looping until all rows are collected. There is no total row cap. Model prompts use smaller, clearly marked samples.
- **Observability:** LangSmith traces capture step latency, failures, model fallback and query usage. Private prompts, answers and result rows are omitted from exported traces.

The prototype implements requirements 2, 3, 5 and 7. 

## Run

Requires Python 3.13, the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install), a Google Cloud project with BigQuery enabled, and a [Gemini API key](https://aistudio.google.com/).

From the project folder:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp -n .env.example .env
```

Set `GOOGLE_API_KEY` and `GOOGLE_CLOUD_PROJECT` in `.env`. To enable tracing, follow the LangSmith setup below.

Replace `your-project-id` below with the same project ID, then authenticate and start the chat:

```sh
gcloud auth application-default login --project=your-project-id
gcloud auth application-default set-quota-project your-project-id
python -m assistant --user manager_a
```

Type a request and press Enter. Press Ctrl+C or Ctrl+D to exit.

### LangSmith setup

The LangSmith dependency is included in `requirements.txt`.

1. Sign in to [LangSmith](https://smith.langchain.com) and [create an API key](https://docs.langchain.com/langsmith/create-account-api-key) under **Settings → API Keys**. Put the key in `.env`.
2. Configure tracing and its destination as shown below.
3. Restart the CLI, ask a question and check for a `retail_assistant_turn` trace in LangSmith. Seeing the trace confirms delivery.

For a workspace with **Tracing Projects**, use:

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=your-langsmith-api-key
LANGSMITH_PROJECT=opsfleet-challenge
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
```

For a workspace with **Agents**, create an agent through **+ Agent → Observability**, then replace `LANGSMITH_PROJECT` with:

```dotenv
LANGSMITH_AGENT_ID=your-agent-id
LANGSMITH_AGENT_ENVIRONMENT=development
```

Copy the registered Agent ID. Remove `LANGSMITH_PROJECT`, `LANGCHAIN_PROJECT` and `LANGCHAIN_SESSION` from `.env` and your shell when using this option. Open the agent's **Tracing → development**, or the `opsfleet-challenge` project for project workspaces. See the [LangSmith tracing guide](https://docs.langchain.com/langsmith/observability-quickstart).

For an EU workspace, use `LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com` and the [EU dashboard](https://eu.smith.langchain.com). If the key covers multiple workspaces or the whole organization, also set `LANGSMITH_WORKSPACE_ID` to the ID under **Settings → General**.

Traces show graph steps, model calls, timings and outcomes. Inputs and outputs are intentionally empty to keep private content out of LangSmith. If no trace appears, check the key, region and destination, then restart the CLI. Set `LANGSMITH_TRACING=false` to run without tracing.

## Demo users and brands

The CLI selects a predefined executive using `--user`, which simulates sign-in. A manager can analyze only their assigned product brands. The CEO can analyze every brand.

| User | Role | Brands |
| --- | --- | --- |
| `manager_a` | Manager | Seven7, Calvin Klein |
| `manager_b` | Manager | Carhartt |
| `ceo` | CEO | All brands |

For example, run `python -m assistant --user ceo` to use the CEO. Assignments are defined in [config/users.yaml](config/users.yaml), using exact names from the dataset's `products.brand`. The dataset's `users` table contains shoppers, so executive access is configured separately. Saved reports stay private to their author, including for the CEO.

## Project layout

| Folder | Contents |
| --- | --- |
| [assistant/](assistant/) | CLI, conversation handling, model and database clients, safety checks, report storage and tracing. |
| [assistant/graph/](assistant/graph/) | LangGraph workflows for planning, analysis and report management, including deletion confirmation. |
| [config/](config/) | Demo users, brand permissions, presentation settings and database guidance for the model. |
| [docs/](docs/) | Production architecture and technical explanation. |
| [docs/diagrams/](docs/diagrams/) | Architecture and agent workflow figures. |
| `data/` | Local SQLite conversations and reports, created when the prototype runs. |

The project root contains the README, dependency lists and environment configuration template.

## Acceptance criteria

Use a separate test database. Start a fresh chat for each case with the user shown below:

```sh
ACCEPTANCE_DB="$(mktemp -d)/reports.db"
python -m assistant --user manager_a --db "$ACCEPTANCE_DB"
```

Change `--user` for cases 2 and 4. Completed sales means `SUM(sale_price)` for `Complete` items delivered by the cutoff, using order creation dates for the period. Check figures against a separately written BigQuery query. The public dataset changes, so expected totals are not hardcoded.

### 1. Customer behavior — `manager_a`

> Show my top 5 customers by completed spend in September 2026, with their shopper IDs, total spend and distinct order counts. Use an analysis cutoff of 2026-10-01 00:00:00 UTC. State the metric, period and brand scope.

**Expected:** Up to five shoppers ranked by completed spend, with distinct order counts and IDs only. Only Seven7 and Calvin Klein purchases contribute. SQL and answer use the requested cutoff. Amounts and counts match an independent query.

### 2. Product performance — `manager_b`

> Compare the two best-selling Carhartt products in September 2026 by completed sales value. Show product IDs and names, sales value, units sold and average selling price. Explain the measured differences and distinguish them from possible causes that the data cannot prove. Use an analysis cutoff of 2026-10-01 00:00:00 UTC.

**Expected:** The correct top two Carhartt products, sales, units and average selling prices. Explain the observed price/volume difference. Treat promotions, demand and seasonality as hypotheses, not proven causes.

### 3. Monthly metrics — `manager_a`

> Show monthly completed sales value for each of my brands for July, August and September 2026, including months with no sales. Calculate each brand's percentage change from August to September. Use an analysis cutoff of 2026-10-01 00:00:00 UTC.

**Expected:** Three months for each permitted brand, in date order, with zero where a month has no qualifying sales. August-to-September changes use 100 × (September − August) / August. A zero baseline is undefined, not an invented percentage. Respect the requested cutoff.

### 4. Current product sales — `ceo`

> Show the top 5 products across all brands by completed sales value for 2026 year to date. Include product ID, product name, brand, sales value and units sold. Use the current time as the analysis cutoff and state it. Do not count deliveries after the cutoff.

**Expected:** Five products ranked across all brands, with matching sales and unit counts. Include only orders placed this year through the cutoff and completed deliveries by that time. State the cutoff timestamp and do not invent a currency.

### 5. Database understanding — `manager_a`

> What tables and joins can you use, which brands can I analyze, and what business questions can you answer? Explain whether the users table contains shoppers or the executives using this assistant. Do not run an analysis query.

**Expected:** Describe products, order_items, orders and shopper users, their joins, supported questions and the two permitted brands. Explain that personal source fields are blocked from the assistant. Execute no BigQuery query.

### 6. Multi-step report workflow — `manager_a`

> Analyze Seven7 completed sales in September 2026 using an analysis cutoff of 2026-10-01 00:00:00 UTC. Write a short report with findings and action items, save it, list the reports created in this conversation, show the report you just saved, then delete only that report after asking for my confirmation.

**Expected:** Analyze → save → list this conversation → show → preview deletion. Save the question, SQL and answer privately. Preserve the analysis cutoff when showing the report. Delete nothing before confirmation. Only the exact previewed IDs may be deleted.

For case 6, reply `no` first and check that the report remains. Ask to delete that exact report ID again, then reply `yes`. The report must disappear, while other reports, including any saved after the preview, remain.
