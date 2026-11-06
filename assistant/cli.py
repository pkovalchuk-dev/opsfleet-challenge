from __future__ import annotations

import argparse
import sys

from prompt_toolkit import HTML, PromptSession
from rich.console import Console
from rich.markdown import Markdown
from rich.rule import Rule

from .config import load_config
from .runner import Runner

PROGRESS = {
    "plan_and_route": "Planning the request", "start_step": "Running the next action",
    "finish_plan": "Putting the results together", "draft_sql": "Drafting SQL",
    "repair_sql": "Repairing the query", "validate_and_execute_query": "Checking and running the query",
    "assess_results": "Checking the evidence", "compose_analysis": "Writing the answer",
    "read_context": "Describing the data", "find_reports": "Looking up your reports",
    "prepare_delete": "Preparing the deletion preview", "commit_delete": "Deleting",
    "output_check": "Checking the answer", "save_last": "Saving the report",
}


def ask(text: str, runner: Runner, console: Console) -> None:
    with console.status("Understanding the question", spinner="dots") as status:
        answer = runner.run_turn(text, on_progress=lambda node: status.update(PROGRESS[node]) if node in PROGRESS else None)
    console.print(Rule(f"Assistant · {runner.user.name} · {runner.user.scope_label}", align="left"))
    console.print(Markdown(answer))
    console.print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="assistant", description="Retail analysis assistant (CLI prototype)")
    parser.add_argument("--user", help="demo user id from config/users.yaml")
    parser.add_argument("--db", help="SQLite path (default data/assistant.db)")
    parser.add_argument("--ask", help="ask one question and exit")
    args, console = parser.parse_args(argv), Console()
    try:
        cfg = load_config()
        user_id = args.user
        if user_id is None:
            console.print("Who are you? " + ", ".join(cfg.users))
            user_id = console.input("> ").strip() or next(iter(cfg.users))
        runner = Runner(cfg, user_id, db_path=args.db)
    except Exception as exc:
        console.print(f"Setup failed: {type(exc).__name__}. Check the user, configuration and database path.")
        return 2
    try:
        if not cfg.api_key:
            console.print("GOOGLE_API_KEY is not set; copy .env.example to .env and add a Gemini key.")
        if args.ask:
            ask(args.ask, runner, console)
            return 1 if runner.last_state.get("outcome") == "failed" else 0
        console.print(f"Retail analysis assistant · {runner.user.name} · {runner.user.scope_label} · {runner.conversation_id}")
        console.print("Ask about sales, customers, products or saved reports. Paste the full request, then press Enter. Ctrl+C or Ctrl+D exits.\n")
        # Bracketed paste keeps paragraphs together until the user presses Enter.
        prompt = PromptSession(HTML("<b><ansicyan>User&gt;</ansicyan></b> ")) if sys.stdin.isatty() else None
        while True:
            text = (prompt.prompt() if prompt else console.input("[bold cyan]User>[/bold cyan] ")).strip()
            if not text:
                continue
            try:
                ask(text, runner, console)
            except Exception as exc:
                console.print(f"Could not process that message ({type(exc).__name__}). The chat is still open.")
    except (EOFError, KeyboardInterrupt):
        console.print()
        return 0
    finally:
        runner.close()


if __name__ == "__main__":
    sys.exit(main())
