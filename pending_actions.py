import json
from redis_client import redis_client

PENDING_TTL_SECONDS = 3600  # an unconfirmed order is abandoned after an hour


def _key(phone_number_id, user_id):
    return f"pending_action:{phone_number_id}:{user_id}"


def set_pending(phone_number_id, user_id, action):
    redis_client.set(_key(phone_number_id, user_id), json.dumps(action), ex=PENDING_TTL_SECONDS)


def get_pending(phone_number_id, user_id):
    raw = redis_client.get(_key(phone_number_id, user_id))
    return json.loads(raw) if raw else None


def clear_pending(phone_number_id, user_id):
    redis_client.delete(_key(phone_number_id, user_id)) 