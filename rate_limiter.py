import time
from collections import defaultdict

request_log = defaultdict(list)


def is_rate_limited(user_id, window_seconds=60, max_requests=10):
    now = time.time()
    request_log[user_id] = [
        t for t in request_log[user_id] if now - t < window_seconds
    ]

    if len(request_log[user_id]) >= max_requests:
        return True

    request_log[user_id].append(now)
    return False