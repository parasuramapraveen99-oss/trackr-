"""Deferred work: the Postgres job queue and its drain.

Four functions (``queue.py``) and one worker loop (``drain.py``). The surface is
deliberately this small so that swapping the body for Celery later is a module change
rather than a refactor -- the same way ``app/storage/factory.py`` lets local and R2 swap
without any caller knowing which is in use.

See docs/BACKFILL_PIPELINE.md section 4 for why this is Postgres and not Redis.
"""
