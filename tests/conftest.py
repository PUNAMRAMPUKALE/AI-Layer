import socket
import threading
import time
from pathlib import Path

import pytest
import requests
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def mock_app_url():
    from mock_app.app import app
    from mock_app import data

    port = _free_port()
    thread = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
        daemon=True,
    )
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            requests.get(base_url + "/", timeout=0.5)
            break
        except requests.ConnectionError:
            time.sleep(0.1)

    data.reset_state()
    yield base_url


@pytest.fixture()
def reset_mock_app(mock_app_url):
    requests.post(mock_app_url + "/__reset", timeout=5)
    yield


@pytest.fixture()
def browser():
    # Function-scoped (not session-scoped) deliberately: agent/loop.py and
    # replay/engine.py each open and fully close their own `with
    # sync_playwright()` per run, matching real usage. A Playwright sync
    # context left open for the whole test session conflicts with those
    # nested invocations in the same thread.
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture()
def page(browser):
    p = browser.new_page()
    yield p
    p.close()
