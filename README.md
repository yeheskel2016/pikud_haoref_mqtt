# pikud_haoref_mqtt

> **Purpose:**  
> Replicate the *Pikud Haoref* Android app’s real-time rocket–alert feed in Python so Home Assistant can receive faster, more reliable notifications.  
> **Status:** Stable, battle-tested for several days; occasional latency only during nationwide alert storms.

---

## ⚠️  Rate-Limit & Behaviour Guidelines

1. **One Android ID = One token.**  
   Generating multiple IDs floods the broker with clients-to-track and risks a ban for this whole solution, so please know what you doing before running it, feel free to ask          questions.  
   *Hard-code or persist the ID after the first run.*
   
2. **Don’t hammer the broker.**  

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
├── mqttest.py           # A working standalone python script that would publish the updates to your set sensor by the set HA mqtt client
├── apps.yaml            # Example for how the apps.yaml should be with the script for appdaemon run
└── missile_alerts_app.py
```

---

## Quick-Start (Home Assistant + AppDaemon)

> *The script is written as an **AppDaemon** app.  
> If you want a standalone script, try the mqttest.py

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
   Example `apps.yaml` snippet is in **`apps.yaml`**.

4. **Create MQTT sensors** in Home Assistant  
   See **`automation_examples/configuration.yaml`** for two ready-made sensors.

5. **Restart HA & AppDaemon**  
   - Check the logs; fix any traceback before leaving it running.  
   - First launch will register the device and fetch a token then register/sub it to the relevant segemnts you have set (cities).


## How It Works

| Component | Role |
|-----------|------|
| **`missile_alerts_app.py`** | Subscribes to city-specific topics, decodes messages, and publishes two Home Assistant-friendly MQTT topics: `selected_areas_active_alerts` and `selected_areas_updates`. |
| **Automations** | Combine this sensor with the [amitfin/oref_alert](https://github.com/amitfin/oref_alert) integration for redundancy and race-condition guards. |

Message samples live in **`data_examples/test_data.jsonl`**.

---

## Known Limitations & Ideas

| Issue | Details | Potential Fix |
|-------|---------|---------------|
| **Threat classification** | Script currently flags titles containing `ירי רקטות` or `כלי טיס` as *unsafe*. | Use `threat_id` or a whitelist from `titles.json` for robustness. |
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
