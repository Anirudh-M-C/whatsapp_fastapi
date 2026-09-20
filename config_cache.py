import json
from redis_client import redis_client
from odoo_connections import (
    get_whatsapp_config as _fetch_whatsapp_config,
    get_ai_config as _fetch_ai_config,
)

CONFIG_TTL_SECONDS = 300


def get_whatsapp_config(phone_number_id):
    key = f"config:whatsapp:{phone_number_id}"
    cached = redis_client.get(key)
    if cached:
        return json.loads(cached)
    config = _fetch_whatsapp_config(phone_number_id)
    redis_client.set(key, json.dumps(config), ex=CONFIG_TTL_SECONDS)
    return config


def get_ai_config(phone_number_id, *args, **kwargs):
    key = f"config:ai:{phone_number_id}"
    cached = redis_client.get(key)
    if cached and not args and not kwargs:
        return json.loads(cached)
    config = _fetch_ai_config(phone_number_id, *args, **kwargs)
    if not args and not kwargs:
        redis_client.set(key, json.dumps(config), ex=CONFIG_TTL_SECONDS)
    return config