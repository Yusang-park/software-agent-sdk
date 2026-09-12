"""Tests for the KasmVNC desktop service."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from openhands.agent_server.desktop_service import (
    KASMVNC_PROXY_PASSWORD,
    KASMVNC_USERNAME,
    DesktopService,
    get_desktop_service,
)


def completed(returncode: int = 0, stderr: str = "") -> MagicMock:
    result = MagicMock()
    result.returncode = returncode
    result.stderr = stderr
    return result


class TestDesktopService:
    def test_initialization_uses_the_existing_desktop_port_contract(self):
        service = DesktopService()
        assert service.novnc_port == int(os.getenv("NOVNC_PORT", "8002"))

    def test_custom_desktop_port(self):
        with patch.dict(os.environ, {"NOVNC_PORT": "9999"}):
            assert DesktopService().novnc_port == 9999

    @pytest.mark.asyncio
    async def test_navigate_uses_shared_browser_executor(self):
        service = DesktopService()
        executor = MagicMock(return_value=MagicMock(is_error=False, text="Navigated"))

        with (
            patch.object(service, "start", AsyncMock(return_value=True)),
            patch(
                "openhands.agent_server.desktop_service.BrowserToolSet.get_or_create_shared_executor",
                return_value=executor,
            ),
        ):
            assert await service.navigate("https://www.google.com") is True

        assert executor.call_args.args[0].url == "https://www.google.com"

    @pytest.mark.asyncio
    async def test_start_returns_when_kasmvnc_is_already_running(self):
        service = DesktopService()
        with (
            patch.object(service, "is_running", return_value=True),
            patch("subprocess.run") as run_mock,
        ):
            assert await service.start() is True
        run_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_fails_when_desktop_directories_cannot_be_created(self):
        service = DesktopService()
        with (
            patch.object(service, "is_running", return_value=False),
            patch("pathlib.Path.mkdir", side_effect=OSError("denied")),
        ):
            assert await service.start() is False

    @pytest.mark.asyncio
    async def test_start_fails_when_xstartup_cannot_be_written(self):
        service = DesktopService()
        with (
            patch.object(service, "is_running", return_value=False),
            patch("pathlib.Path.mkdir"),
            patch("pathlib.Path.exists", return_value=False),
            patch("pathlib.Path.write_text", side_effect=OSError("denied")),
        ):
            assert await service.start() is False

    @pytest.mark.asyncio
    async def test_start_fails_when_kasmvnc_user_cannot_be_configured(self):
        service = DesktopService()
        with (
            patch.object(service, "is_running", return_value=False),
            patch("pathlib.Path.mkdir"),
            patch("pathlib.Path.exists", return_value=True),
            patch("subprocess.run", return_value=completed(1, "bad password")),
        ):
            assert await service.start() is False

    @pytest.mark.asyncio
    async def test_start_fails_when_kasmvnc_server_fails(self):
        service = DesktopService()
        with (
            patch.object(service, "is_running", return_value=False),
            patch("pathlib.Path.mkdir"),
            patch("pathlib.Path.exists", return_value=True),
            patch(
                "subprocess.run",
                side_effect=[completed(), completed(1, "server failed")],
            ),
        ):
            assert await service.start() is False

    @pytest.mark.asyncio
    async def test_start_uses_integrated_kasmvnc_web_server(self):
        service = DesktopService()
        with (
            patch.object(service, "is_running", side_effect=[False, True]),
            patch("pathlib.Path.mkdir"),
            patch("pathlib.Path.exists", return_value=True),
            patch("subprocess.run", side_effect=[completed(), completed()]) as run_mock,
            patch("asyncio.create_subprocess_exec") as create_process,
            patch("asyncio.sleep"),
        ):
            assert await service.start() is True

        password_call, launch_call = run_mock.call_args_list
        assert password_call.args[0][:4] == [
            "vncpasswd",
            "-u",
            KASMVNC_USERNAME,
            "-w",
        ]
        assert password_call.kwargs["input"] == (
            f"{KASMVNC_PROXY_PASSWORD}\n{KASMVNC_PROXY_PASSWORD}\n"
        )

        launch = launch_call.args[0]
        assert launch[0] == "vncserver"
        assert launch[launch.index("-websocketPort") + 1] == "8002"
        assert launch[launch.index("-sslOnly") + 1] == "0"
        assert launch[launch.index("-interface") + 1] == "0.0.0.0"
        assert launch[launch.index("-select-de") + 1] == "manual"
        assert "novnc_proxy" not in " ".join(launch)
        create_process.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_configures_plain_http_behind_the_app_server_proxy(self):
        service = DesktopService()

        def exists(path):
            return not str(path).endswith("kasmvnc.yaml")

        with (
            patch.object(service, "is_running", side_effect=[False, True]),
            patch("pathlib.Path.mkdir"),
            patch("pathlib.Path.exists", exists),
            patch("pathlib.Path.write_text", autospec=True) as write_text,
            patch("subprocess.run", side_effect=[completed(), completed()]),
            patch("asyncio.sleep"),
        ):
            assert await service.start() is True

        written = "\n".join(str(call.args) for call in write_text.call_args_list)
        assert "kasmvnc.yaml" in written
        assert "protocol: http" in written
        assert "require_ssl: false" in written
        assert "websocket_port: 8002" in written
        assert "width: 1280" in written
        assert "height: 800" in written

    @pytest.mark.asyncio
    async def test_start_offloads_blocking_process_calls(self):
        service = DesktopService()
        real_to_thread = asyncio.to_thread
        with (
            patch.object(service, "is_running", side_effect=[False, True]),
            patch("pathlib.Path.mkdir"),
            patch("pathlib.Path.exists", return_value=True),
            patch("subprocess.run", side_effect=[completed(), completed()]) as run_mock,
            patch("asyncio.sleep"),
            patch("asyncio.to_thread", wraps=real_to_thread) as to_thread_spy,
        ):
            assert await service.start() is True

        offloaded = [call.args[0] for call in to_thread_spy.call_args_list]
        assert run_mock in offloaded

    @pytest.mark.asyncio
    async def test_stop_is_a_noop_when_kasmvnc_is_not_running(self):
        service = DesktopService()
        with (
            patch.object(service, "is_running", return_value=False),
            patch("subprocess.run") as run_mock,
        ):
            await service.stop()
        run_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_kills_the_kasmvnc_display(self):
        service = DesktopService()
        with (
            patch.object(service, "is_running", return_value=True),
            patch("subprocess.run", return_value=completed()) as run_mock,
        ):
            await service.stop()
        assert run_mock.call_args.args[0] == ["vncserver", "-kill", ":1"]

    @pytest.mark.asyncio
    async def test_stop_tolerates_a_process_error(self):
        service = DesktopService()
        with (
            patch.object(service, "is_running", return_value=True),
            patch("subprocess.run", side_effect=OSError("failed")),
        ):
            await service.stop()

    @pytest.mark.parametrize(("returncode", "expected"), [(0, True), (1, False)])
    def test_running_check_targets_kasmvnc(self, returncode: int, expected: bool):
        service = DesktopService()
        with patch("subprocess.run", return_value=completed(returncode)) as run_mock:
            assert service.is_running() is expected
        assert run_mock.call_args.args[0] == ["pgrep", "-x", "Xvnc"]

    def test_running_check_tolerates_a_process_error(self):
        service = DesktopService()
        with patch("subprocess.run", side_effect=OSError("failed")):
            assert service.is_running() is False

    def test_viewer_url_autoconnects_to_the_kasmvnc_proxy(self):
        service = DesktopService()
        with patch.object(service, "is_running", return_value=True):
            url = service.get_vnc_url("https://pilot.test/desktop")

        parsed = urlsplit(url or "")
        query = parse_qs(parsed.query)
        assert parsed.path == "/desktop/vnc.html"
        assert query == {
            "autoconnect": ["1"],
            "resize": ["scale"],
            "reconnect": ["true"],
            "username": [KASMVNC_USERNAME],
            "password": [KASMVNC_PROXY_PASSWORD],
        }

    def test_viewer_url_is_absent_when_desktop_is_not_running(self):
        service = DesktopService()
        with patch.object(service, "is_running", return_value=False):
            assert service.get_vnc_url("https://pilot.test/desktop") is None

    def test_viewer_url_default_base_uses_the_desktop_port(self):
        service = DesktopService()
        with patch.object(service, "is_running", return_value=True):
            url = service.get_vnc_url()
        assert url is not None
        assert url.startswith("http://localhost:8002/")


class TestGetDesktopService:
    def setup_method(self):
        import openhands.agent_server.desktop_service

        openhands.agent_server.desktop_service._desktop_service = None

    def test_returns_service_when_enabled(self):
        config = MagicMock(enable_vnc=True)
        with patch(
            "openhands.agent_server.desktop_service.get_default_config",
            return_value=config,
        ):
            assert isinstance(get_desktop_service(), DesktopService)

    def test_returns_none_when_disabled(self):
        config = MagicMock(enable_vnc=False)
        with patch(
            "openhands.agent_server.desktop_service.get_default_config",
            return_value=config,
        ):
            assert get_desktop_service() is None

    def test_returns_process_singleton(self):
        config = MagicMock(enable_vnc=True)
        with patch(
            "openhands.agent_server.desktop_service.get_default_config",
            return_value=config,
        ):
            assert get_desktop_service() is get_desktop_service()
