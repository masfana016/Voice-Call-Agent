import asyncio
import base64
import json
import os

import requests
from dotenv import load_dotenv
from aiohttp import web, WSMsgType
import websockets
from twilio.rest import Client

from agent_think import agent_think_handler
from pharmacy_functions import FUNCTION_MAP

load_dotenv()


# =========================================================
# CONFIG
# =========================================================

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")

# Facebook
FB_VERIFY_TOKEN = os.getenv("FB_VERIFY_TOKEN")
FB_PAGE_ACCESS_TOKEN = os.getenv("FB_PAGE_ACCESS_TOKEN")

# Secure /make-call endpoint
MAKE_CALL_SECRET = os.getenv("MAKE_CALL_SECRET")


# =========================================================
# TWILIO CLIENT
# =========================================================

twilio_client = Client(
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN
)


# =========================================================
# DEEPGRAM
# =========================================================

def sts_connect():
    api_key = os.getenv("DEEPGRAM_API_KEY")

    if not api_key:
        raise Exception("DEEPGRAM_API_KEY not found")

    sts_ws = websockets.connect(
        "wss://agent.deepgram.com/v1/agent/converse",
        subprotocols=["token", api_key]
    )

    return sts_ws


# =========================================================
# LOAD DEEPGRAM / GEMINI CONFIG
# =========================================================

def load_config():
    with open("config.json", "r") as f:
        config = json.load(f)

    agent_think_secret = os.getenv("AGENT_THINK_SECRET", "")
    config["agent"]["think"]["endpoint"]["headers"]["authorization"] = f"Bearer {agent_think_secret}"

    config["agent"]["think"]["endpoint"]["url"] = f"{PUBLIC_BASE_URL}/agent-think"

    return config

# =========================================================
# BARGE-IN
# =========================================================

async def handle_barge_in(
    decoded,
    twilio_ws_send,
    streamsid
):

    if decoded.get("type") == "UserStartedSpeaking":

        clear_message = {
            "event": "clear",
            "streamSid": streamsid
        }

        await twilio_ws_send(
            json.dumps(clear_message)
        )


# =========================================================
# FUNCTION CALL
# =========================================================

def execute_function_call(func_name, arguments):

    if func_name in FUNCTION_MAP:

        result = FUNCTION_MAP[func_name](**arguments)

        print(
            f"Function call result: {result}"
        )

        return result

    else:

        result = {
            "error": f"Unknown function: {func_name}"
        }

        print(result)

        return result


def create_function_call_response(
    func_id,
    func_name,
    result
):

    return {
        "type": "FunctionCallResponse",
        "id": func_id,
        "name": func_name,
        "content": json.dumps(result)
    }


async def handle_function_call_request(
    decoded,
    sts_ws
):

    try:

        for function_call in decoded["functions"]:

            func_name = function_call["name"]
            func_id = function_call["id"]

            arguments = json.loads(
                function_call["arguments"]
            )

            print(
                f"Function call: {func_name} "
                f"(ID: {func_id}), "
                f"arguments: {arguments}"
            )

            result = execute_function_call(
                func_name,
                arguments
            )

            function_result = create_function_call_response(
                func_id,
                func_name,
                result
            )

            await sts_ws.send(
                json.dumps(function_result)
            )

            print(
                f"Sent function result: {function_result}"
            )

    except Exception as e:

        print(
            f"Error calling function: {e}"
        )

        error_result = create_function_call_response(
            func_id if "func_id" in locals() else "unknown",
            func_name if "func_name" in locals() else "unknown",
            {
                "error":
                f"Function call failed with: {str(e)}"
            }
        )

        await sts_ws.send(
            json.dumps(error_result)
        )


async def handle_text_message(
    decoded,
    twilio_ws_send,
    sts_ws,
    streamsid
):

    await handle_barge_in(
        decoded,
        twilio_ws_send,
        streamsid
    )

    if decoded.get("type") == "FunctionCallRequest":

        await handle_function_call_request(
            decoded,
            sts_ws
        )


# =========================================================
# STS SENDER
# =========================================================

async def sts_sender(
    sts_ws,
    audio_queue
):

    print("sts_sender started")

    while True:

        chunk = await audio_queue.get()

        await sts_ws.send(chunk)


# =========================================================
# STS RECEIVER
# =========================================================

async def sts_receiver(
    sts_ws,
    twilio_ws_send,
    streamsid_queue
):

    print("sts_receiver started")

    streamsid = await streamsid_queue.get()

    async for message in sts_ws:

        if isinstance(message, str):

            print(message)

            decoded = json.loads(message)

            await handle_text_message(
                decoded,
                twilio_ws_send,
                sts_ws,
                streamsid
            )

            continue

        raw_mulaw = message

        media_message = {
            "event": "media",
            "streamSid": streamsid,
            "media": {
                "payload":
                base64.b64encode(raw_mulaw).decode("ascii")
            }
        }

        await twilio_ws_send(
            json.dumps(media_message)
        )


# =========================================================
# TWILIO RECEIVER
# =========================================================

async def twilio_receiver(
    ws,
    audio_queue,
    streamsid_queue
):

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

                streamsid_queue.put_nowait(
                    streamsid
                )

            elif event == "connected":

                continue

            elif event == "media":

                media = data["media"]

                chunk = base64.b64decode(
                    media["payload"]
                )

                if media["track"] == "inbound":

                    inbuffer.extend(chunk)

            elif event == "stop":

                break

            while len(inbuffer) >= BUFFER_SIZE:

                chunk = inbuffer[:BUFFER_SIZE]

                audio_queue.put_nowait(chunk)

                inbuffer = inbuffer[BUFFER_SIZE:]

        except Exception as e:

            print(
                f"twilio_receiver error: {e}"
            )

            break


# =========================================================
# TWILIO WEBSOCKET
# =========================================================

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

        await sts_ws.send(
            json.dumps(config_message)
        )

        await asyncio.wait(
            [
                asyncio.ensure_future(
                    sts_sender(
                        sts_ws,
                        audio_queue
                    )
                ),

                asyncio.ensure_future(
                    sts_receiver(
                        sts_ws,
                        twilio_ws_send,
                        streamsid_queue
                    )
                ),

                asyncio.ensure_future(
                    twilio_receiver(
                        ws,
                        audio_queue,
                        streamsid_queue
                    )
                )
            ],
            return_when=asyncio.FIRST_COMPLETED
        )

    print("Twilio WebSocket closed")

    return ws


# =========================================================
# TWIML
# =========================================================

async def twiml_handler(request):

    ws_url = (
        f"{PUBLIC_BASE_URL.replace('https://', 'wss://').replace('http://', 'ws://')}"
        f"/media-stream"
    )

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}" />
    </Connect>
</Response>"""

    return web.Response(
        text=xml,
        content_type="text/xml"
    )


# =========================================================
# OUTBOUND CALL
# =========================================================

def trigger_outbound_call(to_number):

    twiml_url = f"{PUBLIC_BASE_URL}/twiml"

    call = twilio_client.calls.create(
        from_=TWILIO_FROM_NUMBER,
        to=to_number,
        url=twiml_url
    )

    print(
        f"Outbound call initiated: "
        f"{call.sid} -> {to_number}"
    )

    return call.sid


# =========================================================
# /MAKE-CALL
# =========================================================

async def make_call_handler(request):

    secret = request.headers.get(
        "X-API-Secret"
    )

    if MAKE_CALL_SECRET and secret != MAKE_CALL_SECRET:

        return web.json_response(
            {"error": "Unauthorized"},
            status=401
        )

    try:

        data = await request.json()

    except Exception:

        return web.json_response(
            {"error": "Invalid JSON"},
            status=400
        )

    to_number = data.get("to")

    if not to_number:

        return web.json_response(
            {"error": "Missing 'to' number"},
            status=400
        )

    try:

        call_sid = trigger_outbound_call(
            to_number
        )

        return web.json_response(
            {
                "status": "initiated",
                "call_sid": call_sid
            }
        )

    except Exception as e:

        print(
            f"make_call error: {e}"
        )

        return web.json_response(
            {
                "error": str(e)
            },
            status=500
        )


# =========================================================
# FACEBOOK WEBHOOK VERIFICATION
# =========================================================

async def fb_webhook_verify(request):

    mode = request.query.get(
        "hub.mode"
    )

    token = request.query.get(
        "hub.verify_token"
    )

    challenge = request.query.get(
        "hub.challenge"
    )

    print(
        f"Facebook webhook verification: "
        f"mode={mode}"
    )

    if (
        mode == "subscribe"
        and token == FB_VERIFY_TOKEN
    ):

        print(
            "FB webhook verified successfully"
        )

        return web.Response(
            text=challenge
        )

    print(
        "FB webhook verification failed"
    )

    return web.Response(
        status=403
    )


# =========================================================
# FACEBOOK LEAD WEBHOOK
# =========================================================

async def fb_webhook_receive(request):

    print(
        "========== FACEBOOK WEBHOOK =========="
    )

    try:

        payload = await request.json()

    except Exception as e:

        print(
            f"Could not parse Facebook JSON: {e}"
        )

        return web.Response(
            status=200
        )

    print(
        f"FB webhook payload: {payload}"
    )

    # Check Page Access Token
    if not FB_PAGE_ACCESS_TOKEN:

        print(
            "ERROR: FB_PAGE_ACCESS_TOKEN is missing!"
        )

        return web.Response(
            status=200
        )

    try:

        for entry in payload.get(
            "entry",
            []
        ):

            for change in entry.get(
                "changes",
                []
            ):

                if change.get("field") != "leadgen":

                    continue

                value = change.get(
                    "value",
                    {}
                )

                leadgen_id = value.get(
                    "leadgen_id"
                )

                if not leadgen_id:

                    print(
                        "ERROR: No leadgen_id received"
                    )

                    continue

                print(
                    f"Received leadgen_id: {leadgen_id}"
                )

                # -----------------------------------------
                # FETCH LEAD FROM FACEBOOK GRAPH API
                # -----------------------------------------

                lead_url = (
                    f"https://graph.facebook.com/v26.0/"
                    f"{leadgen_id}"
                )

                params = {
                    "access_token":
                    FB_PAGE_ACCESS_TOKEN,

                    "fields":
                    "id,created_time,field_data"
                }

                print(
                    "Requesting lead data from "
                    "Facebook Graph API..."
                )

                resp = requests.get(
                    lead_url,
                    params=params,
                    timeout=10
                )

                print(
                    f"Facebook Graph API status: "
                    f"{resp.status_code}"
                )

                print(
                    f"Facebook Graph API response: "
                    f"{resp.text}"
                )

                try:

                    lead_data = resp.json()

                except Exception:

                    print(
                        "ERROR: Facebook response "
                        "was not valid JSON"
                    )

                    continue

                # -----------------------------------------
                # GRAPH API ERROR
                # -----------------------------------------

                if "error" in lead_data:

                    print(
                        "ERROR fetching lead data:"
                    )

                    print(
                        lead_data["error"]
                    )

                    continue

                # -----------------------------------------
                # FIND PHONE NUMBER
                # -----------------------------------------

                phone_number = None

                for field in lead_data.get(
                    "field_data",
                    []
                ):

                    field_name = field.get(
                        "name"
                    )

                    field_values = field.get(
                        "values",
                        []
                    )

                    print(
                        f"Lead field: "
                        f"{field_name} = {field_values}"
                    )

                    if (
                        field_name
                        in (
                            "phone_number",
                            "phone"
                        )
                    ):

                        if field_values:

                            phone_number = (
                                field_values[0]
                            )

                            break

                # -----------------------------------------
                # CALL CUSTOMER
                # -----------------------------------------

                if phone_number:

                    print(
                        f"Phone number found: "
                        f"{phone_number}"
                    )

                    try:

                        trigger_outbound_call(
                            phone_number
                        )

                    except Exception as e:

                        print(
                            f"ERROR initiating "
                            f"Twilio call: {e}"
                        )

                else:

                    print(
                        "No phone number found "
                        "in lead."
                    )

                    print(
                        f"Complete lead data: "
                        f"{lead_data}"
                    )

    except Exception as e:

        print(
            f"fb_webhook_receive error: {e}"
        )

    # Always acknowledge Facebook
    return web.Response(
        status=200
    )


# =========================================================
# APP SETUP
# =========================================================

def create_app():

    app = web.Application()

    # Twilio
    app.router.add_get(
        "/media-stream",
        twilio_ws_handler
    )

    app.router.add_post(
        "/twiml",
        twiml_handler
    )

    app.router.add_get(
        "/twiml",
        twiml_handler
    )

    # Manual outbound call
    app.router.add_post(
        "/make-call",
        make_call_handler
    )

    # Facebook Lead Ads
    app.router.add_get(
        "/fb-webhook",
        fb_webhook_verify
    )

    app.router.add_post(
        "/fb-webhook",
        fb_webhook_receive
    )

    app.router.add_post(
        "/agent-think",
        agent_think_handler
    )

    return app


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    if not PUBLIC_BASE_URL:

        raise Exception(
            "PUBLIC_BASE_URL not set in .env "
            "(Railway HTTPS URL, no trailing slash)"
        )

    if not FB_PAGE_ACCESS_TOKEN:

        print(
            "WARNING: FB_PAGE_ACCESS_TOKEN "
            "is not set!"
        )

    app = create_app()

    port = int(
        os.getenv(
            "PORT",
            8080
        )
    )

    print(
        f"Starting server on port {port}. "
        f"Public URL: {PUBLIC_BASE_URL}"
    )

    web.run_app(
        app,
        host="0.0.0.0",
        port=port
    )