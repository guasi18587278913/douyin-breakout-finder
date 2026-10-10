"""Best-effort original-cover cache. Public CDN reads only; no API key or ledger use."""
import base64
import hashlib
import io
import concurrent.futures
import re
from urllib.parse import urlsplit
from core import atomic_json, read_json, now

CDNS = ('douyinpic.com', 'byteimg.com', 'pstatp.com', 'ibytedtos.com')
MAX_BYTES = 3 * 1024 * 1024


def allowed_cover(url):
    if not isinstance(url, str) or len(url) > 6000 or any(ord(c) < 32 for c in url):
        return False
    try:
        p = urlsplit(url)
        host = (p.hostname or '').lower()
        return (p.scheme == 'https' and p.port in (None, 443) and not p.username and not p.password
                and any(host == suffix or host.endswith('.' + suffix) for suffix in CDNS))
    except ValueError:
        return False


def image_mime(raw):
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if raw.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if raw.startswith(b'RIFF') and raw[8:12] == b'WEBP':
        return 'image/webp'
    return None


def browser_image(raw):
    mime=image_mime(raw)
    if mime:return raw,mime,{}
    if len(raw)<16 or raw[4:8]!=b'ftyp':return None
    try:
        from PIL import Image,ImageOps
        from pillow_heif import register_heif_opener
        register_heif_opener()
        with Image.open(io.BytesIO(raw)) as original:
            if original.format not in ('HEIF','AVIF') or not 0<original.width*original.height<=16000000:return None
            image=ImageOps.exif_transpose(original)
            image.thumbnail((1200,1200))
            output=io.BytesIO()
            if 'A' in image.getbands():
                image.save(output,format='PNG');mime='image/png'
            else:
                image.convert('RGB').save(output,format='JPEG',quality=90);mime='image/jpeg'
            data=output.getvalue()
            if not 0<len(data)<=MAX_BYTES:return None
            return data,mime,{'source_format':original.format,'source_sha256':hashlib.sha256(raw).hexdigest(),
                              'conversion':'browser-compatible-original-cover'}
    except (ImportError,OSError,ValueError,RuntimeError):
        return None


def valid_cached(entry):
    if not isinstance(entry, dict) or entry.get('mime') not in ('image/png', 'image/jpeg', 'image/webp'):
        return False
    try:
        encoded = entry.get('base64', '')
        if not isinstance(encoded, str) or len(encoded) > MAX_BYTES * 4 // 3 + 8:
            return False
        raw = base64.b64decode(encoded, validate=True)
        return 0 < len(raw) <= MAX_BYTES and image_mime(raw) == entry['mime']
    except (ValueError, TypeError):
        return False


def cache_covers(store, fetcher=None):
    from selection_report import FORMAT
    story = store.state.get('research') or {}
    if story.get('format') != FORMAT or store.state.get('demo'):
        return {'saved': 0, 'unavailable': 0}
    if fetcher is None:
        from transport import request
        fetcher = request
    covers = read_json(store.root / 'covers.json', {})
    if not isinstance(covers, dict):
        covers = {}
    candidates = store.state['candidates']
    from selection_report import eligible_ids
    ids = list(story.get('cases', {}))
    ids += sorted(eligible_ids(store) - set(ids))
    ids = ids[:20]
    def one(vid):
        current = covers.get(vid)
        if valid_cached(current):
            return vid, current
        url = candidates.get(vid, {}).get('cover_url')
        entry = {'status': 'unavailable', 'checked_at': now()}
        if not re.fullmatch(r'\d{10,30}', vid) or not allowed_cover(url):
            return vid, entry
        try:
            # transport disables curl config/redirects; api_key is deliberately never supplied.
            status, raw = fetcher(url, timeout=6, limit=MAX_BYTES)
            if status == 200 and 0 < len(raw) <= MAX_BYTES:
                ready=browser_image(raw)
                if ready:
                    data,mime,metadata=ready
                    return vid, {'mime': mime, 'base64': base64.b64encode(data).decode('ascii'), 'cached_at': now(),**metadata}
        except Exception:
            pass  # A failed cover never turns an otherwise valid report into a pending task.
        return vid, entry
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        for vid, entry in pool.map(one, ids):
            covers[vid] = entry
    atomic_json(store.root / 'covers.json', covers)
    return {'saved': sum(valid_cached(covers.get(v)) for v in ids),
            'unavailable': sum(not valid_cached(covers.get(v)) for v in ids)}
