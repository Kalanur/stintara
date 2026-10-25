import re
from contextlib import asynccontextmanager
from html import escape

import httpx
import pytest

from server.app import main
from server.app.build import build_info, inject_build_label
from server.app.uuid7 import uuid7

REVISION = "0123456789abcdef" * 2 + "01234567"


@pytest.mark.parametrize("value", [None, "", " \t"])
def test_local_build_fallback(monkeypatch, value):
    for key in ("STINTARA_BUILD_VERSION", "STINTARA_BUILD_REVISION"):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    assert build_info() == {"version": "0.1.0", "build_version": "dev", "revision": "unknown"}
    assert inject_build_label("__BUILD_LABEL__") == "Stintara dev · unknown"


@pytest.mark.parametrize("version", ["dev", "v0.1.0"])
async def test_health_preserves_database_check_and_full_build_identity(monkeypatch, version):
    calls = []

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield self

        async def fetchval(self, query):
            calls.append(query)
            return 1

    monkeypatch.setattr(main, "pool", Pool())
    monkeypatch.setenv("STINTARA_BUILD_VERSION", version)
    monkeypatch.setenv("STINTARA_BUILD_REVISION", REVISION)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://localhost") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0", "build_version": version, "revision": REVISION}
    assert calls == ["SELECT 1"]
    assert main.app.version == "0.1.0"


@pytest.mark.parametrize("revision,visible", [(REVISION, REVISION[:7]), ("unknown", "unknown")])
async def test_all_pages_show_static_build_footer(monkeypatch, revision, visible):
    monkeypatch.setenv("STINTARA_BUILD_VERSION", "dev")
    monkeypatch.setenv("STINTARA_BUILD_REVISION", revision)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://localhost") as client:
        for route in ("/", "/drivers/synthetic/live", f"/sessions/{uuid7()}/live"):
            response = await client.get(route)
            assert response.status_code == 200
            footer = re.search(r"<footer[^>]*>(.*?)</footer>", response.text).group(1)
            assert footer == f"Stintara dev · {visible}"
            assert "__BUILD_LABEL__" not in response.text
            assert response.text.index("</footer>") < response.text.index("<script>")
            assert "overflow-wrap:anywhere" in response.text


async def test_build_metadata_is_escaped_in_html_and_never_inserted_in_script(monkeypatch):
    version = '<img src=x onerror="alert(1)"> & __DRIVER_SLUG__'
    revision = "</footer><script>alert('x')</script>\\"
    monkeypatch.setenv("STINTARA_BUILD_VERSION", version)
    monkeypatch.setenv("STINTARA_BUILD_REVISION", revision)
    for response in (await main.frontpage(), await main.driver_live_page("__BUILD_LABEL__"),
                     await main.session_live_page(uuid7())):
        text = response.body.decode()
        footer = re.search(r"<footer[^>]*>(.*?)</footer>", text).group(1)
        assert footer == escape(f"Stintara {version} · {revision}")
        assert text.count("<script>") == 1
        assert "<img" not in text
        assert "alert(" not in text.split("<script>")[1]
    assert build_info()["revision"] == revision
