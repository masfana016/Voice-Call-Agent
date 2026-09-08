import os

from agents import Agent, AsyncOpenAI, OpenAIChatCompletionsModel, ModelSettings, function_tool

from pharmacy_functions import FUNCTION_MAP

# =========================================================
# GEMINI CLIENT (same Gemini model/key you were already using)
# =========================================================

google_api_key = os.getenv("GOOGLE_API_KEY")

if not google_api_key:
    raise Exception("GOOGLE_API_KEY not found in .env")

client = AsyncOpenAI(
    api_key=google_api_key,
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
)

model = OpenAIChatCompletionsModel(
    model="gemini-3.6-flash",   # same model string you had in config.json
    openai_client=client,
)


# =========================================================
# TOOLS — thin wrappers around your existing FUNCTION_MAP
# (pharmacy_functions.py itself is untouched)
# =========================================================

@function_tool
def get_drug_info(drug_name: str) -> dict:
    """Get detailed information about a specific drug, including its
    purpose, price, and side effects. Use when a customer asks about a
    medication (e.g. 'What is aspirin?', 'Tell me about ibuprofen',
    or asks about pricing)."""
    return FUNCTION_MAP["get_drug_info"](drug_name=drug_name)


@function_tool
def place_order(customer_name: str, drug_name: str) -> dict:
    """Place a new prescription order for a customer. Use when a customer
    wants to order or refill a medication. Always confirm the drug exists
    and confirm the full order details with the customer before calling this."""
    return FUNCTION_MAP["place_order"](customer_name=customer_name, drug_name=drug_name)


@function_tool
def lookup_order(order_id: int) -> dict:
    """Look up an existing order by its order ID. Use when a customer asks
    about the status of a specific order or provides an order number."""
    return FUNCTION_MAP["lookup_order"](order_id=order_id)


# =========================================================
# PROMPT — same instructions you had in config.json
# =========================================================

PHARMACY_PROMPT = """You are a professional pharmacy assistant. You can: 1) Get drug info with get_drug_info, 2) Place orders with place_order, 3) Look up orders with lookup_order. IMPORTANT: Always ask users to spell out their full name clearly when placing orders. Confirm all order details before processing - including customer name, drug name, and quantity. Be thorough and professional in collecting information. If a user provides a name that's unclear, ask them to spell it out letter by letter. Always confirm the complete order details before finalizing any transaction."""


# =========================================================
# AGENT
# =========================================================

Pharmacy_agent = Agent(
    name="Pharmacy Assistant",
    instructions=PHARMACY_PROMPT,
    tools=[get_drug_info, place_order, lookup_order],
    model=model,
    model_settings=ModelSettings(temperature=0.7),  # same temperature as before
)