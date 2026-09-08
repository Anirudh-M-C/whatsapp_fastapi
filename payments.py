import os
import time
import hmac
import hashlib
import requests
from odoo_connections import get_whatsapp_config
from dotenv import load_dotenv

load_dotenv()

RAZORPAY_BASE_URL = "https://api.razorpay.com/v1"


def _get_api_credentials(phone_number_id):
    config = get_whatsapp_config(phone_number_id)

    key_id = config.get("razorpay_key_id")
    key_secret = config.get("razorpay_key_secret")
    if not key_id or not key_secret:
        raise ValueError(f"Razorpay API credentials not configured for {phone_number_id}")

    return key_id, key_secret


def create_payment_link(phone_number_id, amount, currency, customer_name,
                         customer_phone, description, reference_id, expire_hours=24):
    key_id, key_secret = _get_api_credentials(phone_number_id)

    payload = {
        "amount": int(round(amount * 100)),
        "currency": currency,
        "accept_partial": False,
        "description": description,
        "customer": {
            "name": customer_name,
            "contact": customer_phone,
        },
        "notify": {"sms": False, "email": False},
        "reference_id": reference_id,
        "callback_method": "get",
        "expire_by": int(time.time()) + expire_hours * 3600,
        "notes": {"order_reference": reference_id},
    }

    response = requests.post(
        f"{RAZORPAY_BASE_URL}/payment_links",
        auth=(key_id, key_secret),
        json=payload,
        timeout=15,
    )
    if response.status_code >= 400:
        raise ValueError(f"Razorpay API error {response.status_code}: {response.text}")
    data = response.json()

    return {
        "payment_link_id": data["id"],
        "short_url": data["short_url"],
        "status": data["status"],
    }


def verify_webhook_signature(phone_number_id, raw_body, signature_header):
    if not signature_header:
        return False

    config = get_whatsapp_config(phone_number_id)
    secret = config.get("razorpay_webhook_secret")
    if not secret:
        return False

    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)