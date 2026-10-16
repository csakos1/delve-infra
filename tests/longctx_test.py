import json, random, urllib.request
URL = "http://127.0.0.1:8080"
def post(path, body):
    req = urllib.request.Request(URL + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=600))

random.seed(1)
W = ["hajó","szél","vitorla","kikötő","hullám","mérce","part","móló","bója","árboc","kötél","horgony"]
def line(i): return f"{i}. bejegyzés: a {random.choice(W)} állapota {random.randint(1,999)} egység, megjegyzés: {' '.join(random.choices(W, k=8))}."

TARGET = 50000
probe = "\n".join(line(i) for i in range(200))
per_line = len(post("/tokenize", {"content": probe})["tokens"]) / 200
n = int(TARGET / per_line)
random.seed(1)
lines = [line(i) for i in range(n)]
lines.insert(int(n * 0.6), "FONTOS: a Lola téli tárolójának kódja 7341-BX, a hely a 17-es csarnok.")
doc = "\n".join(lines)
print(f"dokumentum: {len(post('/tokenize', {'content': doc})['tokens'])} token")

for q in ["Mi a Lola téli tárolójának kódja? Csak a kódot írd.",
          "Melyik csarnokban van a Lola télen? Csak a számot írd."]:
    r = post("/v1/chat/completions", {
        "messages": [{"role": "user", "content": doc + "\n\n" + q}],
        "chat_template_kwargs": {"enable_thinking": False}, "max_tokens": 50})
    t = r["timings"]
    print(f"\nK: {q}\nV: {r['choices'][0]['message']['content'].strip()}")
    print(f"   cache: {t.get('cache_n', 0)} tok | új prompt: {t['prompt_n']} tok, {t['prompt_ms']/1000:.1f} s "
          f"({t['prompt_per_second']:.0f} tok/s) | gen: {t['predicted_per_second']:.1f} tok/s")
