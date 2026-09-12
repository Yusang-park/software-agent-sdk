"""Desktop service backed by KasmVNC's integrated web viewer."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from urllib.parse import urlencode

from openhands.agent_server.config import get_default_config
from openhands.sdk.logger import get_logger
from openhands.sdk.utils import sanitized_env
from openhands.tools.browser_use.definition import (
    BrowserNavigateAction,
    BrowserToolSet,
)


logger = get_logger(__name__)

# This credential is not an authorization boundary. The desktop listener is
# reachable only through Pilot's authenticated, sandbox-and-service-scoped
# DESKTOP proxy, matching the former TigerVNC SecurityTypes=None deployment.
# KasmVNC nevertheless requires an HTTP Basic Auth user, so its bundled client
# receives this proxy-only credential in the iframe URL.
KASMVNC_USERNAME = "pilot"
KASMVNC_PROXY_PASSWORD = "pilot-kasmvnc-proxy"
KASMVNC_WEB_ROOT = "/usr/share/kasmvnc/www"


class DesktopService:
    """Launch and manage one KasmVNC desktop for the shared browser session."""

    def __init__(self):
        # Keep NOVNC_PORT as the external deployment contract. The value now
        # addresses KasmVNC's integrated HTTP/WebSocket server directly; there
        # is no separate noVNC or websockify process.
        self.novnc_port: int = int(os.getenv("NOVNC_PORT", "8002"))

    async def start(self) -> bool:
        """Start KasmVNC and its integrated browser client."""
        if await asyncio.to_thread(self.is_running):
            logger.info("KasmVNC desktop already running")
            return True

        env = sanitized_env()
        display = env.get("DISPLAY", ":1")
        user = env.get("USER") or env.get("USERNAME") or "openhands"
        home = Path(env.get("HOME") or f"/home/{user}")
        vnc_geometry = env.get("VNC_GEOMETRY", "1280x800")
        try:
            width_text, height_text = vnc_geometry.split("x", 1)
            desktop_width = int(width_text)
            desktop_height = int(height_text)
            if desktop_width <= 0 or desktop_height <= 0:
                raise ValueError
        except ValueError:
            logger.error("Invalid VNC_GEOMETRY: %s", vnc_geometry)
            return False

        try:
            for path in (home / ".vnc", home / ".config", home / "Downloads"):
                path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.error("Failed preparing KasmVNC directories: %s", exc)
            return False

        password_file = home / ".kasmpasswd"
        kasmvnc_config = home / ".vnc" / "kasmvnc.yaml"
        if not kasmvnc_config.exists():
            try:
                kasmvnc_config.write_text(
                    "desktop:\n"
                    "  resolution:\n"
                    f"    width: {desktop_width}\n"
                    f"    height: {desktop_height}\n"
                    "  allow_resize: false\n"
                    "network:\n"
                    "  protocol: http\n"
                    "  interface: 0.0.0.0\n"
                    f"  websocket_port: {self.novnc_port}\n"
                    "  ssl:\n"
                    "    require_ssl: false\n"
                    "user_session:\n"
                    "  concurrent_connections_prompt: false\n"
                    "encoding:\n"
                    "  max_frame_rate: 30\n"
                    "server:\n"
                    "  http:\n"
                    f"    httpd_directory: {KASMVNC_WEB_ROOT}\n"
                    "  advanced:\n"
                    f"    kasm_password_file: {password_file}\n"
                    "command_line:\n"
                    "  prompt: false\n"
                )
            except Exception as exc:
                logger.error("Failed writing KasmVNC configuration: %s", exc)
                return False

        xstartup = home / ".vnc" / "xstartup"
        if not xstartup.exists():
            try:
                xstartup.write_text(
                    "#!/bin/sh\n"
                    "unset SESSION_MANAGER\n"
                    "unset DBUS_SESSION_BUS_ADDRESS\n"
                    "exec startxfce4\n"
                )
                xstartup.chmod(0o755)
            except Exception as exc:
                logger.error("Failed writing KasmVNC xstartup: %s", exc)
                return False

        try:
            password_result = await asyncio.to_thread(
                subprocess.run,
                [
                    "vncpasswd",
                    "-u",
                    KASMVNC_USERNAME,
                    "-w",
                    str(password_file),
                ],
                input=f"{KASMVNC_PROXY_PASSWORD}\n{KASMVNC_PROXY_PASSWORD}\n",
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
            )
        except Exception as exc:
            logger.error("Failed configuring the KasmVNC proxy user: %s", exc)
            return False
        if password_result.returncode != 0:
            logger.error(
                "vncpasswd failed with rc=%s: %s",
                password_result.returncode,
                password_result.stderr.strip(),
            )
            return False

        logger.info(
            "Starting KasmVNC on %s (%s), web port %d",
            display,
            vnc_geometry,
            self.novnc_port,
        )
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    "vncserver",
                    display,
                    "-geometry",
                    vnc_geometry,
                    "-depth",
                    "24",
                    "-select-de",
                    "manual",
                    "-interface",
                    "0.0.0.0",
                    "-websocketPort",
                    str(self.novnc_port),
                    # TLS terminates at Pilot's App Server. Keeping the private
                    # hop as HTTP lets the existing DESKTOP proxy reach it.
                    "-sslOnly",
                    "0",
                    "-httpd",
                    KASMVNC_WEB_ROOT,
                    "-KasmPasswordFile",
                    str(password_file),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
        except Exception as exc:
            logger.error("Failed starting KasmVNC: %s", exc)
            return False
        if result.returncode != 0:
            logger.error(
                "vncserver failed with rc=%s: %s",
                result.returncode,
                result.stderr.strip(),
            )
            return False

        # KasmVNC starts Xkasmvnc in the background. Give the integrated web
        # listener a short grace period, then use the process check as the
        # readiness verdict just as the prior desktop service did.
        await asyncio.sleep(2)
        if await asyncio.to_thread(self.is_running):
            logger.info("KasmVNC desktop started successfully")
            return True

        logger.error("KasmVNC failed to become healthy")
        return False

    async def stop(self) -> None:
        """Stop the KasmVNC display if it is running."""
        display = os.getenv("DISPLAY", ":1")
        if not await asyncio.to_thread(self.is_running):
            return
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["vncserver", "-kill", display],
                capture_output=True,
                text=True,
                timeout=10,
                env=sanitized_env(),
            )
            if result.returncode != 0:
                logger.warning("KasmVNC stop returned rc=%s", result.returncode)
        except Exception as exc:
            logger.warning("Failed stopping KasmVNC: %s", exc)

    async def navigate(self, url: str) -> bool:
        """Open a URL in the shared headed browser used by agent browser tools."""
        if not await self.start():
            return False
        executor = BrowserToolSet.get_or_create_shared_executor()
        observation = await asyncio.to_thread(
            executor,
            BrowserNavigateAction(url=url),
        )
        if observation.is_error:
            logger.error("Browser navigation failed: %s", observation.text)
            return False
        return True

    def is_running(self) -> bool:
        """Whether the KasmVNC X server is alive."""
        try:
            result = subprocess.run(
                # The Debian package exposes Xkasmvnc through the Xvnc
                # alternatives path, and that is the process name Linux keeps.
                ["pgrep", "-x", "Xvnc"],
                capture_output=True,
                text=True,
                timeout=3,
                env=sanitized_env(),
            )
            return result.returncode == 0
        except Exception:
            return False

    def get_vnc_url(self, base: str = "http://localhost:8002") -> str | None:
        """Return the KasmVNC viewer URL once the desktop is running."""
        if not self.is_running():
            return None
        query = urlencode(
            {
                "autoconnect": "1",
                "resize": "scale",
                "reconnect": "true",
                "username": KASMVNC_USERNAME,
                "password": KASMVNC_PROXY_PASSWORD,
            }
        )
        return f"{base.rstrip('/')}/vnc.html?{query}"


_desktop_service: DesktopService | None = None


def get_desktop_service() -> DesktopService | None:
    """Get the process-wide desktop service when VNC support is enabled."""
    global _desktop_service
    config = get_default_config()

    if not config.enable_vnc:
        logger.info("KasmVNC desktop is disabled in configuration")
        return None

    if _desktop_service is None:
        _desktop_service = DesktopService()
    return _desktop_service
