#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
missile_alerts.py

A robust Paho-MQTT listener that correctly mimics the Pushy library's
true "connect-once" lifecycle, with a graceful shutdown and modern Paho-MQTT v2 API.
"""

import os
import ssl
import time
import json
import socket
import random
import logging
import threading

from datetime import datetime, timezone
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
MAX_AGE_S = 45  # Maximum age of an alert in seconds before we consider it stale
SEGMENTS = {"5003000", "5003001", "5003006", "5003004", "5003003", "5003002", "5003005", "5001878", "5001347"}

# Local broker for Home Assistant
HA_MQTT_HOST = "localhost"
HA_MQTT_PORT = 1883

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
    # first-run dance
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


# ─── PUSHY-STYLE CALLBACKS ─────────────────────────────────────────────────
_seen = deque(maxlen=2000)


def _publish_to_ha(single, hits):
    """
    This function runs in a separate thread to avoid blocking the main MQTT loop.
    """
    try:
        real_titles = {"ירי רקטות וטילים", "חדירת כלי טיס עוין"}
        pub = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="home-assistant-publisher")
        pub.connect(HA_MQTT_HOST, HA_MQTT_PORT)

        for seg in hits:
            flag = "1" if single["title"] in real_titles else "0"
            pub.publish(f"missile_alerts/{seg}", flag, qos=0, retain=False)
            attr = {
                "selected_areas_active_alerts": [single] if flag == "1" else [],
                "selected_areas_updates": [] if flag == "1" else [single]
            }
            pub.publish(f"missile_alerts/{seg}_attr", json.dumps(attr, ensure_ascii=False), qos=0, retain=False)

        pub.disconnect()
        logger.info(f"Successfully published alert {single['id']} to Home Assistant.")
    except Exception as e:
        logger.error(f"Failed to publish to Home Assistant: {e}")


def _on_message_pushy(msg_payload):
    # This function should do its work as fast as possible and not block.
    now = datetime.now(timezone.utc)  # Use timezone-aware datetime for correct comparison
    if DEBUG:
        logger.debug(f"RAW NOTIFICATION: {msg_payload}")

    aid = (msg_payload.get("alertTitle") or msg_payload.get("id") or "").strip()
    if DEBUG:
        logger.debug(f"Parsed id: {aid}")
    if not aid or aid in _seen:
        return
    _seen.append(aid)

    title = msg_payload.get("title", "").strip()
    raw_t = msg_payload.get("time", "")

    # --- START of new latency check logic ---
    latency = None
    alert_time_utc = None
    try:
        if raw_t:
            # Parse the ISO format string from the payload, which includes timezone info
            alert_time_utc = datetime.fromisoformat(raw_t)
            # Calculate latency in seconds
            latency = (now - alert_time_utc).total_seconds()
            logger.info(f"📩 Received alert '{aid}' with latency: {latency:.2f}s")

            # Optional: drop stale alerts
            if latency > MAX_AGE_S:
                logger.warning(f"Skipping stale alert {aid} (latency: {latency:.2f}s > max_age: {MAX_AGE_S}s)")
                return
    except Exception as e:
        logger.warning(f"Could not parse timestamp '{raw_t}' to calculate latency: {e}")
    # --- END of new latency check logic ---

    # If in debug mode, we can exit after logging the latency.
    if DEBUG:
        logger.debug(f"Title='{title}', Time={raw_t}")
        return

    # If not in debug, continue to process and publish
    try:
        # Format the UTC time to a string for the payload if it exists
        alert_date_str = alert_time_utc.strftime("%Y-%m-%d %H:%M:%S") if alert_time_utc else raw_t
    except:
        alert_date_str = raw_t

    segs = set(msg_payload.get("citiesIds", "").split(","))
    hits = segs & SEGMENTS
    if not hits:
        return

    single = {
        "title": title, "id": aid, "alertDate": alert_date_str,
        "desc": msg_payload.get("desc", ""),
        "cat": msg_payload.get("threatId", "")
    }

    ha_thread = threading.Thread(target=_publish_to_ha, args=(single, hits))
    ha_thread.daemon = True
    ha_thread.start()


# ─── MAIN LISTENER ──────────────────────────────────────────────────────────
class IoRefListener:
    """
    Connects once to a stable endpoint and uses a blocking loop to maintain
    the connection, exactly like the Pushy library does.
    """

    def __init__(self, token, auth):
        self.token = token
        self.auth = auth
        self.stopping = False

        # FINAL: Use clean_session=False to perfectly match the native Android app's behavior.
        # This requires paho-mqtt version 2.0+ and the V2 callback API.
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

    # The 'disconnect_flags' argument was missing.
    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        if not self.stopping:
            logger.warning(f"Disconnected from MQTT (rc: {reason_code}). Paho's loop will attempt to reconnect automatically.")

    def start_loop(self):
        """
        This function runs in a dedicated thread and handles the entire
        connection lifecycle, mimicking the Pushy library.
        """
        socket.setdefaulttimeout(CONNECT_TIMEOUT)

        while not self.stopping:
            try:
                endpoint = get_mqtt_endpoint(int(time.time()))
                port = get_mqtt_port()
                ka = get_mqtt_keepalive()
                logger.info(f"Attempting to connect to: {endpoint}")
                self.client.connect(endpoint, port, ka)

                # This is the blocking call that runs the network loop. It handles
                # all reconnects automatically to the SAME host.
                self.client.loop_forever()

            except (socket.timeout, OSError) as e:
                logger.error(f"Connection error: {e}. Retrying after 5 seconds.")
                time.sleep(5)
            except Exception as e:
                if not self.stopping:
                    logger.error(
                        f"An unexpected error occurred in the listener thread: {e}. Retrying after 10 seconds.")
                    time.sleep(10)


# ─── ENTRY POINT ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    token, auth = ensure_authenticated()
    reconcile_subscriptions(token, auth)

    listener = IoRefListener(token, auth)

    listener_thread = threading.Thread(target=listener.start_loop, daemon=True, name="MQTTListenerLoop")
    listener_thread.start()

    logger.info("🚀 Running; Ctrl-C to quit. Listener is running in the background.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down…")
        listener.stopping = True
        listener.client.disconnect()
        logger.info("Waiting for listener thread to finish...")
        listener_thread.join()  # Wait for the loop to exit cleanly
        logger.info("Shutdown complete.")