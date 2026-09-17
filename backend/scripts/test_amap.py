# -*- coding: utf-8 -*-
import os, sys, asyncio, logging
sys.path.insert(0, "/app")
logging.basicConfig(level=logging.ERROR)
from dotenv import load_dotenv
load_dotenv("/app/.env", override=True)
print("AMAP_KEY:", os.getenv("AMAP_KEY", "")[:8], "...长度", len(os.getenv("AMAP_KEY", "")))
from app.agent.travel.data_source.amap_client import AmapClient


async def main():
    c = AmapClient()
    for city in ["重庆", "杭州"]:
        df = await c.search_attractions(city)
        print(f"{city} 景点 {len(df)} 条")
        if len(df):
            print("  ", "、".join(df["name"].head(5).tolist()))


asyncio.run(main())
