import json
import time
import logging

from redis_client import redis_client
from job_queue import (
    claim_job, requeue_raw,
    acquire_user_lock, release_user_lock,
    mark_done, retry_or_deadletter, recover_stale_jobs,
)
from whatsapp_processor import process_whatsapp_message, process_broadcast_job

logging.basicConfig(
    filename="worker.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

REAPER_INTERVAL_SECONDS = 30
_last_reap = 0


def _maybe_reap_stale_jobs():
    """Runs at most once every REAPER_INTERVAL_SECONDS, from whichever worker
    happens to check first — safe under multiple replicas since recover_stale_jobs
    is race-guarded internally."""
    global _last_reap
    now = time.time()
    if now - _last_reap < REAPER_INTERVAL_SECONDS:
        return
    _last_reap = now
    recovered = recover_stale_jobs()
    if recovered:
        print(f"Recovered {recovered} stale job(s) left by a dead worker.")


def handle_job(job):
    job_type = job.get("type")
    if job_type == "whatsapp_message":
        process_whatsapp_message(job)
    elif job_type == "broadcast":
        process_broadcast_job(job)
    else:
        logger.error("Unknown job type: %r", job_type)


def run():
    print("Worker started. Waiting for jobs...")
    while True:
        _maybe_reap_stale_jobs()

        raw_job_str = claim_job(timeout=5)
        if raw_job_str is None:
            continue

        job = json.loads(raw_job_str)
        job_type = job.get("type")

        locked_user = None
        if job_type == "whatsapp_message":
            phone_number_id = job["phone_number_id"]
            user_id = job["user_id"]
            if not acquire_user_lock(phone_number_id, user_id):
                mark_done(raw_job_str)
                requeue_raw(raw_job_str)
                time.sleep(0.2)
                continue
            locked_user = (phone_number_id, user_id)

        try:
            print("Processing job:", job_type, job.get("id"))
            handle_job(job)
            mark_done(raw_job_str)
            print("Job completed:", job.get("id"))
        except Exception as e:
            logger.error("Job failed (job=%r): %r", job, e)
            retry_or_deadletter(raw_job_str, job)
        finally:
            if locked_user:
                release_user_lock(*locked_user)


if __name__ == "__main__":
    run()