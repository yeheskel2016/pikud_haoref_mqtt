# pikud_haoref_mqtt
Replicate how pikud horef app works in python for quicker and more reliable alerts for home assistant
--------------------------------------------------------------------------------------------------------
Before attempting to use it, please do not abuse it and make sure to follow the proper instructions so it wont get blocked, we are small community anyways... 
*Do not generate too many tokens! The MQTT broker API will generate a unique token each unique AndroidId we provide it, so if running it make sure you hardcode the androidid/generate it only once and save it, as if we run multiple times we would basically "generate" multiple clients and this will get it probably blocked eventually.
*Use exactly the same Pushy libary for the MQTT client - https://pypi.org/project/pushy-python/ we want to have exactly the same behavior as the android app (which using the PushySDK for android) intially I tried mimick the same logic just using a paho-mqtt client (same as the pushy was doing) but the results wasnt so good, of course if someone will digg more probably he might get better results but in my opinion it's better to behave the same as the original android app is doing to not raise any red flags.
(For example the Pushy SDK rotate the MQTT host every minute in the following format "mqtt-{timestamp}.ioref.io" so it's better to follow the same and just use exactly the same libary..)
*You will notice in the code that for the register and auth endpoints we manually making a request and not using the Pushy libary functions, it's because they not having the required payload that the pikudhaoref api is looking for (it missing some parameters such as the AndroidId)

Current code example is running through Appdaemon I have configured together with MQTT (Mosquitto) configured, hopefully the idea is to integrate this script to work as additional source for amitfin/oref_alert HACS integration this way we would have the most reliable and quick data - https://github.com/amitfin/oref_alert/issues/234
--------------------------------------------------------------------------------------------------------

What I noticed in the past 3 days of using this solution is that the results are stable as long as there is no country wide alerts, if there is plenty of alerts I noticed sometimes we receive the alert with a latency, but usually most of the times the latency is instant and much more quicker from the pikudhaoref api by ~5 sec in avg, I did notice sometimes that the Pikudhaoref API will provide the alert (usually the update alerts) quicker, this why I dont see this solution as standalone but as a good addition to already ready integration, in my current automation I'm using both amitfin/oref_alert integration and this solution combined.

See \data_examples\pushy_missile_alerts.log | test_data.jsonl for understanding how the messages arriving from the pikudhaoref mqtt broker.

See \automation_examples for example of automations I currently have running in my HA which using both the MQTT sensors for this integration + 2 sensors from amitfin/oref_alert integration (with proper guards that if automation was already kicked in we ignore next requests to avoid duplications and it's working well)

See \raw_data for explanation on how exactly I grabbed the relevant city id list/all titles from the app database (As in the future we might need to update our lists in case app is updating with new DB)

--------------------------------------------------------------------------------------------------------
# Installation and possible limitation
*This script "pushy_missile_alerts.py" is adapted for Appdaemon, use GPT to convert it to regular python format if needed* 

1.First of all open cities.json and write down the relevant cities you would like to listen to, i.e if I want to listen to "קריית מוצקין" I would take the id of 5001347.
2.Open the main script "pushy_missile_alerts.py" and in "SEGMENTS" modify to your relevant cities ids
3.Put the script in your appdaemon/apps location 
4.Modify your apps.yaml as in the example (app.yaml) so appdaemon will load this script (*Turn off appdaemon before so it wont load yet the script)
5.Create your MQTT sensors (see \automation_examples\configuration.yaml for example of 2 sensors that listen to 2 different cities)
6.Reload HA so the sensors will appear
7.Start Appdaemon

8.Notice right away the created log and see if no any errors, if error stop the appdaemon and fix manually.
Limitation-
1.Most probably there would be an error for the request to /device/auth saying forbidden, I have no clue why the register endpoint works with a simple request and the /device/auth endpoint returns with forbidden from the cloudflare protection there, the workaround I did was manually using a curl command as follows :

curl -X POST https://pushy.ioref.app/devices/auth   -H "Content-Type: application/json"   -H "User-Agent: Mozilla/5.0 (Linux; Android 11; Xiaomi 2107113SI) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/101.0.4951.64 Mobile Safari/537.36"   -d '{"androidId":"your_generated_androidid","appId":"66c20ac875260a035a3af7b2","auth":"your_generated_auth","sdk":10117,"token":"your_generated_token"}'

and it returned with proper success.. this endpoint should be reached just once, this what activates our newly generated token/auth with the pikudhaoref broker, if not going with this route, the broker will not send us alerts.

Now after the curl command you can rerun appdaemon and everything should be init properly, any alerts that will arrive will update your sensor, in the code it check for title of "ירי רקטות" and "כלי טיס" to consider it an unsafe alert (and then it updates it under "selected_areas_active_alerts" attribute to fit amitfin/oref_alert integration and if it's any other title we keep the state as unsafe and updating the "selected_areas_updates")

The correct idea would to base our self by the threat_id (see examples from \data_examples\test_data.jsonl) or build a list of titles from titles.json that should be considered as unsafe alert (the messages are arriving exactly with the same title so we can count on it, as the titles are from the app db and cant be changed suddenly with extra spaces or stuff that we saw in the api)

2.One of the tests I made before fully understand the way the app works was to try listen the MQTT host every 2 sec (disconnect and reconnect every 2 sec) this was a terrible solution and I did it because it was intially the only way I could receive alerts (It took me time to understand that in order for the broker to send me alerts I need to go through /device/auth to activate my generated token...) but when I was using that solution for a day the latency was even before the app it self, I think it's because we manually pulled updates before even pikudhaoref used their tools to spread the alerts to the relevant clients that listen to the relevant topics, also with this solution I had many cooldown disconnections from the pikudhaoref broker as it was irregular thing (the original app disconnect/reconnect to a new host every minute.. not every 2 sec..) but I think if someone who has more knowledge he can surely improve more our connection, for now as it is with the current script and the pushy client I'm pleased with the results, specially when combined with the oref_alert integration it making it robust.







