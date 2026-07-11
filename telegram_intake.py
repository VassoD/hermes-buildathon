"""Telegram intake: turn an incoming support message into a processed ticket.

Invoked by the Hermes 'support-ticket-triage' skill with the sender's
numeric Telegram id and message text. Creates the ticket and runs the full
existing pipeline (memory -> classify -> route -> specialist -> manager
review). Detailed steps are stored in the trace; Telegram only receives a
concise intake acknowledgement and the customer-facing resolution.

Usage:
  python3 telegram_intake.py --user-id 12345 --message "I was charged twice"
  python3 telegram_intake.py --user-id 12345 --message "..." --no-telegram

The final message links to the trace viewer. The base URL is read from
the TRACE_BASE_URL env var, falling back to trace_config.json, so it can
be updated right before a demo without touching code.
"""

import argparse
import json
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

from agency_integrations import (
    IntegrationError,
    Workspace,
    create_linear_escalation,
    get_workspace,
    investigate_ticket,
)
from db import get_connection
from dodo import extract_payment_id
from llm import LLMError
from manager import ClassificationError, log_run, process_ticket, ReviewError
from memory import build_history_summary
from run_review import review_and_finalize
from specialists import run_specialist, SpecialistError

PROJECT_DIR = Path(__file__).parent
HERMES_ENV = Path.home() / ".hermes" / ".env"
SUBJECT_MAX_LENGTH = 60


class IntakeError(Exception):
    """Raised when the intake pipeline cannot complete."""


def load_telegram_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        return token
    if HERMES_ENV.exists():
        for line in HERMES_ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise IntakeError("TELEGRAM_BOT_TOKEN not found in environment or ~/.hermes/.env")


def load_trace_base_url() -> str | None:
    url = os.environ.get("TRACE_BASE_URL")
    if url:
        return url.rstrip("/")
    config_file = PROJECT_DIR / "trace_config.json"
    if config_file.exists():
        try:
            return json.loads(config_file.read_text())["base_url"].rstrip("/")
        except (json.JSONDecodeError, KeyError):
            return None
    return None


def get_reachable_trace_url(
    ticket_id: int,
    *,
    opener=None,
    timeout: float = 3.0,
) -> str | None:
    """Return the ticket trace URL only when its page is currently reachable."""
    base_url = load_trace_base_url()
    if not base_url:
        return None
    url = f"{base_url}/ticket/{ticket_id}"
    request = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": "HermesSupportAgency/1.0"},
    )
    try:
        with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    return url if 200 <= status < 400 else None


class Narrator:
    """Sends progress messages to Telegram and stores the visible transcript."""

    def __init__(
        self,
        chat_id: str,
        dry_run: bool,
        conn: sqlite3.Connection | None = None,
        ticket_id: int | None = None,
        customer_email: str | None = None,
    ):
        self.chat_id = chat_id
        self.dry_run = dry_run
        self.token = None if dry_run else load_telegram_token()
        self.sent: list[str] = []
        self.conn = conn
        self.ticket_id = ticket_id
        self.customer_email = customer_email

    def attach(
        self, conn: sqlite3.Connection, ticket_id: int, customer_email: str
    ) -> None:
        self.conn = conn
        self.ticket_id = ticket_id
        self.customer_email = customer_email

    def _persist(self, text: str) -> None:
        if self.conn is None or self.ticket_id is None or self.customer_email is None:
            return
        self.conn.execute(
            """
            INSERT INTO support_messages
                (ticket_id, customer_email, channel, direction, kind, body)
            VALUES (?, ?, 'telegram', 'outbound', 'progress', ?)
            """,
            (self.ticket_id, self.customer_email, text),
        )
        self.conn.commit()

    def say(self, text: str) -> None:
        self.sent.append(text)
        if self.dry_run:
            print(f"[narrate] {text}")
            self._persist(text)
            return
        payload = json.dumps({"chat_id": self.chat_id, "text": text}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read())
        if not body.get("ok"):
            raise IntakeError(f"Telegram sendMessage failed: {body}")
        self._persist(text)


def make_subject(message: str) -> str:
    first_line = message.strip().splitlines()[0].strip()
    if len(first_line) <= SUBJECT_MAX_LENGTH:
        return first_line
    return first_line[: SUBJECT_MAX_LENGTH - 3].rstrip() + "..."


def insert_ticket(conn: sqlite3.Connection, customer_email: str, message: str) -> sqlite3.Row:
    cursor = conn.execute(
        """
        INSERT INTO tickets (customer_email, subject, body, payment_id)
        VALUES (?, ?, ?, ?)
        """,
        (customer_email, make_subject(message), message.strip(), extract_payment_id(message)),
    )
    conn.execute(
        """
        INSERT INTO support_messages
            (ticket_id, customer_email, channel, direction, kind, body)
        VALUES (?, ?, 'telegram', 'inbound', 'customer_message', ?)
        """,
        (cursor.lastrowid, customer_email, message.strip()),
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM tickets WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()


def run_bug_investigation(
    conn: sqlite3.Connection,
    ticket: sqlite3.Row,
    run_id: str,
    workspace: Workspace,
):
    investigation = investigate_ticket(conn, ticket, run_id, workspace)
    citations = ", ".join(
        f"{item.path}:{item.line}" for item in investigation.evidence
    ) or "none"
    log_run(
        conn,
        run_id,
        ticket["id"],
        step_type="bug_investigation",
        input_summary=f"product={workspace.product_url}; repo={workspace.github_repo or '-'}",
        output_summary=f"verdict={investigation.verdict}; evidence={citations}",
        reasoning=investigation.reasoning,
        cost_estimate=0.0,
        agent_name="BugInvestigator",
    )
    conn.commit()
    return investigation


def publish_bug_to_linear(
    conn: sqlite3.Connection,
    ticket: sqlite3.Row,
    run_id: str,
    investigation,
    workspace: Workspace,
):
    current = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket["id"],)).fetchone()
    if current["status"] != "escalated":
        return None
    issue = create_linear_escalation(
        conn,
        current,
        investigation,
        workspace,
        enforce_confidence_threshold=False,
    )
    note = current["resolution_notes"] or "Escalated to engineering."
    note += f"\n\nEngineering issue: {issue.identifier} — {issue.url}"
    conn.execute(
        "UPDATE tickets SET resolution_notes = ? WHERE id = ?", (note, ticket["id"])
    )
    log_run(
        conn,
        run_id,
        ticket["id"],
        step_type="linear_escalation",
        input_summary=f"bug_verdict={investigation.verdict}",
        output_summary=(
            f"linear_issue_id={issue.id}; identifier={issue.identifier}; url={issue.url}"
        ),
        reasoning="Manager-approved escalation published to Linear after duplicate search.",
        cost_estimate=0.0,
        agent_name="LinearAgent",
    )
    conn.commit()
    return issue


def handle_message(user_id: str, message: str, chat_id: str, dry_run: bool) -> dict:
    import uuid

    narrator = Narrator(chat_id, dry_run)
    customer_email = f"telegram_{user_id}@demo.local"
    conn = get_connection()
    steps_ran: list[str] = []
    try:
        ticket = insert_ticket(conn, customer_email, message)
        narrator.attach(conn, ticket["id"], customer_email)
        steps_ran.append("intake")
        narrator.say(f"📋 I've logged this as support ticket #{ticket['id']} and I'm reviewing it.")

        run_id = str(uuid.uuid4())
        history = build_history_summary(conn, ticket)
        log_run(
            conn,
            run_id,
            ticket["id"],
            step_type="memory_lookup",
            input_summary=f"customer_email={customer_email}",
            output_summary=history,
            reasoning=(
                "Deterministic lookup of this customer's finalized tickets "
                "(status resolved/escalated), no LLM call."
            ),
            cost_estimate=0.0,
        )
        conn.commit()
        steps_ran.append("memory_lookup")
        classification, routing = process_ticket(
            conn, ticket, run_id=run_id, customer_history=history
        )
        ticket = conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket["id"],)
        ).fetchone()
        steps_ran.extend(["classify", "route"])
        workspace = get_workspace(conn)
        investigation = None
        specialist_context = classification.reasoning
        if classification.category == "technical" and workspace is not None:
            investigation = run_bug_investigation(conn, ticket, run_id, workspace)
            specialist_context += "\n\n" + investigation.context_summary()
            steps_ran.append("bug_investigation")
        outcome = None
        for target in routing.targets:
            draft = run_specialist(
                target,
                ticket,
                manager_context=specialist_context,
                customer_history=history,
            )
            log_run(
                conn,
                run_id,
                ticket["id"],
                step_type="specialist_draft",
                input_summary=f"Ticket #{ticket['id']}: {ticket['subject']}",
                output_summary=json.dumps(
                    {
                        "proposed_action": draft.proposed_action,
                        "customer_response": draft.customer_response,
                    }
                ),
                reasoning=draft.reasoning,
                tokens_used=draft.tokens_used,
                cost_estimate=draft.cost_estimate,
                agent_name=draft.agent_name,
            )
            conn.commit()
            steps_ran.append("specialist_draft")
            outcome = review_and_finalize(
                conn, ticket, run_id, draft, customer_history=history
            )
            steps_ran.append("manager_review")
            if not outcome.approved_first_pass:
                steps_ran.append("revision_loop")

        if investigation is not None and workspace is not None and outcome is not None:
            try:
                issue = publish_bug_to_linear(
                    conn, ticket, run_id, investigation, workspace
                )
                if issue is not None:
                    steps_ran.append("linear_escalation")
            except IntegrationError as error:
                log_run(
                    conn,
                    run_id,
                    ticket["id"],
                    step_type="linear_escalation_skipped",
                    input_summary=f"bug_verdict={investigation.verdict}",
                    output_summary="no Linear issue created",
                    reasoning=str(error),
                    cost_estimate=0.0,
                    agent_name="LinearAgent",
                )
                conn.commit()
                steps_ran.append("linear_escalation_skipped")

        final = conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket["id"],)
        ).fetchone()
        final_text = final["resolution_notes"] or (
            "Your request is still under review. A support specialist will follow up."
        )
        trace_url = get_reachable_trace_url(final["id"])
        if trace_url:
            final_text += f"\n\nView support trace: {trace_url}"
        narrator.say(final_text)

        return {
            "ticket_id": final["id"],
            "status": final["status"],
            "action": final["resolution_action"],
            "resolution_notes": final["resolution_notes"],
            "steps_ran": steps_ran,
            "memory": history,
            "narrated_messages": narrator.sent,
        }
    except (LLMError, ClassificationError, ReviewError, SpecialistError) as error:
        narrator.say(
            "⚠️ Something went wrong while processing your ticket. "
            "A human will follow up."
        )
        raise IntakeError(f"Pipeline failed: {error}") from error
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram support intake")
    parser.add_argument("--user-id", required=True, help="sender's numeric Telegram id")
    parser.add_argument("--message", required=True, help="full message text")
    parser.add_argument(
        "--chat-id", default=None, help="chat to narrate into (defaults to user id)"
    )
    parser.add_argument(
        "--no-telegram",
        action="store_true",
        help="dry run: print narration to stdout instead of sending",
    )
    args = parser.parse_args()

    result = handle_message(
        user_id=args.user_id,
        message=args.message,
        chat_id=args.chat_id or args.user_id,
        dry_run=args.no_telegram,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
