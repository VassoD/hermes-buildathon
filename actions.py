"""Execute approved support actions against external systems."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from dodo import DodoClient, DodoError, extract_payment_id


@dataclass(frozen=True)
class ExecutionResult:
    provider: str
    status: str
    external_id: str | None
    detail: str


def _ticket_value(ticket, key: str):
    """Read a key from dicts or sqlite Rows without relying on .get()."""
    try:
        return ticket[key]
    except (KeyError, IndexError):
        return None


def execute_action(
    ticket,
    action: str,
    reason: str,
    dodo_client: DodoClient | None = None,
) -> ExecutionResult:
    """Execute an approved action; never report success without provider proof."""
    if action != "issue_refund":
        return ExecutionResult(
            provider="internal",
            status="not_required",
            external_id=None,
            detail="No external payment operation is required for this action.",
        )

    payment_id = _ticket_value(ticket, "payment_id") or extract_payment_id(
        _ticket_value(ticket, "body") or ""
    )
    if not payment_id:
        return ExecutionResult(
            provider="dodo",
            status="blocked",
            external_id=None,
            detail=(
                "Refund was approved but not executed because the Dodo payment ID "
                "is missing. Ask the customer for an ID beginning with pay_."
            ),
        )

    try:
        client = dodo_client or DodoClient.from_environment()
        refund = client.create_refund(
            payment_id,
            reason=reason,
            metadata={
                "ticket_id": str(_ticket_value(ticket, "id")),
                "customer_email": str(_ticket_value(ticket, "customer_email") or ""),
            },
        )
    except DodoError as error:
        return ExecutionResult(
            provider="dodo",
            status="failed",
            external_id=None,
            detail=str(error),
        )

    environment = getattr(client, "environment", "test")
    return ExecutionResult(
        provider="dodo",
        status=refund.status,
        external_id=refund.refund_id,
        detail=(
            f"Dodo {environment} refund {refund.refund_id} for payment "
            f"{refund.payment_id} returned status={refund.status}."
        ),
    )


def status_for_execution(action: str, result: ExecutionResult) -> str:
    """Map provider evidence to a truthful ticket lifecycle status."""
    if action == "escalate":
        return "escalated"
    if action == "request_more_info":
        return "in_review"
    if action != "issue_refund":
        return "resolved"
    if result.status == "succeeded":
        return "resolved"
    if result.status == "pending":
        return "in_review"
    return "escalated"


def persist_execution(
    conn: sqlite3.Connection,
    run_id: str,
    ticket_id: int,
    action: str,
    result: ExecutionResult,
) -> None:
    conn.execute(
        """
        INSERT INTO action_executions
            (run_id, ticket_id, action, provider, status, external_id, detail)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            ticket_id,
            action,
            result.provider,
            result.status,
            result.external_id,
            result.detail,
        ),
    )
    conn.commit()
