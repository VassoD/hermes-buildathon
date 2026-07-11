"""Trace viewer: inspect a ticket's full agent run, or the whole batch.

Usage:
  python3 trace_viewer.py --ticket 13   # full step-by-step trace
  python3 trace_viewer.py --summary     # one line per ticket
"""

import argparse
import json
import sqlite3
import textwrap

from db import get_connection
from memory import NO_HISTORY

WIDTH = 78
SNIPPET = 90
DRAFT_SNIPPET = 220

STEP_LABELS = {
    "memory_lookup": "MEMORY LOOKUP",
    "classify": "CLASSIFY",
    "route": "ROUTE",
    "specialist_draft": "SPECIALIST DRAFT",
    "manager_review": "MANAGER REVIEW",
    "specialist_revision": "SPECIALIST REVISION",
    "manager_review_2": "MANAGER REVIEW (2nd PASS)",
    "action_execute": "EXECUTE ACTION",
}


def shorten(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def wrap(text: str, indent: str = "     ") -> str:
    paragraphs = (text or "").split("\n")
    wrapped = []
    for paragraph in paragraphs:
        wrapped.append(
            textwrap.fill(
                paragraph, width=WIDTH, initial_indent=indent, subsequent_indent=indent
            )
            if paragraph.strip()
            else ""
        )
    return "\n".join(wrapped)


def format_output_summary(step_type: str, output_summary: str) -> list[str]:
    """Render output_summary; unpack draft JSON instead of dumping it raw."""
    if step_type in ("specialist_draft", "specialist_revision"):
        try:
            payload = json.loads(output_summary)
            return [
                f"proposed_action: {payload['proposed_action']}",
                f"draft: {shorten(payload['customer_response'], DRAFT_SNIPPET)}",
            ]
        except (json.JSONDecodeError, KeyError):
            pass
    if step_type == "memory_lookup":
        # History summaries are multi-line and central to the demo, keep whole.
        return output_summary.split("\n")
    return [shorten(output_summary, SNIPPET)]


def format_cost(cost) -> str:
    return f"${cost:.4f}" if cost is not None else "n/a"


def ticket_trace(conn: sqlite3.Connection, ticket_id: int) -> dict | None:
    """Structured trace for one ticket, shared by the CLI and web views."""
    ticket = conn.execute(
        "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
    ).fetchone()
    if ticket is None:
        return None
    step_rows = conn.execute(
        "SELECT * FROM runs WHERE ticket_id = ? ORDER BY id", (ticket_id,)
    ).fetchall()
    steps = []
    total_tokens = 0
    total_cost = 0.0
    for number, step in enumerate(step_rows, start=1):
        total_tokens += step["tokens_used"] or 0
        total_cost += step["cost_estimate"] or 0.0
        verdict = None
        if step["step_type"] in ("manager_review", "manager_review_2") and step[
            "output_summary"
        ].startswith("verdict="):
            verdict = step["output_summary"].split("=", 1)[1]
        steps.append(
            {
                "number": number,
                "step_type": step["step_type"],
                "label": STEP_LABELS.get(step["step_type"], step["step_type"].upper()),
                "agent_name": step["agent_name"],
                "input_summary": shorten(step["input_summary"], SNIPPET),
                "output_lines": format_output_summary(
                    step["step_type"], step["output_summary"]
                ),
                "verdict": verdict,
                "reasoning": step["reasoning"] or "",
                "tokens_used": step["tokens_used"] or 0,
                "cost_estimate": step["cost_estimate"],
                "timestamp": step["timestamp"],
            }
        )
    return {
        "ticket": ticket,
        "steps": steps,
        "total_tokens": total_tokens,
        "total_cost": total_cost,
    }


def batch_summary(conn: sqlite3.Connection) -> dict:
    """One summary row per ticket, shared by the CLI and web views."""
    tickets = conn.execute("SELECT * FROM tickets ORDER BY id").fetchall()
    rows = []
    batch_cost = 0.0
    for ticket in tickets:
        steps = conn.execute(
            "SELECT step_type, output_summary, cost_estimate FROM runs "
            "WHERE ticket_id = ? ORDER BY id",
            (ticket["id"],),
        ).fetchall()
        step_types = [s["step_type"] for s in steps]

        if "manager_review" not in step_types:
            first_pass = "-"
        elif "manager_review_2" in step_types:
            first_pass = "no"
        else:
            first_pass = "yes"

        memory_hit = "-"
        for s in steps:
            if s["step_type"] == "memory_lookup":
                memory_hit = "no" if s["output_summary"] == NO_HISTORY else "yes"
                break

        cost = sum(s["cost_estimate"] or 0.0 for s in steps)
        batch_cost += cost
        rows.append(
            {
                "ticket": ticket,
                "first_pass": first_pass,
                "memory_hit": memory_hit,
                "cost": cost,
            }
        )
    return {"rows": rows, "batch_cost": batch_cost}


def print_trace(conn: sqlite3.Connection, ticket_id: int) -> None:
    trace = ticket_trace(conn, ticket_id)
    if trace is None:
        print(f"No ticket with id {ticket_id}.")
        return
    if not trace["steps"]:
        print(f"Ticket {ticket_id} has no run history yet.")
        return
    ticket = trace["ticket"]

    print("=" * WIDTH)
    print(f" TICKET #{ticket['id']}  {shorten(ticket['subject'], 60)}")
    print(f" from {ticket['customer_email']}")
    print(
        f" final: status={ticket['status']}"
        f"  action={ticket['resolution_action'] or '-'}"
        f"  category={ticket['category'] or '-'}"
    )
    print("=" * WIDTH)

    for step in trace["steps"]:
        print(f"\n [{step['number']}] {step['label']}  ({step['agent_name']})")
        print(" " + "-" * (WIDTH - 1))
        print(f"   in:  {step['input_summary']}")
        print(f"   out: {step['output_lines'][0]}")
        for line in step["output_lines"][1:]:
            print(f"        {line}")
        print("   why:")
        print(wrap(step["reasoning"]))
        print(
            f"   tokens: {step['tokens_used']}"
            f"   cost: {format_cost(step['cost_estimate'])}"
            f"   at: {step['timestamp']}"
        )

    print("\n" + "=" * WIDTH)
    print(
        f" TOTAL: {len(trace['steps'])} steps   {trace['total_tokens']} tokens   "
        f"${trace['total_cost']:.4f} estimated cost"
    )
    print("=" * WIDTH)


def print_summary(conn: sqlite3.Connection) -> None:
    summary = batch_summary(conn)
    print(
        f"{'ticket':>6} | {'final_status':<12} | {'final_action':<17} | "
        f"{'first_pass':<10} | {'memory_hit':<10} | total_cost"
    )
    print("-" * 78)
    for row in summary["rows"]:
        ticket = row["ticket"]
        print(
            f"{ticket['id']:>6} | {ticket['status']:<12} | "
            f"{ticket['resolution_action'] or '-':<17} | {row['first_pass']:<10} | "
            f"{row['memory_hit']:<10} | ${row['cost']:.4f}"
        )
    print("-" * 78)
    print(f"{'batch total':>60} : ${summary['batch_cost']:.4f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ticket trace viewer")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--ticket", type=int, help="show the full trace for one ticket")
    group.add_argument("--summary", action="store_true", help="one-line summary per ticket")
    args = parser.parse_args()

    conn = get_connection()
    try:
        if args.ticket is not None:
            print_trace(conn, args.ticket)
        else:
            print_summary(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
