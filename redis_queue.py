import redis
import json

redis_client = redis.Redis(
    host="localhost",
    port=6379,
    decode_responses=True
)


def enqueue_message(job):
    redis_client.rpush(
        "whatsapp_queue",
        json.dumps(job)
    )