#!/usr/bin/env python3

import json
import logging
import os
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta

# --- Config ---
DB_PATH = "/mnt/ssd/nyc_traffic.db"
ENV_PATH = "/home/ubuntu/nyc-traffic-visualizer/.env"
API_URL = "https://data.cityofnewyork.us/api/v3/views/i4gi-tjb9/query.json"
POLL_INTERVAL_SECONDS = 300
PAGE_SIZE = 1000

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)


def load_env(path):
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ[key] = val


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS links (
            link_id          TEXT PRIMARY KEY,
            link_name        TEXT,
            borough          TEXT,
            owner            TEXT,
            encoded_poly_line TEXT,
            link_points      TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            link_id     TEXT NOT NULL,
            speed       REAL,
            travel_time REAL,
            data_as_of  TEXT NOT NULL,
            FOREIGN KEY (link_id) REFERENCES links(link_id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_readings_data_as_of
        ON readings(data_as_of)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_readings_link_id
        ON readings(link_id)
    """)
    conn.commit()
    log.info("Database initialized at %s", DB_PATH)


def get_high_water_mark(conn):
    row = conn.execute("SELECT MAX(data_as_of) FROM readings").fetchone()
    if row and row[0]:
        return row[0]
    # First run — start from 10 minutes ago
    fallback = datetime.utcnow() - timedelta(hours=24)
    return fallback.strftime("%Y-%m-%dT%H:%M:%S.000")


def fetch_rows(token, since, page_number=1):
    query = "SELECT * WHERE data_as_of > '{}'".format(since)
    payload = json.dumps({
        "query": query,
        "page": {"pageNumber": page_number, "pageSize": PAGE_SIZE},
        "includeSynthetic": False
    }).encode("utf-8")

    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-App-Token": token
        },
        method="POST"
    )

    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def is_valid_reading(row):
    """Filter out junk rows — bad timestamps, missing speed, etc."""
    try:
        dt = datetime.strptime(row["data_as_of"], "%Y-%m-%dT%H:%M:%S.%f")
        if dt.year < 2010 or dt.year > 2100:
            return False
        float(row["speed"])
        float(row["travel_time"])
        return True
    except (KeyError, ValueError):
        return False


def upsert_link(conn, row):
    conn.execute("""
        INSERT OR IGNORE INTO links (link_id, link_name, borough, owner, encoded_poly_line, link_points)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        row.get("link_id"),
        row.get("link_name"),
        row.get("borough"),
        row.get("owner"),
        row.get("encoded_poly_line"),
        row.get("link_points")
    ))


def insert_reading(conn, row):
    conn.execute("""
        INSERT INTO readings (link_id, speed, travel_time, data_as_of)
        VALUES (?, ?, ?, ?)
    """, (
        row["link_id"],
        float(row["speed"]),
        float(row["travel_time"]),
        row["data_as_of"]
    ))


def poll(conn, token):
    since = get_high_water_mark(conn)
    log.info("Polling for rows newer than %s", since)

    page = 1
    total_inserted = 0

    while True:
        try:
            rows = fetch_rows(token, since, page_number=page)
        except Exception as e:
            log.error("Fetch failed on page %d: %s", page, e)
            break

        if not rows:
            break

        valid = [r for r in rows if is_valid_reading(r)]
        skipped = len(rows) - len(valid)

        for row in valid:
            upsert_link(conn, row)
            insert_reading(conn, row)

        conn.commit()
        total_inserted += len(valid)

        if skipped:
            log.warning("Skipped %d invalid rows on page %d", skipped, page)

        log.info("Page %d: %d rows inserted", page, len(valid))

        # If we got a full page there may be more
        if len(rows) < PAGE_SIZE:
            break

        page += 1

    log.info("Poll complete — %d total rows inserted", total_inserted)


def main():
    load_env(ENV_PATH)
    token = os.environ.get("SOCRATA_APP_TOKEN")
    if not token:
        raise RuntimeError("SOCRATA_APP_TOKEN not set")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    log.info("Poller started. Polling every %ds", POLL_INTERVAL_SECONDS)

    while True:
        try:
            poll(conn, token)
        except Exception as e:
            log.error("Unexpected error during poll: %s", e)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
