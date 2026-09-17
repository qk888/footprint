# -*- coding: utf-8 -*-
"""为 README 生成界面截图（放进仓库 docs/screenshots/）。

v2：
- 规划/记录页直接带 ?adcode=&city= 导航（等价于地图上点城市后点按钮，不依赖 canvas 命中）
- 聊天截图滚到「用户提问 + 卡片开头」，讲得出故事
- >800KB 的截图用 Pillow 压一档（GitHub README 加载快）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2e_ui as E  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "screenshots")
os.makedirs(OUT, exist_ok=True)

VIEW = {"width": 1440, "height": 900}
DEMO_CITY = ("500000", "重庆")        # 演示账号里数据最全的城市：40 份规划 / 37 笔账单


def shot(page, name):
    path = os.path.join(OUT, name + ".png")
    page.screenshot(path=path)
    print("  已保存", name + ".png", f"({os.path.getsize(path)//1024} KB)")


def nav(page, label, settle=2.5):
    page.click(f"button:has-text('{label}')")
    time.sleep(settle)


def wait_card(page):
    """等行程卡片渲染完且内容稳定"""
    for _ in range(90):
        time.sleep(1)
        if not page.query_selector(".msg-card"):
            continue
        stable, last = 0, ""
        for _ in range(12):
            cur = page.query_selector(".msg-card").inner_text()
            if cur == last and len(cur) > 50:
                stable += 1
                if stable >= 3:
                    return True
            else:
                stable, last = 0, cur
            time.sleep(1)
        return True
    return False


def main():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=E.CHROME, headless=True)
        page = b.new_context(viewport=VIEW, device_scale_factor=2).new_page()

        # ── 01 登录页 ──
        page.goto(E.BASE, wait_until="networkidle")
        time.sleep(2)
        shot(page, "01_login")

        E.login(page)
        time.sleep(2)

        # ── 02 足迹地图 ──
        nav(page, "地图", settle=8)
        for _ in range(20):
            if page.query_selector("img.leaflet-tile"):
                break
            time.sleep(1)
        time.sleep(4)
        shot(page, "02_map")

        # ── 03 AI 助手：提问 + 行程卡片 ──
        nav(page, "AI 助手", settle=2)
        before = len(page.query_selector_all(".bubble.ai, .msg-card"))
        page.fill("input[placeholder*='输入消息']", "帮我规划三亚三天")
        page.click("button.send")
        ok = wait_card(page)
        print("  行程卡片就绪:", ok)
        if ok:
            # 滚到「用户提问」处：提问气泡 + 卡片开头同框。
            # 用矩形差值直接改 .chat-list.scrollTop（scrollIntoView 在这页不生效），
            # 并回读位置确认真的滚上去了。
            top = page.evaluate("""() => {
                const el = document.querySelector('.chat-list')
                const qs = document.querySelectorAll('.bubble.me')
                const q = qs[qs.length - 1]
                if (!el || !q) return 'missing'
                const er = el.getBoundingClientRect()
                const qr = q.getBoundingClientRect()
                el.scrollTop += qr.top - er.top - 8
                return Math.round(el.scrollTop)
            }""")
            time.sleep(1.5)
            print("  聊天滚动 scrollTop =", top)
        shot(page, "03_chat")

        # ── 04 行程规划（带参直航，等价于地图选城后点「规划」）──
        page.goto(f"{E.BASE}/plans?adcode={DEMO_CITY[0]}&city={DEMO_CITY[1]}",
                  wait_until="networkidle")
        time.sleep(3.5)
        shot(page, "04_plans")

        # ── 05 旅行记录（展开行程看账单明细）──
        page.goto(f"{E.BASE}/records?adcode={DEMO_CITY[0]}&city={DEMO_CITY[1]}",
                  wait_until="networkidle")
        time.sleep(3)
        if page.query_selector(".trip-head"):
            page.click(".trip-head")
            time.sleep(1.5)
        shot(page, "05_records")

        # ── 06 我的 ──
        nav(page, "我的", settle=3)
        shot(page, "06_profile")

        b.close()

    # ── README 头图 logo：SVG → PNG（直接开 SVG 文件、8 倍缩放截元素）──
    svg = os.path.abspath(os.path.join(os.path.dirname(__file__), "..",
                                       "web", "public", "favicon.svg"))
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=E.CHROME, headless=True)
        ctx = b.new_context(viewport={"width": 200, "height": 200},
                            device_scale_factor=8)
        page = ctx.new_page()
        page.goto("file:///" + svg.replace("\\", "/"))
        time.sleep(1)
        page.locator("svg").screenshot(path=os.path.join(OUT, "logo.png"))
        b.close()
    print("  已保存 logo.png")

    # ── 压缩 >800KB 的截图 ──
    try:
        from PIL import Image
        for f in sorted(os.listdir(OUT)):
            p = os.path.join(OUT, f)
            if os.path.getsize(p) <= 800 * 1024:
                continue
            img = Image.open(p)
            w, h = img.size
            if w > 1920:                                # 先降分辨率
                img = img.resize((1920, int(h * 1920 / w)), Image.LANCZOS)
            img = img.convert("RGB").quantize(colors=256, method=Image.MEDIANCUT)
            img.save(p, optimize=True)
            print(f"  已压缩 {f} → {os.path.getsize(p)//1024} KB")
    except ImportError:
        print("  (无 Pillow，跳过压缩)")

    print("\n全部完成 →", OUT)


if __name__ == "__main__":
    main()
