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
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sensors | sensors.opekk.dev</title>
    <link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=DM+Sans:ital,wght@0,300;0,400;0,500;0,600;1,400&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns"></script>
    <style>
        *, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }
        :root {
            --cream: #E6E3C7;
            --dark: #21201C;
            --gold: #D69F1F;
            --cream-dim: #D8D5B8;
            --dark-soft: #3D3A33;
            --gold-light: #F5E6B8;
            --gold-dark: #A47A12;
            --text-secondary: #6B6860;
        }
        html { scroll-behavior: smooth; }
        body {
            font-family: 'DM Sans', sans-serif;
            background: var(--cream);
            color: var(--dark);
            line-height: 1.6;
            -webkit-font-smoothing: antialiased;
            min-height: 100vh;
            padding: 3rem 2rem;
        }
        .container {
            max-width: 850px;
            margin: 0 auto;
        }
        .section-label {
            font-size: 0.7rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.12em;
            color: var(--gold-dark);
            margin-bottom: 1.5rem;
            display: flex;
            align-items: center;
            gap: 0.6rem;
        }
        .section-label::before {
            content: '';
            width: 20px;
            height: 2px;
            background: var(--gold);
            display: inline-block;
        }
        .card {
            background: rgba(255,255,255,0.35);
            border: 1px solid var(--cream-dim);
            border-radius: 12px;
            padding: 2rem;
            margin-bottom: 1.5rem;
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
            gap: 0.2rem;
        }
        .temp-value {
            font-family: 'DM Serif Display', serif;
            font-size: 4rem;
            letter-spacing: -0.02em;
            line-height: 1;
        }
        .temp-unit {
            font-family: 'DM Sans', sans-serif;
            font-size: 1.5rem;
            color: var(--text-secondary);
        }
        .updated {
            font-size: 0.8rem;
            color: var(--text-secondary);
            text-align: right;
        }
        .updated.stale { color: #b45309; }
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
        .stat-label {
            font-size: 0.7rem;
            font-weight: 600;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.08em;
        }
        .stat-value { font-size: 1.1rem; font-weight: 600; }
        .stat-value.low { color: #2563eb; }
        .stat-value.high { color: #dc2626; }
        .stat-value.avg { color: var(--gold-dark); }
        .no-data { font-size: 1.3rem; color: var(--text-secondary); padding: 1rem 0; }
        .period-buttons {
            display: flex;
            gap: 0.4rem;
            margin-bottom: 1.2rem;
        }
        .period-btn {
            padding: 0.4rem 1rem;
            border: 1px solid var(--dark);
            background: transparent;
            color: var(--dark);
            border-radius: 100px;
            cursor: pointer;
            font-family: 'DM Sans', sans-serif;
            font-size: 0.8rem;
            font-weight: 500;
            letter-spacing: 0.01em;
            transition: all 0.25s ease;
        }
        .period-btn.active {
            background: var(--dark);
            color: var(--cream);
        }
        .period-btn:hover:not(.active) {
            background: var(--dark);
            color: var(--cream);
        }
        .chart-wrap { position: relative; min-height: 200px; }
        .chart-empty {
            position: absolute;
            inset: 0;
            display: flex;
            align-items: center;
            justify-content: center;
            color: var(--text-secondary);
            font-size: 0.95rem;
        }
        @media (max-width: 500px) {
            body { padding: 2rem 1rem; }
            .temp-value { font-size: 3rem; }
            .temp-display { flex-direction: column; gap: 0.5rem; }
            .updated { text-align: left; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="section-label">Temperatura</div>
        <div class="card">
            <div id="reading"><div class="no-data">Loading...</div></div>
        </div>
        <div class="section-label">Historia</div>
        <div class="card">
            <div class="period-buttons">
                <button class="period-btn active" data-period="1h">1h</button>
                <button class="period-btn" data-period="24h">24h</button>
            </div>
            <div id="stats" class="stats" style="margin-bottom:1rem"></div>
            <div class="chart-wrap">
                <div id="chartEmpty" class="chart-empty">Brak danych</div>
                <canvas id="tempChart"></canvas>
            </div>
        </div>
    </div>
    <script>
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
                    el.innerHTML = '<div class="no-data">Brak danych</div>';
                }
            } catch (e) {}
        }
        updateTemp();
        setInterval(updateTemp, 1000);

        const ctx = document.getElementById("tempChart").getContext("2d");
        const chart = new Chart(ctx, {
            type: "line",
            data: {
                datasets: [{
                    label: "Temperature",
                    borderColor: "#A47A12",
                    backgroundColor: "rgba(214,159,31,0.08)",
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
                        ticks: { color: "#6B6860", maxTicksLimit: 6, font: { family: "DM Sans", size: 11 } },
                        grid: { color: "rgba(216,213,184,0.7)" }
                    },
                    y: {
                        ticks: {
                            color: "#6B6860",
                            font: { family: "DM Sans", size: 11 },
                            callback: function(v) { return v.toFixed(1) + "\\u00b0"; }
                        },
                        grid: { color: "rgba(216,213,184,0.7)" }
                    }
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: "#21201C",
                        borderColor: "#3D3A33",
                        borderWidth: 1,
                        titleColor: "#D8D5B8",
                        bodyColor: "#E6E3C7",
                        titleFont: { family: "DM Sans" },
                        bodyFont: { family: "DM Sans" },
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
