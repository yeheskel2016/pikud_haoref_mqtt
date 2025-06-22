#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
missile_alerts.py

Direct Paho-MQTT client, with host-rotation, HTTP auth, and
HTTP-based subscribe/unsubscribe to IoRef topics.
Publishes to local MQTT broker (localhost:1883) on topics:
  missile_alerts/{segment}       → "0" or "1"
  missile_alerts/{segment}_attr  → JSON attrs for HA sensors
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
DEBUG            = True    # True to only log, skip real MQTT publishes
API_HOST         = "https://pushy.ioref.app"
APP_ID           = "66c20ac875260a035a3af7b2"
SDK_VERSION      = 10117
ANDROID_SUFFIX   = "-Xiaomi-2107113SI"
CONNECT_TIMEOUT  = 10       # seconds for HTTP + socket timeouts
RECONNECT_SEC    = 60       # rotate broker every 60s
KEEPALIVE        = 300
PORT             = 443
QOS              = 1

# Your segments of interest
SEGMENTS = {"5001878", "5001347"}

# Local MQTT broker for Home Assistant
LOCAL_MQTT_HOST = "localhost"
LOCAL_MQTT_PORT = 1883
LOCAL_MQTT_USER = None
LOCAL_MQTT_PASS = None

# ─── STORAGE ────────────────────────────────────────────────────────────────
STORAGE_DIR     = os.path.expanduser("/missile_alerts")
os.makedirs(STORAGE_DIR, exist_ok=True)
TOKEN_FILE      = os.path.join(STORAGE_DIR, "token.json")
ANDROID_ID_FILE = os.path.join(STORAGE_DIR, "android_id.txt")
SUBS_FILE       = os.path.join(STORAGE_DIR, "subs.json")

# ─── LOGGING ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)5s %(message)s",
)
logger = logging.getLogger("missile_alerts")

# ─── SSL for IoRef MQTT ─────────────────────────────────────────────────────
SSL_CTX = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
SSL_CTX.minimum_version = ssl.TLSVersion.TLSv1_3
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode    = ssl.CERT_NONE

# ─── HTTP HEADERS ──────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 11; Xiaomi 2107113SI) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/101.0.4951.64 Mobile Safari/537.36"
    ),
    "Content-Type": "application/json",
}

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
    r   = requests.post(url, json=payload, headers=HEADERS, timeout=CONNECT_TIMEOUT)
    try:
        body = r.json()
    except ValueError:
        body = None

    if 200 <= r.status_code < 300:
        return body or {}

    if bypass_status and isinstance(body, dict) and body.get("success") is True:
        return body

    if isinstance(body, dict):
        code = body.get("code","")
        msg  = body.get("error", body.get("message","Unknown error"))
        raise RuntimeError(f"{code}: {msg} (HTTP {r.status_code})")
    raise RuntimeError(f"{r.text.strip()} (HTTP {r.status_code})")

# ─── SUBSCRIBE / UNSUBSCRIBE API ────────────────────────────────────────────
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
    """
    If SUBS_FILE doesn’t exist, always run first-run dance.
    Otherwise, diff prev vs SEGMENTS and update only when needed.
    """
    # First run if we have no record yet
    if not os.path.exists(SUBS_FILE):
        logger.info("No subs.json found → performing first-run subscription dance")
        unsubscribe_topics(token, auth, "*")
        subscribe_topics(token, auth, "1")
        unsubscribe_topics(token, auth, "1")
        prev = set()
    else:
        prev = set(load_json(SUBS_FILE).get("topics", []))

    desired  = set(SEGMENTS)
    to_unsub = list(prev - desired)
    to_sub   = list(desired - prev)

    if not to_unsub and not to_sub:
        logger.info("No subscription changes needed.")
        return

    if to_unsub:
        logger.info(f"Unsubscribing removed segments: {to_unsub}")
        unsubscribe_topics(token, auth, to_unsub)

    if to_sub:
        logger.info(f"Subscribing new segments: {to_sub}")
        subscribe_topics(token, auth, to_sub)

    # Only save after all calls have succeeded
    save_json(SUBS_FILE, {"topics": list(desired)})
    logger.info("Subscription state saved to subs.json")


# ─── AUTHENTICATION ─────────────────────────────────────────────────────────
def ensure_authenticated():
    creds = load_json(TOKEN_FILE)
    if creds.get("token") and creds.get("auth"):
        return creds["token"], creds["auth"]

    logger.info("Registering with IoRef…")
    aid = get_android_id()
    reg = api_post("/register", {
        "androidId": aid,
        "app":       None,
        "appId":     APP_ID,
        "platform":  "android",
        "sdk":       SDK_VERSION
    })
    token = reg["token"]
    auth  = reg["auth"]
    save_json(TOKEN_FILE, {"token": token, "auth": auth})

    logger.info("Authenticating device…")
    api_post("/devices/auth", {
        "androidId": aid,
        "appId":     APP_ID,
        "auth":      auth,
        "sdk":       SDK_VERSION,
        "token":     token
    }, bypass_status=True)

    logger.info("✅ Authenticated!")
    return token, auth

# ─── MQTT LISTENER ──────────────────────────────────────────────────────────
class IoRefListener:
    def __init__(self, token, auth):
        self.token = token
        self.auth  = auth
        self.seen  = deque(maxlen=2000)

        # Publisher (auto-generated client_id)
        self.pub = mqtt.Client()
        if LOCAL_MQTT_USER:
            self.pub.username_pw_set(LOCAL_MQTT_USER, LOCAL_MQTT_PASS)
        if not DEBUG:
            try:
                self.pub.connect(LOCAL_MQTT_HOST, LOCAL_MQTT_PORT)
                self.pub.loop_start()
            except Exception as e:
                logger.error(f"[Publisher] Could not connect to local MQTT broker: {e}")
                self.pub = None

        self.stop_evt = threading.Event()
        self.dcon_evt = threading.Event()

    def start(self):
        threading.Thread(target=self._rotate_loop, daemon=True).start()

    def _rotate_loop(self):
        socket.setdefaulttimeout(CONNECT_TIMEOUT)
        while not self.stop_evt.is_set():
            host     = f"mqtt-{int(time.time())}.ioref.io"
            userdata = {"host": host, "t0": time.time()}

            # Subscriber uses token as client_id
            client = mqtt.Client(
                client_id=self.token,
                clean_session=False,
                userdata=userdata,
                protocol=mqtt.MQTTv311,
            )
            client.username_pw_set(self.token, self.auth)
            client.tls_set_context(SSL_CTX)
            client.on_connect    = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_message    = self._on_message

            wd = threading.Timer(CONNECT_TIMEOUT, self.dcon_evt.set)
            wd.start()
            client._wd = wd

            client.connect_async(host, PORT, KEEPALIVE)
            client.loop_start()

            start = time.time()
            self.dcon_evt.clear()
            while time.time() - start < RECONNECT_SEC \
                  and not self.stop_evt.is_set() \
                  and not self.dcon_evt.is_set():
                time.sleep(0.1)

            wd.cancel()
            client.loop_stop()
            client.disconnect()

        if self.pub:
            self.pub.loop_stop()
            self.pub.disconnect()
        logger.info("Listener stopped")

    def _on_connect(self, client, userdata, flags, rc):
        client._wd.cancel()
        self.dcon_evt.clear()
        h = userdata["host"]
        if rc != 0:
            logger.error(f"[{h}] CONNACK rc={rc}")
            self.dcon_evt.set()
            return
        rtt = time.time() - userdata["t0"]
        logger.info(f"[{h}] Connected (RTT≈{rtt:.3f}s)")
        # Subscribe to your *device* token so IoRef pushes arrive
        client.subscribe(self.token, QOS)

    def _on_disconnect(self, client, userdata, rc):
        h = userdata["host"]
        logger.warning(f"[{h}] Disconnected rc={rc}")
        if rc != 0:
            self.dcon_evt.set()

    def _on_message(self, client, userdata, msg):
        h       = userdata["host"]
        recv_ts = datetime.now(timezone.utc)
        raw     = msg.payload.decode("utf-8", errors="replace")

        if DEBUG:
            logger.debug(f"[{h}] RAW @ {recv_ts.isoformat()}: {raw}")
            return

        try:
            data = json.loads(raw)
        except Exception as e:
            logger.warning(f"[{h}] JSON parse error: {e}")
            return

        aid   = (data.get("alertTitle") or data.get("id") or "").strip()
        if not aid or aid in self.seen:
            return
        self.seen.append(aid)

        title = data.get("title","").strip()
        raw_t = data.get("time","")
        segs  = set(data.get("citiesIds","").split(","))
        hits  = segs & SEGMENTS

        lat = "?"
        if raw_t:
            try:
                dt = datetime.fromisoformat(raw_t)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                lat = f"{(recv_ts - dt).total_seconds():.1f}s"
            except:
                pass

        logger.info(f"[{h}] id={aid} title='{title}' segs={segs} lat={lat}")
        if not hits:
            return

        single = {
            "title": title,
            "id":    aid,
            "time":  raw_t,
            "desc":  data.get("desc",""),
            "cat":   data.get("threatId",""),
        }
        real_titles = {"ירי רקטות וטילים","חדירת כלי טיס עוין"}

        for seg in hits:
            flag = "1" if title in real_titles else "0"
            # publish flag
            if self.pub:
                try:
                    self.pub.publish(f"missile_alerts/{seg}", flag, qos=0, retain=False)
                except Exception as e:
                    logger.error(f"Publish flag failed: {e}")
            # publish attributes
            attr = {
                "selected_areas_active_alerts": [single] if flag=="1" else [],
                "selected_areas_updates":      []        if flag=="1" else [single]
            }
            if self.pub:
                try:
                    self.pub.publish(
                        f"missile_alerts/{seg}_attr",
                        json.dumps(attr, ensure_ascii=False),
                        qos=0, retain=False
                    )
                except Exception as e:
                    logger.error(f"Publish attrs failed: {e}")

            logger.info(f"[{h}] → {'REAL' if flag=='1' else 'UPDATE'} for {seg}")

# ─── ENTRY POINT ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        token, auth = ensure_authenticated()
        reconcile_subscriptions(token, auth)
        listener = IoRefListener(token, auth)
        listener.start()
        logger.info("🚀 MissileAlerts running. Press Ctrl-C to quit.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Interrupted, shutting down…")
    except Exception:
        logger.exception("Fatal error")
        os._exit(1)
