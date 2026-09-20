import os
import json
import base64
import hmac
import hashlib
import uuid
import logging

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool
from dotenv import load_dotenv

from whatsapp_sender import send_text_message, send_document_bytes_message
from rate_limiter import is_rate_limited
from rag import search_knowledge_base
from payments import verify_webhook_signature
from payments_store import get_payment_link, get_payment_link_by_order_name, try_claim, log_event
from notifications import notify
from human_takeover import get_conversation_by_id, log_message_for_conversation
from job_queue import enqueue_job
from redis_client import redis_client
from odoo_connections import get_odoo_connection, get_whatsapp_config, get_ai_config
from companies import COMPANIES
from whatsapp_processor import (
    process_whatsapp_message, process_broadcast_job,
    mark_order_paid, get_invoice_pdf, client,
)

load_dotenv()

logging.basicConfig(
    filename="app.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI()

MESSAGE_DEDUP_TTL_SECONDS = 86400  # 24h — covers Meta's retry window


def is_duplicate_message(phone_number_id, message_id):
    key = f"processed:{phone_number_id}:{message_id}"
    was_set = redis_client.set(key, "1", nx=True, ex=MESSAGE_DEDUP_TTL_SECONDS)
    return not was_set  # True if the key already existed


@app.get("/webhook")
def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode != "subscribe" or not token:
        return {"error": "verification failed"}

    for phone_number_id in COMPANIES:
        try:
            conn = get_odoo_connection(phone_number_id)
            match = conn["models"].execute_kw(
                conn["database"], conn["uid"], conn["api_key"],
                "whatsapp.config", "find_config_by_verify_token",
                [token]
            )
            if match:
                return int(challenge)
        except Exception as e:
            logger.error("Verify-token check failed for %s: %r", phone_number_id, e)

    return {"error": "verification failed"}


INTERNAL_SHARED_SECRET = os.environ.get("INTERNAL_SHARED_SECRET")


@app.post("/internal/conversations/{conversation_id}/reply")
async def employee_reply(conversation_id: int, request: Request):
    if not INTERNAL_SHARED_SECRET or request.headers.get("X-Internal-Secret") != INTERNAL_SHARED_SECRET:
        return {"status": "unauthorized"}

    payload = await request.json()
    phone_number_id = payload["phone_number_id"]
    body = payload["body"]

    try:
        convo = await run_in_threadpool(get_conversation_by_id, phone_number_id, conversation_id)
    except Exception as e:
        logger.error("get_conversation_by_id failed for conversation %s: %r", conversation_id, e)
        return {"status": "lookup_failed"}

    if not convo:
        return {"status": "not_found"}

    try:
        await run_in_threadpool(send_text_message, convo["partner_phone"], body, phone_number_id)
    except Exception as e:
        logger.error("Employee reply send failed for conversation %s: %r", conversation_id, e)
        return {"status": "send_failed"}

    try:
        await run_in_threadpool(
            log_message_for_conversation, phone_number_id, conversation_id, "outbound", "employee", body
        )
    except Exception as e:
        logger.error("log_message_for_conversation failed for conversation %s: %r", conversation_id, e)

    return {"status": "ok"}


async def test_ai_config(request: Request):
    if not INTERNAL_SHARED_SECRET or request.headers.get("X-Internal-Secret") != INTERNAL_SHARED_SECRET:
        return {"status": "unauthorized"}

    payload = await request.json()
    phone_number_id = payload["phone_number_id"]

    try:
        ai_config = await run_in_threadpool(get_ai_config, phone_number_id, True)
        response = await run_in_threadpool(
            client.chat.completions.create,
            model=ai_config["chat_model"],
            messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            max_tokens=5,
        )
        return {
            "status": "ok",
            "model": ai_config["chat_model"],
            "reply": response.choices[0].message.content,
        }
    except Exception as e:
        logger.error("AI config test failed for %s: %r", phone_number_id, e)
        return {"status": "error", "error": str(e)}


@app.post("/internal/knowledge/test")
async def test_knowledge(request: Request):
    if not INTERNAL_SHARED_SECRET or request.headers.get("X-Internal-Secret") != INTERNAL_SHARED_SECRET:
        return {"status": "unauthorized"}

    payload = await request.json()
    phone_number_id = payload["phone_number_id"]
    query = payload["query"]

    try:
        result = await run_in_threadpool(search_knowledge_base, query, phone_number_id)
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error("Knowledge test failed for %s: %r", phone_number_id, e)
        return {"status": "error", "error": str(e)}


@app.post("/internal/broadcast/send")
async def broadcast_send(request: Request):
    if not INTERNAL_SHARED_SECRET or request.headers.get("X-Internal-Secret") != INTERNAL_SHARED_SECRET:
        return {"status": "unauthorized"}

    payload = await request.json()
    phone_number_id = payload["phone_number_id"]
    recipients = payload.get("recipients", [])

    job = {
        "id": str(uuid.uuid4()),
        "type": "broadcast",
        "phone_number_id": phone_number_id,
        "recipients": recipients,
    }
    await run_in_threadpool(enqueue_job, job)

    return {"status": "queued", "job_id": job["id"], "recipient_count": len(recipients)}


def verify_signature(payload_body, signature_header, phone_number_id):
    logger.info(
        "verify_signature: phone_number_id=%s body_len=%d header_present=%s",
        phone_number_id, len(payload_body), bool(signature_header)
    )

    if not signature_header:
        logger.warning("verify_signature: no X-Hub-Signature-256 header on request")
        return False

    config = get_whatsapp_config(phone_number_id)
    app_secret = config.get("whatsapp_app_secret")
    print("DEBUG APP SECRET PRESENT:", bool(app_secret))
    print("DEBUG APP SECRET LENGTH:", len(app_secret) if app_secret else 0)
    logger.info(
        "verify_signature: app_secret_present=%s app_secret_len=%s",
        bool(app_secret), len(app_secret) if app_secret else 0
    )
    if not app_secret:
        logger.warning("verify_signature: no whatsapp_app_secret for phone_number_id=%s", phone_number_id)
        return False

    expected = hmac.new(
        app_secret.encode(), payload_body, hashlib.sha256
    ).hexdigest()

    received = signature_header.replace("sha256=", "")

    logger.info(
        "verify_signature: expected_prefix=%s received_prefix=%s expected_len=%d received_len=%d match=%s",
        expected[:8], received[:8], len(expected), len(received),
        hmac.compare_digest(expected, received)
    )

    return hmac.compare_digest(expected, received)


@app.post("/webhook")
async def webhook(request: Request):
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    try:
        body_peek = json.loads(raw_body)
        phone_number_id = body_peek["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"]
    except (KeyError, IndexError, ValueError):
        return {"status": "ignored"}

    try:
        signature_ok = verify_signature(raw_body, signature, phone_number_id)
    except Exception as e:
        print("SIGNATURE VERIFICATION ERROR:", repr(e))
        signature_ok = False

    if not signature_ok:
        print("SIGNATURE MISMATCH — rejecting request")
        return {"status": "unauthorized"}

    body = json.loads(raw_body)
    print("INCOMING:", body)

    try:
        entry = body["entry"][0]
        change = entry["changes"][0]["value"]
        message = change["messages"][0]
        user_id = message["from"]
        message_id = message.get("id")
        phone_number_id = change["metadata"]["phone_number_id"]
    except (KeyError, IndexError):
        return {"status": "ignored"}

    if message["type"] not in ("text", "audio", "image", "document"):
        return {"status": "ignored"}

    if is_duplicate_message(phone_number_id, message_id):
        print("DUPLICATE, skipping:", message_id)
        return {"status": "duplicate"}

    whatsapp_config = get_whatsapp_config(phone_number_id)
    rate_limit_key = f"{phone_number_id}:{user_id}"
    if is_rate_limited(
        rate_limit_key,
        whatsapp_config.get("rate_limit_window_seconds", 60),
        whatsapp_config.get("rate_limit_max_requests", 10)
    ):
        print("RATE LIMITED:", rate_limit_key)
        await run_in_threadpool(
            send_text_message, user_id,
            "You're sending messages too quickly — please wait a moment.",
            phone_number_id
        )
        return {"status": "rate_limited"}

    job = {
        "id": str(uuid.uuid4()),
        "type": "whatsapp_message",
        "phone_number_id": phone_number_id,
        "user_id": user_id,
        "message_id": message_id,
        "message": message,
    }
    await run_in_threadpool(enqueue_job, job)

    return {"status": "queued"}


@app.post("/webhook/razorpay/{phone_number_id}")
async def razorpay_webhook(phone_number_id: str, request: Request):
    """
    Razorpay Payment Links webhook. Each company registers its own webhook
    URL (this endpoint, with its own phone_number_id) in its Razorpay
    dashboard, using that company's own webhook secret — this is what lets
    verify_webhook_signature look up the right secret before trusting the
    payload.
    """
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature")

    try:
        signature_ok = verify_webhook_signature(phone_number_id, raw_body, signature)
    except Exception as e:
        logger.error("Razorpay signature verification crashed: %r", e)
        signature_ok = False

    if not signature_ok:
        logger.warning("Razorpay webhook signature mismatch for %s", phone_number_id)
        return {"status": "unauthorized"}

    body = await request.json()
    event = body.get("event")

    if event not in ("payment_link.paid", "payment_link.expired", "payment.failed"):
        return {"status": "ignored"}

    payment_link_id = None
    try:
        payment_link_id = body["payload"]["payment_link"]["entity"]["id"]
    except (KeyError, IndexError):
        pass

    if payment_link_id:
        record = get_payment_link(payment_link_id)
    else:
        try:
            order_reference = body["payload"]["payment"]["entity"]["notes"].get("order_reference")
        except (KeyError, IndexError, AttributeError):
            order_reference = None

        if not order_reference:
            logger.error("Malformed Razorpay webhook payload (no payment_link id or notes): %r", body)
            return {"status": "ignored"}

        record = get_payment_link_by_order_name(order_reference)
        if record:
            payment_link_id = record["payment_link_id"]

    if not record:
        logger.error("No local record for Razorpay event: %r", body.get("payload"))
        log_event(payment_link_id, event, "unknown_payment_link", body)
        return {"status": "unknown_payment_link"}

    if event == "payment_link.expired":
        claimed = try_claim(payment_link_id, "expired", valid_from_statuses=("pending",))
        log_event(payment_link_id, event, "claimed" if claimed else "duplicate", body)

        if not claimed:
            print("DUPLICATE/RACE PAYMENT WEBHOOK, skipping:", payment_link_id)
            return {"status": "duplicate"}

        logger.warning(
            "Payment link expired unpaid for order %s (%s) — customer never completed payment.",
            record["order_name"], payment_link_id
        )
        try:
            await run_in_threadpool(
                notify, "payment_expired", record["user_id"], record["phone_number_id"],
                {"order_name": record["order_name"]}
            )
        except Exception as e:
            logger.error("send_text_message failed for %s: %r", record["user_id"], e)
        return {"status": "ok"}

    if event == "payment.failed":
        try:
            payment_entity = body["payload"]["payment"]["entity"]
            error_reason = payment_entity.get("error_description") or "unknown reason"
        except (KeyError, IndexError):
            error_reason = "unknown reason"

        log_event(payment_link_id, event, "attempt_failed", body)

        if record["status"] != "pending":
            return {"status": "ignored"}

        logger.warning(
            "Payment attempt failed for order %s (%s): %s",
            record["order_name"], payment_link_id, error_reason
        )
        try:
            await run_in_threadpool(
                notify, "payment_failed", record["user_id"], record["phone_number_id"],
                {"order_name": record["order_name"], "error_reason": error_reason}
            )
        except Exception as e:
            logger.error("send_text_message failed for %s: %r", record["user_id"], e)
        return {"status": "ok"}

    try:
        payment_entity = body["payload"]["payment"]["entity"]
        payment_reference = payment_entity["id"]
        amount_paid = payment_entity["amount"] / 100
    except (KeyError, IndexError):
        logger.error("Malformed Razorpay webhook payload: %r", body)
        log_event(payment_link_id, event, "malformed_payload", body)
        return {"status": "ignored"}

    claimed = try_claim(
        payment_link_id, "paid",
        payment_reference=payment_reference, amount_paid=amount_paid,
        valid_from_statuses=("pending",)
    )
    log_event(payment_link_id, event, "claimed" if claimed else "duplicate", body)

    if not claimed:
        print("DUPLICATE PAYMENT WEBHOOK, skipping:", payment_link_id)
        return {"status": "duplicate"}

    try:
        result = await run_in_threadpool(
            mark_order_paid, record["order_id"], payment_reference, amount_paid, phone_number_id
        )
    except Exception as e:
        logger.error("mark_order_paid failed for order %s: %r", record["order_id"], e)
        return {"status": "error"}

    if result.get("stock_issue"):
        logger.warning(
            "Order %s paid but couldn't be confirmed due to insufficient stock — needs manual refund review.",
            result.get("order_name")
        )
        try:
            await run_in_threadpool(
                notify, "payment_stock_issue", record["user_id"], record["phone_number_id"],
                {"order_name": result.get("order_name")}
            )
        except Exception as e:
            logger.error("send_text_message failed for %s: %r", record["user_id"], e)
        return {"status": "ok"}

    if result.get("found"):
        try:
            await run_in_threadpool(
                notify, "payment_confirmation", record["user_id"], record["phone_number_id"],
                {"order_name": result.get("order_name")}
            )
        except Exception as e:
            logger.error("send_text_message failed for %s: %r", record["user_id"], e)

        if result.get("invoice_created") and result.get("invoice_name"):
            try:
                await run_in_threadpool(
                    notify, "invoice_notification", record["user_id"], record["phone_number_id"],
                    {"order_name": result.get("order_name"), "invoice_name": result.get("invoice_name")}
                )
            except Exception as e:
                logger.error("Invoice notification failed for %s: %r", record["user_id"], e)

            try:
                pdf_result = await run_in_threadpool(
                    get_invoice_pdf, record["order_id"], record["phone_number_id"]
                )
                if pdf_result.get("found") and pdf_result.get("pdf_base64"):
                    pdf_bytes = base64.b64decode(pdf_result["pdf_base64"])
                    await run_in_threadpool(
                        send_document_bytes_message,
                        record["user_id"], pdf_bytes, record["phone_number_id"],
                        f"{pdf_result.get('invoice_name', result.get('invoice_name'))}.pdf",
                    )
                else:
                    logger.error("Invoice PDF not found for order %s", record["order_id"])
            except Exception as e:
                logger.error("Invoice PDF send failed for %s: %r", record["user_id"], e)

    return {"status": "ok"}