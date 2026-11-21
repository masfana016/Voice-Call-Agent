import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
from xml.sax.saxutils import quoteattr

import aiohttp
import websockets
from aiohttp import WSMsgType, web
from dotenv import load_dotenv
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient

load_dotenv()

from solar_functions import (  # noqa: E402
    FUNCTION_MAP,
    TZ_NAME,
    answers_text,
    create_lead,
    delete_lead,
    get_lead,
    init_db,
    is_do_not_call,
    today_text,
)

# =========================================================
# CONFIG
# =========================================================

PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
LEADS_API_KEY = os.getenv("LEADS_API_KEY")
COMPANY_NAME = os.getenv("COMPANY_NAME", "Your Solar Company")
AGENT_NAME = os.getenv("AGENT_NAME", "Sara")

# Facebook Lead Ads (optional: the webhook is switched on only if all three are set)
FB_VERIFY_TOKEN = os.getenv("FB_VERIFY_TOKEN")
FB_PAGE_ACCESS_TOKEN = os.getenv("FB_PAGE_ACCESS_TOKEN")
FB_APP_SECRET = os.getenv("FB_APP_SECRET")
FB_GRAPH_VERSION = os.getenv("FB_GRAPH_VERSION", "v26.0")
FB_ENABLED = all([FB_VERIFY_TOKEN, FB_PAGE_ACCESS_TOKEN, FB_APP_SECRET])

# For numbers that arrive without a + sign, e.g. 92 for Pakistan. Leave empty if unsure.
DEFAULT_COUNTRY_CODE = (os.getenv("DEFAULT_COUNTRY_CODE") or "").lstrip("+")

for _name, _value in {
    "PUBLIC_BASE_URL": PUBLIC_BASE_URL,
    "TWILIO_ACCOUNT_SID": TWILIO_ACCOUNT_SID,
    "TWILIO_AUTH_TOKEN": TWILIO_AUTH_TOKEN,
    "TWILIO_PHONE_NUMBER": TWILIO_PHONE_NUMBER,
    "LEADS_API_KEY": LEADS_API_KEY,
    "DEEPGRAM_API_KEY": os.getenv("DEEPGRAM_API_KEY"),
}.items():
    if not _value:
        raise RuntimeError(f"{_name} is not set in .env")
if not PUBLIC_BASE_URL.startswith("https://"):
    raise RuntimeError("PUBLIC_BASE_URL must use HTTPS")

validator = RequestValidator(TWILIO_AUTH_TOKEN)
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")
SEEN_LEADGEN: set = set()  # Facebook can send the same lead twice
BACKGROUND_TASKS: set = set()


def normalize_phone(raw):
    """Return the number as +<country><digits>, or None if it cannot be trusted."""
    p = re.sub(r"[\s\-().]", "", str(raw or ""))
    if p.startswith("00"):
        p = "+" + p[2:]
    if not p.startswith("+") and DEFAULT_COUNTRY_CODE:
        if p.startswith("0"):
            p = "+" + DEFAULT_COUNTRY_CODE + p.lstrip("0")
        elif p.startswith(DEFAULT_COUNTRY_CODE):
            p = "+" + p
        else:
            p = "+" + DEFAULT_COUNTRY_CODE + p
    return p if PHONE_RE.match(p) else None


def validate_twilio_request(url, params, signature):
    if not signature:
        return False
    try:
        return validator.validate(url, params, signature)
    except Exception as e:
        print(f"Signature validation error: {e}")
        return False


def sts_connect():
    return websockets.connect(
        "wss://agent.deepgram.com/v1/agent/converse",
        subprotocols=["token", os.getenv("DEEPGRAM_API_KEY")],
    )


def build_config(lead):
    """Deepgram Settings message, personalised for this lead."""
    with open("config.json", "r") as f:
        config = json.load(f)
    with open("solar_agent_config.json", "r") as f:
        solar = json.load(f)

    first_name = (lead.get("name") or "there").split()[0]

    def fill(text):
        return (
            text.replace("{first_name}", first_name)
            .replace("{company}", COMPANY_NAME)
            .replace("{agent_name}", AGENT_NAME)
            .replace("{today}", today_text())
            .replace("{timezone}", TZ_NAME)
            # last, so nothing typed into the form can be swapped into the prompt
            .replace("{form_details}", answers_text(lead.get("answers") or {}) or "none provided")
        )

    agent = config["agent"]
    agent["greeting"] = fill(solar["greeting"])
    agent["think"]["prompt"] = fill(solar["prompt"])
    agent["think"]["functions"] = solar["functions"]
    return config


# =========================================================
# BARGE-IN + FUNCTION CALLS
# =========================================================

async def handle_barge_in(decoded, twilio_ws_send, streamsid):
    if decoded.get("type") == "UserStartedSpeaking":
        await twilio_ws_send(json.dumps({"event": "clear", "streamSid": streamsid}))


def execute_function_call(func_name, arguments):
    func = FUNCTION_MAP.get(func_name)
    if not func:
        return {"error": f"Unknown function: {func_name}"}
    return func(**arguments)


async def handle_function_call_request(decoded, sts_ws, call_ctx):
    lead = call_ctx["lead"]
    for fc in decoded.get("functions", []):
        if fc.get("client_side") is False:
            continue
        func_id = fc.get("id", "unknown")
        func_name = fc.get("name", "unknown")
        try:
            arguments = json.loads(fc.get("arguments") or "{}")
            # lead id and phone come from our server, never from the model
            arguments["lead_id"] = lead["id"]
            arguments["phone"] = lead["phone"]
            print(f"Function call: {func_name} ({func_id}) lead={lead['id']}")
            result = await asyncio.to_thread(execute_function_call, func_name, arguments)
        except Exception as e:
            print(f"Function {func_name} failed: {e}")
            result = {"error": "The system could not complete that request."}

        await sts_ws.send(json.dumps({
            "type": "FunctionCallResponse",
            "id": func_id,
            "name": func_name,
            "content": json.dumps(result),
        }))


async def handle_text_message(decoded, twilio_ws_send, sts_ws, streamsid, call_ctx):
    await handle_barge_in(decoded, twilio_ws_send, streamsid)
    if decoded.get("type") == "FunctionCallRequest":
        await handle_function_call_request(decoded, sts_ws, call_ctx)


# =========================================================
# AUDIO BRIDGE
# =========================================================

async def sts_sender(sts_ws, audio_queue):
    while True:
        chunk = await audio_queue.get()
        await sts_ws.send(chunk)


async def sts_receiver(sts_ws, twilio_ws_send, call_ctx):
    streamsid = call_ctx["stream_sid"]
    async for message in sts_ws:
        if isinstance(message, str):
            decoded = json.loads(message)
            msg_type = decoded.get("type")
            if msg_type in ("Warning", "Error"):
                print(f"Deepgram {msg_type}: {decoded}")
            else:
                print(f"Deepgram: {msg_type}")  # types only: transcripts hold personal data
            await handle_text_message(decoded, twilio_ws_send, sts_ws, streamsid, call_ctx)
            continue

        await twilio_ws_send(json.dumps({
            "event": "media",
            "streamSid": streamsid,
            "media": {"payload": base64.b64encode(message).decode("ascii")},
        }))


async def twilio_receiver(ws, audio_queue, call_ctx, ready):
    BUFFER_SIZE = 20 * 160
    inbuffer = bytearray()

    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue
        try:
            data = json.loads(msg.data)
            event = data.get("event")

            if event == "start":
                start = data["start"]
                params = start.get("customParameters", {})
                call_ctx["lead"] = await asyncio.to_thread(get_lead, params.get("lead_id", ""))
                call_ctx["stream_sid"] = start["streamSid"]
                ready.set()

            elif event == "media":
                media = data["media"]
                if media.get("track") == "inbound":
                    inbuffer.extend(base64.b64decode(media["payload"]))
                    while len(inbuffer) >= BUFFER_SIZE:
                        audio_queue.put_nowait(bytes(inbuffer[:BUFFER_SIZE]))
                        del inbuffer[:BUFFER_SIZE]

            elif event == "stop":
                break
        except Exception as e:
            print(f"twilio_receiver error: {e}")
            break


async def twilio_ws_handler(request):
    # Reconstruct the exact public WebSocket URL Twilio signed
    signature = request.headers.get("X-Twilio-Signature", "")
    base = PUBLIC_BASE_URL + str(request.rel_url)
    wss = base.replace("https://", "wss://", 1)
    candidates = [wss, wss + "/", base, base + "/"]
    matched = next((u for u in candidates if validate_twilio_request(u, {}, signature)), None)

    if not matched:
        print(f"Rejected unauthorized WebSocket connection (signature present: {bool(signature)})")
        return web.Response(status=403, text="Forbidden")

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    print("Authenticated Twilio WebSocket connected")

    audio_queue = asyncio.Queue()
    call_ctx = {}
    ready = asyncio.Event()

    async def twilio_ws_send(text):
        await ws.send_str(text)

    # Start reading Twilio first: we need the lead before configuring the agent
    twilio_task = asyncio.create_task(twilio_receiver(ws, audio_queue, call_ctx, ready))
    try:
        await asyncio.wait_for(ready.wait(), timeout=10)
    except asyncio.TimeoutError:
        print("No start event from Twilio")
        twilio_task.cancel()
        await ws.close()
        return ws

    if not call_ctx.get("lead"):
        print("Unknown lead for this stream, closing")
        twilio_task.cancel()
        await ws.close()
        return ws

    async with sts_connect() as sts_ws:
        await sts_ws.send(json.dumps(build_config(call_ctx["lead"])))

        tasks = [
            asyncio.create_task(sts_sender(sts_ws, audio_queue)),
            asyncio.create_task(sts_receiver(sts_ws, twilio_ws_send, call_ctx)),
            twilio_task,
        ]

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        for t in done:
            if not t.cancelled() and t.exception():
                print(f"Task ended with error: {t.exception()}")

    print("Twilio WebSocket closed")
    return ws


# =========================================================
# OUTBOUND: /leads places the call, Twilio then asks /twiml-outbound what to do
# =========================================================

async def start_call(name, phone, source, answers=None):
    """Shared by /leads and the Facebook webhook. Returns (http_status, body)."""
    phone = normalize_phone(phone)
    if not phone:
        return 400, {"error": "phone must be in international format, like +923001234567"}
    if await asyncio.to_thread(is_do_not_call, phone):
        return 200, {"status": "skipped", "reason": "do_not_call"}

    lead = await asyncio.to_thread(
        create_lead, str(name or "").strip(), phone, str(source or "unknown"), answers
    )
    lead_id = lead["id"]

    try:
        call = await asyncio.to_thread(
            twilio_client.calls.create,
            to=phone,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{PUBLIC_BASE_URL}/twiml-outbound?lead_id={lead_id}",
            method="POST",
        )
    except Exception as e:
        await asyncio.to_thread(delete_lead, lead_id)
        print(f"Twilio call failed: {type(e).__name__}")
        return 502, {"error": "could not place the call"}

    print(f"Calling lead {lead_id} (source: {lead['source']}), call sid {call.sid}")
    return 200, {"status": "calling", "lead_id": lead_id, "call_sid": call.sid}


async def leads_handler(request):
    """POST /leads  {"name": "...", "phone": "+923001234567", "source": "test"}
    Send the key in the X-API-Key header (or ?key=...). GHL, Zapier or a test
    script can all use this one entry point."""
    key = request.headers.get("X-API-Key") or request.query.get("key", "")
    if not hmac.compare_digest(key.encode(), LEADS_API_KEY.encode()):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "send a JSON body"}, status=400)

    status, result = await start_call(
        body.get("name"), body.get("phone"), body.get("source"), body.get("answers")
    )
    return web.json_response(result, status=status)


# =========================================================
# FACEBOOK LEAD ADS
# =========================================================

async def fb_webhook_verify(request):
    if (
        request.query.get("hub.mode") == "subscribe"
        and request.query.get("hub.verify_token") == FB_VERIFY_TOKEN
    ):
        print("Facebook webhook verified")
        return web.Response(text=request.query.get("hub.challenge", ""))
    print("Facebook webhook verification failed")
    return web.Response(status=403)


async def process_facebook_lead(leadgen_id):
    """Fetch the lead from the Graph API, then call it. Runs in the background."""
    url = f"https://graph.facebook.com/{FB_GRAPH_VERSION}/{leadgen_id}"
    params = {"access_token": FB_PAGE_ACCESS_TOKEN, "fields": "id,created_time,field_data"}
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as resp:
                lead_data = await resp.json()
                status = resp.status
    except Exception as e:
        print(f"Facebook lead {leadgen_id}: fetch failed ({type(e).__name__})")
        return

    if status != 200 or "error" in lead_data:
        err = lead_data.get("error", {}) if isinstance(lead_data, dict) else {}
        print(f"Facebook lead {leadgen_id}: Graph API error {status} {err.get('message', '')}")
        return

    fields = {}
    for f in lead_data.get("field_data", []):
        values = f.get("values") or []
        if values:
            fields[f.get("name", "")] = values[0]

    phone = fields.get("phone_number") or fields.get("phone")
    name = (
        fields.get("full_name")
        or f"{fields.get('first_name', '')} {fields.get('last_name', '')}".strip()
    )
    if not phone:
        print(f"Facebook lead {leadgen_id}: no phone number in the form")
        return

    # every other question on the form (property, bill, city...) goes along as answers
    status, result = await start_call(name, phone, "facebook", fields)
    print(f"Facebook lead {leadgen_id}: {result.get('status') or result.get('error')}")


async def fb_webhook_receive(request):
    raw = await request.read()

    # Only accept posts really signed by Facebook with your app secret
    signature = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(FB_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.encode(), expected.encode()):
        print("Rejected Facebook webhook with a bad signature")
        return web.Response(status=403)

    try:
        payload = json.loads(raw)
    except Exception:
        return web.Response(status=200)

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "leadgen":
                continue
            leadgen_id = (change.get("value") or {}).get("leadgen_id")
            if not leadgen_id or leadgen_id in SEEN_LEADGEN:
                continue
            if len(SEEN_LEADGEN) > 5000:
                SEEN_LEADGEN.clear()
            SEEN_LEADGEN.add(leadgen_id)
            # Answer Facebook straight away and do the slow work in the background
            task = asyncio.create_task(process_facebook_lead(str(leadgen_id)))
            BACKGROUND_TASKS.add(task)
            task.add_done_callback(BACKGROUND_TASKS.discard)

    return web.Response(status=200)


async def twiml_outbound(request):
    """Twilio fetches this once the lead picks up."""
    data = await request.post()
    signature_url = PUBLIC_BASE_URL + str(request.rel_url)
    signature = request.headers.get("X-Twilio-Signature", "")
    if not validate_twilio_request(signature_url, data, signature):
        print("Rejected invalid Twilio webhook signature")
        return web.Response(status=403, text="Forbidden")

    lead_id = request.query.get("lead_id", "")
    if not await asyncio.to_thread(get_lead, lead_id):
        return web.Response(status=404, text="Unknown lead")

    ws_url = PUBLIC_BASE_URL.replace("https://", "wss://", 1) + "/media-stream"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url={quoteattr(ws_url)}>
            <Parameter name="lead_id" value={quoteattr(lead_id)} />
        </Stream>
    </Connect>
</Response>"""
    return web.Response(text=xml, content_type="text/xml")


async def health(request):
    return web.Response(text="ok")


def create_app():
    app = web.Application()
    app.router.add_get("/media-stream", twilio_ws_handler)
    app.router.add_post("/twiml-outbound", twiml_outbound)
    app.router.add_post("/leads", leads_handler)
    app.router.add_get("/health", health)
    if FB_ENABLED:
        app.router.add_get("/fb-webhook", fb_webhook_verify)
        app.router.add_post("/fb-webhook", fb_webhook_receive)
    return app


if __name__ == "__main__":
    init_db()  # creates the tables in Supabase on first run
    port = int(os.getenv("PORT", 8080))
    if not FB_ENABLED:
        print("Facebook webhook is OFF (set FB_VERIFY_TOKEN, FB_PAGE_ACCESS_TOKEN and FB_APP_SECRET to turn it on)")
    print(f"Starting server on port {port}. Public URL: {PUBLIC_BASE_URL}")
    web.run_app(create_app(), host="0.0.0.0", port=port)