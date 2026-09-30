"""HF tarjima modellarini haqiqiy manhwa gaplarida sinash: python mtbench.py <model>"""
import json
import sys
import time

import torch
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

torch.set_num_threads(4)
name = sys.argv[1]
N = int(sys.argv[2]) if len(sys.argv) > 2 else 30
data = json.load(open("testset.json", encoding="utf-8"))[:N]
src = [s for s, _ in data]

t0 = time.perf_counter()
tok = AutoTokenizer.from_pretrained(name, src_lang="eng_Latn") if "nllb" in name else AutoTokenizer.from_pretrained(name)
model = AutoModelForSeq2SeqLM.from_pretrained(name, dtype=torch.bfloat16 if "3b" in name.lower() or "3.3B" in name else torch.float32)
model.eval()
load = time.perf_counter() - t0

if "nllb" in name:
    inputs, kw = src, {"forced_bos_token_id": tok.convert_tokens_to_ids("uzn_Latn")}
elif "madlad" in name:
    inputs, kw = ["<2uz> " + s for s in src], {}
elif "m2m100" in name:
    tok.src_lang = "en"
    inputs, kw = src, {"forced_bos_token_id": tok.get_lang_id("uz")}
else:
    raise SystemExit("noma'lum model")

out = []
t0 = time.perf_counter()
with torch.inference_mode():
    for i in range(0, len(inputs), 8):
        batch = tok(inputs[i:i + 8], return_tensors="pt", padding=True)
        gen = model.generate(**batch, max_new_tokens=128, num_beams=4, **kw)
        out += tok.batch_decode(gen, skip_special_tokens=True)
dt = time.perf_counter() - t0
short = name.split("/")[-1]
json.dump(out, open(f"mt_{short}.json", "w", encoding="utf-8"), ensure_ascii=False, indent=0)
print(f"{short}: yuklash {load:.0f} s, tarjima {dt:.1f} s ({dt / len(src):.2f} s/gap)")
