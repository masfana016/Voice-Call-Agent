import asyncio
import base64
import json
import os

import requests
from dotenv import load_dotenv
from aiohttp import web, WSMsgType
import websockets
from twilio.rest import Client

from pharmacy_functions import FUNCTION_MAP

load_dotenv()

# ---------- Config ----------
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")  # e.g. https://xxxx.up.railway.app (no trailing slash)

# Naye env vars - Facebook integration ke liye
FB_VERIFY_TOKEN = os.getenv("FB_VERIFY_TOKEN")            # khud koi random string choose karo
FB_PAGE_ACCESS_TOKEN = os.getenv("FB_PAGE_ACCESS_TOKEN")  # Facebook App se milega

# Naya env var - /make-call ko secure karne ke liye
MAKE_CALL_SECRET = os.getenv("MAKE_CALL_SECRET")  # khud koi random secret bana lo

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def sts_connect():
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        raise Exception("DEEPGRAM_API_KEY not found")
    sts_ws = websockets.connect(
        "wss://agent.deepgram.com/v1/agent/converse",
        subprotocols=["token", api_key]
    )
    return sts_ws


def load_config():
    with open("config.json", "r") as f:
        config = json.load(f)
    google_api_key = os.getenv("GOOGLE_API_KEY")
    if not google_api_key:
        raise Exception("GOOGLE_API_KEY not found in .env")
    config["agent"]["think"]["endpoint"]["headers"]["authorization"] = f"Bearer {google_api_key}"
    return config


async def handle_barge_in(decoded, twilio_ws_send, streamsid):
    if decoded["type"] == "UserStartedSpeaking":
        clear_message = {
            "event": "clear",
            "streamSid": streamsid
        }
        await twilio_ws_send(json.dumps(clear_message))


def execute_function_call(func_name, arguments):
    if func_name in FUNCTION_MAP:
        result = FUNCTION_MAP[func_name](**arguments)
        print(f"Function call result: {result}")
        return result
    else:
        result = {"error": f"Unknown function: {func_name}"}
        print(result)
        return result


def create_function_call_response(func_id, func_name, result):
    return {
        "type": "FunctionCallResponse",
        "id": func_id,
        "name": func_name,
        "content": json.dumps(result)
    }


async def handle_function_call_request(decoded, sts_ws):
    try:
        for function_call in decoded["functions"]:
            func_name = function_call["name"]
            func_id = function_call["id"]
            arguments = json.loads(function_call["arguments"])

            print(f"Function call: {func_name} (ID: {func_id}), arguments: {arguments}")

            result = execute_function_call(func_name, arguments)
            function_result = create_function_call_response(func_id, func_name, result)
            await sts_ws.send(json.dumps(function_result))
            print(f"Sent function result: {function_result}")

    except Exception as e:
        print(f"Error calling function: {e}")
        error_result = create_function_call_response(
            func_id if "func_id" in locals() else "unknown",
            func_name if "func_name" in locals() else "unknown",
            {"error": f"Function call failed with: {str(e)}"}
        )
        await sts_ws.send(json.dumps(error_result))


async def handle_text_message(decoded, twilio_ws_send, sts_ws, streamsid):
    await handle_barge_in(decoded, twilio_ws_send, streamsid)
    if decoded["type"] == "FunctionCallRequest":
        await handle_function_call_request(decoded, sts_ws)


async def sts_sender(sts_ws, audio_queue):
    print("sts_sender started")
    while True:
        chunk = await audio_queue.get()
        await sts_ws.send(chunk)


async def sts_receiver(sts_ws, twilio_ws_send, streamsid_queue):
    print("sts_receiver started")
    streamsid = await streamsid_queue.get()

    async for message in sts_ws:
        if isinstance(message, str):
            print(message)
            decoded = json.loads(message)
            await handle_text_message(decoded, twilio_ws_send, sts_ws, streamsid)
            continue

        raw_mulaw = message
        media_message = {
            "event": "media",
            "streamSid": streamsid,
            "media": {"payload": base64.b64encode(raw_mulaw).decode("ascii")}
        }
        await twilio_ws_send(json.dumps(media_message))


async def twilio_receiver(ws, audio_queue, streamsid_queue):
    BUFFER_SIZE = 20 * 160
    inbuffer = bytearray(b"")

    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue
        try:
            data = json.loads(msg.data)
            event = data["event"]

            if event == "start":
                print("get our streamsid")
                start = data["start"]
                streamsid = start["streamSid"]
                streamsid_queue.put_nowait(streamsid)
            elif event == "connected":
                continue
            elif event == "media":
                media = data["media"]
                chunk = base64.b64decode(media["payload"])
                if media["track"] == "inbound":
                    inbuffer.extend(chunk)
            elif event == "stop":
                break

            while len(inbuffer) >= BUFFER_SIZE:
                chunk = inbuffer[:BUFFER_SIZE]
                audio_queue.put_nowait(chunk)
                inbuffer = inbuffer[BUFFER_SIZE:]
        except Exception as e:
            print(f"twilio_receiver error: {e}")
            break


# ---------- WebSocket handler (aiohttp) ----------
async def twilio_ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    print("Twilio WebSocket connected")

    audio_queue = asyncio.Queue()
    streamsid_queue = asyncio.Queue()

    async def twilio_ws_send(text):
        await ws.send_str(text)

    async with sts_connect() as sts_ws:
        config_message = load_config()
        await sts_ws.send(json.dumps(config_message))

        await asyncio.wait(
            [
                asyncio.ensure_future(sts_sender(sts_ws, audio_queue)),
                asyncio.ensure_future(sts_receiver(sts_ws, twilio_ws_send, streamsid_queue)),
                asyncio.ensure_future(twilio_receiver(ws, audio_queue, streamsid_queue)),
            ],
            return_when=asyncio.FIRST_COMPLETED
        )

    print("Twilio WebSocket closed")
    return ws


# ---------- TwiML endpoint ----------
async def twiml_handler(request):
    ws_url = f"{PUBLIC_BASE_URL.replace('https://', 'wss://').replace('http://', 'ws://')}/media-stream"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}" />
    </Connect>
</Response>"""
    return web.Response(text=xml, content_type="text/xml")


# ---------- Helper: outbound call trigger (reusable) ----------
def trigger_outbound_call(to_number):
    twiml_url = f"{PUBLIC_BASE_URL}/twiml"
    call = twilio_client.calls.create(
        from_=TWILIO_FROM_NUMBER,
        to=to_number,
        url=twiml_url
    )
    print(f"Outbound call initiated: {call.sid} -> {to_number}")
    return call.sid


# ---------- Outbound call trigger endpoint (ab secured hai) ----------
async def make_call_handler(request):
    # Security check - sirf sahi secret wale requests allow honge
    secret = request.headers.get("X-API-Secret")
    if MAKE_CALL_SECRET and secret != MAKE_CALL_SECRET:
        return web.json_response({"error": "Unauthorized"}, status=401)

    data = await request.json()
    to_number = data.get("to")

    if not to_number:
        return web.json_response({"error": "Missing 'to' number"}, status=400)

    call_sid = trigger_outbound_call(to_number)
    return web.json_response({"status": "initiated", "call_sid": call_sid})


# ---------- Facebook Lead Ads webhook: verification (GET) ----------
async def fb_webhook_verify(request):
    mode = request.query.get("hub.mode")
    token = request.query.get("hub.verify_token")
    challenge = request.query.get("hub.challenge")

    if mode == "subscribe" and token == FB_VERIFY_TOKEN:
        print("FB webhook verified successfully")
        return web.Response(text=challenge)

    return web.Response(status=403)


# ---------- Facebook Lead Ads webhook: lead notification (POST) ----------
async def fb_webhook_receive(request):
    payload = await request.json()
    print(f"FB webhook payload: {payload}")

    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                if change.get("field") != "leadgen":
                    continue

                leadgen_id = change["value"]["leadgen_id"]

                # Leadgen ID se poora lead data fetch karo (isme phone number hota hai)
                lead_url = f"https://graph.facebook.com/v26.0/{leadgen_id}"
                params = {"access_token": FB_PAGE_ACCESS_TOKEN}
                resp = requests.get(lead_url, params=params, timeout=10)
                lead_data = resp.json()

                if "error" in lead_data:
                    print(f"Error fetching lead data: {lead_data['error']}")
                    continue

                phone_number = None
                for field in lead_data.get("field_data", []):
                    if field["name"] in ("phone_number", "phone"):
                        phone_number = field["values"][0]
                        break

                if phone_number:
                    trigger_outbound_call(phone_number)
                else:
                    print(f"No phone number found in lead: {lead_data}")

    except Exception as e:
        print(f"fb_webhook_receive error: {e}")

    # Meta ko hamesha 200 return karo, warna wo retry karta rahega
    return web.Response(status=200)


# ---------- App setup ----------
def create_app():
    app = web.Application()
    app.router.add_get("/media-stream", twilio_ws_handler)
    app.router.add_post("/twiml", twiml_handler)
    app.router.add_get("/twiml", twiml_handler)  # Twilio can hit GET too
    app.router.add_post("/make-call", make_call_handler)

    # Naye Facebook Lead Ads webhook routes
    app.router.add_get("/fb-webhook", fb_webhook_verify)
    app.router.add_post("/fb-webhook", fb_webhook_receive)

    return app


if __name__ == "__main__":
    if not PUBLIC_BASE_URL:
        raise Exception("PUBLIC_BASE_URL not set in .env (your ngrok https URL, no trailing slash)")

    app = create_app()
    port = int(os.getenv("PORT", 8080))
    print(f"Starting server on port {port}. Public URL: {PUBLIC_BASE_URL}")
    web.run_app(app, host="0.0.0.0", port=port)