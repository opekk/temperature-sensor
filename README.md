# Temperature Sensor

A live temperature monitoring station running at [sensors.opekk.dev](https://sensors.opekk.dev).

## What it does

An ESP32 microcontroller reads temperature from a DS18B20 probe once per second, shows the current value on a small OLED display, and streams each reading over USB to a server. The server stores the history, exposes a simple JSON API, and serves a dashboard where you can see:

- the current temperature with a "last updated" indicator,
- a 1-hour chart with raw readings,
- a 24-hour chart averaged into 2-minute buckets,
- min, max and average for the selected period.

The page refreshes itself, so it always reflects the physical room the sensor is sitting in.

## How it's done

**Sensor side.** An ESP32 runs a small Arduino sketch that drives a DS18B20 over OneWire and an SSD1306 OneLED over I²C. Each second it requests a fresh reading, redraws the screen, and prints a `TEMP:<value>` line to the USB serial port.

**Server side.** A single Python file (standard library only, plus `pyserial`) runs on a machine that the ESP32 is plugged into. One thread keeps a serial connection open and parses incoming lines; another thread serves HTTP. Readings are persisted to SQLite at most once every 10 seconds to keep the database small while still giving smooth charts. Historical queries either return raw rows (for the 1h view) or aggregate with SQL `AVG` and time bucketing (for the 24h view).

**Frontend.** The dashboard is a single HTML page embedded in the Python server. It polls the current-temperature endpoint every second and the history endpoint every ten seconds, and renders the chart with Chart.js. Typography and color are intentionally a bit editorial rather than a typical dashboard look.

**Deployment.** The server ships as a Docker image based on `python:3.13-alpine`. The container runs as a non-root user that belongs to the `dialout` group so it can read the USB serial device, and persists the SQLite database on a mounted `/data` volume. The public site is reverse-proxied to port 8000.

## Hardware

- ESP32 dev board
- DS18B20 waterproof temperature probe (+ 4.7 kΩ pull-up)
- SSD1306 128×64 OLED over I²C
