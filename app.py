import json
import math
import os
import queue
import smtplib
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import customtkinter as ctk
import requests
from dotenv import load_dotenv, set_key
from tkinter import ttk, messagebox

# Optional secure storage
try:
    import keyring  # pip install keyring
except Exception:
    keyring = None

# Optional cross-platform sound fallback
try:
    import pygame  # pip install pygame
except Exception:
    pygame = None

# Windows sound
try:
    import winsound
except Exception:
    winsound = None


APP_NAME = "Tanker Heat-Watch"
SERVICE_NAME = "tanker-heat-watch"
KEYRING_USER_API = "samsara_api_key"

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
HISTORY_PATH = BASE_DIR / "history.json"

VAN_WERT_LAT = 40.8694
VAN_WERT_LON = -84.5841
RADIUS_MILES = 5.0
POLL_SECONDS = 300
ALERT_COOLDOWN_SECONDS = 1800  # 30 minutes


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    # Earth radius in miles
    r = 3958.8
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)

    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


@dataclass
class TrailerReading:
    trailer_id: str
    temp: float
    lat: float
    lon: float
    distance_miles: float


class StorageManager:
    def __init__(self, path: Path):
        self.path = path
        if not self.path.exists():
            self._write_json({"trailers": {}, "alerts": {}})

    def _read_json(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"trailers": {}, "alerts": {}}

    def _write_json(self, data: Dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(self.path)

    def get_last_temp(self, trailer_id: str) -> Optional[float]:
        data = self._read_json()
        trailer = data.get("trailers", {}).get(trailer_id, {})
        return safe_float(trailer.get("temp"))

    def update_temp(self, trailer_id: str, temp: float) -> None:
        data = self._read_json()
        data.setdefault("trailers", {})
        data["trailers"][trailer_id] = {"temp": temp, "timestamp": now_iso()}
        self._write_json(data)

    def get_last_alert_time(self, key: str) -> Optional[float]:
        data = self._read_json()
        val = data.get("alerts", {}).get(key)
        return safe_float(val)

    def set_last_alert_time(self, key: str, ts_epoch: float) -> None:
        data = self._read_json()
        data.setdefault("alerts", {})
        data["alerts"][key] = ts_epoch
        self._write_json(data)


class SecretManager:
    """
    API key protection:
    1) Try OS keychain via keyring.
    2) Fallback to env var (runtime only if keyring unavailable).
    """
    def __init__(self, env_path: Path):
        self.env_path = env_path

    def get_api_key(self) -> str:
        if keyring:
            try:
                val = keyring.get_password(SERVICE_NAME, KEYRING_USER_API)
                if val:
                    return val
            except Exception:
                pass
        return os.getenv("SAMSARA_API_KEY", "").strip()

    def set_api_key(self, value: str) -> None:
        value = value.strip()
        if not value:
            return
        if keyring:
            try:
                keyring.set_password(SERVICE_NAME, KEYRING_USER_API, value)
                # In .env, store only non-secret reference
                set_key(str(self.env_path), "SAMSARA_API_KEY_STORAGE", "keyring")
                # Remove plaintext env key if present
                set_key(str(self.env_path), "SAMSARA_API_KEY", "")
                return
            except Exception:
                pass
        # fallback (less secure)
        set_key(str(self.env_path), "SAMSARA_API_KEY", value)
        set_key(str(self.env_path), "SAMSARA_API_KEY_STORAGE", "dotenv")


class SamsaraClient:
    def __init__(self, api_key_getter):
        self.api_key_getter = api_key_getter
        self.base_url = os.getenv("SAMSARA_BASE_URL", "https://api.samsara.com")
        self.endpoint = os.getenv("SAMSARA_TRAILERS_ENDPOINT", "/fleet/trailers")
        self.timeout = int(os.getenv("HTTP_TIMEOUT_SECONDS", "30"))

    def fetch_trailer_data(self) -> Dict[str, Any]:
        api_key = self.api_key_getter()
        if not api_key:
            raise RuntimeError("Missing Samsara API key.")
        url = self.base_url.rstrip("/") + self.endpoint
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = requests.get(url, headers=headers, timeout=self.timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"Samsara API error {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def parse_trailers(self, payload: Dict[str, Any]) -> List[TrailerReading]:
        """
        Adapter parser with flexible shape handling.
        If your endpoint differs, adjust this method only.
        Expected per trailer:
        - id
        - location lat/lon
        - temperature value
        """
        candidates: List[Dict[str, Any]] = []

        if isinstance(payload, dict):
            if isinstance(payload.get("data"), list):
                candidates = payload["data"]
            elif isinstance(payload.get("trailers"), list):
                candidates = payload["trailers"]
            elif isinstance(payload.get("items"), list):
                candidates = payload["items"]

        out: List[TrailerReading] = []
        for t in candidates:
            trailer_id = str(
                t.get("id")
                or t.get("name")
                or t.get("trailerId")
                or t.get("assetId")
                or "unknown"
            )

            # Try multiple common location shapes
            lat = (
                safe_float(t.get("latitude"))
                or safe_float((t.get("location") or {}).get("latitude"))
                or safe_float((t.get("gps") or {}).get("latitude"))
                or safe_float((t.get("position") or {}).get("lat"))
            )
            lon = (
                safe_float(t.get("longitude"))
                or safe_float((t.get("location") or {}).get("longitude"))
                or safe_float((t.get("gps") or {}).get("longitude"))
                or safe_float((t.get("position") or {}).get("lon"))
            )

            # Try multiple common temperature shapes
            temp = (
                safe_float(t.get("temperature"))
                or safe_float((t.get("sensors") or {}).get("temperature"))
                or safe_float((t.get("telemetry") or {}).get("temperature"))
                or safe_float((t.get("reefer") or {}).get("temperature"))
            )

            if lat is None or lon is None or temp is None:
                continue

            dist = haversine_miles(VAN_WERT_LAT, VAN_WERT_LON, lat, lon)

            out.append(
                TrailerReading(
                    trailer_id=trailer_id,
                    temp=temp,
                    lat=lat,
                    lon=lon,
                    distance_miles=dist,
                )
            )
        return out


class AlertManager:
    def __init__(self, storage: StorageManager):
        self.storage = storage

    def _cooldown_ok(self, trailer_id: str, reason: str) -> bool:
        key = f"{trailer_id}|{reason}"
        last = self.storage.get_last_alert_time(key)
        now_ts = time.time()
        if last is None:
            return True
        return (now_ts - last) >= ALERT_COOLDOWN_SECONDS

    def _mark_alert(self, trailer_id: str, reason: str) -> None:
        key = f"{trailer_id}|{reason}"
        self.storage.set_last_alert_time(key, time.time())

    def play_sound(self) -> None:
        try:
            if winsound:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                return
            if pygame:
                pygame.mixer.init()
                # Fallback generated beep-like behavior is not native;
                # if you have an alert.mp3/wav, load it here.
                # pygame.mixer.music.load("alert.wav")
                # pygame.mixer.music.play()
                # Minimal fallback:
                print("ALERT SOUND: pygame available, add alert.wav for custom sound.")
                return
        except Exception:
            pass

    def send_email(self, subject: str, body: str) -> None:
        host = os.getenv("SMTP_HOST", "").strip()
        port = int(os.getenv("SMTP_PORT", "587"))
        user = os.getenv("SMTP_USER", "").strip()
        password = os.getenv("SMTP_PASS", "").strip()
        sender = os.getenv("SMTP_SENDER", user).strip()
        recipient = os.getenv("SMTP_RECIPIENT", "").strip()

        if not all([host, port, user, password, sender, recipient]):
            return  # Email config not complete; fail quietly for now

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = recipient
        msg.set_content(body)

        with smtplib.SMTP(host, port, timeout=20) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)

    def trigger_alert_if_needed(
        self,
        trailer_id: str,
        current_temp: float,
        previous_temp: Optional[float],
        rise_threshold: float,
        max_threshold: float,
        distance_miles: float,
    ) -> str:
        rise = None if previous_temp is None else (current_temp - previous_temp)
        rise_hit = rise is not None and rise >= rise_threshold
        max_hit = current_temp >= max_threshold

        if not rise_hit and not max_hit:
            return "Normal"

        reason = "both" if (rise_hit and max_hit) else ("rise" if rise_hit else "max")
        if not self._cooldown_ok(trailer_id, reason):
            return "Alert (cooldown)"

        self._mark_alert(trailer_id, reason)
        self.play_sound()

        subject = f"Warning: Trailer {trailer_id} is heating up"
        body = (
            f"Trailer ID: {trailer_id}\n"
            f"Current Temp: {current_temp:.2f}\n"
            f"Previous Temp: {previous_temp if previous_temp is not None else 'n/a'}\n"
            f"Rise: {rise if rise is not None else 'n/a'}\n"
            f"Distance from Van Wert: {distance_miles:.2f} miles\n"
            f"Trigger: {reason}\n"
            f"Timestamp (UTC): {now_iso()}\n"
        )

        try:
            self.send_email(subject, body)
        except Exception:
            # Keep app stable if SMTP fails
            pass

        return "ALERT"


class HeatWatchApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1100x650")
        self.minsize(900, 550)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        load_dotenv(ENV_PATH)

        self.storage = StorageManager(HISTORY_PATH)
        self.secrets = SecretManager(ENV_PATH)
        self.client = SamsaraClient(self.secrets.get_api_key)
        self.alerts = AlertManager(self.storage)

        self.result_queue: queue.Queue = queue.Queue()
        self.poll_in_flight = False

        self._build_ui()
        self._load_env_into_ui()

        # initial poll soon after startup
        self.after(1200, self.run_poll_cycle)
        self.after(1000, self._drain_queue)

    def _build_ui(self):
        amber = "#f59e0b"
        bg = "#0f172a"

        self.configure(fg_color=bg)

        top = ctk.CTkFrame(self, fg_color="#111827")
        top.pack(fill="x", padx=12, pady=12)

        # API key row
        ctk.CTkLabel(top, text="Samsara API Key", text_color=amber).grid(row=0, column=0, padx=8, pady=8, sticky="w")
        self.api_entry = ctk.CTkEntry(top, width=420, show="*")
        self.api_entry.grid(row=0, column=1, padx=8, pady=8, sticky="w")
        ctk.CTkButton(top, text="Save Key", fg_color=amber, text_color="black", command=self.save_api_key).grid(
            row=0, column=2, padx=8, pady=8
        )

        # Rise slider
        ctk.CTkLabel(top, text="Temperature Rise Threshold", text_color=amber).grid(row=1, column=0, padx=8, pady=8, sticky="w")
        self.rise_slider = ctk.CTkSlider(top, from_=1, to=10, number_of_steps=9, progress_color=amber)
        self.rise_slider.set(3)
        self.rise_slider.grid(row=1, column=1, padx=8, pady=8, sticky="we")
        self.rise_label = ctk.CTkLabel(top, text="3°", text_color="white")
        self.rise_label.grid(row=1, column=2, padx=8, pady=8)
        self.rise_slider.configure(command=lambda v: self.rise_label.configure(text=f"{int(round(v))}°"))

        # Max slider
        ctk.CTkLabel(top, text="Max Temperature Threshold", text_color=amber).grid(row=2, column=0, padx=8, pady=8, sticky="w")
        self.max_slider = ctk.CTkSlider(top, from_=80, to=200, number_of_steps=120, progress_color=amber)
        self.max_slider.set(120)
        self.max_slider.grid(row=2, column=1, padx=8, pady=8, sticky="we")
        self.max_label = ctk.CTkLabel(top, text="120°", text_color="white")
        self.max_label.grid(row=2, column=2, padx=8, pady=8)
        self.max_slider.configure(command=lambda v: self.max_label.configure(text=f"{int(round(v))}°"))

        ctk.CTkButton(top, text="Poll Now", fg_color=amber, text_color="black", command=self.run_poll_cycle).grid(
            row=3, column=2, padx=8, pady=8
        )

        top.grid_columnconfigure(1, weight=1)

        # Table container
        table_frame = ctk.CTkFrame(self, fg_color="#111827")
        table_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        style = ttk.Style()
        style.theme_use("default")
        style.configure("Treeview", background="#0b1220", fieldbackground="#0b1220", foreground="white", rowheight=28)
        style.configure("Treeview.Heading", background="#1f2937", foreground="#f59e0b")
        style.map("Treeview", background=[("selected", "#374151")])

        columns = ("trailer_id", "current_temp", "last_change", "distance", "status")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        self.tree.heading("trailer_id", text="Trailer ID")
        self.tree.heading("current_temp", text="Current Temp")
        self.tree.heading("last_change", text="Last Change")
        self.tree.heading("distance", text="Distance from Van Wert")
        self.tree.heading("status", text="Status")
        self.tree.column("trailer_id", width=180, anchor="w")
        self.tree.column("current_temp", width=140, anchor="center")
        self.tree.column("last_change", width=140, anchor="center")
        self.tree.column("distance", width=180, anchor="center")
        self.tree.column("status", width=140, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=8, pady=8)

        self.status_var = ctk.StringVar(value="Ready.")
        status = ctk.CTkLabel(self, textvariable=self.status_var, anchor="w", text_color="#cbd5e1")
        status.pack(fill="x", padx=12, pady=(0, 10))

    def _load_env_into_ui(self):
        # do not reveal full key
        has_key = bool(self.secrets.get_api_key())
        self.api_entry.delete(0, "end")
        if has_key:
            self.api_entry.insert(0, "••••••••••••••••")
            self.status_var.set("API key loaded from secure storage.")
        else:
            self.status_var.set("Enter API key and click Save Key.")

    def save_api_key(self):
        raw = self.api_entry.get().strip()
        if not raw or "•" in raw:
            messagebox.showinfo("Info", "Enter a real API key before saving.")
            return
        self.secrets.set_api_key(raw)
        self.api_entry.delete(0, "end")
        self.api_entry.insert(0, "••••••••••••••••")
        self.status_var.set("API key saved securely.")

    def run_poll_cycle(self):
        if self.poll_in_flight:
            return
        self.poll_in_flight = True
        self.status_var.set("Polling Samsara...")
        t = threading.Thread(target=self._poll_worker, daemon=True)
        t.start()

    def _poll_worker(self):
        try:
            payload = self.client.fetch_trailer_data()
            trailers = self.client.parse_trailers(payload)

            in_radius = [t for t in trailers if t.distance_miles <= RADIUS_MILES]
            rise_threshold = float(int(round(self.rise_slider.get())))
            max_threshold = float(int(round(self.max_slider.get())))

            rows = []
            for tr in in_radius:
                prev = self.storage.get_last_temp(tr.trailer_id)
                change_str = "n/a" if prev is None else f"{tr.temp - prev:+.2f}°"
                status = self.alerts.trigger_alert_if_needed(
                    trailer_id=tr.trailer_id,
                    current_temp=tr.temp,
                    previous_temp=prev,
                    rise_threshold=rise_threshold,
                    max_threshold=max_threshold,
                    distance_miles=tr.distance_miles,
                )

                self.storage.update_temp(tr.trailer_id, tr.temp)

                rows.append(
                    (
                        tr.trailer_id,
                        f"{tr.temp:.2f}°",
                        change_str,
                        f"{tr.distance_miles:.2f} mi",
                        status,
                    )
                )

            rows.sort(key=lambda x: x[0])
            self.result_queue.put(("ok", rows, len(trailers), len(in_radius)))
        except Exception as e:
            self.result_queue.put(("err", str(e)))
        finally:
            self.poll_in_flight = False

    def _drain_queue(self):
        try:
            while True:
                msg = self.result_queue.get_nowait()
                kind = msg[0]

                if kind == "ok":
                    _, rows, total_count, radius_count = msg
                    for item in self.tree.get_children():
                        self.tree.delete(item)
                    for r in rows:
                        self.tree.insert("", "end", values=r)

                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self.status_var.set(
                        f"Last poll: {ts} | Total trailers parsed: {total_count} | Within 5 mi: {radius_count}"
                    )
                else:
                    _, err = msg
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self.status_var.set(f"Last poll failed at {ts}: {err}")
        except queue.Empty:
            pass

        # Re-run on schedule
        self.after(1000, self._drain_queue)
        self.after(POLL_SECONDS * 1000, self.run_poll_cycle)


if __name__ == "__main__":
    app = HeatWatchApp()
    app.mainloop()