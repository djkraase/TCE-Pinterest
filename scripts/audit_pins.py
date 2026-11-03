#!/usr/bin/env python3
"""Read-only TCE Pinterest manifest audit; never publishes or changes approval."""
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
MANIFEST = ROOT / "campaign" / "pins.json"
VALID_STATUSES = {"sent", "scheduled", "draft", "asset-pending", "review", "approved"}

def audit():
    pins = json.loads(MANIFEST.read_text(encoding="utf-8"))["pins"]
    seen_ids, seen_intents, seen_posts = set(), set(), set()
    results = []
    for p in pins:
        pid = p.get("id", "")
        intent = (p.get("source_id"), p.get("search_intent"))
        asset = p.get("asset", "")
        path = (ASSETS / asset).resolve()
        hosted = bool(asset) and path.is_relative_to(ASSETS.resolve()) and path.is_file()
        parsed = urlparse(p.get("destination_url", ""))
        status = p.get("status", "")
        approved = p.get("editorial_approved") is True
        post_id = p.get("buffer_post_id")
        issues = []
        if not pid or pid in seen_ids: issues.append("missing or duplicate pin id")
        seen_ids.add(pid)
        if not all(intent) or intent in seen_intents: issues.append("missing or duplicate source/search intent")
        seen_intents.add(intent)
        if post_id:
            if post_id in seen_posts: issues.append("duplicate Buffer post id")
            seen_posts.add(post_id)
        if status not in VALID_STATUSES: issues.append("invalid status")
        if not hosted: issues.append("asset not hosted in repository")
        if parsed.scheme != "https" or not parsed.netloc: issues.append("invalid HTTPS destination")
        if not p.get("board_service_id"): issues.append("board missing")
        if status not in ("sent", "scheduled") and approved:
            from urllib.parse import parse_qs
            tracked = urlparse(p.get("published_destination_url") or p.get("destination_url", ""))
            params = parse_qs(tracked.query)
            required = {"utm_source": "pinterest", "utm_medium": "organic_social", "utm_content": pid}
            for k, v in required.items():
                if params.get(k) != [v]:
                    issues.append("prepublication tracking missing: " + k)
        if status in ("sent", "scheduled") and not post_id: issues.append("published/scheduled without Buffer post id")
        if status in ("sent", "scheduled") and not approved: issues.append("published/scheduled without approval")
        if status == "approved" and not approved: issues.append("approved status without editorial approval")
        eligible = approved and hosted and not issues and status in ("approved", "draft")
        blocking = [x for x in issues if x not in ("asset not hosted in repository",)]
        if approved and not hosted: blocking.append("approved pin missing image")
        results.append({"id": pid, "status": status, "hosted": hosted,
                        "editorial_approved": approved, "eligible_to_schedule": eligible,
                        "issues": issues, "blocking": blocking})
    return results

def main():
    results = audit()
    summary = {"total": len(results), "sent": sum(x["status"] == "sent" for x in results),
               "scheduled": sum(x["status"] == "scheduled" for x in results),
               "ready_to_schedule": sum(x["eligible_to_schedule"] for x in results),
               "pending": sum(not x["eligible_to_schedule"] and x["status"] not in ("sent", "scheduled") for x in results),
               "blocking_errors": sum(bool(x["blocking"]) for x in results),
               "pins": results}
    print(json.dumps(summary, indent=2))
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        lines = ["## TCE Pinterest readiness", "",
                 f"Sent: {summary['sent']} | Scheduled: {summary['scheduled']} | Ready: {summary['ready_to_schedule']} | Pending: {summary['pending']}", "",
                 "| Pin | State | Hosted | Approved | Action |", "|---|---|---|---|---|"]
        for p in results:
            action = "Ready" if p["eligible_to_schedule"] else "; ".join(p["issues"]) or "Already processed"
            lines.append(f"| {p['id']} | {p['status']} | {p['hosted']} | {p['editorial_approved']} | {action} |")
        Path(summary_file).open("a", encoding="utf-8").write("\n".join(lines) + "\n")
    if summary["blocking_errors"]:
        print("Manifest integrity errors detected", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
