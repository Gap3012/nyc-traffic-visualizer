import urllib.request
import json
import os
import sys

with open(".env") as f:
    for line in f:
        key, val = line.strip().split("=", 1)
        os.environ[key] = val

token = os.environ.get("SOCRATA_APP_TOKEN")

query = sys.argv[1]

payload = json.dumps({
    "query": query,
    "page": {"pageNumber": 1, "pageSize": 1000},
    "includeSynthetic": False
}).encode("utf-8")

req = urllib.request.Request(
    "https://data.cityofnewyork.us/api/v3/views/i4gi-tjb9/query.json",
    data=payload,
    headers={
        "Content-Type": "application/json",
        "X-App-Token": token
    },
    method="POST"
)

print("Sending request...")
with urllib.request.urlopen(req) as response:
    print("Got response, reading...")
    data = json.loads(response.read().decode("utf-8"))
    for row in data:
        print(row)
    print(f"\nTotal rows: {len(data)}")
