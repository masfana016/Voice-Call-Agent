import os
import time
import uuid
import json

from aiohttp import web
from agents import Runner, RunConfig, set_tracing_disabled

from pharmacy_agent import Pharmacy_agent, model

# Tracing needs an OPENAI_API_KEY, which this pharmacy setup doesn't have.
# Turn it off so it doesn't error out.
set_tracing_disabled(True)

# Optional shared-secret protection, same pattern as MAKE_CALL_SECRET.
# Set AGENT_THINK_SECRET in .env if you want this endpoint locked down.
AGENT_THINK_SECRET = os.getenv("AGENT_THINK_SECRET")

run_config = RunConfig(
    model=model,
    model_provider=model.openai_client if hasattr(model, "openai_client") else None,
    tracing_disabled=True,
)


def _messages_to_agent_input(messages):
    """
    Deepgram sends the full conversation history each time, OpenAI
    chat-completions style: [{"role": "system"/"user"/"assistant", "content": "..."}].
    The Agent already carries the system prompt via `instructions`, so we
    strip any system message and hand the rest straight to Runner.run,
    which accepts a list of role/content dicts as input.
    """
    return [m for m in messages if m.get("role") != "system"]


def _build_completion_response(text, model_name):
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def _sse_wrap(completion_dict):
    """
    Wrap a full completion as a single Server-Sent-Events chunk, for
    clients that request stream=true. This isn't token-by-token
    streaming (the Agent SDK's tool-calling loop has to finish first),
    but it keeps compatibility with a streaming caller.
    """
    chunk = {
        "id": completion_dict["id"],
        "object": "chat.completion.chunk",
        "created": completion_dict["created"],
        "model": completion_dict["model"],
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": completion_dict["choices"][0]["message"]["content"],
                },
                "finish_reason": None,
            }
        ],
    }
    final_chunk = {
        "id": completion_dict["id"],
        "object": "chat.completion.chunk",
        "created": completion_dict["created"],
        "model": completion_dict["model"],
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }

    body = f"data: {json.dumps(chunk)}\n\n"
    body += f"data: {json.dumps(final_chunk)}\n\n"
    body += "data: [DONE]\n\n"
    return body


async def agent_think_handler(request):
    """
    Endpoint Deepgram's `think` provider calls. Must accept an OpenAI
    chat-completions style POST body and return an OpenAI-compatible
    response, but internally the reply is produced by the Agent SDK
    Runner (Agent + tools), not a raw model call.
    """

    if AGENT_THINK_SECRET:
        auth_header = request.headers.get("authorization", "")
        expected = f"Bearer {AGENT_THINK_SECRET}"
        if auth_header != expected:
            return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    messages = data.get("messages", [])
    model_name = data.get("model", "gemini-3.6-flash")
    stream = data.get("stream", False)

    agent_input = _messages_to_agent_input(messages)

    try:
        result = await Runner.run(
            Pharmacy_agent,
            input=agent_input,
            run_config=run_config,
        )
        reply_text = result.final_output

    except Exception as e:
        print(f"agent_think_handler error: {e}")
        reply_text = (
            "Sorry, I'm having trouble processing that right now. "
            "Could you please repeat that?"
        )

    completion = _build_completion_response(reply_text, model_name)

    if stream:
        return web.Response(
            text=_sse_wrap(completion),
            content_type="text/event-stream",
        )

    return web.json_response(completion)