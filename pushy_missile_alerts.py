#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# apps/pushy_missile_alerts.py
#
# Requires:
#   pip install pushy-python==1.0.12 requests

import sys
import os
import json
import random
import threading
import requests
from collections import deque
from datetime import datetime

import appdaemon.plugins.hass.hassapi as hass
import pushy
from pushy.util import localStorage
from pushy.config import config
import pushy.lib.mqtt as pmqtt
import logging

# ──────────────────────────────────────────────────────────────
# 0) Logfile path
# ──────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(__file__)
LOG_PATH = os.path.join(SCRIPT_DIR, "pushy_missile_alerts.log")

# ──────────────────────────────────────────────────────────────
# 1) Application logger
# ──────────────────────────────────────────────────────────────
logger = logging.getLogger("pushy_missile")
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s"))
fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
fh.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s"))
logger.addHandler(ch)
logger.addHandler(fh)

# ──────────────────────────────────────────────────────────────
# 2) HTTP debug (urllib3)
# ──────────────────────────────────────────────────────────────
u3 = logging.getLogger("urllib3.connectionpool")
u3.setLevel(logging.DEBUG)
u3.propagate = False
u3.addHandler(ch); u3.addHandler(fh)

# ──────────────────────────────────────────────────────────────
# 3) Paho-MQTT internals
# ──────────────────────────────────────────────────────────────
mqtt_log = logging.getLogger("paho.mqtt.client")
mqtt_log.setLevel(logging.DEBUG)
mqtt_log.propagate = False
mqtt_log.addHandler(ch); mqtt_log.addHandler(fh)
pmqtt.client.enable_logger(mqtt_log)

# ──────────────────────────────────────────────────────────────
# 4) Pushy “connected” listener
# ──────────────────────────────────────────────────────────────
def _on_pushy_conn():
    logger.info("✅ Pushy reports: Connected successfully")

# ──────────────────────────────────────────────────────────────
# Settings - #Do not touch this must be exactly like in the application
# ──────────────────────────────────────────────────────────────
APP_ID             = "66c20ac875260a035a3af7b2"
API_HOST           = "https://pushy.ioref.app"
MQTT_HOST_TEMPLATE = "mqtt-{timestamp}.ioref.io"
KEEPALIVE_SEC      = 300
# ──────────────────────────────────────────────────────────────

SEGMENTS           = {"5001878", "5001347"} #Kiryat Haim , Kiryat Motzkin | can be anything according to cities.json, segments are subbed only one time, any changes will apply the relevant unsub/sub logic if required
MAX_AGE_S          = 45 #Latency protection, if we receive from the broker alert that was created 30 sec ago or more, we ignore it.

KS      = config["storageKeys"]
SUB_KEY = "pushySubscribedTopics"

# Mimic Android
config["sdk"]["platform"] = "android"
config["sdk"]["version"]  = "10117"

# Stable Android-ID suffix
ANDROID_SUFFIX = "-Xiaomi-2107113SI"
HEADERS = {
    "User-Agent": (
      "Mozilla/5.0 (Linux; Android 11; Xiaomi 2107113SI) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/101.0.4951.64 Mobile Safari/537.36"
    ),
    "Content-Type": "application/json",
}

# ──────────────────────────────────────────────────────────────
# Helper: Pushy-style POST with error-handling
#   • Accepts non-2xx when bypass_status=True
#   • Treats {"success": true} in body as real success
# ──────────────────────────────────────────────────────────────
def api_post(path, payload, *, bypass_status=False):
    url = f"{API_HOST}{path}"
    r   = requests.post(url, json=payload, headers=HEADERS, timeout=10)

    # Try to decode body once – we’ll need it regardless of status
    try:
        body = r.json()
    except ValueError:
        body = None                    # not JSON, keep raw for error messages

    # 1) Standard HTTP success
    if 200 <= r.status_code <= 299:
        return body if body is not None else r.text

    # 2) Non-2xx *but* caller says “trust the body”
    if bypass_status and isinstance(body, dict) and body.get("success") is True:
        return body                    # treat as success, ignore 4xx/5xx

    # 3) Everything else → raise
    if isinstance(body, dict):
        code = body.get("code", "")
        err  = body.get("error", body.get("message", "Unknown error"))
        raise Exception(f"{code}: {err} (HTTP {r.status_code})")
    raise Exception(f"{r.text.strip()} (HTTP {r.status_code})")

class PushyMissileAlerts(hass.Hass):

    def initialize(self):
        logger.debug(">>> initialize()")
        # dedupe buffer
        self._seen = deque(maxlen=2000)
        # start all segments OFF
        for seg in SEGMENTS:
            self._publish(seg, "0")

        # 1) configure SDK endpoints & keepalive
        pushy.setEnterpriseConfig(API_HOST, MQTT_HOST_TEMPLATE)
        pushy.setHeartbeatInterval(KEEPALIVE_SEC)
        config["mqtt"]["enterprisePortNumber"] = "443"

        # 2) clear on start? check apps.yaml if flag is true or false, if true we clear all our stored token keys and next run need to set it to false and run it so it would reregister
        if self.args.get("clear_on_start", False):
            logger.warning("🔄 clear_on_start → full wipe")
            pushy.setEnterpriseConfig(None, None)
            localStorage.delete(SUB_KEY)
            localStorage.delete("pushyAndroidId")
            logger.info("Clear- Success, make sure to turn the flag back to false")
            return

        # 3) stable androidId
        android_id = localStorage.get("pushyAndroidId")
        if not android_id:
            android_id = f"{random.getrandbits(64):016x}{ANDROID_SUFFIX}"
            localStorage.set("pushyAndroidId", android_id)
        logger.debug(f"Using androidId={android_id}")

        # 4) register if needed
        if not pushy.isRegistered():
            logger.info("🆕 No creds → /register")
            payload = {
                "androidId": android_id,
                "app":       "null",
                "appId":     APP_ID,
                "platform":  "android",
                "sdk":       int(config["sdk"]["version"])
            }
            try:
                data  = api_post("/register", payload)
                token = data["token"]
                auth  = data["auth"]
                logger.info(f"✅ Registered → token={token} auth={auth}")
                localStorage.set(KS["token"],     token)
                localStorage.set(KS["tokenAuth"], auth)
                localStorage.set(KS["tokenAppId"], APP_ID)
            except Exception:
                logger.exception("❌ /register failed")
                sys.exit(1)
                
            # **NOW** call /devices/auth (must succeed or we 403)
            logger.info("🔒 Calling /devices/auth")
            auth_payload = {
                "androidId": android_id,
                "appId":     APP_ID,
                "auth":      localStorage.get(KS["tokenAuth"]),
                "sdk":       int(config["sdk"]["version"]),
                "token":     localStorage.get(KS["token"]),
            }
            try:
                res = api_post("/devices/auth", auth_payload, bypass_status=True)
                if not res.get("success", False):
                    raise ValueError(res)
                logger.info("🔒 /devices/auth succeeded")
            except Exception as e:
                logger.error(f"❌ /devices/auth failed: {e}")
                sys.exit(1)
                
        else:
            tok  = localStorage.get(KS["token"])
            aut  = localStorage.get(KS["tokenAuth"])
            logger.info(f"ℹ️ Cached → token={tok} auth={aut}")

        # 5) spawn MQTT thread
        t = threading.Thread(target=self._start_pushy_loop, daemon=True, name="PushyLoop")
        t.start()
        logger.info("🚀 initialize() complete")

    def _start_pushy_loop(self):
        logger.debug(">>> _start_pushy_loop entered")
        # a) open MQTT socket
        try:
            pushy.listen()
            logger.info("📡 MQTT socket is up")
        except Exception:
            logger.exception("❌ pushy.listen() failed")
            return

        # load previous SUB_KEY to see which segments need to be added/removed/nothing
        prev_raw = localStorage.get(SUB_KEY) or "[]"
        try:
            prev = set(json.loads(prev_raw))
        except:
            prev = set()

        # ─── only on first run ─────────────────────── #This is the original app behaivor, do not remove we want to keep the same logic even if making no sense to do so..
        if not prev:
            try:
                pushy.unsubscribe("*");  logger.debug("🔕 Stock unsubscribe-all (`*`)")
                pushy.subscribe("1");    logger.debug("🔔 Stock subscribe `1`")
                pushy.unsubscribe("1");  logger.debug("🔕 Stock unsubscribe `1`")
            except Exception:
                logger.exception("⚠️ Stock top-level dance failed")
        # ──────────────────────────────────────────────

        # reconcile desired segments
        desired = set(SEGMENTS)

        # unsubscribe removed - if any changes detected to segments we unsub our self from them
        removed = prev - desired
        if removed:
            try:
                pushy.unsubscribe(list(removed))
                logger.info(f"🔕 Unsubscribed removed segments → {removed}")
            except Exception:
                logger.exception("❌ Failed to unsubscribe removed segments")

        # subscribe new - if any new segements we need to sub we do it here, just once!
        new_topics = list(desired - prev)
        if new_topics:
            try:
                pushy.subscribe(new_topics)
                logger.info(f"🔔 Subscribed new segments → {new_topics}")
                localStorage.set(SUB_KEY, json.dumps(list(desired)))
            except Exception:
                logger.exception("❌ Failed to subscribe new segments")
                return
        else:
            logger.debug("ℹ️ No new segments to subscribe")

        # hook notification callback & hand off to blocking loop
        pushy.setNotificationListener(self._on_alert)
        pushy.loop_forever()

    def terminate(self):
        pushy.disconnect()
        logger.info("🛑 terminate()")

    def _on_alert(self, data):
        now = datetime.now()
        aid = (data.get("alertTitle") or data.get("id") or "").strip()
        if not aid or aid in self._seen: #if it's alert_id we already saw we ignore it
            return
        self._seen.append(aid) 

        title      = data.get("title","").strip()
        
        # original: alert_time = data.get("time","") convert the time to be ase OrefAlert integration for better compatability in the automations
        raw_time = data.get("time") or ""
        try:
            if raw_time:
                if "T" in raw_time:
                    # try native ISO first; fall back to %z which accepts +HHMM
                    try:
                        dt = datetime.fromisoformat(raw_time)
                    except ValueError:
                        dt = datetime.strptime(raw_time, "%Y-%m-%dT%H:%M:%S%z")
                else:
                    dt = datetime.strptime(raw_time, "%Y-%m-%d %H:%M:%S")
                alert_time = dt.strftime("%Y-%m-%d %H:%M:%S")
            else:
                alert_time = ""
        except Exception:
            alert_time = raw_time
        
        segs       = set(data.get("citiesIds","").split(","))
        hits       = segs & SEGMENTS #alert that has cities we looking f or
        
        # drop stale alerts that came in delay, usually when large alerts happening country wide
        latency = None
        if alert_time:
            try:
                dt = datetime.strptime(alert_time, "%Y-%m-%d %H:%M:%S")
                latency = (now - dt).total_seconds()
            except:
                pass
                
        if latency is None or latency > MAX_AGE_S: #45 sec or longer we ignore it
            logger.info(f"📩 {aid} “{title}” → segs={segs} lat={latency:.1f}s")
            logger.info(f"Skipped stale alert {aid} (lat={latency})")
            return
        logger.info(f"📩 {aid} “{title}” → segs={segs} lat={latency:.1f}s")
        
        if not hits: #if it's alert that not relevant to our segments, actually it shouldnt happen as we get alerts only for our subscribed topics
            return
        
        logger.info(f"📩 {aid} “{title}” → segs={hits} lat={latency:.1f}s")
        
        single = dict(
            title=title, id=aid, alertDate=alert_time,
            desc=data.get("desc",""), cat=data.get("threatId","")
        )
        real_titles = {"ירי רקטות וטילים","חדירת כלי טיס עוין"} #would be more logical to not focus on titles but base it on threat_id / a full list of all possible alarm titles from titles.json

        #Updating the MQTT sensors in HA, first the attribute then the sensor state, to avoid trigger without the attribute details
        for seg in hits:
            is_real = (title in real_titles)
            attr = {
                "selected_areas_active_alerts": [single] if is_real else [],
                "selected_areas_updates":       []        if is_real else [single]
            }
            self._publish(f"{seg}_attr", json.dumps(attr, ensure_ascii=False))
            self._publish(seg, "1" if is_real else "0")
            logger.info(f"→ {'REAL' if is_real else 'UPDATE'} alert for {seg}")

    def _publish(self, suffix, payload):
        self.call_service(
            "mqtt/publish",
            topic   = f"missile_alerts/{suffix}",
            payload = payload,
            qos     = 0,
            retain  = False,
        )
