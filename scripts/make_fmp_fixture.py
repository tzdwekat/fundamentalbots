"""Save raw FMP responses for a ticker as test fixtures. Usage: uv run scripts/make_fmp_fixture.py AAPL"""
import json
import sys
from pathlib import Path

import httpx

from fundabot.config import settings

t = sys.argv[1].upper()
base = "https://financialmodelingprep.com/stable"
calls = {
    "estimates_annual": ("analyst-estimates", {"symbol": t, "period": "annual", "limit": 8}),
    "earnings": ("earnings", {"symbol": t}),  # no limit: >5 is a premium parameter, but the unbounded call is allowed
    "price_target": ("price-target-consensus", {"symbol": t}),
}
for name, (path, params) in calls.items():
    r = httpx.get(f"{base}/{path}", params={**params, "apikey": settings.fmp_api_key}, timeout=30)
    out = Path("tests/fixtures") / f"fmp_{t.lower()}_{name}.json"
    text = r.text
    if name == "earnings" and r.status_code == 200:
        text = json.dumps(json.loads(text)[:12])  # keep the fixture small
    out.write_text(text)
    print(out, r.status_code, len(r.text), "bytes")
