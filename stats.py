#!/usr/bin/env python3

import logging
import math
import os
import signal
import sqlite3
import sys
from datetime import datetime

import pytz

# --- Config ---
DB_PATH = "/mnt/ssd/nyc_traffic.db"
ENV_PATH = "/home/ubuntu/nyc-traffic-visualizer/.env"
PID_FILE = "/tmp/stats.pid"
NYC_TZ = pytz.timezone("America/New_York")

# Speed filter per Section 4.7: discard zeros and implausible readings
MIN_SPEED = 0.1
MAX_SPEED = 90.0

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


def write_pid(path):
    with open(path, "w") as f:
        f.write(str(os.getpid()))
    log.info("PID %d written to %s", os.getpid(), path)


def to_nyc_time(data_as_of_str):
    """Parse data_as_of string and convert to NYC local time."""
    # Timestamps from the API are UTC
    dt_utc = datetime.strptime(data_as_of_str, "%Y-%m-%dT%H:%M:%S.%f")
    dt_utc = pytz.utc.localize(dt_utc)
    return dt_utc.astimezone(NYC_TZ)


def get_bucket(dt_nyc):
    """Map a NYC-local datetime to a 30-minute bucket (0-47)."""
    return (dt_nyc.hour * 60 + dt_nyc.minute) // 30


def get_day_of_week(dt_nyc):
    """Monday=0, Sunday=6 — consistent with Python's weekday()."""
    return dt_nyc.weekday()


def welford_update(count, mean, m2, new_value):
    """
    Welford's online algorithm for mean and variance.
    Returns updated (count, mean, m2).
    stddev = sqrt(m2 / count) when needed.
    """
    count += 1
    delta = new_value - mean
    mean += delta / count
    delta2 = new_value - mean
    m2 += delta * delta2
    return count, mean, m2


def process_readings(conn):
    cursor = conn.cursor()

    # Get last processed reading ID per link — NULL means start from 0
    cursor.execute("SELECT link_id, last_processed_reading_id FROM links")
    links = {row[0]: row[1] or 0 for row in cursor.fetchall()}

    if not links:
        log.warning("No links found in database")
        return

    total_processed = 0

    for link_id, last_id in links.items():
        cursor.execute(
            """
            SELECT id, speed, travel_time, data_as_of
            FROM readings
            WHERE link_id = ? AND id > ?
            ORDER BY id ASC
            """,
            (link_id, last_id)
        )
        rows = cursor.fetchall()

        if not rows:
            continue

        new_last_id = last_id

        for row_id, speed, travel_time, data_as_of in rows:
            # Filter invalid readings per Section 4.7
            if not (MIN_SPEED <= speed <= MAX_SPEED):
                new_last_id = row_id
                continue
            if travel_time <= 0:
                new_last_id = row_id
                continue

            try:
                dt_nyc = to_nyc_time(data_as_of)
            except ValueError:
                new_last_id = row_id
                continue

            bucket = get_bucket(dt_nyc)
            day_of_week = get_day_of_week(dt_nyc)

            # Fetch existing Welford state for (link_id, day_of_week, bucket)
            cursor.execute(
                """
                SELECT count, mean_speed, m2_speed, mean_tt, m2_tt
                FROM link_stats
                WHERE link_id = ? AND day_of_week = ? AND bucket = ?
                """,
                (link_id, day_of_week, bucket)
            )
            existing = cursor.fetchone()

            if existing:
                count, mean_speed, m2_speed, mean_tt, m2_tt = existing
            else:
                count, mean_speed, m2_speed, mean_tt, m2_tt = 0, 0.0, 0.0, 0.0, 0.0

            # Update Welford state for speed and travel_time independently
            count, mean_speed, m2_speed = welford_update(count, mean_speed, m2_speed, speed)
            count, mean_tt, m2_tt = welford_update(count - 1, mean_tt, m2_tt, travel_time)

            # SQLite 3.22 compatible upsert — no ON CONFLICT DO UPDATE
            if existing:
                cursor.execute(
                    """
                    UPDATE link_stats
                    SET count=?, mean_speed=?, m2_speed=?, mean_tt=?, m2_tt=?
                    WHERE link_id=? AND day_of_week=? AND bucket=?
                    """,
                    (count, mean_speed, m2_speed, mean_tt, m2_tt, link_id, day_of_week, bucket)
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO link_stats (link_id, day_of_week, bucket, count, mean_speed, m2_speed, mean_tt, m2_tt)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (link_id, day_of_week, bucket, count, mean_speed, m2_speed, mean_tt, m2_tt)
                )

            new_last_id = row_id
            total_processed += 1

        # Update high-water mark for this link
        if new_last_id > last_id:
            cursor.execute(
                "UPDATE links SET last_processed_reading_id = ? WHERE link_id = ?",
                (new_last_id, link_id)
            )

    conn.commit()
    log.info("Processed %d readings across %d links", total_processed, len(links))


def run(conn):
    log.info("Running stats update...")
    try:
        process_readings(conn)
    except Exception as e:
        log.error("Error during stats processing: %s", e)


def main():
    load_env(ENV_PATH)
    write_pid(PID_FILE)

    conn = sqlite3.connect(DB_PATH)

    # Handle SIGUSR1 — triggered by poller.py after each successful poll
    def handle_sigusr1(signum, frame):
        log.info("Received SIGUSR1 from poller")
        run(conn)

    signal.signal(signal.SIGUSR1, handle_sigusr1)
    log.info("Stats service started, waiting for signals...")

    # Run once on startup to backfill any existing readings
    run(conn)

    # Sleep indefinitely, waking only on SIGUSR1
    while True:
        signal.pause()


if __name__ == "__main__":
    main()
