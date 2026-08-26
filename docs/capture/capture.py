#!/usr/bin/env python3
"""Shoots the README screenshots against the seeded demo instance.

Start `python3 docs/capture/serve_demo.py` first. Everything photographed is
invented seed data served by the `dev` provider -- no real number or message.

    python3 docs/capture/capture.py [base-url]   # default http://127.0.0.1:8323

Writes docs/shots/*.png.
"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8323").rstrip("/")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shots")
os.makedirs(OUT, exist_ok=True)
DEMO_KEY = "demo-key"


def settle(page, ms=2200):
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(ms)
    page.mouse.move(2, 2)


with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1280, "height": 900}, device_scale_factor=2)
    # The viewer keeps its API key in localStorage rather than baking one in.
    ctx.add_init_script(f"localStorage.setItem('sms-relay-key', {DEMO_KEY!r})")
    page = ctx.new_page()
    page.goto(BASE + "/", wait_until="load")
    try:                                  # whatever the storage key is called,
        page.fill("#key", DEMO_KEY)       # filling the field is what loads rows
        page.dispatch_event("#key", "change")
    except Exception:
        pass
    settle(page, 2600)
    page.screenshot(path=os.path.join(OUT, "log.png"))
    print("log        message log viewer")

    page.goto(BASE + "/docs", wait_until="load")
    settle(page, 2600)
    page.screenshot(path=os.path.join(OUT, "api.png"))
    print("api        OpenAPI docs")
    b.close()
