import json
import time
from redis_client import redis_client

QUEUE_KEY = "whatsapp_queue"
PROCESSING_KEY = "whatsapp_queue:processing"  # sorted set: member=raw_job_str, score=claimed_at
FAILED_KEY = "whatsapp_queue:failed"
MAX_ATTEMPTS = 3
LOCK_TTL_SECONDS = 120
PROCESSING_VISIBILITY_SECONDS = 120  # a job claimed longer than this is assumed abandoned


def enqueue_job(job):
    job.setdefault("attempts", 0)
    redis_client.rpush(QUEUE_KEY, json.dumps(job))


def _user_lock_key(phone_number_id, user_id):
    return f"lock:{phone_number_id}:{user_id}"


def acquire_user_lock(phone_number_id, user_id):
    return bool(redis_client.set(_user_lock_key(phone_number_id, user_id), "1", nx=True, ex=LOCK_TTL_SECONDS))


def release_user_lock(phone_number_id, user_id):
    redis_client.delete(_user_lock_key(phone_number_id, user_id))


def claim_job(timeout=5):
    """Pops the next job and records it as in-progress with a timestamp,
    replacing the old brpoplpush-into-a-plain-list approach."""
    result = redis_client.blpop(QUEUE_KEY, timeout=timeout)
    if result is None:
        return None
    _, raw_job_str = result
    redis_client.zadd(PROCESSING_KEY, {raw_job_str: time.time()})
    return raw_job_str


def requeue_raw(raw_job_str):
    redis_client.rpush(QUEUE_KEY, raw_job_str)


def mark_done(raw_job_str):
    redis_client.zrem(PROCESSING_KEY, raw_job_str)


def retry_or_deadletter(raw_job_str, job):
    mark_done(raw_job_str)
    job["attempts"] = job.get("attempts", 0) + 1
    if job["attempts"] >= MAX_ATTEMPTS:
        redis_client.rpush(FAILED_KEY, json.dumps(job))
    else:
        redis_client.rpush(QUEUE_KEY, json.dumps(job))


def recover_stale_jobs(visibility_timeout=PROCESSING_VISIBILITY_SECONDS):
    """Requeues only jobs claimed longer than visibility_timeout ago — safe to
    call from every worker, repeatedly, while others are live: a job still
    within the window is left alone.

    ZREM's return value is the race guard: if two workers spot the same stale
    entry, only the one whose ZREM actually removes it (returns 1) requeues
    it — the other's is a no-op on an already-gone member.
    """
    cutoff = time.time() - visibility_timeout
    stale = redis_client.zrangebyscore(PROCESSING_KEY, 0, cutoff)
    recovered = 0
    for raw_job_str in stale:
        if redis_client.zrem(PROCESSING_KEY, raw_job_str):
            requeue_raw(raw_job_str)
            recovered += 1
    return recovered