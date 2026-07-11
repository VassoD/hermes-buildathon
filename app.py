"""Web dashboard and product workspace setup for the support crew.

Run with: python app.py  (serves on http://127.0.0.1:5050)
Templates and styling live in templates/ and static/. All data comes from
tickets.db via the shared query logic in trace_viewer.py.
"""

import os
import re
import secrets
from collections import defaultdict

from flask import Flask, render_template, abort, request, session

from agency_integrations import (
    IntegrationError,
    Workspace,
    get_workspace,
    save_workspace,
)
from credentials import (
    CredentialStoreError,
    has_provider_secret,
    save_provider_secret,
)
from db import get_connection
from trace_viewer import batch_summary, ticket_trace

app = Flask(__name__)
app.secret_key = os.urandom(32)


def credentials_are_editable() -> bool:
    from urllib.parse import urlsplit

    hostname = (urlsplit(f"//{request.host}").hostname or "").lower()
    return hostname in {"localhost", "127.0.0.1", "::1"}


def get_all_tickets() -> list[dict]:
    conn = get_connection()
    try:
        summary = batch_summary(conn)
    finally:
        conn.close()
    return [
        {
            **dict(row["ticket"]),
            "total_cost": row["cost"],
            "had_memory_hit": row["memory_hit"] == "yes",
        }
        for row in summary["rows"]
    ]


def ticket_change_token() -> str:
    """Cheap fingerprint of the tickets table so the dashboard can detect new tickets."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS ticket_count, COALESCE(MAX(id), 0) AS max_id FROM tickets"
        ).fetchone()
    finally:
        conn.close()
    return f"{row['ticket_count']}:{row['max_id']}"


def get_stats(tickets: list[dict]) -> dict:
    return {
        "total": len(tickets),
        "resolved": sum(1 for t in tickets if t["status"] == "resolved"),
        "escalated": sum(1 for t in tickets if t["status"] == "escalated"),
        "in_review": sum(1 for t in tickets if t["status"] == "in_review"),
        "total_cost": sum(t["total_cost"] for t in tickets),
    }


TOPIC_RULES = (
    ("article generation", ("generation", "generate", "article")),
    ("sign-up and login", ("sign up", "signup", "log in", "login", "password")),
    ("export", ("export", "download", "pdf", "csv")),
    ("integrations", ("integration", "slack", "webhook", "sync")),
    ("billing", ("billing", "invoice", "charged", "payment", "plan")),
    ("refunds", ("refund", "money back")),
)


def _ticket_text(ticket: dict) -> str:
    return " ".join(
        str(ticket.get(field) or "")
        for field in ("subject", "body", "resolution_notes")
    ).lower()


def _ticket_topic(ticket: dict) -> str:
    text = _ticket_text(ticket)
    for topic, keywords in TOPIC_RULES:
        if any(keyword in text for keyword in keywords):
            return topic
    return "product workflow"


def _action_card(
    *, signal: str, priority: str, topic: str, tickets: list[dict], recommendation: str
) -> dict:
    latest = max(tickets, key=lambda item: (item.get("created_at", ""), item["id"]))
    customers = {ticket["customer_email"] for ticket in tickets}
    title_stems = {
        "Recurring bug": "Investigate recurring",
        "Potential bug": "Validate possible",
        "UX friction": "Remove friction from",
        "Product gap": "Evaluate missing capability in",
        "Account risk": "Recover at-risk customer around",
    }
    evidence = re.sub(r"\s+", " ", str(latest.get("body") or latest["subject"])).strip()
    if len(evidence) > 180:
        evidence = evidence[:177].rstrip() + "…"
    return {
        "signal": signal,
        "priority": priority,
        "title": f"{title_stems[signal]} {topic}",
        "recommendation": recommendation,
        "ticket_count": len(tickets),
        "customer_count": len(customers),
        "confidence": "High" if len(tickets) >= 2 else "Medium",
        "evidence": evidence,
        "customer_email": next(iter(customers)) if len(customers) == 1 else None,
    }


def propose_csm_actions(tickets: list[dict]) -> list[dict]:
    """Turn ticket-level agent analysis into a small, prioritized CSM action queue."""
    bug_markers = (
        "error", "failed", "broken", "doesn't", "does not", "stuck", "crash",
        "blank", "wrong status", "not working", "synchronization", "bug",
    )
    ux_markers = (
        "can't find", "cannot find", "confusing", "don't understand",
        "do not understand", "misleading", "contradictory", "how do i",
    )
    gap_markers = (
        "can't attach", "cannot attach", "missing", "no option", "wish",
        "feature request", "doesn't include", "does not include",
    )
    by_bug_topic: dict[str, list[dict]] = defaultdict(list)
    by_ux_topic: dict[str, list[dict]] = defaultdict(list)
    by_gap_topic: dict[str, list[dict]] = defaultdict(list)
    by_customer: dict[str, list[dict]] = defaultdict(list)

    for ticket in tickets:
        text = _ticket_text(ticket)
        topic = _ticket_topic(ticket)
        by_customer[ticket["customer_email"]].append(ticket)
        if ticket.get("category") == "technical" and (
            ticket.get("status") == "escalated" or any(marker in text for marker in bug_markers)
        ):
            by_bug_topic[topic].append(ticket)
        if any(marker in text for marker in ux_markers):
            by_ux_topic[topic].append(ticket)
        if any(marker in text for marker in gap_markers):
            by_gap_topic[topic].append(ticket)

    actions: list[dict] = []
    for topic, evidence in by_bug_topic.items():
        recurring = len(evidence) >= 2
        actions.append(_action_card(
            signal="Recurring bug" if recurring else "Potential bug",
            priority="high" if recurring else "medium",
            topic=topic,
            tickets=evidence,
            recommendation=(
                "Consolidate the evidence, confirm one engineering owner, and update affected customers."
                if recurring else
                "Validate the report with product or engineering and keep the customer informed."
            ),
        ))
    for topic, evidence in by_ux_topic.items():
        actions.append(_action_card(
            signal="UX friction",
            priority="medium",
            topic=topic,
            tickets=evidence,
            recommendation="Review the journey and update the UI or guidance where customers get lost.",
        ))
    for topic, evidence in by_gap_topic.items():
        actions.append(_action_card(
            signal="Product gap",
            priority="medium",
            topic=topic,
            tickets=evidence,
            recommendation="Validate demand with product and tell customers whether this belongs on the roadmap.",
        ))
    for email, evidence in by_customer.items():
        unresolved = [
            ticket for ticket in evidence
            if ticket.get("status") in {"open", "in_review", "escalated"}
        ]
        if len(unresolved) >= 3:
            actions.append(_action_card(
                signal="Account risk",
                priority="high",
                topic=_ticket_topic(unresolved[-1]),
                tickets=unresolved,
                recommendation=f"Assign one CSM owner and send {email} a single outcome-focused update.",
            ))

    priority_order = {"high": 0, "medium": 1, "low": 2}
    return sorted(
        actions,
        key=lambda action: (
            priority_order[action["priority"]], -action["ticket_count"], action["title"]
        ),
    )[:4]


def group_tickets_by_customer(tickets: list[dict]) -> list[dict]:
    """Collapse ticket rows into an Intercom-style customer inbox."""
    grouped: dict[str, dict] = {}
    ordered = sorted(
        tickets, key=lambda ticket: (ticket["created_at"], ticket["id"]), reverse=True
    )
    for ticket in ordered:
        email = ticket["customer_email"]
        if email not in grouped:
            grouped[email] = {
                "customer_email": email,
                "latest_ticket_id": ticket["id"],
                "latest_subject": ticket["subject"],
                "latest_status": ticket["status"],
                "latest_category": ticket.get("category"),
                "last_seen": ticket["created_at"],
                "ticket_count": 0,
                "total_cost": 0.0,
                "has_memory": False,
            }
        conversation = grouped[email]
        conversation["ticket_count"] += 1
        conversation["total_cost"] += ticket.get("total_cost") or 0
        conversation["has_memory"] = (
            conversation["has_memory"] or ticket.get("had_memory_hit", False)
        )
    return list(grouped.values())


def get_customer_tickets(email: str) -> list[dict]:
    tickets = [t for t in get_all_tickets() if t["customer_email"] == email]
    return sorted(tickets, key=lambda t: (t["created_at"], t["id"]))


def get_customer_messages(email: str) -> list[dict]:
    """Return the customer's chat transcript, including legacy ticket data."""
    conn = get_connection()
    try:
        tickets = conn.execute(
            "SELECT * FROM tickets WHERE customer_email = ? ORDER BY created_at, id",
            (email,),
        ).fetchall()
        transcript: list[dict] = []
        for ticket in tickets:
            stored = conn.execute(
                """
                SELECT direction, kind, body, created_at, ticket_id
                FROM support_messages
                WHERE ticket_id = ?
                ORDER BY created_at, id
                """,
                (ticket["id"],),
            ).fetchall()
            if stored:
                transcript.extend(dict(message) for message in stored)
                continue

            transcript.append(
                {
                    "direction": "inbound",
                    "kind": "customer_message",
                    "body": ticket["body"],
                    "created_at": ticket["created_at"],
                    "ticket_id": ticket["id"],
                }
            )
            if ticket["resolution_notes"]:
                transcript.append(
                    {
                        "direction": "outbound",
                        "kind": "resolution",
                        "body": ticket["resolution_notes"],
                        "created_at": ticket["resolved_at"] or ticket["created_at"],
                        "ticket_id": ticket["id"],
                    }
                )
        return transcript
    finally:
        conn.close()


def get_ticket(ticket_id: int) -> dict | None:
    conn = get_connection()
    try:
        trace = ticket_trace(conn, ticket_id)
    finally:
        conn.close()
    return dict(trace["ticket"]) if trace else None


def get_steps(ticket_id: int) -> list[dict]:
    conn = get_connection()
    try:
        trace = ticket_trace(conn, ticket_id)
    finally:
        conn.close()
    if trace is None:
        return []

    def summary_text(step: dict) -> str:
        if step["verdict"] == "APPROVE":
            return "Draft approved."
        if step["verdict"] == "REVISE":
            return "Sent back to the specialist for revision."
        return " · ".join(step["output_lines"])

    return [
        {
            "step_type": step["step_type"],
            "agent_name": step["agent_name"],
            "input_summary": step["input_summary"],
            "output_summary": summary_text(step),
            "reasoning": step["reasoning"],
            "tokens_used": step["tokens_used"],
            "cost_estimate": step["cost_estimate"],
            "verdict": step["verdict"],
        }
        for step in trace["steps"]
    ]


@app.route("/")
def index():
    tickets = get_all_tickets()
    return render_template(
        "index.html",
        actions=propose_csm_actions(tickets),
        conversations=group_tickets_by_customer(tickets),
        tickets=tickets,
        stats=get_stats(tickets),
        ticket_version=ticket_change_token(),
    )


@app.route("/api/ticket-version")
def ticket_version():
    return {"version": ticket_change_token()}


@app.route("/customer/<email>")
def customer_detail(email: str):
    tickets = get_customer_tickets(email)
    if not tickets:
        abort(404)
    return render_template(
        "customer.html",
        email=email,
        tickets=tickets,
        messages=get_customer_messages(email),
        total_cost=sum(t["total_cost"] for t in tickets),
    )


@app.route("/ticket/<int:ticket_id>")
def ticket_detail(ticket_id: int):
    ticket = get_ticket(ticket_id)
    if not ticket:
        abort(404)
    steps = get_steps(ticket_id)
    total_cost = sum(s["cost_estimate"] or 0 for s in steps)
    total_tokens = sum(s["tokens_used"] or 0 for s in steps)
    return render_template(
        "ticket.html",
        ticket=ticket,
        steps=steps,
        total_cost=total_cost,
        total_tokens=total_tokens,
    )


@app.route("/workspace", methods=["GET", "POST"])
def workspace_setup():
    conn = get_connection()
    message = None
    error = None
    csrf_token = session.get("workspace_csrf_token")
    if csrf_token is None:
        csrf_token = secrets.token_urlsafe(32)
        session["workspace_csrf_token"] = csrf_token
    try:
        workspace = get_workspace(conn)
        if request.method == "POST":
            submitted = Workspace(
                id=workspace.id if workspace else None,
                name=request.form.get("name", ""),
                product_url=request.form.get("product_url", ""),
                github_repo=request.form.get("github_repo", ""),
                github_branch=request.form.get("github_branch", "main"),
                linear_team_id=request.form.get("linear_team_id", ""),
                linear_project_id=request.form.get("linear_project_id", ""),
                bug_confidence_threshold=request.form.get(
                    "bug_confidence_threshold", "likely"
                ),
            )
            try:
                workspace = save_workspace(conn, submitted)
                message = "Workspace saved. New technical tickets will use this context."
                github_token = request.form.get("github_token", "").strip()
                linear_api_key = request.form.get("linear_api_key", "").strip()
                if github_token or linear_api_key:
                    submitted_token = request.form.get("csrf_token", "")
                    if not secrets.compare_digest(submitted_token, csrf_token):
                        error = "The workspace security token is invalid. Reload and try again."
                    elif not credentials_are_editable():
                        error = "Credentials can only be changed from localhost."
                    else:
                        if github_token:
                            save_provider_secret("github", github_token)
                        if linear_api_key:
                            save_provider_secret("linear", linear_api_key)
                        message = "Workspace and provider credentials saved."
            except CredentialStoreError as exc:
                error = str(exc)
            except IntegrationError as exc:
                workspace = submitted
                error = str(exc)
        return render_template(
            "workspace.html",
            workspace=workspace,
            message=message,
            error=error,
            github_configured=has_provider_secret("github"),
            linear_configured=has_provider_secret("linear"),
            credentials_editable=credentials_are_editable(),
            csrf_token=csrf_token,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    app.run(port=5050)
