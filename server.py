import json
import os
import sqlite3
import threading
import time as time_module
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import serial

SERIAL_PORT = os.environ.get("SERIAL_PORT", "/dev/ttyUSB0")
BAUD_RATE = 115200
DB_FILE = Path("/data/sensors.db")

latest_reading = {"temperature": None, "updated_at": None}
_last_db_save = 0


def init_db():
    with sqlite3.connect(str(DB_FILE)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                temperature REAL,
                recorded_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_recorded_at ON readings(recorded_at)")


def load_latest():
    global latest_reading
    try:
        with sqlite3.connect(str(DB_FILE)) as conn:
            row = conn.execute(
                "SELECT temperature, recorded_at FROM readings ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                latest_reading = {"temperature": row[0], "updated_at": row[1]}
    except Exception:
        pass


def save_reading():
    global _last_db_save
    now = time_module.time()
    if now - _last_db_save < 10:
        return
    _last_db_save = now
    try:
        with sqlite3.connect(str(DB_FILE)) as conn:
            conn.execute(
                "INSERT INTO readings (temperature, recorded_at) VALUES (?, ?)",
                (latest_reading["temperature"], latest_reading["updated_at"]),
            )
    except Exception as e:
        print(f"DB error: {e}")


def get_history(period):
    periods = {
        "1h":  {"delta": timedelta(hours=1),  "mode": "raw"},
        "24h": {"delta": timedelta(hours=24), "mode": "minutes", "bucket_minutes": 2},
    }
    config = periods.get(period, periods["24h"])
    since = (datetime.now(timezone.utc) - config["delta"]).isoformat()

    with sqlite3.connect(str(DB_FILE)) as conn:
        if config["mode"] == "raw":
            rows = conn.execute(
                "SELECT temperature, recorded_at FROM readings WHERE recorded_at > ? ORDER BY recorded_at",
                (since,),
            ).fetchall()
            return [{"temp": r[0], "time": r[1]} for r in rows]

        minutes = config["bucket_minutes"]
        rows = conn.execute(
            """
            SELECT
                AVG(temperature),
                strftime('%Y-%m-%dT%H:', recorded_at) ||
                    printf('%02d', (CAST(strftime('%M', recorded_at) AS INT) / ? * ?)) ||
                    ':00' as bucket
            FROM readings
            WHERE recorded_at > ?
            GROUP BY bucket
            ORDER BY bucket
            """,
            (minutes, minutes, since),
        ).fetchall()
        return [{"temp": round(r[0], 2) if r[0] else None, "time": r[1]} for r in rows]


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sensors | sensors.opekk.dev</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #0f172a;
            color: #e2e8f0;
            min-height: 100vh;
            padding: 2rem 1rem;
        }
        .container {
            max-width: 850px;
            margin: 0 auto;
        }
        h1 {
            font-size: 1.1rem;
            font-weight: 500;
            color: #64748b;
            margin-bottom: 1.5rem;
            letter-spacing: 0.05em;
            text-transform: uppercase;
        }
        .card {
            background: #1e293b;
            border-radius: 1rem;
            box-shadow: 0 4px 24px rgba(0, 0, 0, 0.3);
            padding: 2rem;
            margin-bottom: 1rem;
        }
        .temp-display {
            display: flex;
            align-items: baseline;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 1rem;
        }
        .temp-main {
            display: flex;
            align-items: baseline;
            gap: 0.3rem;
        }
        .temp-value { font-size: 4rem; font-weight: 700; line-height: 1; }
        .temp-unit { font-size: 1.5rem; color: #94a3b8; }
        .temp-label { font-size: 0.85rem; color: #64748b; margin-bottom: 0.5rem; }
        .updated {
            font-size: 0.8rem;
            color: #475569;
            text-align: right;
        }
        .updated.stale { color: #f59e0b; }
        .stats {
            display: flex;
            gap: 1.5rem;
            flex-wrap: wrap;
        }
        .stat {
            display: flex;
            flex-direction: column;
            gap: 0.15rem;
        }
        .stat-label { font-size: 0.75rem; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; }
        .stat-value { font-size: 1.1rem; font-weight: 600; }
        .stat-value.low { color: #38bdf8; }
        .stat-value.high { color: #f87171; }
        .stat-value.avg { color: #a78bfa; }
        .no-data { font-size: 1.3rem; color: #475569; padding: 1rem 0; }
        .period-buttons {
            display: flex;
            gap: 0.4rem;
            margin-bottom: 1.2rem;
        }
        .period-btn {
            padding: 0.5rem 1.2rem;
            border: 1px solid #334155;
            background: transparent;
            color: #94a3b8;
            border-radius: 0.5rem;
            cursor: pointer;
            font-size: 0.9rem;
            transition: all 0.15s;
        }
        .period-btn.active {
            background: #3b82f6;
            color: #fff;
            border-color: #3b82f6;
        }
        .period-btn:hover:not(.active) { background: #334155; }
        .chart-wrap { position: relative; min-height: 200px; }
        .chart-empty {
            position: absolute;
            inset: 0;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #475569;
            font-size: 0.95rem;
        }
        @media (max-width: 500px) {
            .temp-value { font-size: 3rem; }
            .temp-display { flex-direction: column; gap: 0.5rem; }
            .updated { text-align: left; }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>sensors.opekk.dev</h1>
        <div class="card">
            <div class="temp-label">Temperatura</div>
            <div id="reading"><div class="no-data">Loading...</div></div>
        </div>
        <div class="card">
            <div class="period-buttons">
                <button class="period-btn active" data-period="1h">1h</button>
                <button class="period-btn" data-period="24h">24h</button>
            </div>
            <div id="stats" class="stats" style="margin-bottom:1rem"></div>
            <div class="chart-wrap">
                <div id="chartEmpty" class="chart-empty">No history data yet</div>
                <canvas id="tempChart"></canvas>
            </div>
        </div>
    </div>
    <script>
        // Live temperature update
        async function updateTemp() {
            try {
                const res = await fetch("/api/temperature");
                const data = await res.json();
                const el = document.getElementById("reading");
                if (data.temperature !== null) {
                    let ago = "";
                    if (data.updated_at) {
                        const diff = Math.floor((Date.now() - new Date(data.updated_at).getTime()) / 1000);
                        const stale = diff > 60;
                        if (diff < 10) ago = "just now";
                        else if (diff < 60) ago = diff + "s ago";
                        else if (diff < 3600) ago = Math.floor(diff/60) + "m ago";
                        else ago = Math.floor(diff/3600) + "h ago";
                        el.innerHTML = '<div class="temp-display">'
                            + '<div class="temp-main"><span class="temp-value">' + data.temperature.toFixed(1) + '</span><span class="temp-unit">\\u00b0C</span></div>'
                            + '<div class="updated ' + (stale ? 'stale' : '') + '">Updated ' + ago + '</div>'
                            + '</div>';
                    } else {
                        el.innerHTML = '<div class="temp-display"><div class="temp-main"><span class="temp-value">' + data.temperature.toFixed(1) + '</span><span class="temp-unit">\\u00b0C</span></div></div>';
                    }
                } else {
                    el.innerHTML = '<div class="no-data">No data yet</div>';
                }
            } catch (e) {}
        }
        updateTemp();
        setInterval(updateTemp, 1000);

        // Chart
        const ctx = document.getElementById("tempChart").getContext("2d");
        const chart = new Chart(ctx, {
            type: "line",
            data: {
                datasets: [{
                    label: "Temperature",
                    borderColor: "#3b82f6",
                    backgroundColor: "rgba(59, 130, 246, 0.08)",
                    borderWidth: 2,
                    pointRadius: 0,
                    pointHitRadius: 10,
                    fill: true,
                    tension: 0.3,
                    data: []
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: true,
                interaction: { intersect: false, mode: "index" },
                scales: {
                    x: {
                        type: "time",
                        ticks: { color: "#64748b", maxTicksLimit: 6, font: { size: 11 } },
                        grid: { color: "rgba(51,65,85,0.5)" }
                    },
                    y: {
                        ticks: {
                            color: "#64748b",
                            font: { size: 11 },
                            callback: function(v) { return v.toFixed(1) + "\\u00b0"; }
                        },
                        grid: { color: "rgba(51,65,85,0.5)" }
                    }
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: "#1e293b",
                        borderColor: "#334155",
                        borderWidth: 1,
                        titleColor: "#94a3b8",
                        bodyColor: "#e2e8f0",
                        padding: 10,
                        callbacks: {
                            label: function(ctx) { return " " + ctx.parsed.y.toFixed(1) + "\\u00b0C"; }
                        }
                    }
                }
            }
        });

        let currentPeriod = "1h";

        function updateStats(data) {
            const el = document.getElementById("stats");
            if (!data.length) { el.innerHTML = ""; return; }
            const temps = data.map(d => d.temp).filter(t => t !== null);
            if (!temps.length) { el.innerHTML = ""; return; }
            const min = Math.min(...temps);
            const max = Math.max(...temps);
            const avg = temps.reduce((a, b) => a + b, 0) / temps.length;
            el.innerHTML =
                '<div class="stat"><span class="stat-label">Min</span><span class="stat-value low">' + min.toFixed(1) + '\\u00b0C</span></div>'
                + '<div class="stat"><span class="stat-label">Max</span><span class="stat-value high">' + max.toFixed(1) + '\\u00b0C</span></div>'
                + '<div class="stat"><span class="stat-label">Avg</span><span class="stat-value avg">' + avg.toFixed(1) + '\\u00b0C</span></div>';
        }

        async function loadChart(period) {
            currentPeriod = period;
            try {
                const res = await fetch("/api/readings/history?period=" + period);
                const data = await res.json();
                const empty = document.getElementById("chartEmpty");
                if (data.length === 0) {
                    empty.style.display = "flex";
                } else {
                    empty.style.display = "none";
                }
                chart.data.datasets[0].data = data.map(d => ({
                    x: new Date(d.time),
                    y: d.temp
                }));
                chart.update();
                updateStats(data);
            } catch (e) {}
        }

        document.querySelectorAll(".period-btn").forEach(btn => {
            btn.addEventListener("click", () => {
                document.querySelectorAll(".period-btn").forEach(b => b.classList.remove("active"));
                btn.classList.add("active");
                loadChart(btn.dataset.period);
            });
        });

        loadChart("1h");
        setInterval(() => loadChart(currentPeriod), 10000);
    </script>
</body>
</html>"""


class SensorHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/temperature":
            self._json_response(latest_reading)
            return

        if parsed.path == "/api/readings/history":
            params = parse_qs(parsed.query)
            period = params.get("period", ["24h"])[0]
            history = get_history(period)
            self._json_response(history)
            return

        if parsed.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode())
            return

        self.send_response(404)
        self.end_headers()

    def _json_response(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        print(f"{self.client_address[0]} - {format % args}")


def serial_reader():
    while True:
        try:
            with serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=5) as ser:
                print(f"Reading from {SERIAL_PORT}")
                while True:
                    line = ser.readline().decode("utf-8", errors="ignore").strip()
                    if line.startswith("TEMP:"):
                        try:
                            temperature = float(line[5:])
                            latest_reading["temperature"] = temperature
                            latest_reading["updated_at"] = datetime.now(timezone.utc).isoformat()
                            save_reading()
                            print(f"Temperature updated: {temperature}")
                        except ValueError:
                            pass
        except serial.SerialException as e:
            print(f"Serial error: {e}, retrying in 5s...")
            time_module.sleep(5)


if __name__ == "__main__":
    init_db()
    load_latest()
    reader = threading.Thread(target=serial_reader, daemon=True)
    reader.start()
    server = HTTPServer(("0.0.0.0", 8000), SensorHandler)
    print("Sensors server running on port 8000")
    server.serve_forever()
