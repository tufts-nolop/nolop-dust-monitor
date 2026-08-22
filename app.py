from __future__ import annotations

import csv
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import serial
from flask import Flask, Response, jsonify, render_template


SERIAL_PORT = os.getenv("DYLOS_PORT", "/dev/serial/by-id/usb-FTDI_USB_Serial_Converter_FTDN821F-if00-port0")
BAUDRATE = int(os.getenv("DYLOS_BAUDRATE", "9600"))
APP_ROOT = Path(__file__).resolve().parent

PURPLEAIR_API_KEY = os.getenv("PURPLEAIR_API_KEY", "")
PURPLEAIR_SENSOR_INDEX = os.getenv("PURPLEAIR_SENSOR_INDEX", "")
PURPLEAIR_POLL_INTERVAL = float(os.getenv("PURPLEAIR_POLL_INTERVAL", "300"))
DEFAULT_OUTDOOR_AQI_MAX = 200  # default yaxis2 ceiling, gets overridden if AQI is higher


def resolve_log_path(path: str | os.PathLike[str] | Path) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate
    return APP_ROOT / candidate


LOG_FILE = resolve_log_path(os.getenv("DYLOS_LOG_FILE", str(APP_ROOT / "dylos_log.csv")))
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
MAX_POINTS = int(os.getenv("DYLOS_MAX_POINTS", "20000"))
RECONNECT_DELAY = float(os.getenv("DYLOS_RECONNECT_DELAY", "5"))

OUTDOOR_LOG_FILE = resolve_log_path(os.getenv("OUTDOOR_LOG_FILE", str(APP_ROOT / "outdoor_aqi_log.csv")))
OUTDOOR_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
OUTDOOR_MAX_POINTS = int(os.getenv("OUTDOOR_MAX_POINTS", "20000"))

MAX_DYLOS_COUNT = 3000  # default, gets overridden if data particle count is higher

# "take the difference between the two readings, the .5 and the 2.5, then divide by 100 to get micrograms per cubic meter"
# to estimate PM2.5, per Dylos support
DYLOS_COUNTS_TO_UG_PER_CUBIC_METER_CONVERSION = 100
OSHA_15_MIN_STEL_MG_PER_CUBIC_METER = 10
OSHA_8_HR_TWA_PEL_MG_PER_CUBIC_METER = 5
NIOSH_8_HR_TWA_REL_MG_PER_CUBIC_METER = 1
UG_PER_MG = 1000

app = Flask(__name__)

history: deque[dict[str, Any]] = deque(maxlen=MAX_POINTS)
history_lock = threading.Lock()

outdoor_history: deque[dict[str, Any]] = deque(maxlen=OUTDOOR_MAX_POINTS)
outdoor_history_lock = threading.Lock()

status_lock = threading.Lock()
status = {
    "connected": False,
    "message": "Starting",
    "last_sample": None,
    "small": None,
    "large": None,
}

outdoor_status_lock = threading.Lock()
outdoor_status = {
    "connected": False,
    "message": "Starting",
    "last_sample": None,
    "pm25": None,
    "aqi": None,
}

service_lock = threading.Lock()
services_started = False


def find_free_port(start_port: int, max_tries: int = 20) -> int:
    for offset in range(max_tries):
        candidate = start_port + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("0.0.0.0", candidate))
                return candidate
            except OSError:
                continue
    return start_port


def set_status(**updates: Any) -> None:
    with status_lock:
        status.update(updates)


def get_status() -> dict[str, Any]:
    with status_lock:
        return dict(status)


def set_outdoor_status(**updates: Any) -> None:
    with outdoor_status_lock:
        outdoor_status.update(updates)


def get_outdoor_status() -> dict[str, Any]:
    with outdoor_status_lock:
        return dict(outdoor_status)


def parse_dylos_line(raw_line: str) -> tuple[float, float | None] | None:
    text = raw_line.strip()

    if not text:
        return None

    if text.count(",") > 1:
        return None

    parts = [part.strip() for part in text.split(",")]

    if len(parts) == 1:
        try:
            return float(parts[0]), None
        except ValueError:
            return None

    if len(parts) == 2:
        if not parts[0] and not parts[1]:
            return None

        first_text, second_text = parts

        if not first_text and not second_text:
            return None

        if not first_text:
            try:
                return 0.0, float(second_text)
            except ValueError:
                return None

        if not second_text:
            try:
                return float(first_text), None
            except ValueError:
                return None

        try:
            return float(first_text), float(second_text)
        except ValueError:
            return None

    return None


def append_log(
    timestamp: datetime,
    raw_line: str,
    small: float | None,
    large: float | None,
) -> None:
    log_path = resolve_log_path(LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not log_path.exists() or log_path.stat().st_size == 0

    with log_path.open("a", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)

        if needs_header:
            writer.writerow(["timestamp", "raw_line", "small", "large"])

        writer.writerow(
            [
                timestamp.isoformat(timespec="seconds"),
                raw_line,
                small,
                large,
            ]
        )


def load_existing_history() -> None:
    log_path = resolve_log_path(LOG_FILE)
    if not log_path.exists():
        return

    with log_path.open("r", newline="", encoding="utf-8-sig") as csvfile:
        reader = csv.reader(csvfile)

        for row in reader:
            if not row:
                continue

            if row[0].strip().lower() == "timestamp":
                continue

            if len(row) >= 4:
                timestamp_text = row[0].strip()
                small_text = row[-2].strip()
                large_text = row[-1].strip()
            elif len(row) == 3:
                timestamp_text = row[0].strip()
                small_text = row[1].strip()
                large_text = row[2].strip()
            else:
                continue

            try:
                timestamp = datetime.fromisoformat(timestamp_text)
                small = float(small_text)
                large = float(large_text)
            except (ValueError, TypeError):
                continue

            history.append(
                {
                    "timestamp": timestamp,
                    "small": small,
                    "large": large,
                }
            )


def pm25_to_aqi(pm25: float) -> float:
    """Convert a raw PM2.5 concentration (ug/m3) to AQI using the EPA's
    published piecewise-linear breakpoint formula. This is the same
    calculation used to derive the AQI shown on PurpleAir's own map."""
    pm25 = max(0.0, pm25)

    # (pm_low, pm_high, aqi_low, aqi_high)
    breakpoints = [
        (0.0, 12.0, 0, 50),
        (12.1, 35.4, 51, 100),
        (35.5, 55.4, 101, 150),
        (55.5, 150.4, 151, 200),
        (150.5, 250.4, 201, 300),
        (250.5, 350.4, 301, 400),
        (350.5, 500.4, 401, 500),
    ]

    for pm_low, pm_high, aqi_low, aqi_high in breakpoints:
        if pm_low <= pm25 <= pm_high:
            return round(
                (aqi_high - aqi_low) / (pm_high - pm_low) * (pm25 - pm_low) + aqi_low
            )

    # above the top breakpoint (hazardous), cap at 500
    return 500


def fetch_purpleair_pm25() -> float:
    url = f"https://api.purpleair.com/v1/sensors/{PURPLEAIR_SENSOR_INDEX}?fields=pm2.5"
    request = urllib.request.Request(url, headers={"X-API-Key": PURPLEAIR_API_KEY})

    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))

    return float(payload["sensor"]["pm2.5"])


def append_outdoor_log(timestamp: datetime, pm25: float, aqi: float) -> None:
    log_path = resolve_log_path(OUTDOOR_LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not log_path.exists() or log_path.stat().st_size == 0

    with log_path.open("a", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)

        if needs_header:
            writer.writerow(["timestamp", "pm25", "aqi"])

        writer.writerow([timestamp.isoformat(timespec="seconds"), pm25, aqi])


def load_existing_outdoor_history() -> None:
    log_path = resolve_log_path(OUTDOOR_LOG_FILE)
    if not log_path.exists():
        return

    with log_path.open("r", newline="", encoding="utf-8-sig") as csvfile:
        reader = csv.reader(csvfile)

        for row in reader:
            if not row:
                continue

            if row[0].strip().lower() == "timestamp":
                continue

            if len(row) < 3:
                continue

            try:
                timestamp = datetime.fromisoformat(row[0].strip())
                pm25 = float(row[1].strip())
                aqi = float(row[2].strip())
            except (ValueError, TypeError):
                continue

            outdoor_history.append({"timestamp": timestamp, "pm25": pm25, "aqi": aqi})


def purpleair_worker() -> None:
    if not PURPLEAIR_API_KEY or not PURPLEAIR_SENSOR_INDEX:
        message = (
            "PurpleAir not configured (set PURPLEAIR_API_KEY and "
            "PURPLEAIR_SENSOR_INDEX). Outdoor AQI will not be shown."
        )
        print(message)
        set_outdoor_status(connected=False, message=message)
        return

    while True:
        try:
            pm25 = fetch_purpleair_pm25()
            aqi = pm25_to_aqi(pm25)
            timestamp = datetime.now().astimezone()

            append_outdoor_log(timestamp, pm25, aqi)

            with outdoor_history_lock:
                outdoor_history.append(
                    {"timestamp": timestamp, "pm25": pm25, "aqi": aqi}
                )

            set_outdoor_status(
                connected=True,
                message="Connected to PurpleAir",
                last_sample=timestamp,
                pm25=pm25,
                aqi=aqi,
            )

            print(f"{timestamp:%Y-%m-%d %H:%M:%S} outdoor pm2.5={pm25:g}, aqi={aqi:g}")

        except (urllib.error.URLError, ValueError, KeyError, TimeoutError) as exc:
            message = f"PurpleAir request failed: {exc}. Retrying in {PURPLEAIR_POLL_INTERVAL:g} seconds."
            print(message)
            set_outdoor_status(connected=False, message=message)

        time.sleep(PURPLEAIR_POLL_INTERVAL)


def serial_worker() -> None:
    while True:
        set_status(connected=False, message=f"Opening {SERIAL_PORT}")

        try:
            with serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1.0) as ser:
                ser.reset_input_buffer()
                set_status(connected=True, message=f"Connected to {SERIAL_PORT}")

                print(f"Listening on {SERIAL_PORT} at {BAUDRATE} baud.")
                print(f"Logging to {LOG_FILE.resolve()}")

                while True:
                    raw_line = ser.readline().decode("utf-8", errors="ignore").strip()

                    if not raw_line:
                        continue

                    timestamp = datetime.now().astimezone()
                    parsed = parse_dylos_line(raw_line)

                    if parsed is None:
                        append_log(timestamp, raw_line, None, None)
                        print(f"Ignored malformed line: {raw_line!r}")
                        continue

                    small, large = parsed
                    small_value = 0.0 if small is None else small
                    large_value = 0.0 if large is None else large
                    append_log(timestamp, raw_line, small_value, large_value)

                    with history_lock:
                        history.append(
                            {
                                "timestamp": timestamp,
                                "small": small_value,
                                "large": large_value,
                            }
                        )

                    set_status(
                        connected=True,
                        message=f"Connected to {SERIAL_PORT}",
                        last_sample=timestamp,
                        small=small_value,
                        large=large_value,
                    )

                    print(
                        f"{timestamp:%Y-%m-%d %H:%M:%S} "
                        f"fine={small:g}, coarse={large:g}"
                    )

        except (serial.SerialException, OSError) as exc:
            message = (
                f"Serial connection failed: {exc}. "
                f"Retrying in {RECONNECT_DELAY:g} seconds."
            )
            print(message)
            set_status(connected=False, message=message)
            time.sleep(RECONNECT_DELAY)


def start_services() -> None:
    global services_started

    with service_lock:
        if services_started:
            return

        load_existing_history()
        load_existing_outdoor_history()

        thread = threading.Thread(
            target=serial_worker,
            name="dylos-serial-reader",
            daemon=True,
        )
        thread.start()

        outdoor_thread = threading.Thread(
            target=purpleair_worker,
            name="purpleair-poller",
            daemon=True,
        )
        outdoor_thread.start()

        services_started = True


@app.before_request
def ensure_services_started() -> None:
    if not services_started:
        start_services()


@app.get("/")
def index() -> str:
    return render_template(
        "index.html",
        status=get_status(),
        port=SERIAL_PORT,
    )


@app.get("/data.json")
def data_json() -> Response:
    with history_lock:
        samples = list(history)

    with outdoor_history_lock:
        outdoor_samples = list(outdoor_history)

    timestamps = [sample["timestamp"].isoformat() for sample in samples]
    fine_counts = [sample["small"] for sample in samples]
    coarse_counts = [sample["large"] for sample in samples]

    y_max = MAX_DYLOS_COUNT
    if fine_counts:
        y_max = max(MAX_DYLOS_COUNT, max(fine_counts) + 100)

    outdoor_timestamps = [sample["timestamp"].isoformat() for sample in outdoor_samples]
    outdoor_aqi = [sample["aqi"] for sample in outdoor_samples]

    outdoor_y_max = DEFAULT_OUTDOOR_AQI_MAX
    if outdoor_aqi:
        outdoor_y_max = max(DEFAULT_OUTDOOR_AQI_MAX, max(outdoor_aqi) + 20)

    payload = {
        "timestamps": timestamps,
        "fine": fine_counts,
        "coarse": coarse_counts,
        "y_max": y_max,
        "outdoor_timestamps": outdoor_timestamps,
        "outdoor_aqi": outdoor_aqi,
        "outdoor_y_max": outdoor_y_max,
        "thresholds": [
            {
                "label": "OSHA 15-min exposure limit, non-exotic wood dust (10 mg/m\u00b3)",
                "value": OSHA_15_MIN_STEL_MG_PER_CUBIC_METER
                * UG_PER_MG
                * DYLOS_COUNTS_TO_UG_PER_CUBIC_METER_CONVERSION,
                "color": "#ff4d4d",
            },
            {
                "label": "OSHA 8-hr average limit, softwood dust (5 mg/m\u00b3)",
                "value": OSHA_8_HR_TWA_PEL_MG_PER_CUBIC_METER
                * UG_PER_MG
                * DYLOS_COUNTS_TO_UG_PER_CUBIC_METER_CONVERSION,
                "color": "#ffa63d",
            },
            {
                "label": "NIOSH 8-hr average limit, softwood dust (1 mg/m\u00b3)",
                "value": NIOSH_8_HR_TWA_REL_MG_PER_CUBIC_METER
                * UG_PER_MG
                * DYLOS_COUNTS_TO_UG_PER_CUBIC_METER_CONVERSION,
                "color": "#f5e642",
            },
        ],
    }

    return jsonify(payload)


@app.get("/status-panel")
def status_panel() -> str:
    return render_template(
        "status.html",
        status=get_status(),
        port=SERIAL_PORT,
    )


if __name__ == "__main__":
    start_services()

    requested_port = int(os.getenv("DYLOS_WEB_PORT", "5000"))
    port = requested_port if requested_port != 0 else find_free_port(5000)
    if requested_port != 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("0.0.0.0", requested_port))
            except OSError:
                port = find_free_port(requested_port)

    print(f"Starting Flask dashboard on port {port}")
    app.run(
        host=os.getenv("DYLOS_HOST", "0.0.0.0"),
        port=port,
        threaded=True,
        debug=False,
        use_reloader=False,
    )
