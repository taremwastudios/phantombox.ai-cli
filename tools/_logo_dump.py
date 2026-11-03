import io
import json

lines = io.open("logo.txt", encoding="utf-8").read().split("\n")
out = []
for i, l in enumerate(lines):
    out.append(f"{i:2d} len={len(l):3d} rstripped={len(l.rstrip()):3d} {json.dumps(l, ensure_ascii=False)}")
io.open("logo_dump.txt", "w", encoding="utf-8").write("\n".join(out))
print("ok")
