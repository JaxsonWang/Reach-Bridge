"""原生二维码会话：不启动浏览器，凭据只在服务端验证和保存。"""

import json
from dataclasses import replace
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from reach_bridge.admin import Manager
from reach_bridge.app import create_app
from reach_bridge.runner import BridgeError

from .conftest import TOKEN
from .test_admin import ADMIN, ORIGIN, login, wait_job

KEY = "a" * 32


@pytest.fixture
def qr_client(settings, tmp_path, monkeypatch):
    calls, outcomes = [], ["waiting_scan"]
    monkeypatch.setattr("reach_bridge.admin.QR_POLL_SECONDS", 0.03)

    async def cli(self, args, *, secret=None):
        calls.append(("cli", args))
        return {"added": True, "channel": "bilibili"}

    async def helper(self, operation, platform="-", data=None):
        calls.append((operation, platform, data))
        if operation == "bilibili_qr_start":
            return {"qrcode_key": KEY, "url": "https://account.bilibili.com/scan?qrcode_key=" + KEY}
        if operation == "bilibili_qr_poll":
            value = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
            if isinstance(value, Exception):
                raise value
            if isinstance(value, dict):
                return value
            return {
                "state": value,
                "saved": value == "authenticated",
                "verified": value == "authenticated",
            }
        if operation == "describe":
            return {"channels": {"bilibili": {"native_qr": True, "login": True}}}
        return {}

    monkeypatch.setattr(Manager, "cli", cli)
    monkeypatch.setattr(Manager, "helper", helper)
    config = replace(
        settings,
        admin_token=ADMIN,
        admin_origin=ORIGIN,
        allow_insecure_http=True,
        state_dir=tmp_path / "private",
        browser_enabled=False,
    )
    with TestClient(create_app(config)) as client:
        client.calls, client.outcomes = calls, outcomes
        login(client)
        yield client


def begin(client):
    response = client.post("/admin/api/qr-login", json={"platform": "bilibili"})
    assert response.status_code == 200
    return response.json()["id"]


def test_native_qr_success_svg_privacy_and_gate(qr_client):
    c = qr_client
    c.outcomes[:] = ["waiting_scan", "waiting_confirm", "authenticated"]
    identifier = begin(c)
    wait_job(c, identifier, "waiting_user")
    image = c.get(f"/admin/api/qr-login/{identifier}/image")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/svg+xml"
    assert image.headers["cache-control"] == "no-store"
    assert ElementTree.fromstring(image.content).tag.endswith("svg")
    jobs = c.get("/admin/api/jobs")
    assert KEY not in jobs.text and "private" not in jobs.text
    response = c.post(
        "/v1/search",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"platform": "bilibili", "query": "fixture"},
    )
    assert response.status_code == 503
    done = wait_job(c, identifier)
    assert done["result"] == {"saved": True, "verified": True}
    assert c.get(f"/admin/api/qr-login/{identifier}/image").status_code == 409
    assert c.calls[0] == ("cli", ["add", "bilibili"])
    assert not any("browser" in row[0] for row in c.calls)
    assert c.get("/admin/api/credentials").json()["capabilities"]["channels"]["bilibili"] == {
        "native_qr": True,
        "login": False,
        "cookie_import": True,
    }


def test_qr_cancel_clears_private_data_and_restores_queries(qr_client):
    c = qr_client
    identifier = begin(c)
    wait_job(c, identifier, "waiting_user")
    assert c.post(f"/admin/api/jobs/{identifier}/cancel", json={}).status_code == 200
    wait_job(c, identifier, "cancelled")
    assert "private" not in c.app.state.admin.jobs[identifier]
    response = c.post(
        "/v1/search",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"platform": "bilibili", "query": "fixture"},
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "outcome,code",
    [
        ("expired", "QR_EXPIRED"),
        (BridgeError("QR_FAILED", "请重新生成二维码"), "QR_FAILED"),
        ({"state": "authenticated", "saved": False, "verified": True}, "INVALID_UPSTREAM"),
        ("unknown", "INVALID_UPSTREAM"),
    ],
)
def test_qr_failures_are_terminal_without_poll_retry(qr_client, outcome, code):
    import time

    c = qr_client
    c.outcomes[:] = [outcome]
    identifier = begin(c)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = c.get("/admin/api/jobs").json()["jobs"][0]
        if job["state"] == "failed":
            break
        time.sleep(0.01)
    assert job["state"] == "failed" and job["code"] == code
    assert len([row for row in c.calls if row[0] == "bilibili_qr_poll"]) == 1
    assert KEY not in json.dumps(job)
    assert c.get(f"/admin/api/qr-login/{identifier}/image").status_code == 409


def test_qr_image_and_cancel_are_owner_scoped(qr_client):
    c = qr_client
    identifier = begin(c)
    wait_job(c, identifier, "waiting_user")
    first_cookie, first_headers = dict(c.cookies), dict(c.headers)
    login(c)
    assert c.get(f"/admin/api/qr-login/{identifier}/image").status_code == 409
    assert c.get("/admin/api/jobs").json()["jobs"] == []
    c.post(f"/admin/api/jobs/{identifier}/cancel", json={})
    assert c.app.state.admin.active["id"] == identifier
    c.cookies.clear()
    c.cookies.update(first_cookie)
    c.headers.update(first_headers)
    assert c.post("/admin/logout", json={}).status_code == 200
    assert c.app.state.admin.active is None


def test_qr_start_requires_csrf_and_rejects_other_platform(qr_client):
    c = qr_client
    assert c.post("/admin/api/qr-login", json={"platform": "youtube"}).status_code == 422
    c.headers.pop("X-CSRF-Token")
    assert c.post("/admin/api/qr-login", json={"platform": "bilibili"}).status_code == 403
    assert c.calls == []
