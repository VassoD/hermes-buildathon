"""Build and sync the support-agency cluster, macro, and rating layer to Convex.

SQLite remains the execution ledger. Convex is the realtime, partner-facing
store for the support playbook produced from finalized ticket runs.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import defaultdict
from typing import Any


SEVERITY_BY_ACTION = {
    "issue_refund": "high",
    "escalate": "high",
    "deny_refund": "medium",
    "request_more_info": "medium",
    "reply_only": "low",
}

ACTION_LABELS = {
    "issue_refund": "approved refunds",
    "deny_refund": "refund denials",
    "escalate": "human escalations",
    "request_more_info": "missing-information requests",
    "reply_only": "resolved questions",
}


class ConvexConfigurationError(RuntimeError):
    """Raised when a Convex operation is requested without a deployment URL."""


def _latest_draft(conn: sqlite3.Connection, ticket_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT output_summary
        FROM runs
        WHERE ticket_id = ?
          AND step_type IN ('specialist_draft', 'specialist_revision')
        ORDER BY id DESC
        LIMIT 1
        """,
        (ticket_id,),
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["output_summary"])
    except (json.JSONDecodeError, TypeError):
        return None


def _qa_score(conn: sqlite3.Connection, ticket_id: int) -> int:
    steps = {
        row["step_type"]
        for row in conn.execute(
            "SELECT step_type FROM runs WHERE ticket_id = ?", (ticket_id,)
        ).fetchall()
    }
    if "manager_review_2" in steps:
        return 90
    if "manager_review" in steps:
        return 100
    return 75


def build_support_snapshot(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """Derive ranked clusters and reusable macros from reviewed real tickets."""
    tickets = conn.execute(
        """
        SELECT * FROM tickets
        WHERE status IN ('in_review', 'resolved', 'escalated')
          AND category IS NOT NULL
          AND resolution_action IS NOT NULL
        ORDER BY id
        """
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for ticket in tickets:
        grouped[f"{ticket['category']}:{ticket['resolution_action']}"] .append(ticket)

    now = int(time.time() * 1000)
    clusters: list[dict[str, Any]] = []
    macros: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    ordered_groups = sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))
    for rank, (stable_key, members) in enumerate(ordered_groups, start=1):
        exemplar = members[-1]
        category = exemplar["category"]
        action = exemplar["resolution_action"]
        label = ACTION_LABELS.get(action, action.replace("_", " "))
        subjects = "; ".join(ticket["subject"] for ticket in members[:3])
        clusters.append(
            {
                "stableKey": stable_key,
                "name": f"{category.title()} · {label.title()}",
                "description": f"Common signals: {subjects}",
                "volume": len(members),
                "severity": SEVERITY_BY_ACTION.get(action, "medium"),
                "ticketIds": [ticket["id"] for ticket in members],
                "rank": rank,
                "updatedAt": now,
            }
        )

        draft = _latest_draft(conn, exemplar["id"])
        response = (
            (draft or {}).get("customer_response")
            or exemplar["resolution_notes"]
            or "A support specialist must review this cluster before publishing a macro."
        )
        score = _qa_score(conn, exemplar["id"])
        macros.append(
            {
                "stableKey": stable_key,
                "clusterKey": stable_key,
                "title": f"Macro: {category.title()} {label}",
                "response": response,
                "qaScore": score,
                "version": 1,
                "status": "published" if score >= 90 else "review",
                "sourceTicketId": exemplar["id"],
                "updatedAt": now,
            }
        )

        events.extend(
            [
                {
                    "stableKey": f"{stable_key}:cluster",
                    "clusterKey": stable_key,
                    "agentName": "Clusterer",
                    "stepType": "clustered",
                    "detail": f"Grouped {len(members)} ticket(s) into rank #{rank}.",
                    "createdAt": now,
                },
                {
                    "stableKey": f"{stable_key}:macro",
                    "clusterKey": stable_key,
                    "agentName": "MacroWriter",
                    "stepType": "drafted",
                    "detail": f"Drafted macro from ticket #{exemplar['id']}.",
                    "createdAt": now + 1,
                },
                {
                    "stableKey": f"{stable_key}:qa",
                    "clusterKey": stable_key,
                    "agentName": "QAReviewer",
                    "stepType": "qa_scored",
                    "detail": f"Scored macro {score}/100 and marked it {'published' if score >= 90 else 'review'}.",
                    "createdAt": now + 2,
                },
            ]
        )

    return {"clusters": clusters, "macros": macros, "events": events}


def get_convex_store():
    """Return the official Python Convex client when CONVEX_URL is configured."""
    try:
        from dotenv import load_dotenv

        load_dotenv(".env.local")
    except ImportError:
        pass
    url = os.getenv("CONVEX_URL")
    if not url:
        return None
    try:
        from convex import ConvexClient
    except ImportError as error:
        raise ConvexConfigurationError(
            "CONVEX_URL is set but the Python 'convex' package is not installed"
        ) from error
    return ConvexClient(url)


def load_support_dashboard(
    conn: sqlite3.Connection, store=None
) -> dict[str, Any]:
    """Load the realtime Convex playbook, with a local preview fallback."""
    store = store if store is not None else get_convex_store()
    if store is not None:
        dashboard = store.query("supportOps:dashboard")
        return {"mode": "convex_live", **dashboard}
    return {"mode": "local_preview", **build_support_snapshot(conn)}


def sync_support_ops(conn: sqlite3.Connection, store=None) -> dict[str, Any]:
    """Upsert the current support playbook into Convex in one mutation."""
    snapshot = build_support_snapshot(conn)
    store = store if store is not None else get_convex_store()
    if store is None:
        return {
            "status": "disabled",
            "clusters": len(snapshot["clusters"]),
            "macros": len(snapshot["macros"]),
        }
    result = store.mutation("supportOps:syncSnapshot", snapshot)
    return {"status": "synced", **(result or {})}
