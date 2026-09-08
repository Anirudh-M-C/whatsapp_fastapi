"""
Central notification engine: Odoo/Payment/Order event -> template -> WhatsApp.

Template bodies now live in Odoo (whatsapp.notification.template) instead of
Python, so a business owner can edit customer-facing copy without touching
code. This file just fetches, renders, and sends.
"""

from whatsapp_sender import send_text_message
from odoo_connections import get_odoo_connection


def notify(template_key, to, phone_number_id, context=None):
    conn = get_odoo_connection(phone_number_id)
    body = conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.notification.template", "get_template_body",
        [template_key]
    )

    if body is None:
        raise ValueError(f"No notification template found in Odoo for key: {template_key}")

    try:
        message = body.format(**(context or {}))
    except KeyError as e:
        raise ValueError(
            f"Template '{template_key}' references placeholder {e} which wasn't provided in context"
        )

    return send_text_message(to, message, phone_number_id)