"""
Thin wrapper around the whatsapp.conversation Odoo model. Every function
here is a direct XML-RPC call, same pattern as the rest of odoo_connections.py
— nothing here talks to WhatsApp/Meta directly.
"""

from odoo_connections import get_odoo_connection


def get_conversation_state(phone_number_id, partner_phone):
    conn = get_odoo_connection(phone_number_id)
    return conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.conversation", "get_state_for_customer",
        [phone_number_id, partner_phone]
    )


def request_handoff(phone_number_id, partner_phone, reason):
    conn = get_odoo_connection(phone_number_id)
    return conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.conversation", "request_handoff",
        [phone_number_id, partner_phone, reason]
    )


def log_message(phone_number_id, partner_phone, direction, sender_type, body,
                 whatsapp_message_id=None):
    conn = get_odoo_connection(phone_number_id)
    return conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.conversation", "log_message",
        [phone_number_id, partner_phone, direction, sender_type, body, whatsapp_message_id]
    )


def contains_handoff_keyword(user_message, handoff_keywords):
    lowered = user_message.strip().lower()
    return any(kw in lowered for kw in handoff_keywords)

def get_conversation_by_id(phone_number_id, conversation_id):
    conn = get_odoo_connection(phone_number_id)
    return conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.conversation", "get_conversation_by_id",
        [conversation_id]
    )


def log_message_for_conversation(phone_number_id, conversation_id, direction, sender_type, body,
                                  whatsapp_message_id=None):
    conn = get_odoo_connection(phone_number_id)
    return conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.conversation", "log_message_for_conversation",
        [conversation_id, direction, sender_type, body, whatsapp_message_id]
    )