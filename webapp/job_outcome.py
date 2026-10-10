"""Read-only terminal-job assessment, cached only while its evidence is unchanged."""
from functools import lru_cache
from core import StopRun
from verification import audit_run


@lru_cache(maxsize=256)
def _assessment(root, signature):
    return audit_run(root)['completion']


def assessment(folder):
    if not (folder/'state.json').is_file(): return None
    try:
        paths = list(folder.glob('*.json')) + list((folder/'cache').glob('*.json'))
        paths += list((folder/'evidence').rglob('*'))
        paths += [folder/'requests.jsonl', folder/'report.html']
        # Model review receipts also depend on the actual received response.
        paths += list((folder.parent/'model-diagnostics').glob('*.json'))
        paths += [folder.parent/'model-events.jsonl']
        signature = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_ctime_ns, p.stat().st_size)
                          for p in sorted(paths) if p.is_file())
        return _assessment(str(folder), signature)
    except (StopRun, OSError, ValueError, KeyError, TypeError):
        return None


def cannot_recover_data(result):
    recovery = (result or {}).get('cached_recovery', {})
    return recovery.get('data_incomplete') is True and not recovery.get('numerically_qualified_ids')
