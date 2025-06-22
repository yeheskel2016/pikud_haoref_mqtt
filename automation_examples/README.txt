In my automations I'm using also https://github.com/amitfin/oref_alert integration for enjoying both worlds, usually the MQTT will provide the alarms in near instant (zero latency), sometimes it might miss something, get in delay so we ignore it
and for that we have the oref_alert integration that gets the data from the api it self (sometimes the api has it's setbacks so having both solution together gives the best experience for the automations, of course relevant guards and conditions are added
to prevent duplicate runs)


For my chime_tts I'm using to broadcast mp3 file of the alert that was generated using https://elevenlabs.io/ hebrew voice to all alexa devices I have, red alert automations will trigger the shalter android tv to open and launch mako tv
and also trigger all the yeelights in the house if they are turned on to switch to red light.