# pikud_haoref_mqtt

> **Purpose:**  
> Replicate the *Pikud Haoref* Android app’s real-time rocket–alert feed in Python so Home Assistant can receive faster, more reliable notifications.  
> **Status:** Stable, battle-tested for several days; occasional latency only during nationwide alert storms.

---

## ⚠️  Rate-Limit & Behaviour Guidelines

1. **One Android ID = One token.**  
   Generating multiple IDs floods the broker with clients-to-track and risks a ban for this whole solution, so please know what you doing before running it, feel free to ask          questions.  
   *Hard-code or persist the ID after the first run.*

2. **Use Pushy’s official Python SDK** (`pushy-python`).  
   - The SDK mimics the mobile app’s rotating host pattern (`mqtt-{timestamp}.ioref.io`) and other quirks.  
   - Plain `paho-mqtt` works, but leads to spurious disconnects and slower delivery.

3. **Manual activation step:**  
   `POST /devices/auth` must include `androidId`. Pushy’s helper doesn’t expose it, so call the endpoint yourself **once** (see below).

4. **Don’t hammer the broker.**  
   The original app reconnects **once per minute**. Polling every few seconds causes cooldown kicks. (This irrelevant if you would follow the rules of using the pushy-python          libary)

---

## Contents

```
.
├── data_examples/
│   ├── pushy_missile_alerts.log
│   └── test_data.jsonl
├── automation_examples/
│   ├── configuration.yaml
│   └── app.yaml
├── raw_data/
│   └── (DB-scraping helpers & title lists)
├── cities.json          # All city IDs (all the test cities should be removed in the final filtered .json, usually 500300 topic broadcast every few hours a test alert)
├── titles.json          # list of all possible hebrew titles that the app can send out to users
└── pushy_missile_alerts.py
```

---

## Quick-Start (Home Assistant + AppDaemon)

> *The script is written as an **AppDaemon** app.  
> If you want a standalone script, just strip the AppDaemon class wrapper – everything else is pure Python.*

1. **Pick your cities**  
   - Open **`cities.json`** and copy the IDs of the localities you care about (e.g. `5001347` for “קריית מוצקין”).  

2. **Configure `pushy_missile_alerts.py`**  
   ```python
   SEGMENTS = [5001347, 5001234, ...]  # <- your city IDs
   ```

3. **Deploy**  
   ```text
   /config/appdaemon/apps/pushy_missile_alerts.py
   /config/appdaemon/apps/apps.yaml
   ```
   Example `apps.yaml` snippet is in **`automation_examples/app.yaml`**.

4. **Create MQTT sensors** in Home Assistant  
   See **`automation_examples/configuration.yaml`** for two ready-made sensors.

5. **Restart HA & AppDaemon**  
   - Check the logs; fix any traceback before leaving it running.  
   - First launch will register the device and fetch a token.
   - Check log if the /device/auth request went successfully or with forbidden request, if forbidden skip to the next step (and turn off appdaemon or else it would keep run the script in endless) , if no any error so all is OK!

---

## One-Time `/device/auth` Activation - Skip if didnt have forbidden error in script log

Pushy’s SDK misses some required parameters, so run this **once** | Only if the log shows that you received forbidden error in our request attempt.. :

```bash
curl -X POST https://pushy.ioref.app/devices/auth   -H "Content-Type: application/json"   -H "User-Agent: Mozilla/5.0 (Linux; Android 11; Xiaomi 2107113SI) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/101.0.4951.64 Mobile Safari/537.36"   -d '{"androidId":"<YOUR_ANDROID_ID>",
       "appId":"66c20ac875260a035a3af7b2",
       "auth":"<YOUR_AUTH>",
       "sdk":10117,
       "token":"<YOUR_TOKEN>"}'
```

After the 200 OK, restart AppDaemon – the broker will now push alerts to your topics.

---

## How It Works

| Component | Role |
|-----------|------|
| **Pushy SDK** | Maintains an MQTT connection, rotating hosts every minute like the Android app. |
| **`pushy_missile_alerts.py`** | Subscribes to city-specific topics, decodes messages, and publishes two Home Assistant-friendly MQTT topics: `selected_areas_active_alerts` and `selected_areas_updates`. |
| **Automations** | Combine this sensor with the [amitfin/oref_alert](https://github.com/amitfin/oref_alert) integration for redundancy and race-condition guards. |

Message samples live in **`data_examples/test_data.jsonl`**.

---

## Known Limitations & Ideas

| Issue | Details | Potential Fix |
|-------|---------|---------------|
| **`/devices/auth` returns 403** | Cloudflare blocks the request call; manual curl might be required (see above). | Patch Pushy SDK or wrap the request in Python. |
| **Latency during nationwide barrages** | Multiple simultaneous city alerts create backlog. | Investigate QoS, reconnect strategy, or multi-threaded client. |
| **Threat classification** | Script currently flags titles containing `ירי רקטות` or `כלי טיס` as *unsafe*. | Use `threat_id` or a whitelist from `titles.json` for robustness. |
| **Aggressive reconnect experiments** | Polling every 2 s lowers latency but triggers broker cooldowns. | Stick to 60 s rotations that the pushy-libary already does same as original app. (As MQTT not need any pulling, it's working by having active connection to broker and wait till it publish out a message, pulling was just a test I did during the inital stage where I couldnt get message automatically due to not knowing how to activate my generated token.) |

*For the latency issue that happened few times to me, I'm currently checking out some more different configurations, if manage to produce better results I will update the script here.*
---

## Contributing

1. Fork ➜ Branch ➜ PR.  
2. Follow *PEP 8*; keep external deps minimal (Pushy + stdlib).  
3. Describe testing steps in your PR.

---

## License

MIT – do what you want, but no responsibility for missed alerts or broker bans.

---

> *“Because five seconds can save lives.”*
