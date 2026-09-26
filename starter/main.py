"""
Customer Support AI Agent — Starter Code
==========================================
Your task is to complete this file by implementing all sections marked
with # TODO comments.

Reference the step-by-step solution files and INSTRUCTIONS.md for guidance.
Do NOT copy the solution directly — work through each section yourself.

Run locally (after filling in config values):
    uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
    agentcore deploy

Invoke deployed agent:
    agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
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
    return {s["type"]: s["namespaces"][0] for s in strategies}


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
                self.memory_client.create_event(
                    memory_id=self.memory_id,
                    actor_id=self.actor_id,
                    session_id=self.session_id,
                    messages=[
                        (user_text, "USER"),
                        (agent_text, "ASSISTANT"),
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

    Args:
        loyalty_points: Customer's current points balance
        tier: Customer tier -- Silver, Gold, or Platinum
        order_total: Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price
    """
    code = f"""
import json, math

loyalty_points = {loyalty_points}
tier = "{tier}"
order_total = {order_total}
product_category = "{product_category}"

earn_rates = {{"standard": 1, "device": 2, "fresh": 5}}
tier_rates = {{"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}}

points_redeemed = (loyalty_points // 500) * 500
max_redeemable = int(order_total * 0.5)
points_redeemed = min(points_redeemed, max_redeemable)
points_value = points_redeemed * 0.001

subtotal_after_points = order_total - points_value
tier_discount_rate = tier_rates.get(tier, 0.00)
tier_discount = subtotal_after_points * tier_discount_rate

final_total = subtotal_after_points - tier_discount
total_savings = order_total - final_total
points_earned = int(order_total * earn_rates.get(product_category, 1))
remaining_points = loyalty_points - points_redeemed + points_earned

result = {{
    "loyalty_points_used": points_redeemed,
    "points_value": round(points_value, 2),
    "tier": tier,
    "tier_discount_rate": f"{{tier_discount_rate * 100:.0f}}%",
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
                return json.dumps(event["result"])

    except Exception as e:
        tier_discount_rate = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}.get(tier, 0.00)
        tier_discount = order_total * tier_discount_rate
        final_total = order_total - tier_discount
        remaining = loyalty_points + int(order_total * {"standard": 1, "device": 2, "fresh": 5}.get(product_category, 1))
        fallback = {
            "tier": tier,
            "tier_discount_rate": f"{tier_discount_rate * 100:.0f}%",
            "tier_discount": round(tier_discount, 2),
            "original_total": order_total,
            "final_total": round(final_total, 2),
            "remaining_points": remaining,
            "note": "Code Interpreter unavailable; fallback tier discount applied.",
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
to provide the best possible support experience."""



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

    client = MCPClient(
        lambda: streamable_http_client(url=GATEWAY_URL)
    )

    with client:
        gateway_tools = client.list_tools_sync()
        tools.extend(gateway_tools)

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
