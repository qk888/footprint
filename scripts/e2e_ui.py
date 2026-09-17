"""足迹 Web 端 UI 自助验收（真实浏览器）

用 Playwright 驱动**本机已有的 Chrome**，自己登录、自己点，跑完关键交互后断言。
改完前端 UI 后跑一遍：

    python scripts/e2e_ui.py                       # 默认打 http://127.0.0.1:5173
    E2E_BASE=http://localhost:8080 python scripts/e2e_ui.py

前置：
    pip install playwright                         # 不需要 playwright install（用本机 Chrome）
    cd web && npx vite --port 5173                 # 或者 node node_modules/vite/bin/vite.js

覆盖的回归点（都是踩过的坑）：
  1. 规划页同城多份：切换按钮文字不能被裁、整条可横向滑
  2. 地图 keep-alive：切走再切回后瓦片必须仍铺满容器
     —— 不调 invalidateSize 的话，放大窗口切回来覆盖率只有 0.19（中间一块、四周空白）
  3. 行程卡片保存：成功提示要区分「已新建一份」/「已覆盖原规划」

退出码 0 = 全过。
"""
from __future__ import annotations

import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE = os.getenv("E2E_BASE", "http://127.0.0.1:5173")
CHROME = os.getenv(
    "E2E_CHROME",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)
USER = os.getenv("E2E_USER", "demo@footprint.dev")
PWD = os.getenv("E2E_PWD", "demo1234")
ADCODE = os.getenv("E2E_ADCODE", "500000")
CITY = os.getenv("E2E_CITY", "重庆")
SHOT_DIR = os.getenv("E2E_SHOT", "")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print("  %-5s %-34s %s" % ("PASS" if ok else "FAIL", name, detail))


def login(page) -> None:
    page.goto(BASE, wait_until="domcontentloaded")
    time.sleep(2)
    page.fill("input[type=email]", USER)
    page.fill("input[type=password]", PWD)
    page.click("button:has-text('登录')")
    time.sleep(3.5)


def shot(page, name: str) -> None:
    if SHOT_DIR:
        page.screenshot(path=os.path.join(SHOT_DIR, name + ".png"))


# ── 地图：所有已铺瓦片的包围盒 vs 容器，覆盖率 1 表示铺满 ──
COVER_JS = """
() => {
  const c = document.querySelector('.map-canvas')
  if (!c) return null
  const cr = c.getBoundingClientRect()
  let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9, n = 0
  document.querySelectorAll('.leaflet-tile').forEach((t) => {
    const r = t.getBoundingClientRect()
    if (r.width <= 0) return
    x0 = Math.min(x0, r.left); y0 = Math.min(y0, r.top)
    x1 = Math.max(x1, r.right); y1 = Math.max(y1, r.bottom)
    n++
  })
  if (!n) return { cw: Math.round(cr.width), ch: Math.round(cr.height), n: 0, cover: 0 }
  const ix = Math.max(0, Math.min(x1, cr.right) - Math.max(x0, cr.left))
  const iy = Math.max(0, Math.min(y1, cr.bottom) - Math.max(y0, cr.top))
  return { cw: Math.round(cr.width), ch: Math.round(cr.height), n,
           cover: +(ix * iy / (cr.width * cr.height)).toFixed(3) }
}
"""


def map_cover(page) -> dict:
    return page.evaluate(COVER_JS)


def run(pw) -> None:
    browser = pw.chromium.launch(executable_path=CHROME, headless=True)
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    errs: list[str] = []
    page.on("pageerror", lambda e: errs.append("pageerror: " + str(e)[:160]))

    login(page)

    # ── 1. 规划页：多份切换条 ──
    print("=" * 72)
    print("1. 规划页同城多份的切换条")
    page.goto("%s/plans?adcode=%s&city=%s" % (BASE, ADCODE, CITY), wait_until="domcontentloaded")
    time.sleep(2.5)
    shot(page, "ui_plans")
    btns = page.query_selector_all("button.pick")
    if not btns:
        check("该城市存在多份规划", False, "picker 为空（先用对话保存两份再跑）")
    else:
        clipped = 0
        for b in btns:
            if b.evaluate("e => e.scrollWidth > e.clientWidth + 1"):
                clipped += 1
        check("切换按钮文字不被裁切", clipped == 0, "%d 个按钮，%d 个被裁" % (len(btns), clipped))
        scrollable = page.evaluate(
            "() => { const e = document.querySelector('.picker');"
            " return !!e && e.scrollWidth >= e.clientWidth }")
        check("切换条可横向滑动", scrollable, "")

    # ── 2. 地图 keep-alive ──
    print("=" * 72)
    print("2. 地图切 tab 回来后瓦片是否仍铺满（invalidateSize）")
    page.click("button:has-text('地图')")
    time.sleep(7)
    a = map_cover(page)
    shot(page, "ui_map_first")
    check("首次进入瓦片铺满", a["cover"] >= 0.9, "cover=%.3f (%dx%d)" % (a["cover"], a["cw"], a["ch"]))

    # 切走 → 改窗口 → 切回（必须走 in-app 导航，page.goto 会整页刷新，测不到 keep-alive）
    for tag, (w, h) in [("放大", (1600, 1000)), ("缩小", (900, 650))]:
        page.click("button:has-text('AI 助手')")
        time.sleep(2)
        page.set_viewport_size({"width": w, "height": h})
        page.click("button:has-text('地图')")
        time.sleep(7)
        s = map_cover(page)
        shot(page, "ui_map_%s" % tag)
        check("%s窗口后切回仍铺满" % tag, s["cover"] >= 0.9,
              "cover=%.3f (%dx%d)" % (s["cover"], s["cw"], s["ch"]))

    # ── 3. 卡片保存：新建 / 覆盖的反馈 ──
    print("=" * 72)
    print("3. 行程卡片保存的反馈文案")
    page.set_viewport_size({"width": 1280, "height": 800})
    page.click("button:has-text('AI 助手')")
    time.sleep(2)
    page.fill("input[placeholder*='输入消息']", "帮我规划%s两日游" % CITY)
    page.click("button.send")
    got = False
    for _ in range(90):
        time.sleep(1)
        if page.query_selector("button:has-text('保存到规划')"):
            got = True
            break
    check("排出行程卡片", got, "")
    if got:
        page.click("button:has-text('保存到规划')")
        time.sleep(1.5)
        page.click("button:has-text('新建一份')")
        time.sleep(1.2)
        b1 = page.query_selector("button.save-btn")
        t1 = (b1.inner_text() or "").strip() if b1 else "-"
        check("新建后提示『已新建一份』", "新建" in t1, t1)
        shot(page, "ui_card_new")

        time.sleep(4)
        page.click("button:has-text('保存到规划')")
        time.sleep(1.5)
        page.click("button:has-text('覆盖最新一份')")
        time.sleep(1.2)
        b2 = page.query_selector("button.save-btn")
        t2 = (b2.inner_text() or "").strip() if b2 else "-"
        check("覆盖后提示『已覆盖原规划』", "覆盖" in t2, t2)
        shot(page, "ui_card_overwrite")

    print("=" * 72)
    if errs:
        print("前端报错:")
        for e in errs[:10]:
            print("   ", e)
        check("无前端报错", False, "%d 条" % len(errs))
    else:
        check("无前端报错", True, "")

    browser.close()


def main() -> int:
    if not os.path.exists(CHROME):
        print("找不到 Chrome：%s（用 E2E_CHROME 指定）" % CHROME)
        return 2
    with sync_playwright() as pw:
        run(pw)
    print()
    ok = all(v for _, v, _ in results)
    print("总判定：%s（%d/%d）" % ("全部通过" if ok else "有失败项",
                                  sum(1 for _, v, _ in results if v), len(results)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
