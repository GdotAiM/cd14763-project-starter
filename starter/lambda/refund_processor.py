"""
Refund Processor Lambda
========================
Handles refund-related operations for the customer support agent.
Invoked directly by the AgentCore Gateway (not through API Gateway).

How tool routing works:
  AgentCore Gateway passes the tool name in the Lambda client context under
  the key "bedrockAgentCoreToolName".  The value has the format:
    "TargetName___toolName"
  This handler strips the prefix and branches on the bare tool name.

Tools handled:
  initiate_refund     — create and approve a new refund
  check_refund_status — look up the status of an existing refund
  get_return_label    — generate a prepaid return shipping label

Tool schema is declared in lambda_schema (JSON file in the same directory).
That schema tells the Gateway which arguments to pass for each tool.
"""
import json
import random
import string
from datetime import datetime, timedelta, timezone


# ── Order totals ─────────────────────────────────────────────────────────────
# Mirrors the mock order data in order_tracker.py. The refund and order-tracker
# tools are separate Lambdas behind separate Gateway targets, so this handler
# can't call order_tracker directly -- it keeps its own copy of order totals
# just to validate that a requested refund amount is sane (not $0, and not
# more than what the customer actually paid). In a real system this would be
# a shared database lookup instead of a duplicated table.
def _order_totals():
    return {
        "ORD-001": 89.99,
        "ORD-002": 139.99,
        "ORD-003": 124.97,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _new_refund_id() -> str:
    """
    Generate a unique refund ID of the form REF-XXXXXXXX.

    Uses random ASCII uppercase letters and digits.  In a real system this
    would be a database-generated ID (e.g. a UUID or auto-increment key).
    """
    return "REF-" + "".join(
        random.choices(string.ascii_uppercase + string.digits, k=8)
    )


# ── Handler ───────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    """
    Main Lambda entry point.

    Args:
        event   — dict of tool arguments passed by the Gateway
        context — Lambda context object; client_context carries the tool name
    """
    # ── Resolve tool name ─────────────────────────────────────────────────────
    raw_tool = ""
    if context.client_context and context.client_context.custom:
        # The Gateway sets bedrockAgentCoreToolName to "TargetName___toolName".
        raw_tool = context.client_context.custom.get("bedrockAgentCoreToolName", "")

    # Strip the target-name prefix to get just the bare tool name.
    # If the separator is absent, use the raw value as-is.
    tool = raw_tool.split("___", 1)[-1] if "___" in raw_tool else raw_tool

    print(f"Tool called: {tool} | Event: {json.dumps(event)}")

    # ── initiate_refund ───────────────────────────────────────────────────────
    if tool == "initiate_refund":
        order_id = event.get("order_id")
        amount = event.get("amount", 0)
        order_total = _order_totals().get((order_id or "").upper())

        # Refuse a refund with no real amount, or one that exceeds what was
        # actually paid for the order. The caller (the agent) is expected to
        # look up the order total first and pass it as `amount`.
        if not amount or amount <= 0:
            return {
                "statusCode": 400,
                "body": json.dumps({
                    "error": "invalid_amount",
                    "message": "Refund amount must be greater than $0. "
                               "Look up the order total and pass it as `amount`.",
                    "order_id": order_id,
                }),
            }
        if order_total is not None and amount > order_total:
            return {
                "statusCode": 400,
                "body": json.dumps({
                    "error": "amount_exceeds_order_total",
                    "message": f"Refund amount ${amount} exceeds the order total "
                               f"(${order_total}) for {order_id}.",
                    "order_id": order_id,
                    "order_total": order_total,
                }),
            }

        return {
            "statusCode": 200,
            "body": json.dumps({
                "refund_id":  _new_refund_id(),
                "order_id":   order_id,
                "status":     "APPROVED",
                "amount":     amount,
                "message":    "Refund approved. Credit appears in 3-5 business days.",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }),
        }

    # ── check_refund_status ───────────────────────────────────────────────────
    if tool == "check_refund_status":
        # In a real system, this would look up the refund in a database.
        return {
            "statusCode": 200,
            "body": json.dumps({
                "refund_id": event.get("refund_id"),
                "status":    "PROCESSING",
                "eta":       "2-3 business days",
            }),
        }

    # ── get_return_label ──────────────────────────────────────────────────────
    if tool == "get_return_label":
        order_id = event.get("order_id", "")
        return {
            "statusCode": 200,
            "body": json.dumps({
                "order_id":    order_id,
                # Simulated pre-signed return label URL.
                "label_url":   f"https://returns.amazon.com/label/{order_id}",
                "carrier":     "UPS",
                "valid_until": (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"),
            }),
        }

    # ── Unknown tool ──────────────────────────────────────────────────────────
    return {
        "statusCode": 400,
        "body": json.dumps({"error": f"Unknown tool: {tool}"}),
    }
