#!/usr/bin/env python3

import json
import logging
import math
import os
import sqlite3
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytz

# --- Config ---
DB_PATH = "/mnt/ssd/nyc_traffic.db"
ENV_PATH = "/home/ubuntu/nyc-traffic-visualizer/.env"
PORT = 8080
NYC_TZ = pytz.timezone("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
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


def get_current_bucket():
    now_nyc = datetime.now(NYC_TZ)
    bucket = (now_nyc.hour * 60 + now_nyc.minute) // 30
    day_of_week = now_nyc.weekday()
    return bucket, day_of_week


def get_stats():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    bucket, day_of_week = get_current_bucket()

    # Get latest speed per link
    cursor.execute("""
        SELECT r.link_id, r.speed, r.travel_time, r.data_as_of
        FROM readings r
        INNER JOIN (
            SELECT link_id, MAX(id) as max_id
            FROM readings
            GROUP BY link_id
        ) latest ON r.link_id = latest.link_id AND r.id = latest.max_id
    """)
    latest = {row["link_id"]: dict(row) for row in cursor.fetchall()}

    # Get historical stats for current time bucket
    cursor.execute("""
        SELECT ls.link_id, ls.count, ls.mean_speed, ls.m2_speed, ls.mean_tt, ls.m2_tt,
               l.link_name, l.borough, l.encoded_poly_line
        FROM link_stats ls
        JOIN links l ON ls.link_id = l.link_id
        WHERE ls.day_of_week = ? AND ls.bucket = ?
    """, (day_of_week, bucket))

    features = []
    for row in cursor.fetchall():
        link_id = row["link_id"]
        count = row["count"]
        mean_speed = row["mean_speed"]
        m2_speed = row["m2_speed"]
        encoded_poly = row["encoded_poly_line"]

        if not encoded_poly:
            continue

        # Compute stddev
        stddev_speed = math.sqrt(m2_speed / count) if count > 1 else 0.0

        # Get live speed
        live = latest.get(link_id)
        live_speed = live["speed"] if live else None
        data_as_of = live["data_as_of"] if live else None

        # Z-score: positive = slower than normal (congested)
        if stddev_speed > 0 and live_speed is not None:
            z_score = (mean_speed - live_speed) / stddev_speed
        else:
            z_score = None

        features.append({
            "link_id": link_id,
            "link_name": row["link_name"],
            "borough": row["borough"],
            "encoded_poly": encoded_poly,
            "mean_speed": round(mean_speed, 2),
            "stddev_speed": round(stddev_speed, 2),
            "live_speed": round(live_speed, 2) if live_speed is not None else None,
            "z_score": round(z_score, 2) if z_score is not None else None,
            "count": count,
            "bucket": bucket,
            "day_of_week": day_of_week,
            "data_as_of": data_as_of,
        })

    conn.close()

    return {
        "features": features,
        "bucket": bucket,
        "day_of_week": day_of_week,
        "generated_at": datetime.now(NYC_TZ).isoformat(),
    }


def get_all_links():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # All-time mean speed per link across all buckets/days
    cursor.execute("""
        SELECT
            l.link_id, l.link_name, l.borough, l.encoded_poly_line,
            SUM(ls.count) as total_count,
            SUM(ls.mean_speed * ls.count) / SUM(ls.count) as overall_mean_speed
        FROM links l
        JOIN link_stats ls ON l.link_id = ls.link_id
        GROUP BY l.link_id
    """)

    features = []
    for row in cursor.fetchall():
        if not row["encoded_poly_line"]:
            continue
        features.append({
            "link_id": row["link_id"],
            "link_name": row["link_name"],
            "borough": row["borough"],
            "encoded_poly": row["encoded_poly_line"],
            "mean_speed": round(row["overall_mean_speed"], 2) if row["overall_mean_speed"] else None,
            "count": row["total_count"],
            "z_score": None,
            "stddev_speed": None,
            "live_speed": None,
            "data_as_of": None,
            "bucket": None,
            "day_of_week": None,
        })

    conn.close()
    bucket, day_of_week = get_current_bucket()
    return {
        "features": features,
        "bucket": bucket,
        "day_of_week": day_of_week,
        "mode": "all_links",
        "generated_at": datetime.now(NYC_TZ).isoformat(),
    }


class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        log.info("%s - %s", self.address_string(), format % args)

    def do_GET(self):
        if self.path == "/":
            self._serve_file("visualizer.html", "text/html")
        elif self.path == "/api/stats":
            self._serve_json(get_stats())
        elif self.path == "/api/links":
            self._serve_json(get_all_links())
        else:
            self.send_error(404)

    def _serve_file(self, filename, content_type):
        filepath = os.path.join(os.path.dirname(__file__), filename)
        try:
            with open(filepath, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(404)

    def _serve_json(self, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


def main():
    load_env(ENV_PATH)
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log.info("Visualizer running at http://localhost:%d", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
