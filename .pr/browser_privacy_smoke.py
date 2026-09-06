import base64
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from PIL import Image

from openhands.tools.browser_use.definition import (
    BrowserClickAction,
    BrowserGetStateAction,
    BrowserNavigateAction,
    BrowserTypeAction,
)
from openhands.tools.browser_use.impl import BrowserToolExecutor


EMAIL = "privacy-account@example.test"
PASSWORD = "sensitive-password-123"
HTML = b"""<title>Screenshot privacy proof</title>
<style>body {font: 20px sans-serif; margin: 40px} input,button {
 display:block;width:350px;height:40px;margin:20px 0} #account {
 display:block;position:absolute;left:40px;top:160px;width:350px;height:40px}
</style><h1>Native screenshot privacy</h1><input placeholder="Email">
<input placeholder="Password"><button onclick="document.body.innerHTML =
'<h1>Signed in successfully</h1><span id=account>privacy-account@example.test</span>'">
Sign in</button>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(HTML)

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = Thread(target=server.serve_forever, daemon=True)
thread.start()
executor = BrowserToolExecutor()
try:
    executor.set_sensitive_values([EMAIL])
    executor.set_sensitive_values([PASSWORD])
    result = executor(
        BrowserNavigateAction(url=f"http://127.0.0.1:{server.server_port}")
    )
    assert not result.is_error, result.text
    for index, value in enumerate([EMAIL, PASSWORD]):
        result = executor(BrowserTypeAction(index=index, text=value))
        assert not result.is_error, result.text
        assert value not in result.text
    form = executor(BrowserGetStateAction())
    assert isinstance(form.screenshot_data, str)
    form_bytes = base64.b64decode(form.screenshot_data)
    Path(".pr/browser-privacy-form.jpg").write_bytes(form_bytes)
    result = executor(BrowserClickAction(index=2))
    assert not result.is_error, result.text
    state = executor(BrowserGetStateAction())
    assert isinstance(state.screenshot_data, str)
    screenshot = base64.b64decode(state.screenshot_data)
    Path(".pr/browser-privacy-signed-in.jpg").write_bytes(screenshot)
    pixels = Image.open(io.BytesIO(screenshot)).convert("RGB")
    assert pixels.getpixel((100, 175)) == (0, 0, 0)
    assert pixels.getpixel((800, 175)) == (255, 255, 255)
    metadata = executor.browser_metadata()
    assert EMAIL not in str(metadata)
    assert PASSWORD not in str(metadata)
    assert "Signed in successfully" in metadata["text"]
    assert "<secret>" in metadata["text"]
    print(
        {
            "form_screenshot_bytes": len(form_bytes),
            "signed_in_screenshot_bytes": len(screenshot),
            "masked_account_pixel": pixels.getpixel((100, 175)),
            "metadata": metadata,
        }
    )
finally:
    executor.close()
    server.shutdown()
    server.server_close()
    thread.join()
