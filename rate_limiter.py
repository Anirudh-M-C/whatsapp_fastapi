import time
import uuid
from redis_client import redis_client


def is_rate_limited(user_id, window_seconds=60, max_requests=10):
    key = f"ratelimit:{user_id}"
    now = time.time()
    window_start = now - window_seconds

    pipe = redis_client.pipeline()
    pipe.zremrangebyscore(key, 0, window_start)   # drop entries older than the window
    pipe.zcard(key)                                # count what's left
    _, count = pipe.execute()

    if count >= max_requests:
        return True

    # unique member so two calls in the same millisecond don't collide
    pipe = redis_client.pipeline()
    pipe.zadd(key, {f"{now}:{uuid.uuid4().hex}": now})
    pipe.expire(key, window_seconds)
    pipe.execute()
    return False