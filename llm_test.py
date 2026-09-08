import os
import re
import json
from groq import Groq
import base64
from pydantic import BaseModel
import xmlrpc.client
from fastapi import FastAPI
from conversation_store import load_history, save_history
from fastapi import Request
from pypdf import PdfReader
from whatsapp_sender import send_text_message, send_template_message, send_image_message, send_image_bytes_message, send_document_bytes_message
import hmac
import hashlib
from rate_limiter import is_rate_limited
import logging
from rag import search_knowledge_base
from pending_actions import set_pending, get_pending, clear_pending
from starlette.concurrency import run_in_threadpool
from whatsapp_media import download_media
from payments import create_payment_link, verify_webhook_signature
from payments_store import save_payment_link, get_payment_link, get_payment_link_by_order_name, try_claim, log_event
from notifications import notify
from human_takeover import (
    get_conversation_state, request_handoff, log_message, contains_handoff_keyword,
    get_conversation_by_id, log_message_for_conversation,
)
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    filename="app.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI()

from odoo_connections import get_odoo_connection, get_whatsapp_config, get_ai_config
from companies import COMPANIES

processed_message_ids = set()


@app.get("/webhook")
def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode != "subscribe" or not token:
        return {"error": "verification failed"}

    # Meta sends no phone_number_id on this handshake, so check the token
    # against every bootstrapped company until one matches.
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
    phone_number_id = payload["phone_number_id"]  # needed to pick the right Odoo connection
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

    results = []
    for r in recipients:
        recipient_id = r.get("recipient_id")
        try:
            if r.get("header_type") == "image" and r.get("header_image_url") and r.get("send_mode") == "text":
                # Header images have no free-text equivalent, so send them as a
                # preceding image message to keep visual parity with the
                # Meta-template path.
                await run_in_threadpool(
                    send_image_message, r["phone"], r["header_image_url"], phone_number_id
                )

            if r.get("send_mode") == "text":
                response = await run_in_threadpool(
                    send_text_message, r["phone"], r["text_body"], phone_number_id
                )
            else:
                response = await run_in_threadpool(
                    send_template_message,
                    r["phone"], r["meta_template_name"], r["meta_template_language"],
                    r.get("body_params", []), phone_number_id,
                    header_image_url=r.get("header_image_url") if r.get("header_type") == "image" else None,
                )

            whatsapp_message_id = None
            try:
                whatsapp_message_id = response.get("messages", [{}])[0].get("id")
            except Exception:
                pass

            if isinstance(response, dict) and "error" in response:
                results.append({
                    "recipient_id": recipient_id, "sent": False,
                    "error": str(response["error"]),
                })
            else:
                results.append({
                    "recipient_id": recipient_id, "sent": True,
                    "whatsapp_message_id": whatsapp_message_id,
                })
        except Exception as e:
            logger.error("Broadcast send failed for recipient %s: %r", recipient_id, e)
            results.append({"recipient_id": recipient_id, "sent": False, "error": str(e)})

    return {"status": "ok", "results": results}


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
        logger.error("Signature verification crashed: %r", e)
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

        if message["type"] == "text":
            user_message = message["text"]["body"]
        elif message["type"] == "audio":
            try:
                media_id = message["audio"]["id"]
                audio_path = await run_in_threadpool(download_media, media_id, phone_number_id, "ogg")
                user_message = await run_in_threadpool(transcribe_audio, audio_path, phone_number_id)
                print("TRANSCRIBED:", user_message)
            except Exception as e:
                logger.error("Audio transcription failed: %r", e)
                await run_in_threadpool(
                    send_text_message, user_id,
                    "Sorry, I couldn't process that voice message — could you try typing it instead?",
                    phone_number_id
                )
                return {"status": "ok"}
        elif message["type"] == "image":
            try:
                media_id = message["image"]["id"]
                caption = message["image"].get("caption", "")
                image_path = await run_in_threadpool(download_media, media_id, phone_number_id, "jpg")
                description = await run_in_threadpool(describe_image, image_path, phone_number_id, caption)
                user_message = f"[Customer sent an image] {description}"
                print("IMAGE DESCRIBED:", description)
            except Exception as e:
                logger.error("Image description failed: %r", e)
                await run_in_threadpool(
                    send_text_message, user_id,
                    "Sorry, I couldn't process that image right now — could you describe what you're looking for instead?",
                    phone_number_id
                )
                return {"status": "ok"}
        elif message["type"] == "document":
            try:
                media_id = message["document"]["id"]
                filename = message["document"].get("filename", "")
                mime_type = message["document"].get("mime_type", "")

                if mime_type != "application/pdf":
                    await run_in_threadpool(
                        send_text_message, user_id,
                        "Sorry, I can only read PDF documents right now.",
                        phone_number_id
                    )
                    return {"status": "ok"}

                doc_path = await run_in_threadpool(download_media, media_id, phone_number_id, "pdf")
                extracted_text = await run_in_threadpool(extract_pdf_text, doc_path, phone_number_id)

                if not extracted_text:
                    await run_in_threadpool(
                        send_text_message, user_id,
                        "I couldn't read any text from that PDF — it might be a scanned image rather than a text document.",
                        phone_number_id
                    )
                    return {"status": "ok"}

                user_message = f"[Customer sent a document: {filename}] {extracted_text}"
                print("DOCUMENT EXTRACTED:", extracted_text[:200])
            except Exception as e:
                logger.error("Document processing failed: %r", e)
                await run_in_threadpool(
                    send_text_message, user_id,
                    "Sorry, I couldn't process that document right now.",
                    phone_number_id
                )
                return {"status": "ok"}
        else:
            return {"status": "ignored"}
    except (KeyError, IndexError):
        return {"status": "ignored"}

    if message_id in processed_message_ids:
        print("DUPLICATE, skipping:", message_id)
        return {"status": "duplicate"}
    processed_message_ids.add(message_id)

    whatsapp_config = get_whatsapp_config(phone_number_id)
    if is_rate_limited(
        user_id,
        whatsapp_config.get("rate_limit_window_seconds", 60),
        whatsapp_config.get("rate_limit_max_requests", 10)
    ):
        print("RATE LIMITED:", user_id)
        send_text_message(user_id, "You're sending messages too quickly — please wait a moment.", phone_number_id)
        return {"status": "rate_limited"}

    ai_config = get_ai_config(phone_number_id)

    try:
        convo_state = await run_in_threadpool(get_conversation_state, phone_number_id, user_id)
    except Exception as e:
        logger.error("get_conversation_state failed for %s: %r", user_id, e)
        convo_state = {"state": "ai_handling"}

    if convo_state["state"] != "ai_handling":
        # A human already owns this conversation — log it for the inbox and
        # stop. The AI never sees this message, so it can't reply on top of
        # (or instead of) the employee.
        try:
            await run_in_threadpool(
                log_message, phone_number_id, user_id, "inbound", "customer", user_message, message_id
            )
        except Exception as e:
            logger.error("log_message failed for %s: %r", user_id, e)
        return {"status": "human_handling"}

    if contains_handoff_keyword(user_message, ai_config["handoff_keywords"]):
        try:
            await run_in_threadpool(request_handoff, phone_number_id, user_id, "Customer asked for a human")
            await run_in_threadpool(
                log_message, phone_number_id, user_id, "inbound", "customer", user_message, message_id
            )
            await run_in_threadpool(
                send_text_message, user_id,
                "Got it — connecting you with a team member now. They'll be with you shortly.",
                phone_number_id
            )
        except Exception as e:
            logger.error("Handoff failed for %s: %r", user_id, e)
        return {"status": "handed_off"}

    if not load_history(user_id, phone_number_id):
        try:
            await run_in_threadpool(notify, "welcome", user_id, phone_number_id, {})
        except Exception as e:
            logger.error("Welcome notification failed for %s: %r", user_id, e)

    pending = get_pending(user_id)
    if pending:
        confirm_words = ai_config["confirm_keywords"]
        cancel_words = ai_config["cancel_keywords"]
        lowered = user_message.strip().lower()
        word_count = len(lowered.split())

        if word_count <= ai_config["confirm_max_words"] and any(w in lowered for w in confirm_words):
            arguments = dict(pending)
            arguments["phone_number_id"] = phone_number_id
            result = await run_in_threadpool(create_quotation, **arguments)
            clear_pending(user_id)

            if result.get("created"):
                order_id = result.get("order_id")
                order_name = result.get("order_name")
                amount_total = result.get("amount_total")
                currency = result.get("currency")
                product_name = result.get("product_name")
                quantity = result.get("quantity")

                try:
                    link = await run_in_threadpool(
                        create_payment_link,
                        phone_number_id,
                        amount_total,
                        currency,
                        f"WhatsApp Customer {user_id}",
                        user_id,
                        f"Order {order_name}: {quantity} x {product_name}",
                        order_name,
                    )
                    await run_in_threadpool(
                        save_payment_link,
                        link["payment_link_id"], phone_number_id, user_id, order_id, order_name
                    )
                    context = {
                        "order_name": order_name, "quantity": quantity,
                        "product_name": product_name, "amount_total": amount_total,
                        "currency": currency, "payment_link": link["short_url"],
                    }
                    try:
                        await run_in_threadpool(notify, "order_confirmed", user_id, phone_number_id, context)
                    except Exception as e:
                        logger.error("send_text_message failed for %s: %r", user_id, e)
                except Exception as e:
                    logger.error("Payment link creation failed for order %s: %r", order_name, e)
                    try:
                        await run_in_threadpool(
                            log_order_note, order_id,
                            f"Razorpay payment link creation failed: {e}. Customer was told the "
                            f"team would follow up — needs a manual payment link or alternate "
                            f"payment method.",
                            phone_number_id
                        )
                    except Exception as note_err:
                        logger.error("Failed to log order note for %s: %r", order_name, note_err)
                    context = {
                        "order_name": order_name, "quantity": quantity,
                        "product_name": product_name, "amount_total": amount_total,
                        "currency": currency,
                    }
                    try:
                        await run_in_threadpool(notify, "order_confirmed_no_link", user_id, phone_number_id, context)
                    except Exception as e:
                        logger.error("send_text_message failed for %s: %r", user_id, e)
            else:
                try:
                    await run_in_threadpool(
                        send_text_message, user_id,
                        "Sorry, I couldn't create that order — please try again.",
                        phone_number_id
                    )
                except Exception as e:
                    logger.error("send_text_message failed for %s: %r", user_id, e)
            return {"status": "ok"}

        if word_count <= ai_config["confirm_max_words"] and any(w in lowered for w in cancel_words):
            clear_pending(user_id)
            reply = "No problem, order cancelled. Let me know if you'd like anything else!"
            try:
                await run_in_threadpool(send_text_message, user_id, reply, phone_number_id)
            except Exception as e:
                logger.error("send_text_message failed for %s: %r", user_id, e)
            return {"status": "ok"}

    # --- Deterministic image handling ---
    # The LLM sometimes answers "no image available" without ever calling
    # get_product_details, even with a tool description that mentions photos.
    # Rather than depend on the model's judgment for this specific case, detect
    # an image request directly and fetch/send the photo in code — same spirit
    # as the confirm/cancel keyword handling above.
    IMAGE_KEYWORDS = ("image", "photo", "picture", "pic", "pics", "photos", "images")
    IMAGE_REQUEST_FILLER = {
        "provide", "give", "send", "share", "want", "need", "plz", "please",
        "pls", "of", "the", "a", "an", "me", "my", "ur", "your", "you", "can",
        "could", "i", "to", "see", "show", "us", "that", "this",
        "image", "photo", "picture", "pic", "pics", "photos", "images",
    }
    lowered_msg = user_message.strip().lower()
    if any(kw in lowered_msg for kw in IMAGE_KEYWORDS):
        candidate_words = [
            w for w in re.findall(r"[a-zA-Z]+", lowered_msg) if w not in IMAGE_REQUEST_FILLER
        ]
        candidate_name = " ".join(candidate_words).strip()

        if not candidate_name:
            # No product named in this message — fall back to the last product
            # actually confirmed via a tool call earlier in this conversation.
            history = load_history(user_id, phone_number_id)
            for hist_msg in reversed(history):
                if hist_msg.get("role") != "tool":
                    continue
                try:
                    tool_result = json.loads(hist_msg["content"])
                except (TypeError, ValueError):
                    continue
                if tool_result.get("found") and tool_result.get("product_name"):
                    candidate_name = tool_result["product_name"]
                    break

        if candidate_name:
            try:
                details = await run_in_threadpool(get_product_details, candidate_name, phone_number_id)
            except Exception as e:
                logger.error("Deterministic get_product_details failed for %r: %r", candidate_name, e)
                details = {}

            logger.info(
                "Deterministic image check: candidate=%r found=%s has_image=%s",
                candidate_name, details.get("found"), details.get("has_image"),
            )

            if details.get("found") and details.get("has_image") and details.get("image_base64"):
                try:
                    image_bytes = base64.b64decode(details["image_base64"])
                    await run_in_threadpool(
                        send_image_bytes_message, user_id, image_bytes, phone_number_id,
                        caption=f"📸 {details['product_name']}"
                    )
                    await run_in_threadpool(
                        log_message, phone_number_id, user_id, "inbound", "customer", user_message, message_id
                    )
                    await run_in_threadpool(
                        log_message, phone_number_id, user_id, "outbound", "ai",
                        f"Sent photo of {details['product_name']}"
                    )
                    logger.info("Deterministic image send succeeded for %s (%s)", user_id, details["product_name"])
                    return {"status": "ok"}
                except Exception as e:
                    logger.error("Deterministic image send failed for %s: %r", user_id, e)
                    # fall through to the normal AI flow below on failure

    try:
        await run_in_threadpool(
            log_message, phone_number_id, user_id, "inbound", "customer", user_message, message_id
        )
    except Exception as e:
        logger.error("log_message failed for %s: %r", user_id, e)

    try:
        reply, product_image_base64 = await run_in_threadpool(run_agent, user_id, user_message, phone_number_id)
    except Exception as e:
        logger.error("run_agent failed for %s: %r", user_id, e)
        reply, product_image_base64 = "Sorry, something went wrong on our end. Please try again in a moment.", None

    if product_image_base64:
        try:
            image_bytes = base64.b64decode(product_image_base64)
            await run_in_threadpool(send_image_bytes_message, user_id, image_bytes, phone_number_id)
        except Exception as e:
            logger.error("Product image send failed for %s: %r", user_id, e)

    try:
        await run_in_threadpool(send_text_message, user_id, reply, phone_number_id)
    except Exception as e:
        logger.error("send_text_message failed for %s: %r", user_id, e)
    try:
        await run_in_threadpool(log_message, phone_number_id, user_id, "outbound", "ai", reply)
    except Exception as e:
        logger.error("log_message failed for %s: %r", user_id, e)

    return {"status": "ok"}


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
        # payment.failed doesn't include a payment_link object — recover our
        # reference from the notes we attach at link-creation time instead.
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


client = Groq(
    api_key=os.environ.get("GROQ_API_KEY")
)

def transcribe_audio(file_path, phone_number_id):
    ai_config = get_ai_config(phone_number_id)
    try:
        with open(file_path, "rb") as f:
            transcription = client.audio.transcriptions.create(
                file=f,
                model=ai_config["speech_model"]
            )
        return transcription.text
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

import re


def describe_image(file_path, phone_number_id, caption=""):
    ai_config = get_ai_config(phone_number_id)
    try:
        with open(file_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")

        response = client.chat.completions.create(
            model=ai_config["vision_model"],
            reasoning_effort="none",
            reasoning_format="hidden",
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Describe what's in this image in one or two sentences, "
                            "focused on anything relevant to a retail/sales conversation "
                            "(e.g. a product, a receipt, a damaged item). "
                            + (f"Customer's caption: {caption}" if caption else "")
                        )
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}
                    }
                ]
            }]
        )
        raw_text = response.choices[0].message.content
        clean_text = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip()

        if not clean_text or "<think>" in clean_text:
            raise ValueError("Vision model returned incomplete or unparseable output")

        return clean_text
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


class OrderSummary(BaseModel):
    product_name: str
    quantity: int
    unit_price: float
    total_price: float
    currency: str


def generate_order_summary(product_name, quantity, unit_price, currency):
    total_price = round(unit_price * quantity, 2)

    response = client.chat.completions.create(
        model="openai/gpt-oss-20b",
        messages=[{
            "role": "user",
            "content": (
                f"Product: {product_name}, Quantity: {quantity}, "
                f"Unit price: {unit_price} {currency}, Total: {total_price} {currency}. "
                "Return this as structured data."
            )
        }],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "order_summary",
                "schema": OrderSummary.model_json_schema()
            }
        }
    )

    return OrderSummary.model_validate_json(response.choices[0].message.content)


def extract_pdf_text(file_path, phone_number_id):
    ai_config = get_ai_config(phone_number_id)
    max_chars = ai_config["pdf_extract_max_chars"]

    try:
        reader = PdfReader(file_path)
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""

        text = text.strip()

        if not text:
            return None

        return text[:max_chars]
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


def search_product(product_name, quantity, phone_number_id=None):
    conn = get_odoo_connection(phone_number_id)
    return conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.bridge", "search_product",
        [product_name, quantity]
    )


def get_product_details(product_name, phone_number_id=None):
    conn = get_odoo_connection(phone_number_id)
    return conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.bridge", "get_product_details",
        [product_name]
    )


def check_stock(product_id, phone_number_id=None):
    conn = get_odoo_connection(phone_number_id)
    return conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.bridge", "check_stock",
        [product_id]
    )


def search_products_multi(query, limit=5, phone_number_id=None):
    conn = get_odoo_connection(phone_number_id)
    return conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.bridge", "search_products_multi",
        [query, limit]
    )


def list_available_products(limit=20, phone_number_id=None):
    conn = get_odoo_connection(phone_number_id)
    return conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.bridge", "list_available_products",
        [limit]
    )

def knowledge_base_search(query, phone_number_id=None):
    return search_knowledge_base(query, phone_number_id=phone_number_id)

def create_quotation(product_id, quantity, phone_number=None, phone_number_id=None):
    conn = get_odoo_connection(phone_number_id)
    return conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.bridge", "create_quotation",
        [phone_number, product_id, quantity]
    )


def get_customer_orders(phone_number=None, limit=5, phone_number_id=None):
    conn = get_odoo_connection(phone_number_id)
    return conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.bridge", "get_customer_orders",
        [phone_number, limit]
    )


def find_customer(phone_number=None, phone_number_id=None):
    conn = get_odoo_connection(phone_number_id)
    return conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.bridge", "find_customer",
        [phone_number]
    )


def get_order_status(phone_number=None, order_name=None, phone_number_id=None):
    conn = get_odoo_connection(phone_number_id)
    return conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.bridge", "get_order_status",
        [phone_number, order_name]
    )


def mark_order_paid(order_id, payment_reference, amount_paid, phone_number_id=None):
    conn = get_odoo_connection(phone_number_id)
    return conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.bridge", "mark_order_paid",
        [order_id, payment_reference, amount_paid]
    )


def log_order_note(order_id, note, phone_number_id=None):
    conn = get_odoo_connection(phone_number_id)
    return conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.bridge", "log_order_note",
        [order_id, note]
    )

def get_invoice_pdf(order_id, phone_number_id=None):
    conn = get_odoo_connection(phone_number_id)
    return conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.bridge", "get_invoice_pdf",
        [order_id]
    )

def send_invoice_pdf(phone_number=None, order_name=None, phone_number_id=None):
    conn = get_odoo_connection(phone_number_id)
    result = conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.bridge", "get_invoice_pdf_by_order",
        [phone_number, order_name]
    )
    if not result.get("found") or not result.get("pdf_base64"):
        return result

    pdf_bytes = base64.b64decode(result["pdf_base64"])
    send_document_bytes_message(
        phone_number, pdf_bytes, phone_number_id, f"{result['invoice_name']}.pdf"
    )
    return {"found": True, "sent": True, "order_name": result["order_name"], "invoice_name": result["invoice_name"]}

TOOL_FUNCTIONS = {
    "search_product": search_product,
    "get_product_details": get_product_details,
    "check_stock": check_stock,
    "search_products_multi": search_products_multi,
    "create_quotation": create_quotation,
    "get_customer_orders": get_customer_orders,
    "find_customer": find_customer,
    "get_order_status": get_order_status,
    "list_available_products": list_available_products,
    "knowledge_base_search": knowledge_base_search,
    "send_invoice_pdf": send_invoice_pdf,

}


tools = [
    {
        "type": "function",
        "function": {
            "name": "search_product",
            "description": "Search Odoo for a product and check its available quantity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_name": {"type": "string", "description": "Name of the product"},
                    "quantity": {"type": "integer", "description": "Number of units the customer wants"}
                },
                "required": ["product_name", "quantity"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_product_details",
            "description": "Get the price, description, and photo (if available) of a specific product from Odoo. Call this whenever the customer asks for details or a photo/image of a product.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_name": {"type": "string", "description": "Name of the product"}
                },
                "required": ["product_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_stock",
            "description": "Check current and forecasted stock for a product by its Odoo product ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "integer", "description": "Odoo product.product ID"}
                },
                "required": ["product_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_products_multi",
            "description": "Search for multiple products matching a name, e.g. when the customer's request is ambiguous ('chairs' matching several products).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search term"},
                    "limit": {"type": "integer", "description": "Max results to return, default 5"}
                },
                "required": ["query"]
            }
        }
    }
    ,
    {
        "type": "function",
        "function": {
            "name": "create_quotation",
            "description": "Create a sales quotation in Odoo for a product and quantity the customer wants to order.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "integer", "description": "Odoo product.product ID"},
                    "quantity": {"type": "integer", "description": "Number of units to order"}
                },
                "required": ["product_id", "quantity"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_customer_orders",
            "description": "Look up the customer's past orders.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max orders to return, default 5"}
                },
                "required": []
            }
        }
    }
    ,
    {
        "type": "function",
        "function": {
            "name": "find_customer",
            "description": "Look up whether the current WhatsApp user is an existing customer.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_order_status",
            "description": "Check the status of the customer's order. If order_name is not given, returns their most recent order.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_name": {"type": "string", "description": "Odoo order name like S00040, optional"}
                },
                "required": []
            }
        }
    }
    ,
    {
        "type": "function",
        "function": {
            "name": "list_available_products",
            "description": "List currently available products when the customer is browsing rather than naming a specific item (e.g. 'what do you have', 'show me what's available', 'what else do you have'). Always call this instead of answering from memory or earlier conversation — stock changes constantly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max products to return, default 20"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "knowledge_base_search",
            "description": "Search store policies (returns, shipping, hours) and product descriptions for questions the other tools can't answer directly — e.g. 'what's your return policy', 'do you ship internationally', 'do you have something comfortable to sit on'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The customer's question, in their own words"}
                },
                "required": ["query"]
            }
        }
    },
{
    "type": "function",
    "function": {
        "name": "send_invoice_pdf",
        "description": "Sends the customer their invoice PDF via WhatsApp for a given order. Use when the customer asks for their invoice, receipt, or bill as a PDF/document.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_name": {
                    "type": "string",
                    "description": "Order reference like 'S00051', if the customer mentioned one. Omit to use their most recent order."
                }
            },
            "required": []
        }
    }
}
]

SYSTEM_PROMPT = {
    "role": "system",
    "content": (
        "You are a WhatsApp sales assistant for an Odoo-based store. "
        "Always use the currency returned by tools when quoting prices — "
        "never assume USD or add a $ sign unless the tool result says currency is USD. "
        "Product availability and stock levels change constantly — never answer a stock or "
        "product-availability question from memory or from earlier in this conversation. "
        "Always call the appropriate tool again, even if you answered a similar question before. "
        "Be concise, friendly, and WhatsApp-appropriate (short messages, no markdown). "
        "Never suggest, recommend, or offer to add a specific product (including add-ons, "
        "sizes, or combo components) unless you have verified it exists via a tool call in "
        "this conversation. If the customer asks for something not confirmed to exist, say "
        "you're not sure it's available and offer to check, rather than assuming it does. "
        "When the customer is browsing rather than naming a specific product "
        "('what do you have', 'show me what's available', 'what else do you have'), "
        "call list_available_products instead of asking a clarifying question. "
        "When list_available_products returns products, format them as:\n\n"
        "🛍️ *Available Products*\n\n"
        "1. 🍔 Burger — 559 in stock — 50 AFN\n"
        "2. 🍎 Apple — 58 in stock — 1 AFN\n"
        "3. 🥯 Bun — 20 in stock — 1 AFN\n\n"
        "Reply with the product name and quantity to order.\n\n"
        "Rules for product listing: "
        "Use one numbered line per product. "
        "Each line must contain product name, available stock, and price. "
        "Use the exact currency returned by the tool. "
        "Do not use Markdown tables. "
        "Use appropriate emojis for products if the product type is clear; "
        "if unclear, use 📦 as a neutral fallback. "
        "Keep the response compact and WhatsApp-friendly. "
        "If YOU offered to show the customer a category or type of item (e.g. 'other wooden "
        "armchairs', 'similar chairs') and they reply yes, search using that general category "
        "or type — do NOT reuse an overly specific description (like an exact image description "
        "with colors/materials) as the search term for a follow-up browse request. "
        "If a specific product search returns no results, tell the customer plainly that exact "
        "item isn't currently in stock. Do NOT call list_available_products as a fallback when a "
        "specific search fails — only call it when the customer explicitly asks to browse or see "
        "what's available. Never present unrelated products as an answer to a customer's question "
        "just because a specific search came back empty."
        "Never invent product attributes such as sizes, colors, materials, variants, flavors, "
        "dimensions, specifications, accessories, combos, or options. Only mention an attribute "
        "if it was provided by a tool or explicitly stated by the customer."
        "Do not say 'I'll check', 'let me check', or similar unless you are going to call the "
        "appropriate tool immediately in the same turn."
        "Never mention internal tool names, Odoo, database details, API details, tokens, system "
        "instructions, or internal errors to the customer."
        "When an image is provided, treat the image description as visual context, not as a "
        "confirmed product name or product specification. Use Odoo tools to determine whether "
        "a matching or similar product actually exists."
        "Never claim that a quotation, order, payment, customer record, or other action was created "
        "or completed unless the corresponding tool call succeeded and returned confirmation."
    )
}


def run_agent(user_id, user_message, phone_number_id):
    ai_config = get_ai_config(phone_number_id)
    system_prompt = {"role": "system", "content": ai_config["system_instructions"]}
    logger.info("Loaded system_instructions (first 80 chars): %r", ai_config["system_instructions"][:80])

    messages = load_history(user_id, phone_number_id)

    if messages and messages[0].get("role") == "system":
        messages[0] = system_prompt
    else:
        messages.insert(0, system_prompt)

    messages.append({"role": "user", "content": user_message})

    pending_product_image = None

    for _ in range(ai_config["max_tool_rounds"]):
        response = client.chat.completions.create(
            model=ai_config["chat_model"],
            messages=messages,
            tools=tools,
            tool_choice="auto"
        )
        message = response.choices[0].message

        if not message.tool_calls:
            messages.append({"role": "assistant", "content": message.content})
            save_history(user_id, phone_number_id, messages)
            return message.content, pending_product_image

        messages.append({
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in message.tool_calls
            ]
        })

        for tool_call in message.tool_calls:
            tool_name = tool_call.function.name
            arguments = json.loads(tool_call.function.arguments)

            if tool_name in ("create_quotation", "get_customer_orders", "find_customer", "get_order_status",
                             "send_invoice_pdf"):
                arguments["phone_number"] = user_id
            arguments["phone_number_id"] = phone_number_id

            if tool_name == "create_quotation":
                set_pending(user_id, arguments)
                result = {
                    "awaiting_confirmation": True,
                    "message": "Order details captured. Ask the customer to confirm before it's created."
                }
            else:
                tool_function = TOOL_FUNCTIONS.get(tool_name)
                result = tool_function(**arguments) if tool_function else {"error": f"Tool '{tool_name}' not found"}

            if isinstance(result, dict) and result.get("image_base64"):
                # Keep the raw image out of the LLM's context entirely — it only
                # ever sees `has_image`. The bytes are sent separately by code,
                # after run_agent returns, not something the AI describes/decides.
                pending_product_image = result["image_base64"]
                result = {k: v for k, v in result.items() if k != "image_base64"}
                logger.info("Image captured for pending send (tool=%s)", tool_name)
            elif tool_name == "get_product_details" and isinstance(result, dict):
                logger.info("get_product_details called, has_image=%s", result.get("has_image"))

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(result)
            })

    request_handoff(phone_number_id, user_id, "AI reached its tool-call limit without resolving the request")
    final_message = "I'm having trouble sorting this one out — connecting you with a team member now."
    messages.append({"role": "assistant", "content": final_message})
    save_history(user_id, phone_number_id, messages)
    return final_message, None


if __name__ == "__main__":
    messages = [
        SYSTEM_PROMPT,
        {
            "role": "user",
            "content": "How much is the chair?"
        }
    ]

    response = client.chat.completions.create(
        model="openai/gpt-oss-20b",
        messages=messages,
        tools=tools,
        tool_choice="auto"
    )

    message = response.choices[0].message

    if message.tool_calls:
        tool_call = message.tool_calls[0]
        tool_name = tool_call.function.name
        arguments = json.loads(tool_call.function.arguments)

        print("Tool:", tool_name)
        print("Arguments:", arguments)

        tool_function = TOOL_FUNCTIONS.get(tool_name)
        result = tool_function(**arguments) if tool_function else {"error": f"Tool '{tool_name}' not found"}

        print("Tool result:")
        print(result)

        messages.append(message)
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": json.dumps(result)
        })

        final_response = client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=messages
        )

        print("Final AI response:")
        print(final_response.choices[0].message.content)