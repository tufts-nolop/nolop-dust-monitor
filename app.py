from __future__ import annotations

import csv
import io
import os
import socket
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import serial
from flask import Flask, Response, render_template


SERIAL_PORT = os.getenv("DYLOS_PORT", "/dev/serial/by-id/usb-FTDI_USB_Serial_Converter_FTDN821F-if00-port0")
BAUDRATE = int(os.getenv("DYLOS_BAUDRATE", "9600"))
APP_ROOT = Path(__file__).resolve().parent


def resolve_log_path(path: str | os.PathLike[str] | Path) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate
    return APP_ROOT / candidate


LOG_FILE = resolve_log_path(os.getenv("DYLOS_LOG_FILE", str(APP_ROOT / "dylos_log.csv")))
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
MAX_POINTS = int(os.getenv("DYLOS_MAX_POINTS", "500"))
PLOT_WINDOW_POINTS = int(os.getenv("DYLOS_PLOT_WINDOW_POINTS", "240"))
RECONNECT_DELAY = float(os.getenv("DYLOS_RECONNECT_DELAY", "5"))

MAX_DYLOS_COUNT = 3000 # default, gets overridden if data particle count is higher

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

status_lock = threading.Lock()
status = {
    "connected": False,
    "message": "Starting",
    "last_sample": None,
    "small": None,
    "large": None,
}

plot_condition = threading.Condition()
latest_plot: bytes | None = None
plot_version = 0

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


def make_plot_png() -> bytes:
    with history_lock:
        samples = list(history)

    current_status = get_status()
    fig, ax = plt.subplots(figsize=(14, 7), dpi=120)

    if samples:
        samples = samples[-PLOT_WINDOW_POINTS:]
        timestamps = [sample["timestamp"] for sample in samples]
        fine_counts = [sample["small"] for sample in samples]
        coarse_counts = [sample["large"] for sample in samples]

        ax.plot(timestamps, fine_counts, label="Fine particles", linewidth=2.5)
        ax.plot(timestamps, coarse_counts, label="Coarse particles", linewidth=2.5)

        y_max = max(MAX_DYLOS_COUNT, max(fine_counts) + 100)
        ax.set_ylim(0, y_max)

        display_tz = timestamps[-1].tzinfo
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M", tz=display_tz))
        fig.autofmt_xdate(rotation=25, ha="right")

        latest = samples[-1]
        subtitle = (
            f'Latest sample: {latest["timestamp"]:%Y-%m-%d %H:%M:%S}   '
            f'Fine: {latest["small"]:g}   '
            f'Coarse: {latest["large"]:g}'
        )
    else:
        ax.text(
            0.5,
            0.5,
            "Waiting for valid Dylos data...",
            transform=ax.transAxes,
            ha="center",
            va="center",
            fontsize=20,
        )
        subtitle = "No valid samples have been received yet."

    connection_text = (
        f"Connected to {SERIAL_PORT}"
        if current_status["connected"]
        else current_status["message"]
    )

    ax.set_title(
        "Particle Counts Over Time\n"
        f"{subtitle}\n"
        f"{connection_text}",
        fontsize=16,
        pad=14,
    )
    ax.set_xlabel("Time")
    ax.set_ylabel("Particle Count")
    ax.grid(True, alpha=0.3)

    ax.axhline(
        y=OSHA_15_MIN_STEL_MG_PER_CUBIC_METER * UG_PER_MG * DYLOS_COUNTS_TO_UG_PER_CUBIC_METER_CONVERSION,
        color="crimson",
        linestyle="--",
        linewidth=1.5,
        label="OSHA 15 minute exposure limit for non-exotic wood dust(10 mg/m³)",
    )

    ax.axhline(
        y=OSHA_8_HR_TWA_PEL_MG_PER_CUBIC_METER * UG_PER_MG * DYLOS_COUNTS_TO_UG_PER_CUBIC_METER_CONVERSION,
        color="darkorange",
        linestyle="--",
        linewidth=1.5,
        label="OSHA 8-hour average limit for softwood dust (5 mg/m³)",
    )

    ax.axhline(
        y=NIOSH_8_HR_TWA_REL_MG_PER_CUBIC_METER * UG_PER_MG * DYLOS_COUNTS_TO_UG_PER_CUBIC_METER_CONVERSION,
        color="yellow",
        linestyle="--",
        linewidth=1.5,
        label="NIOSH 8-hour average limit for softwood dust (1 mg/m³)",
    )

    ax.legend(loc="upper left")
    fig.tight_layout()

    image_buffer = io.BytesIO()
    fig.savefig(image_buffer, format="png", bbox_inches="tight")
    plt.close(fig)
    return image_buffer.getvalue()


def update_plot() -> None:
    global latest_plot, plot_version

    new_plot = make_plot_png()

    with plot_condition:
        latest_plot = new_plot
        plot_version += 1
        plot_condition.notify_all()


def serial_worker() -> None:
    while True:
        set_status(connected=False, message=f"Opening {SERIAL_PORT}")
        update_plot()

        try:
            with serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1.0) as ser:
                ser.reset_input_buffer()
                set_status(connected=True, message=f"Connected to {SERIAL_PORT}")
                update_plot()

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

                    update_plot()
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
            update_plot()
            time.sleep(RECONNECT_DELAY)


def start_services() -> None:
    global services_started

    with service_lock:
        if services_started:
            return

        load_existing_history()
        update_plot()

        thread = threading.Thread(
            target=serial_worker,
            name="dylos-serial-reader",
            daemon=True,
        )
        thread.start()
        services_started = True


@app.before_request
def ensure_services_started() -> None:
    if not services_started:
        start_services()


@app.get("/")
def index() -> str:
    return render_template("index.html")


@app.get("/plot-stream")
def plot_stream() -> Response:
    def generate():
        seen_version = -1

        while True:
            with plot_condition:
                plot_condition.wait_for(lambda: plot_version != seen_version)
                image = latest_plot
                seen_version = plot_version

            if image is None:
                continue

            yield (
                b"--frame\r\n"
                b"Content-Type: image/png\r\n"
                b"Cache-Control: no-cache\r\n\r\n"
                + image
                + b"\r\n"
            )

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.get("/plot.png")
def plot_png() -> Response:
    global latest_plot

    if latest_plot is None:
        update_plot()

    return Response(
        latest_plot,
        mimetype="image/png",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


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
