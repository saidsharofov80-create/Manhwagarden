"""Ollama'dagi LLM ni manhwa gaplarida sinash: python llmbench.py <model> [N]"""
import json
import re
import sys
import time
import urllib.request

model = sys.argv[1]
N = int(sys.argv[2]) if len(sys.argv) > 2 else 30
src = [s for s, _ in json.load(open("testset.json", encoding="utf-8"))[:N]]

SYSTEM = (
    "Siz manhwa (koreys komiksi) tarjimonisiz. Inglizcha dialog qatorlarini jonli, tabiiy "
    "o'zbek tiliga (lotin yozuvi) tarjima qiling. Qahramonlar gapirayotganini unutmang: "
    "so'zlashuv uslubi, qisqa (pufakchaga sig'sin), ma'no bo'yicha - so'zma-so'z emas. "
    "Ismlar (Rutiger, Nassau, Gaidar) o'zgarmaydi. Har qatorni o'z raqami bilan qaytaring, "
    "boshqa hech narsa yozmang."
)
user = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(src))
body = json.dumps({
    "model": model, "stream": False, "options": {"temperature": 0.2, "num_ctx": 4096},
    "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
}).encode()
t0 = time.perf_counter()
req = urllib.request.Request("http://127.0.0.1:11434/api/chat", data=body,
                             headers={"content-type": "application/json"})
text = json.loads(urllib.request.urlopen(req, timeout=3600).read())["message"]["content"]
dt = time.perf_counter() - t0
text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)   # qwen3 fikrlash qismi
out = [""] * len(src)
for line in text.splitlines():
    m = re.match(r"\s*(\d+)[.)]\s*(.+)", line)
    if m and 1 <= int(m.group(1)) <= len(src):
        out[int(m.group(1)) - 1] = m.group(2).strip()
short = re.sub(r"[^\w.-]", "_", model)[-40:]
json.dump(out, open(f"mt_{short}.json", "w", encoding="utf-8"), ensure_ascii=False, indent=0)
print(f"{model}: {dt:.0f} s, {sum(1 for o in out if o)}/{len(src)} qator -> mt_{short}.json")
