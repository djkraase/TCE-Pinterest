#!/usr/bin/env python3
"""Read-only Buffer API connectivity test. Never schedules or publishes."""
import json, os, sys, urllib.request, urllib.error
key=os.environ.get("BUFFER_API_KEY","")
if not key:
    print("BLOCKED: BUFFER_API_KEY GitHub Actions secret is not configured.")
    sys.exit(2)
query={"query":"query TCEBufferConnection { account { organizations { id } } }"}
req=urllib.request.Request("https://api.buffer.com",data=json.dumps(query).encode(),headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"},method="POST")
try:
    with urllib.request.urlopen(req,timeout=20) as response:
        result=json.load(response)
except urllib.error.HTTPError as e:
    print("Buffer authentication/API error HTTP",e.code)
    sys.exit(1)
except Exception as e:
    print("Buffer connectivity error:",type(e).__name__)
    sys.exit(1)
if result.get("errors"):
    print("Buffer GraphQL query failed; inspect API schema and authorization.")
    sys.exit(1)
ids=[o.get("id") for o in result.get("data",{}).get("account",{}).get("organizations",[])]
expected="6ad44c983788a9f13eb0c4cf"
if expected not in ids:
    print("Connected Buffer account does not include the authorized TCE organization.")
    sys.exit(1)
print("PASS: Buffer API authentication and authorized organization verified (read-only).")
