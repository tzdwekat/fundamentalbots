import hashlib
import json
import time

import httpx

from fundabot.config import settings


class EdgarError(Exception):
    pass


_last_call = 0.0
MIN_GAP = 0.15  # seconds; SEC allows 10 req/s


def _throttle():
    global _last_call
    wait = _last_call + MIN_GAP - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def get_bytes(url: str, max_age_hours: float = 24) -> bytes:
    """Rate-limited GET with the EDGAR User-Agent, cached on disk under settings.cache_dir."""
    cache = settings.cache_dir / "edgar" / hashlib.sha256(url.encode()).hexdigest()
    if cache.exists() and (time.time() - cache.stat().st_mtime) < max_age_hours * 3600:
        return cache.read_bytes()
    _throttle()
    r = httpx.get(url, headers={"User-Agent": settings.edgar_user_agent}, timeout=60, follow_redirects=True)
    if r.status_code != 200:
        raise EdgarError(f"{r.status_code} for {url}")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(r.content)
    return r.content


def get_json(url: str, max_age_hours: float = 24) -> dict:
    return json.loads(get_bytes(url, max_age_hours))
