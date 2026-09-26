"""
Customer Support AI Agent (Amazon Bedrock AgentCore + Strands Agents)
======================================================================
A customer support agent for an Amazon-style store, served by the AgentCore
Runtime (BedrockAgentCoreApp). It uses Amazon Nova 2 Lite via Bedrock and has:

- search_knowledge_base       -- RAG over the product catalog / policies (Bedrock KB)
- calculate_loyalty_discount  -- exact loyalty maths in the AgentCore Code Interpreter
- AgentCore Browser           -- live web lookups
- Gateway (MCP) tools         -- order tracking and refund processing Lambdas
- MemoryHook                  -- AgentCore long-term memory (facts + preferences)
                                 keyed by customer_id

Set GATEWAY_URL, KB_ID, REGION and MEMORY_ID below before running.

Deploy with the Starter Toolkit (from starter/):
    agentcore configure --entrypoint main.py --name <agent-name> --deployment-type direct_code_deploy --runtime PYTHON_3_13 --disable-memory
    agentcore deploy
    uv run setup_permissions.py

Invoke the deployed agent:
    agentcore invoke '{"prompt": "Can you track order ORD-001?", "customer_id": "CUST-123", "session_id": "t1"}'

Local CLI test: swap app.run() for main() at the bottom of this file, then
    uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
"""

# -- Imports ------------------------------------------------------------------
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamable_http_client
import argparse, json
import os, asyncio, boto3
from strands.hooks import (
    HookProvider, AfterInvocationEvent, HookRegistry, MessageAddedEvent,
)
import logging
import re
import uuid
from typing import Dict
from bedrock_agentcore.tools.code_interpreter_client import code_session
from strands_tools.browser import AgentCoreBrowser


logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")


# -- TODO 1 -- App Initialisation ---------------------------------------------
app = BedrockAgentCoreApp()


# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"


# -- TODO 2 -- Configuration --------------------------------------------------
GATEWAY_URL = "https://customersupportagent-customersupportgateway-klmzs0vuvz.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
KB_ID       = "APLMUD7GVI"
REGION      = "us-east-1"
MEMORY_ID   = "CustomerSupportAgent_CustomerSupportMemory-jacOiD6KOb"


# -- TODO 3 -- Model and Clients ----------------------------------------------
model_id = "global.amazon.nova-2-lite-v1:0"

model = BedrockModel(model_id=model_id)
memory_client = MemoryClient(region_name=REGION)
_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)


# -- TODO 4 -- Namespace Helper -----------------------------------------------
def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type to namespace template string."""
    strategies = mem_client.get_memory_strategies(memory_id)
    namespaces = {}
    for s in strategies:
        # Current API field is namespaceTemplates; namespaces is the legacy name.
        ns = s.get("namespaceTemplates") or s.get("namespaces") or []
        strategy_type = (
            s.get("type") or s.get("memoryStrategyType") or s.get("strategyType")
        )
        if not ns or not strategy_type:
            continue
        namespaces[strategy_type] = ns[0]
    return namespaces


# Matches dollar figures like "$99", "$11.50", "$1,234.56" so they can be
# stripped out before a turn is saved to long-term memory. Memory should hold
# who the customer is and their preferences, not calculated money figures
# (totals, discounts, breakdown lines) that go stale the moment prices change.
_MONEY_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")


def _redact_money(text: str) -> str:
    """Replace every dollar amount in text with a neutral placeholder."""
    return _MONEY_RE.sub("[amount omitted]", text)


# -- TODO 5 -- Memory Hook ----------------------------------------------------
class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_client = memory_client
        self.memory_id = memory_id
        self.namespaces = get_namespaces(self.memory_client, self.memory_id)

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        messages = event.agent.messages
        if (
            not messages
            or messages[-1]["role"] != "user"
            or "toolResult" in messages[-1]["content"][0]
        ):
            return

        user_query = messages[-1]["content"][0]["text"]
        try:
            all_context = []
            for strategy_type, namespace in self.namespaces.items():
                resolved_namespace = namespace.format(actorId=self.actor_id)
                memories = self.memory_client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=resolved_namespace,
                    query=user_query,
                    top_k=5,
                )
                for memory in memories:
                    if isinstance(memory, dict):
                        text = memory.get("content", {}).get("text", "").strip()
                        if text:
                            all_context.append(f"[{strategy_type}] {text}")
            if all_context:
                context_block = "\n".join(all_context)
                original_text = messages[-1]["content"][0]["text"]
                messages[-1]["content"][0]["text"] = (
                    f"Customer Context:\n{context_block}\n\n{original_text}"
                )
        except Exception as exc:
            logger.error("Failed to retrieve customer context: %s", exc)

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        try:
            messages = event.agent.messages
            user_text = agent_text = None

            for msg in reversed(messages):
                if msg["role"] == "assistant" and not agent_text:
                    content = msg["content"]
                    if isinstance(content, list):
                        agent_text = content[0].get("text", "")
                    else:
                        agent_text = str(content)
                elif (
                    msg["role"] == "user"
                    and not user_text
                    and "toolResult" not in msg["content"][0]
                ):
                    user_text = msg["content"][0]["text"]
                    break

            if user_text and agent_text:
                # Strip dollar figures (totals, discounts, breakdown lines)
                # before persisting -- money must always come from a fresh
                # tool call, never from a remembered turn.
                self.memory_client.create_event(
                    memory_id=self.memory_id,
                    actor_id=self.actor_id,
                    session_id=self.session_id,
                    messages=[
                        (_redact_money(user_text), "USER"),
                        (_redact_money(agent_text), "ASSISTANT"),
                    ],
                )
        except Exception as exc:
            logger.error("Failed to save support interaction: %s", exc)

    def register_hooks(self, registry: HookRegistry) -> None:
        """Register both memory callbacks."""
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)



# -- TODO 6 -- Knowledge Base Tool --------------------------------------------
@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    if not KB_ID:
        return "Knowledge base not configured."

    resp = _bedrock_runtime.retrieve(
        knowledgeBaseId=KB_ID,
        retrievalQuery={"text": query},
    )
    results = resp.get("retrievalResults", [])
    if not results:
        return f"No information found for: {query}"

    chunks = [r["content"]["text"] for r in results]
    return "\n---\n".join(chunks)



# -- TODO 7 -- Loyalty Discount Tool (Code Interpreter) -----------------------
@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Order of operations: points are redeemed first (reducing the order
    total), and the tier discount percentage is then applied to what's
    left AFTER points -- not to the original order total.

    Args:
        loyalty_points: Customer's current points balance
        tier: Customer tier -- Silver, Gold, or Platinum
        order_total: Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price. Report tier_discount,
        points_value, and final_total exactly as returned -- do not
        recalculate or round them yourself.
    """
    code = f"""
import json, math

loyalty_points = {loyalty_points}
tier = "{tier}"
order_total = {order_total}
product_category = "{product_category}"

earn_rates = {{"standard": 1, "device": 2, "fresh": 5}}
tier_rates = {{"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}}

# 100 points = $1; redeem in 500-point blocks (500 minimum),
# capped so the points value never exceeds 50% of the order total.
POINTS_PER_DOLLAR = 100
REDEEM_BLOCK = 500
max_redeemable_points = int(order_total * 0.5 * POINTS_PER_DOLLAR) // REDEEM_BLOCK * REDEEM_BLOCK
points_redeemed = (loyalty_points // REDEEM_BLOCK) * REDEEM_BLOCK
points_redeemed = min(points_redeemed, max_redeemable_points)
points_value = points_redeemed / POINTS_PER_DOLLAR

subtotal_after_points = order_total - points_value
tier_discount_rate = tier_rates.get(tier, 0.00)
tier_discount = subtotal_after_points * tier_discount_rate

final_total = subtotal_after_points - tier_discount
total_savings = order_total - final_total
points_earned = int(order_total * earn_rates.get(product_category, 1))
remaining_points = loyalty_points - points_redeemed + points_earned

result = {{
    "points_redeemed": points_redeemed,
    "points_value": round(points_value, 2),
    "tier": tier,
    "tier_discount_pct": round(tier_discount_rate * 100, 2),
    "tier_discount": round(tier_discount, 2),
    "original_total": order_total,
    "final_total": round(final_total, 2),
    "total_savings": round(total_savings, 2),
    "points_earned": points_earned,
    "remaining_points": remaining_points,
}}

print(json.dumps(result, indent=2))
"""

    try:
        with code_session(REGION) as code_client:
            response = code_client.invoke("executeCode", {
                "code": code,
                "language": "python",
                "clearContext": True,
            })

            for event in response["stream"]:
                result = event.get("result", {})
                if result.get("isError"):
                    raise RuntimeError(f"Code Interpreter error: {result}")
                # Prefer the sandbox's stdout; fall back to the text content blocks.
                stdout = (result.get("structuredContent") or {}).get("stdout")
                if not stdout:
                    stdout = "".join(
                        c.get("text", "")
                        for c in result.get("content", [])
                        if c.get("type") == "text"
                    )
                # Validate and re-emit the JSON the generated code printed.
                return json.dumps(json.loads(stdout))

            raise RuntimeError("Code Interpreter returned no result")

    except Exception as e:
        logger.warning("Code Interpreter unavailable, using fallback: %s", e)
        # Fallback: tier discount only (no points redemption / earn).
        tier_rates = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
        tier_discount_rate = tier_rates.get(tier, 0.00)
        tier_discount = order_total * tier_discount_rate
        fallback = {
            "tier": tier,
            "tier_discount_pct": round(tier_discount_rate * 100, 2),
            "tier_discount": round(tier_discount, 2),
            "note": "Code Interpreter unavailable; fallback tier discount only applied.",
        }
        return json.dumps(fallback)


# -- TODO 8 -- Agent Entrypoint -----------------------------------------------
SYSTEM_PROMPT = """You are a customer support agent for an Amazon store. You help customers with:
- Product questions and specifications
- Order tracking and status updates
- Refund and return processing
- Loyalty program information and discount calculations
- General support inquiries
You have access to:
- A knowledge base with product catalog, return policies, and loyalty program details
- Order tracking and refund processing tools via the AgentCore Gateway
- A code interpreter for precise loyalty discount calculations
- A browser for looking up live web information
- Persistent memory across sessions to remember customer preferences
Always be helpful, accurate, and professional. Use the tools available to you
to provide the best possible support experience.

Critical rules:
- All prices, discounts, and totals must come from a tool call made in THIS
  conversation (calculate_loyalty_discount, order tracking, etc.). Memory is
  only for who the customer is and their preferences -- never trust a
  remembered dollar figure over a fresh tool result, even if memory
  disagrees with the tool.
- Before processing any refund, first look up the order (order tracking
  tool) to get its real total, and pass that total as the refund amount.
  Never call the refund tool with a zero or missing amount.
- When calculate_loyalty_discount returns, report tier_discount,
  points_value, and final_total exactly as the tool returned them. Never
  recalculate, re-derive, or round these yourself."""



@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
    prompt (str, required) -- the customer's message
    customer_id (str, optional) -- unique customer identifier
    session_id (str, optional) -- session identifier; generated if absent
    """
    user_input = payload.get("prompt", "Hello!")
    actor_id = payload.get("customer_id", "anonymous")
    session_id = payload.get("session_id", str(uuid.uuid4()))

    memory_hook = MemoryHook(
        actor_id=actor_id,
        session_id=session_id,
        memory_client=memory_client,
        memory_id=MEMORY_ID,
    )

    agent_core_browser = AgentCoreBrowser(region=REGION)

    tools = [search_knowledge_base, calculate_loyalty_discount, agent_core_browser.browser]

    gateway_client = MCPClient(
        lambda: streamable_http_client(url=GATEWAY_URL)
    )

    with gateway_client:
        try:
            gateway_tools = gateway_client.list_tools_sync()
            tools.extend(gateway_tools)

            logger.info(
                "Gateway connected successfully. Loaded %d tools.",
                len(gateway_tools),
            )

        except TimeoutError:
            logger.exception("Gateway tool loading timed out")

        except ConnectionError:
            logger.exception("Gateway connection failed")

        except Exception as exc:
            logger.exception(
                "Gateway tool loading failed: %s", exc
            )

        agent = Agent(
            model=model,
            system_prompt=SYSTEM_PROMPT,
            tools=tools,
            state={"actor_id": actor_id, "session_id": session_id},
            hooks=[memory_hook],
        )

        response = agent(user_input)
        return response


# -- CLI entry point (do not modify) -----------------------------------------
def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    app.run()
# Uncomment the line below and comment app.run() for local CLI testing:
# main()
