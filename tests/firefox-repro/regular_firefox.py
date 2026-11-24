"""Issue #2: does a REGULAR Firefox (not Playwright's patched build) load the
dashboard reliably with the pattern that stalls under Playwright - a new tab,
navigated immediately, repeatedly, across all views?

Driven through WebDriver (geckodriver) with Selenium. Investigation tool; it
prints a summary and exits non-zero only if a view genuinely failed to load.

    pip install selenium
    python3 tests/firefox-repro/regular_firefox.py [rounds]
"""

import os
import subprocess
import sys
import tempfile
import time
import urllib.request

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.ui import WebDriverWait

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PORT = 18960
BASE = "http://127.0.0.1:%d" % PORT
VIEWS = ["overview", "devices", "aria2", "history", "health", "settings"]


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    data = tempfile.mkdtemp(prefix="bw-ff-")
    demo = subprocess.Popen([sys.executable, "-m", "bandwatch", "demo", "--port", str(PORT), "--data-dir", data, "--days", "20"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(BASE + "/api/session", timeout=2)
                break
            except OSError:
                time.sleep(0.5)
        opts = Options()
        opts.add_argument("-headless")
        drv = webdriver.Firefox(options=opts)
        print("Firefox", drv.capabilities.get("browserVersion"), "(regular build via geckodriver)")
        times, failures = [], []
        for r in range(rounds):
            for v in VIEWS:
                drv.switch_to.new_window("tab")  # new tab, navigated at once
                t0 = time.time()
                try:
                    drv.get("%s/#/%s" % (BASE, v))
                    WebDriverWait(drv, 30).until(ec.presence_of_element_located((By.CSS_SELECTOR, "#main .card")))
                    times.append(time.time() - t0)
                except Exception as exc:  # investigation summary: record any failure
                    state = drv.execute_script("return [document.readyState, location.href, document.querySelectorAll('#main .card').length]")
                    failures.append((r, v, type(exc).__name__, state))
                # keep a few tabs alive (like kept-open contexts), close the rest
                if len(drv.window_handles) > 4:
                    drv.switch_to.window(drv.window_handles[0])
                    drv.close()
                    drv.switch_to.window(drv.window_handles[-1])
        drv.quit()
        times.sort()
        print("loads: %d ok, %d failed; median %.0f ms, max %.0f ms" % (len(times), len(failures), 1000 * times[len(times) // 2], 1000 * times[-1]) if times else "no successful loads")
        for f in failures[:10]:
            print("FAILED:", f)
        return 1 if failures else 0
    finally:
        demo.terminate()


if __name__ == "__main__":
    sys.exit(main())
