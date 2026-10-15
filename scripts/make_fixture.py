"""Download a companyfacts JSON and trim it to the tags fundabot uses. Usage: uv run scripts/make_fixture.py AAPL"""
import json
import sys
from pathlib import Path

import httpx
import yaml

from fundabot.config import settings

ticker = sys.argv[1].upper()
headers = {"User-Agent": settings.edgar_user_agent}
tickers = httpx.get("https://www.sec.gov/files/company_tickers.json", headers=headers).json()
cik = next(v["cik_str"] for v in tickers.values() if v["ticker"] == ticker)
payload = httpx.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", headers=headers, timeout=60).json()
keep = {t for tags in yaml.safe_load(open("fundabot/analytics/tag_map.yaml")).values() for t in tags}
payload["facts"]["us-gaap"] = {k: v for k, v in payload["facts"].get("us-gaap", {}).items() if k in keep}
out = Path("tests/fixtures") / f"{ticker}_companyfacts.json"
out.write_text(json.dumps(payload, separators=(",", ":")))
print(out, f"{out.stat().st_size/1e6:.2f} MB")
