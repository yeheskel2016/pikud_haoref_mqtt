#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
missile_alerts.py

A robust Paho-MQTT listener that correctly mimics the Pushy Android SDK's
true "connect-once" and persistent session lifecycle, now with full
Home Assistant integration and state cleanup.
"""

import os
import ssl
import time
import json
import socket
import random
import logging
import threading

from datetime import datetime, timezone, timedelta
from collections import deque

import requests
import paho.mqtt.client as mqtt

# ─── CONFIG ────────────────────────────────────────────────────────────────
DEBUG = True
API_HOST = "https://pushy.ioref.app"
APP_ID = "66c20ac875260a035a3af7b2"
SDK_VERSION = 10117
ANDROID_SUFFIX = "-Xiaomi-2107113SI"
CONNECT_TIMEOUT = 10  # HTTP + socket timeout
KEEPALIVE_SEC = 300  # MQTT keepalive
MQTT_TEMPLATE = "mqtt-{timestamp}.ioref.io"
MQTT_PORT = 443  # same for Pro & Enterprise
QOS = 1
MAX_AGE_S = 45  # Maximum age in seconds for an alert to be considered "fresh"
EXPIRY_S = 600  # Purge list entries older than 10 minutes (600s)

SEGMENTS = {"5001878", "5001347"}

# --- Home Assistant MQTT Config ---
HA_MQTT_HOST = ""
HA_MQTT_PORT = 1883
HA_MQTT_USER = ""
HA_MQTT_PASS = ""

# --- Home Assistant Topic Config ---
# NOTE: Using a single, combined sensor for simplicity based on your AppDaemon script
STATE_TOPIC = f"missile_alerts/test"
ATTR_TOPIC = f"missile_alerts/test_attr"

# ─── STORAGE ────────────────────────────────────────────────────────────────
STORAGE_DIR = os.path.expanduser("missile_alerts")
os.makedirs(STORAGE_DIR, exist_ok=True)
TOKEN_FILE = os.path.join(STORAGE_DIR, "token.json")
ANDROID_ID_FILE = os.path.join(STORAGE_DIR, "android_id.txt")
SUBS_FILE = os.path.join(STORAGE_DIR, "subs.json")

# ─── LOGGING ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)5s %(message)s"
)
logger = logging.getLogger("missile_alerts")


# ─── HELPERS ────────────────────────────────────────────────────────────────

def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def get_android_id():
    if os.path.exists(ANDROID_ID_FILE):
        return open(ANDROID_ID_FILE).read().strip()
    aid = f"{random.getrandbits(64):016x}{ANDROID_SUFFIX}"
    with open(ANDROID_ID_FILE, "w") as f:
        f.write(aid)
    return aid


def api_post(path, payload, *, bypass_status=False):
    url = API_HOST + path
    r = requests.post(url, json=payload, timeout=CONNECT_TIMEOUT,
                      headers={"Content-Type": "application/json"})
    try:
        body = r.json()
    except ValueError:
        body = None

    if 200 <= r.status_code < 300:
        return body or {}
    if bypass_status and isinstance(body, dict) and body.get("success") is True:
        return body
    if isinstance(body, dict):
        code = body.get("code", "")
        msg = body.get("error", body.get("message", "Unknown error"))
        raise RuntimeError(f"{code}: {msg} (HTTP {r.status_code})")
    raise RuntimeError(f"{r.text.strip()} (HTTP {r.status_code})")


# ─── SUBSCRIBE / UNSUBSCRIBE ────────────────────────────────────────────────

def subscribe_topics(token, auth, topics):
    if isinstance(topics, str):
        topics = [topics]
    resp = api_post("/devices/subscribe",
                    {"token": token, "auth": auth, "topics": topics},
                    bypass_status=True)
    if not resp.get("success", False):
        raise RuntimeError("Subscribe failed: " + json.dumps(resp))


def unsubscribe_topics(token, auth, topics):
    if isinstance(topics, str):
        topics = [topics]
    resp = api_post("/devices/unsubscribe",
                    {"token": token, "auth": auth, "topics": topics},
                    bypass_status=True)
    if not resp.get("success", False):
        raise RuntimeError("Unsubscribe failed: " + json.dumps(resp))


def reconcile_subscriptions(token, auth):
    if not os.path.exists(SUBS_FILE):
        logger.info("No subs.json → first-run dance (sub1,unsub1)")
        subscribe_topics(token, auth, "1")
        unsubscribe_topics(token, auth, "1")
        prev = set()
    else:
        prev = set(load_json(SUBS_FILE).get("topics", []))

    desired = set(SEGMENTS)
    to_unsub = list(prev - desired)
    to_sub = list(desired - prev)

    if to_unsub:
        logger.info(f"Unsubscribing removed: {to_unsub}")
        unsubscribe_topics(token, auth, to_unsub)
    if to_sub:
        logger.info(f"Subscribing new: {to_sub}")
        subscribe_topics(token, auth, to_sub)

    if to_unsub or to_sub:
        save_json(SUBS_FILE, {"topics": list(desired)})
        logger.info("Saved updated subs.json")
    else:
        logger.info("No subscription changes needed")


# ─── AUTHENTICATION ─────────────────────────────────────────────────────────

def ensure_authenticated():
    creds = load_json(TOKEN_FILE)
    if creds.get("token") and creds.get("auth"):
        logger.info("Loaded credentials from token.json")
        return creds["token"], creds["auth"]

    aid = get_android_id()
    reg = api_post("/register", {
        "androidId": aid, "app": None,
        "appId": APP_ID, "platform": "android",
        "sdk": SDK_VERSION
    })
    token = reg["token"]
    auth = reg["auth"]
    save_json(TOKEN_FILE, {"token": token, "auth": auth})
    logger.info("Registered new device and saved credentials")
    return token, auth


# ─── MQTT UTILS (Pushy-style) ───────────────────────────────────────────────

def get_mqtt_endpoint(ts):
    return MQTT_TEMPLATE.replace("{timestamp}", str(ts))


def get_mqtt_port():
    return MQTT_PORT


def get_mqtt_keepalive():
    return KEEPALIVE_SEC


# ─── PUSHY-STYLE CALLBACKS & HA PUBLISHING ──────────────────────────────────
_seen = deque(maxlen=2000)
attr_state = {
    "selected_areas_active_alerts": [],
    "selected_areas_updates": []
}
attr_state_lock = threading.Lock()

name_map = {
    "5001878": "חיפה - קריית חיים ושמואל",
    "5001347": "קריית מוצקין"
}


def _publish_to_ha():
    global attr_state
    try:
        pub = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="home-assistant-publisher")
        pub.username_pw_set(HA_MQTT_USER, HA_MQTT_PASS)
        pub.connect(HA_MQTT_HOST, HA_MQTT_PORT)

        with attr_state_lock:
            pub.publish(ATTR_TOPIC, json.dumps(attr_state, ensure_ascii=False), qos=0, retain=False)
            active = "1" if attr_state["selected_areas_active_alerts"] else "0"
            pub.publish(STATE_TOPIC, active, qos=0, retain=False)

        pub.disconnect()
        logger.info(f"Successfully published state to Home Assistant. Active: {active}")
    except Exception as e:
        logger.error(f"Failed to publish to Home Assistant: {e}")


def _on_message_pushy(msg_payload):
    global attr_state
    now = datetime.now(timezone.utc)
    if DEBUG:
        logger.debug(f"RAW NOTIFICATION: {msg_payload}")

    aid = (msg_payload.get("alertTitle") or msg_payload.get("id") or "").strip()
    if not aid or aid in _seen:
        return
    _seen.append(aid)

    title = msg_payload.get("title", "").strip()
    raw_time = msg_payload.get("time", "")

    # --- START: Re-integrated time parsing and latency logic from AppDaemon script ---
    alert_time = ""
    latency = None
    try:
        if raw_time:
            dt_object = None
            if "T" in raw_time:
                # Try native ISO format first; fall back to format with %z for timezone
                try:
                    dt_object = datetime.fromisoformat(raw_time)
                except ValueError:
                    dt_object = datetime.strptime(raw_time, "%Y-%m-%dT%H:%M:%S%z")
            else:
                # Handle the simpler "YYYY-MM-DD HH:MM:SS" format
                dt_object = datetime.strptime(raw_time, "%Y-%m-%d %H:%M:%S")

            if dt_object:
                # Convert to naive datetime to calculate latency with now()
                dt_naive = dt_object.astimezone(None).replace(tzinfo=None)
                latency = (now - dt_naive).total_seconds()
                alert_time = dt_naive.strftime("%Y-%m-%d %H:%M:%S")
                logger.info(f"📩 Received alert '{aid}' for '{title}' with latency: {latency:.2f}s")

        if latency is not None and latency > MAX_AGE_S:
            logger.warning(f"Skipping stale alert {aid} (latency: {latency:.2f}s > max_age: {MAX_AGE_S}s)")
            return

    except Exception as e:
        logger.warning(f"Could not parse timestamp '{raw_time}': {e}")
        alert_time = raw_time  # Fallback to raw time if parsing fails
    # --- END: Re-integrated time parsing ---

    segs = set(msg_payload.get("citiesIds", "").split(","))
    hits = segs & SEGMENTS
    if not hits:
        return

    logger.info(f"✅ Alert '{title}' is relevant for segments: {hits}")

    real_titles = {"ירי רקטות וטילים", "חדירת כלי טיס עוין"}
    is_real = (title in real_titles)

    attr_list_key = "selected_areas_active_alerts" if is_real else "selected_areas_updates"
    clear_list_key = "selected_areas_updates" if is_real else "selected_areas_active_alerts"



    with attr_state_lock:
        attr_state[clear_list_key].clear()

        for seg in hits:
            entry = {
                "alertDate": alert_time,
                "title": title,
                "data": name_map.get(seg, seg),
                "category": msg_payload.get("threatId", ""),
                "id": aid  # Add alert ID to the entry itself
            }
            attr_state[attr_list_key].append(entry)

    ha_thread = threading.Thread(target=_publish_to_ha)
    ha_thread.daemon = True
    ha_thread.start()


# ─── MAIN LISTENER ──────────────────────────────────────────────────────────
class IoRefListener:
    def __init__(self, token, auth):
        self.token = token
        self.auth = auth
        self.stopping = False

        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.token,
            clean_session=False
        )

        self.client.username_pw_set(self.token, self.auth)
        self.client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS)

        self.client.on_connect = self._on_connect
        self.client.on_message = lambda c, u, m: _on_message_pushy(json.loads(m.payload.decode()))
        self.client.on_disconnect = self._on_disconnect
        self.client.enable_logger(logger)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            logger.info("Connection Successful (rc: 0)")
            client.subscribe(self.token, QOS)
        else:
            logger.error(f"Connection failed: {reason_code}. Paho's loop will retry.")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        if not self.stopping:
            logger.warning(
                f"Disconnected from MQTT (rc: {reason_code}). Paho's loop will attempt to reconnect automatically.")

    def start_loop(self):
        socket.setdefaulttimeout(CONNECT_TIMEOUT)
        while not self.stopping:
            try:
                endpoint = get_mqtt_endpoint(int(time.time()))
                port = get_mqtt_port()
                ka = get_mqtt_keepalive()
                logger.info(f"Attempting to connect to: {endpoint}")
                self.client.connect(endpoint, port, ka)
                self.client.loop_forever()
            except (socket.timeout, OSError) as e:
                logger.error(f"Connection error: {e}. Retrying after 15 seconds.")
                time.sleep(15)
            except Exception as e:
                if not self.stopping:
                    logger.error(
                        f"An unexpected error occurred in the listener thread: {e}. Retrying after 15 seconds.")
                    time.sleep(15)


# --- Cleanup function and loop ---
def _cleanup_and_republish_loop():
    global attr_state
    while True:
        time.sleep(30)  # Check every 30 seconds

        now = datetime.now(timezone.utc)
        dirty = False

        with attr_state_lock:
            for key in ("selected_areas_active_alerts", "selected_areas_updates"):
                fresh_list = []
                for item in attr_state[key]:
                    try:
                        # FIX: Parse the full ISO 8601 string directly
                        alert_dt = datetime.fromisoformat(item["alertDate"])

                        if (now - alert_dt).total_seconds() < EXPIRY_S:
                            fresh_list.append(item)
                        else:
                            logger.info(f"Expiring old alert: {item.get('id', 'N/A')}")
                            dirty = True
                    except Exception as e:
                        logger.warning(f"Could not parse date for cleanup: {item.get('alertDate', 'N/A')} - {e}")
                        fresh_list.append(item)

                attr_state[key] = fresh_list

        if dirty:
            logger.info("State has changed due to expired alerts, republishing to HA.")
            _publish_to_ha()


def initialize_ha_sensor():
    logger.info("Publishing initial state to Home Assistant...")
    _publish_to_ha()


# ─── ENTRY POINT ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    initialize_ha_sensor()
    token, auth = ensure_authenticated()
    reconcile_subscriptions(token, auth)

    listener = IoRefListener(token, auth)

    listener_thread = threading.Thread(target=listener.start_loop, daemon=True, name="MQTTListenerLoop")
    listener_thread.start()

    cleanup_thread = threading.Thread(target=_cleanup_and_republish_loop, daemon=True, name="HACleanupLoop")
    cleanup_thread.start()

    logger.info("🚀 Running; Ctrl-C to quit. All listener threads are running in the background.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down…")
        listener.stopping = True
        listener.client.disconnect()
        logger.info("Waiting for listener thread to finish...")
        listener_thread.join()
        logger.info("Shutdown complete.")