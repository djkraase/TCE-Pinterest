#!/usr/bin/env python3
"""Read-only Pinterest release readiness audit. No publishing or secrets."""
import json
from pathlib import Path
from urllib.parse import urlparse

ROOT=Path(__file__).resolve().parents[1]
ASSETS=ROOT/"assets"
MANIFEST=ROOT/"campaign"/"pins.json"
def main():
    pins=json.loads(MANIFEST.read_text())["pins"]
    report=[]
    seen=set()
    for p in pins:
        asset=p.get("asset","")
        key=(p.get("source_id"),p.get("search_intent"))
        path=ASSETS/asset
        safe=path.resolve().is_relative_to(ASSETS.resolve())
        dest=p.get("destination_url","")
        url=urlparse(dest)
        reasons=[]
        if key in seen: reasons.append("duplicate source/search intent")
        seen.add(key)
        if not safe or not path.is_file(): reasons.append("missing hosted asset")
        if url.scheme!="https" or not url.netloc: reasons.append("invalid destination")
        if not p.get("editorial_approved"): reasons.append("editorial approval pending")
        if not p.get("board_service_id"): reasons.append("board assignment missing")
        report.append({"id":p["id"],"ready":not reasons,"reasons":reasons})
    print(json.dumps({"ready":sum(x["ready"] for x in report),"total":len(report),"pins":report},indent=2))
if __name__=="__main__": main()
