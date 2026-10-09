"""Bounded I/O only. Callers merge workflow state on their owning thread."""
from concurrent.futures import ThreadPoolExecutor

MAX_WORKERS = 3


def call_many(client, calls):
    def run(call):
        op, params, options = call
        try:
            return client.call(op, params, **options), None
        except Exception as error:
            return None, error
    if not getattr(client, 'parallel_safe', False) or len(calls) < 2:
        return [run(call) for call in calls]
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(calls))) as pool:
        return list(pool.map(run, calls))
