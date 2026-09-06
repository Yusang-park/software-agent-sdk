import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlparse

from openhands.tools.browser_use.definition import (
    BrowserGetStateAction,
    BrowserNavigateAction,
)
from openhands.tools.browser_use.impl import BrowserToolExecutor


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<title>SDK boundary proof</title><h1>Native screenshot evidence</h1>"
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = Thread(target=server.serve_forever, daemon=True)
thread.start()
seen = []


async def policy(url):
    seen.append(url)
    if urlparse(url).hostname != "127.0.0.1":
        raise ValueError("Outside test origin")


executor = BrowserToolExecutor(navigation_policy=policy)
try:
    result = executor(
        BrowserNavigateAction(url=f"http://127.0.0.1:{server.server_port}")
    )
    assert not result.is_error, result.text
    state = executor(BrowserGetStateAction())
    assert not state.is_error, state.text
    assert isinstance(state.screenshot_data, str)
    screenshot = base64.b64decode(state.screenshot_data)
    Path(".pr/browser-boundary.jpg").write_bytes(screenshot)
    metadata = executor.browser_metadata()
    assert metadata["title"] == "SDK boundary proof"
    assert metadata["text"] == "Native screenshot evidence"
    denied = executor(BrowserNavigateAction(url="data:text/html,blocked"))
    assert denied.is_error
    print(
        {
            "native_screenshot_bytes": len(screenshot),
            "metadata": metadata,
            "policy_calls": len(seen),
            "denied_non_network_url": denied.is_error,
        }
    )
finally:
    executor.close()
    server.shutdown()
    server.server_close()
    thread.join()
