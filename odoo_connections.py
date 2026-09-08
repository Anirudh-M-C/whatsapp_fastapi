import xmlrpc.client
import os
from companies import get_company

_connections = {}
_config_cache = {}
_ai_config_cache = {}


def get_ai_config(phone_number_id, force_refresh=False):
    if not force_refresh and phone_number_id in _ai_config_cache:
        return _ai_config_cache[phone_number_id]

    conn = get_odoo_connection(phone_number_id)
    config = conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.ai.config", "get_config_for_company",
        [phone_number_id]
    )
    if not config:
        raise ValueError(f"No whatsapp.ai.config found for phone_number_id {phone_number_id}")

    _ai_config_cache[phone_number_id] = config
    return config


def get_odoo_connection(phone_number_id):
    if phone_number_id in _connections:
        return _connections[phone_number_id]

    company = get_company(phone_number_id)
    if not company:
        raise ValueError(f"No company configured for phone_number_id {phone_number_id}")

    odoo_url = company["odoo_url"]
    database = company["odoo_db"]
    username = company["odoo_username"]
    api_key = os.environ.get(company["odoo_api_key_env"])
    if not api_key:
        raise ValueError(
            f"Environment variable {company['odoo_api_key_env']!r} is not set "
            f"(required for phone_number_id {phone_number_id})"
        )

    common = xmlrpc.client.ServerProxy(f"{odoo_url}/xmlrpc/2/common", allow_none=True)
    uid = common.authenticate(database, username, api_key, {})
    models = xmlrpc.client.ServerProxy(f"{odoo_url}/xmlrpc/2/object", allow_none=True)

    conn = {"models": models, "uid": uid, "database": database, "api_key": api_key}
    _connections[phone_number_id] = conn
    return conn


def get_whatsapp_config(phone_number_id, force_refresh=False):
    """Fetches whatsapp.config from Odoo, cached per phone_number_id.
    force_refresh bypasses the cache — use after an admin edits config in
    Odoo and you need it picked up without restarting the whole process."""
    if not force_refresh and phone_number_id in _config_cache:
        return _config_cache[phone_number_id]

    conn = get_odoo_connection(phone_number_id)
    config = conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.config", "get_config_by_phone_number_id",
        [phone_number_id]
    )
    if not config:
        raise ValueError(f"No whatsapp.config found in Odoo for phone_number_id {phone_number_id}")

    _config_cache[phone_number_id] = config
    return config


def find_config_by_verify_token(phone_number_id, verify_token):
    """Meta's GET /webhook verification handshake has no phone_number_id —
    we only know which Odoo instance to ask because there's currently one
    company. With multiple companies this needs to try each configured
    company in turn until one matches; see llm_test.py's verify_webhook."""
    conn = get_odoo_connection(phone_number_id)
    return conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.config", "find_config_by_verify_token",
        [verify_token]
    )