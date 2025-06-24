#
# apps/missile_alerts_app.py
#
# A self-contained, high-performance AppDaemon app for missile alerts.
# This version uses a direct Paho-MQTT client for listening and correctly
# integrates all logging and services with the AppDaemon framework.
#

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
import appdaemon.plugins.hass.hassapi as hass

# ─── APPDAEMON CLASS ─────────────────────────────────────────────────────────

class MissileAlertsApp(hass.Hass):

    def initialize(self):
        """Initialize the AppDaemon application."""
        self.log("🚀 Initializing Missile Alerts App...")

        # --- Load Configuration from apps.yaml ---
        self.config = self.args
        self.DEBUG = self.config.get("debug", True)
        self.API_HOST = self.config.get("api_host", "https://pushy.ioref.app")
        self.APP_ID = self.config.get("app_id", "66c20ac875260a035a3af7b2")
        self.SDK_VERSION = self.config.get("sdk_version", 10117)
        self.ANDROID_SUFFIX = self.config.get("android_suffix", "-Xiaomi-2107113SI")
        self.CONNECT_TIMEOUT = self.config.get("connect_timeout", 10)
        self.KEEPALIVE_SEC = self.config.get("keepalive_sec", 300)
        self.MQTT_TEMPLATE = self.config.get("mqtt_template", "mqtt-{timestamp}.ioref.io")
        self.MQTT_PORT = self.config.get("mqtt_port", 443)
        self.QOS = self.config.get("qos", 1)
        self.MAX_AGE_S = self.config.get("max_age_s", 45)
        self.EXPIRY_S = self.config.get("expiry_s", 600)
        self.SEGMENTS = set(self.config.get("segments", {}))

        # --- Home Assistant Topic Config ---
        # NOTE: Using a single, combined sensor for simplicity based on your AppDaemon script
        self.STATE_TOPIC = self.config.get("state_topic", "missile_alerts/5001347_5001878")
        self.ATTR_TOPIC = self.config.get("attr_topic", "missile_alerts/5001347_5001878_attr")
        
        # --- App-Specific Storage ---
        self.STORAGE_DIR = os.path.join(self.app_dir, "missile_alerts_storage")
        os.makedirs(self.STORAGE_DIR, exist_ok=True)
        self.TOKEN_FILE = os.path.join(self.STORAGE_DIR, "token.json")
        self.ANDROID_ID_FILE = os.path.join(self.STORAGE_DIR, "android_id.txt")
        self.SUBS_FILE = os.path.join(self.STORAGE_DIR, "subs.json")

        # --- Global State Variables ---
        self._seen = deque(maxlen=2000)
        self.attr_state = {
            "selected_areas_active_alerts": [],
            "selected_areas_updates": []
        }
        self.attr_state_lock = threading.Lock()
        self.name_map = self.config.get("name_map", {})
        
        # --- Initialize and Start All Processes ---
        self.initialize_ha_sensor()
        self.token, self.auth = self._ensure_authenticated()
        self._reconcile_subscriptions()

        self.listener = IoRefListener(self) # Pass the app instance to the listener
        
        self.listener_thread = threading.Thread(target=self.listener.start_loop, daemon=True, name="MQTTListenerLoop")
        self.listener_thread.start()
        
        # Use AppDaemon's built-in scheduler for the cleanup task
        self.run_every(self._cleanup_and_republish, "now+15", 30)

        self.log("✅ Missile Alerts App Initialized and Running.")

    def terminate(self):
        """Called by AppDaemon on shutdown."""
        self.log("🛑 Shutting down Missile Alerts App...")
        if hasattr(self, 'listener') and self.listener:
            self.listener.stop()
        if hasattr(self, 'listener_thread') and self.listener_thread.is_alive():
            self.listener_thread.join()
        self.log("Shutdown complete.")

    def _publish_to_ha(self):
        """Publishes the current state to Home Assistant via AppDaemon's service."""
        try:
            with self.attr_state_lock:
                self.call_service("mqtt/publish",
                                  topic=self.ATTR_TOPIC,
                                  payload=json.dumps(self.attr_state, ensure_ascii=False),
                                  qos=0, retain=False)
                
                active = "1" if self.attr_state["selected_areas_active_alerts"] else "0"
                self.call_service("mqtt/publish",
                                  topic=self.STATE_TOPIC,
                                  payload=active,
                                  qos=0, retain=False)
            
            self.log(f"Successfully published state to Home Assistant. Active: {active}", level="INFO")
        except Exception as e:
            self.error(f"Failed to publish to Home Assistant: {e}", level="ERROR")
            
    def _on_message_pushy(self, msg_payload):
        """Handles incoming messages from the alert service."""
        now = datetime.now() # Naive datetime for AppDaemon compatibility
        self.log(f"RAW NOTIFICATION @ {now.isoformat()}: {msg_payload}", level="DEBUG")
        if self.DEBUG: return

        aid = (msg_payload.get("alertTitle") or msg_payload.get("id") or "").strip()
        if not aid or aid in self._seen: return
        self._seen.append(aid)

        title = msg_payload.get("title", "").strip()
        raw_time = msg_payload.get("time", "")

        alert_time = ""
        latency = None
        try:
            if raw_time:
                dt_object = None
                if "T" in raw_time:
                    try:
                        dt_object = datetime.fromisoformat(raw_time)
                    except ValueError:
                        dt_object = datetime.strptime(raw_time, "%Y-%m-%dT%H:%M:%S%z")
                else:
                    dt_object = datetime.strptime(raw_time, "%Y-%m-%d %H:%M:%S")

                if dt_object:
                    dt_naive = dt_object.astimezone(None).replace(tzinfo=None)
                    latency = (now - dt_naive).total_seconds()
                    alert_time = dt_naive.strftime("%Y-%m-%d %H:%M:%S")
                    self.log(f"📩 Received alert '{aid}' for '{title}' with latency: {latency:.2f}s", level="INFO")
            
            if latency is not None and latency > self.MAX_AGE_S:
                self.log(f"Skipping stale alert {aid} (latency: {latency:.2f}s > max_age: {self.MAX_AGE_S}s)", level="WARNING")
                return
        except Exception as e:
            self.log(f"Could not parse timestamp '{raw_time}': {e}", level="WARNING")
            alert_time = raw_time

        segs = set(msg_payload.get("citiesIds", "").split(","))
        hits = segs & self.SEGMENTS
        if not hits: return

        self.log(f"✅ Alert '{title}' is relevant for segments: {hits}", level="INFO")
        
        is_real = (title in {"ירי רקטות וטילים", "חדירת כלי טיס עוין"})
        attr_list_key = "selected_areas_active_alerts" if is_real else "selected_areas_updates"
        clear_list_key = "selected_areas_updates" if is_real else "selected_areas_active_alerts"

        with self.attr_state_lock:
            self.attr_state[clear_list_key].clear()
            for seg in hits:
                entry = {
                    "alertDate": alert_time,
                    "title": title,
                    "data": self.name_map.get(seg, seg),
                    "category": msg_payload.get("threatId", ""),
                    "id": aid
                }
                self.attr_state[attr_list_key].append(entry)
        
        self._publish_to_ha()

    def _cleanup_and_republish(self, kwargs):
        """Periodically checks for and removes stale alerts."""
        now = datetime.now() # Use naive datetime for comparison
        dirty = False
        with self.attr_state_lock:
            for key in ("selected_areas_active_alerts", "selected_areas_updates"):
                fresh_list = []
                for item in self.attr_state[key]:
                    try:
                        alert_dt = datetime.strptime(item["alertDate"], "%Y-%m-%d %H:%M:%S")
                        if (now - alert_dt).total_seconds() < self.EXPIRY_S:
                            fresh_list.append(item)
                        else:
                            self.log(f"Expiring old alert: {item.get('id', 'N/A')}", level="INFO")
                            dirty = True
                    except Exception as e:
                        self.log(f"Could not parse date for cleanup: {item.get('alertDate', 'N/A')} - {e}", level="WARNING")
                        fresh_list.append(item)
                
                if len(fresh_list) < len(self.attr_state[key]):
                    self.attr_state[key] = fresh_list
                    dirty = True

        if dirty:
            self.log("State has changed due to expired alerts, republishing to HA.", level="INFO")
            self._publish_to_ha()

    def initialize_ha_sensor(self):
        self.log("Publishing initial state to Home Assistant...", level="INFO")
        self._publish_to_ha()
    
    def _load_json(self, path):
        try:
            with open(path, "r", encoding="utf-8") as f: return json.load(f)
        except: return {}

    def _save_json(self, path, data):
        with open(path, "w", encoding="utf-8") as f: json.dump(data, f)

    def _get_android_id(self):
        if os.path.exists(self.ANDROID_ID_FILE): return open(self.ANDROID_ID_FILE).read().strip()
        aid = f"{random.getrandbits(64):016x}{self.ANDROID_SUFFIX}"
        with open(self.ANDROID_ID_FILE, "w") as f: f.write(aid)
        return aid

    def _api_post(self, path, payload, *, bypass_status=False):
        url = self.API_HOST + path
        try:
            r = requests.post(url, json=payload, timeout=self.CONNECT_TIMEOUT, headers={"Content-Type": "application/json"})
            r.raise_for_status() # Raise an exception for bad status codes
            body = r.json()
            if bypass_status and isinstance(body, dict) and body.get("success") is True: return body
            return body or {}
        except requests.exceptions.RequestException as e:
            self.error(f"API request to {url} failed: {e}")
            raise

    def _subscribe_topics(self, token, auth, topics):
        if isinstance(topics, str): topics = [topics]
        try:
            resp = self._api_post("/devices/subscribe", {"token": token, "auth": auth, "topics": topics}, bypass_status=True)
            if not resp.get("success", False): self.error(f"Subscribe failed: {resp}")
        except Exception as e: self.error(f"Exception during subscribe: {e}")

    def _unsubscribe_topics(self, token, auth, topics):
        if isinstance(topics, str): topics = [topics]
        try:
            resp = self._api_post("/devices/unsubscribe", {"token": token, "auth": auth, "topics": topics}, bypass_status=True)
            if not resp.get("success", False): self.error(f"Unsubscribe failed: {resp}")
        except Exception as e: self.error(f"Exception during unsubscribe: {e}")

    def _reconcile_subscriptions(self):
        if not os.path.exists(self.SUBS_FILE):
            self.log("No subs.json → first-run dance (sub1,unsub1)")
            self._subscribe_topics(self.token, self.auth, "1")
            self._unsubscribe_topics(self.token, self.auth, "1")
            prev = set()
        else:
            prev = set(self._load_json(self.SUBS_FILE).get("topics", []))
        desired = set(self.SEGMENTS)
        to_unsub = list(prev - desired)
        to_sub = list(desired - prev)
        if to_unsub:
            self.log(f"Unsubscribing removed: {to_unsub}")
            self._unsubscribe_topics(self.token, self.auth, to_unsub)
        if to_sub:
            self.log(f"Subscribing new: {to_sub}")
            self._subscribe_topics(self.token, self.auth, to_sub)
        if to_unsub or to_sub:
            self._save_json(self.SUBS_FILE, {"topics": list(desired)})
            self.log("Saved updated subs.json")
        else:
            self.log("No subscription changes needed.")

    def _ensure_authenticated(self):
        creds = self._load_json(self.TOKEN_FILE)
        if creds.get("token") and creds.get("auth"):
            self.log("Loaded credentials from token.json")
            return creds["token"], creds["auth"]
        aid = self._get_android_id()
        reg = self._api_post("/register", {
            "androidId": aid, "app": None, "appId": self.APP_ID,
            "platform": "android", "sdk": self.SDK_VERSION
        })
        token, auth = reg["token"], reg["auth"]
        self._save_json(self.TOKEN_FILE, {"token": token, "auth": auth})
        self.log("Registered new device and saved credentials")
        return token, auth

class IoRefListener:
    def __init__(self, app_instance: MissileAlertsApp):
        self.app = app_instance
        self.token = app_instance.token
        self.auth = app_instance.auth
        self.stopping = False
        
        # Use the main app logger provided by AppDaemon
        self.logger = app_instance.log

        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2, 
            client_id=self.token, 
            clean_session=False
        )
        
        self.client.username_pw_set(self.token, self.auth)
        self.client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS)

        self.client.on_connect = self._on_connect
        self.client.on_message = lambda c, u, m: self.app._on_message_pushy(json.loads(m.payload.decode()))
        self.client.on_disconnect = self._on_disconnect
        
        # Paho can use the standard logger, but AppDaemon's log methods are preferred
        # self.client.enable_logger(logging.getLogger("paho_mqtt_client"))

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            self.app.log("MQTT Connection Successful (rc: 0)")
            client.subscribe(self.token, self.app.QOS)
        else:
            self.app.error(f"MQTT Connection failed: {reason_code}. Paho's loop will retry.")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        if not self.stopping:
            self.app.log(f"Disconnected from MQTT (rc: {reason_code}). Paho's loop will attempt to reconnect automatically.", level="WARNING")

    def start_loop(self):
        socket.setdefaulttimeout(self.app.CONNECT_TIMEOUT)
        while not self.stopping:
            try:
                endpoint = self.app.MQTT_TEMPLATE.replace("{timestamp}", str(int(time.time())))
                port = self.app.MQTT_PORT
                ka = self.app.KEEPALIVE_SEC
                self.app.log(f"Attempting to connect to: {endpoint}")
                self.client.connect(endpoint, port, ka)
                self.client.loop_forever()
            except (socket.timeout, OSError) as e:
                self.app.error(f"Connection error: {e}. Retrying after 5 seconds.")
                time.sleep(5)
            except Exception as e:
                if not self.stopping:
                    self.app.error(f"An unexpected error occurred in the listener thread: {e}. Retrying after 10 seconds.")
                    time.sleep(10)
    
    def stop(self):
        self.stopping = True
        self.client.disconnect()