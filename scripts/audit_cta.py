#!/usr/bin/env python3
"""Read-only published CTA and landing-page audit. Never edits Pinterest or Buffer."""
import json, os, urllib.request, urllib.error
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlunparse

root=Path(__file__).resolve().parents[1]
pins=json.loads((root/"campaign/pins.json").read_text())["pins"]
results=[]
for p in pins:
    if p.get("status")!="sent": continue
    actual=p.get("published_destination_url","")
    expected=p.get("destination_url","")
    a,e=urlparse(actual),urlparse(expected)
    issues=[]
    if (a.scheme,a.netloc,a.path)!=(e.scheme,e.netloc,e.path):
        issues.append("published destination differs from canonical target")
    if not p.get("pinterest_url"): issues.append("published Pinterest URL not recorded")
    params=parse_qs(a.query)
    required={"utm_source":"pinterest","utm_medium":"organic_social","utm_campaign":"kg_diagnostic","utm_content":p["id"]}
    missing=[k for k,v in required.items() if params.get(k)!=[v]]
    if missing: issues.append("missing/mismatched tracking: "+", ".join(missing))
    # GET instead of HEAD: many storefronts reject HEAD. No checkout or purchase.
    landing="unknown"
    if a.scheme=="https" and a.netloc:
        try:
            req=urllib.request.Request(actual,headers={"User-Agent":"Mozilla/5.0 (compatible; TCE-CTA-Audit/1.0)"})
            with urllib.request.urlopen(req,timeout=15) as response:
                landing=f"HTTP {response.status}"
                final=urlparse(response.url)
                if final.netloc not in ("kraase.gumroad.com","gumroad.com","www.gumroad.com"):
                    issues.append("unexpected redirect host")
        except urllib.error.HTTPError as exc:
            landing=f"HTTP {exc.code} (may be bot protection)"
            issues.append("landing HTTP error; manual review needed")
        except Exception as exc:
            landing=type(exc).__name__
            issues.append("landing inaccessible from runner; manual review needed")
    results.append({"id":p["id"],"pinterest_url":p.get("pinterest_url"),"landing":landing,"issues":issues})
print(json.dumps(results,indent=2))
summary=os.environ.get("GITHUB_STEP_SUMMARY")
if summary:
    lines=["## Published Pinterest CTA audit","","| Pin | Landing | Findings |","|---|---|---|"]
    for r in results:
        lines.append(f"| {r['id']} | {r['landing']} | {'; '.join(r['issues']) or 'No issues found'} |")
    Path(summary).open("a").write("\n".join(lines)+"\n")
# Do not fail a scheduled run on bot blocking; report evidence rather than infer checkout success.
