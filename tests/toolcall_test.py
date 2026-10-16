import json, sys, urllib.request
URL = "http://127.0.0.1:8080/v1/chat/completions"
THINK = len(sys.argv) > 1 and sys.argv[1] == "think"

TOOLS = [
  {"type":"function","function":{"name":"get_water_level",
   "description":"Balatoni vízállás lekérése egy mérőállomásról (cm, 24 órás trend cm-ben)",
   "parameters":{"type":"object","properties":{"station":{"type":"string"}},"required":["station"]}}},
  {"type":"function","function":{"name":"publish_card",
   "description":"Kártya küldése a felhasználó telefonjára",
   "parameters":{"type":"object","properties":{
     "type":{"type":"string","enum":["water_level","system_health","arch_news"]},
     "payload":{"type":"object"},
     "severity":{"type":"string","enum":["info","warning","critical"]}},
     "required":["type","payload","severity"]}}},
]
MOCK = {"get_water_level": lambda a: {"station": a.get("station"), "cm": 94, "trend_24h": -3},
        "publish_card":    lambda a: {"ok": True, "id": 1}}

msgs = [{"role":"user","content":"Nézd meg a siófoki vízállást. Ha 100 cm alatt van, küldj róla warning kártyát az értékkel és a trenddel, különben info kártyát."}]
for step in range(6):
    body = {"messages":msgs,"tools":TOOLS,"chat_template_kwargs":{"enable_thinking":THINK}}
    req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type":"application/json"})
    r = json.load(urllib.request.urlopen(req))
    m = r["choices"][0]["message"]; t = r["timings"]
    print(f"--- lépés {step}: {t['predicted_n']} tok, {t['predicted_per_second']:.1f} tok/s")
    msgs.append(m)
    calls = m.get("tool_calls") or []
    if not calls:
        print("VÉGE:", m.get("content")); break
    for c in calls:
        args = json.loads(c["function"]["arguments"])
        res = MOCK[c["function"]["name"]](args)
        print(f"  CALL {c['function']['name']}({json.dumps(args, ensure_ascii=False)}) -> {res}")
        msgs.append({"role":"tool","tool_call_id":c["id"],"content":json.dumps(res)})
