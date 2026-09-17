# -*- coding: utf-8 -*-
# 诊断 .env 为何 dotenv 读不到
with open("/app/.env", "rb") as f:
    raw = f.read()
print("前6字节:", raw[:6])
print("是否UTF8-BOM:", raw[:3] == b"\xef\xbb\xbf")
from dotenv import dotenv_values
vals = dotenv_values("/app/.env")
print("dotenv解析出的键:", list(vals.keys()))
print("AMAP_KEY =", repr(vals.get("AMAP_KEY")))
# 找 AMAP 那行的原始字节
for line in raw.split(b"\n"):
    if b"AMAP" in line:
        print("AMAP行字节:", line)
