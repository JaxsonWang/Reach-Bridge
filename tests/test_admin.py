"""管理认证、任务独占与动态只读接口的回归检查。"""

import asyncio
import json
import sys
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from reach_bridge.admin import Manager
from reach_bridge.app import create_app
from reach_bridge.catalog import AccessGate
from reach_bridge.runner import BridgeError, Runner

from .conftest import TOKEN

ADMIN = "test-admin-placeholder-with-more-than-32-characters"
ORIGIN = "http://testserver"


@pytest.fixture
def admin_client(settings, tmp_path, monkeypatch):
    calls = []

    async def helper(self, operation, platform="-", data=None):
        calls.append((operation, platform, data))
        if operation == "describe":
            return {"channels": {"bilibili": {"login": True, "cookie_import": True}}}
        if operation == "credentials_status":
            return {"keys": {"github-token": False}, "channels": {}}
        if operation == "browser_login":
            return {"target_id": "private-target", "url": "https://www.bilibili.com"}
        if operation == "browser_frame":
            return {"image": "fixture-png", "url": "https://www.bilibili.com"}
        return {"saved": True}

    async def cli(self, args, *, secret=None):
        calls.append(("cli", args, secret))
        return {"saved": True}

    monkeypatch.setattr(Manager, "helper", helper)
    monkeypatch.setattr(Manager, "cli", cli)
    config = replace(
        settings,
        admin_token=ADMIN,
        admin_origin=ORIGIN,
        allow_insecure_http=True,
        state_dir=tmp_path / "private",
    )
    app = create_app(config)
    with TestClient(app) as client:
        client.calls = calls
        client.app_instance = app
        yield client


def login(client):
    response = client.post("/admin/login", headers={"Origin": ORIGIN}, json={"token": ADMIN})
    assert response.status_code == 200
    client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf"]})
    return response


def wait_job(client, identifier, state="succeeded"):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = next(j for j in client.get("/admin/api/jobs").json()["jobs"] if j["id"] == identifier)
        if job["state"] == state:
            return job
        if job["state"] == "failed":
            pytest.fail(str(job))
        time.sleep(0.01)
    pytest.fail("管理任务未达到预期状态")


def test_admin_disabled_keeps_query_contract(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/admin/bootstrap").json()["enabled"] is False
        assert (
            client.post(
                "/admin/login", json={"token": ADMIN}, headers={"Origin": ORIGIN}
            ).status_code
            == 503
        )
        assert (
            client.get("/v1/channels", headers={"Authorization": f"Bearer {TOKEN}"}).status_code
            == 200
        )


def test_admin_auth_roles_origin_csrf_and_logout(admin_client):
    c = admin_client
    assert (
        c.get("/admin/api/credentials", headers={"Authorization": f"Bearer {TOKEN}"}).status_code
        == 401
    )
    assert (
        c.post("/admin/login", json={"token": TOKEN}, headers={"Origin": ORIGIN}).status_code == 401
    )
    assert (
        c.post(
            "/admin/login", json={"token": ADMIN}, headers={"Origin": "http://evil.invalid"}
        ).status_code
        == 403
    )
    response = login(c)
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert c.get("/admin/api/session").status_code == 200
    assert c.get("/v1/channels").status_code == 401
    headers = dict(c.headers)
    c.headers.pop("X-CSRF-Token")
    assert c.post("/admin/api/status", json={}).status_code == 403
    c.headers.update(headers)
    assert (
        c.post("/admin/api/status", json={}, headers={"Origin": "http://evil.invalid"}).status_code
        == 403
    )
    assert c.post("/admin/logout", json={}).status_code == 200
    assert c.get("/admin/api/session").status_code == 401


def test_admin_rate_limit_and_no_input_echo(admin_client):
    for _ in range(5):
        response = admin_client.post(
            "/admin/login", headers={"Origin": ORIGIN}, json={"token": "private-wrong"}
        )
        assert response.status_code == 401
        assert "private-wrong" not in response.text
    assert (
        admin_client.post(
            "/admin/login", headers={"Origin": ORIGIN}, json={"token": ADMIN}
        ).status_code
        == 429
    )


def test_policy_is_persistent_and_immediate(admin_client):
    c = admin_client
    login(c)
    response = c.put(
        "/admin/api/channels/bilibili",
        json={"hosts": ["www.bilibili.com"], "enabled": False, "actions": ["read"]},
    )
    assert response.status_code == 200
    assert (
        c.get("/v1/channels", headers={"Authorization": f"Bearer {TOKEN}"}).json()["channels"] == []
    )
    assert (
        c.post(
            "/v1/search",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"platform": "bilibili", "query": "hello"},
        ).status_code
        == 403
    )
    path = c.app_instance.state.admin.catalog.policy_file
    assert json.loads(path.read_text())["bilibili"]["enabled"] is False
    assert path.stat().st_mode & 0o777 == 0o600
    assert (
        c.put(
            "/admin/api/channels/bilibili",
            json={"hosts": ["www.bilibili.com"], "actions": ["execute"]},
        ).status_code
        == 422
    )


def test_secrets_only_reach_mutation_and_never_job_history(admin_client):
    c = admin_client
    login(c)
    job = c.post(
        "/admin/api/credentials", json={"key": "github-token", "value": "private-value"}
    ).json()
    result = wait_job(c, job["id"])
    assert result["state"] == "succeeded"
    assert ("cli", ["configure", "github-token", "--stdin"], "private-value") in c.calls
    assert "private-value" not in c.get("/admin/api/jobs").text
    assert (
        c.post(
            "/admin/api/credentials", json={"key": "browser-port", "value": "private-value"}
        ).status_code
        == 422
    )
    assert (
        c.post(
            "/admin/api/cookies", json={"platform": "bilibili", "cookies": "[invalid-private"}
        ).status_code
        == 422
    )
    assert c.post("/admin/api/cookies", content=b"x" * 131073).status_code == 413


def test_login_preparation_order_exclusive_owner_and_cancel(admin_client):
    c = admin_client
    login(c)
    job = c.post("/admin/api/login", json={"platform": "bilibili"}).json()
    state = wait_job(c, job["id"], "waiting_user")
    assert "target_id" not in state and "owner" not in state and "task" not in state
    assert c.calls[:3] == [
        ("browser_prepare", "bilibili", None),
        ("cli", ["add", "bilibili"], None),
        ("browser_login", "bilibili", None),
    ]
    assert (
        c.post(
            "/v1/search",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"platform": "bilibili", "query": "hello"},
        ).status_code
        == 503
    )
    assert c.post("/admin/api/status", json={}).status_code == 409
    assert c.get("/admin/api/login/frame").json()["image"] == "fixture-png"
    assert c.post("/admin/api/jobs/" + job["id"] + "/cancel", json={}).status_code == 200
    wait_job(c, job["id"], "cancelled")
    assert (
        c.post(
            "/v1/search",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"platform": "bilibili", "query": "hello"},
        ).status_code
        == 200
    )


def test_browser_finish_and_logout_cancels_active_job(admin_client):
    c = admin_client
    login(c)
    job = c.post("/admin/api/login", json={"platform": "bilibili"}).json()
    wait_job(c, job["id"], "waiting_user")
    assert c.post("/admin/api/login/finish", json={}).status_code == 200
    wait_job(c, job["id"])
    assert c.calls[-1] == ("browser_finish", "bilibili", {"target_id": "private-target"})
    job = c.post("/admin/api/login", json={"platform": "bilibili"}).json()
    wait_job(c, job["id"], "waiting_user")
    assert c.post("/admin/logout", json={}).status_code == 200
    assert c.app_instance.state.admin.active is None


def test_dynamic_action_validation_and_argv(admin_client):
    c = admin_client
    headers = {"Authorization": f"Bearer {TOKEN}"}
    query = "--foo=$(echo not-a-command)"
    response = c.post(
        "/v1/actions/bilibili/search", headers=headers, json={"params": {"query": query}}
    )
    assert response.status_code == 200
    assert response.json()["meta"]["argv"][-2:] == ["--", query]
    for params in ({}, {"query": "hello", "execute": True}, {"query": "\nprivate"}):
        assert (
            c.post(
                "/v1/actions/bilibili/search", headers=headers, json={"params": params}
            ).status_code
            == 422
        )
    assert (
        c.post(
            "/v1/actions/bilibili/read",
            headers=headers,
            json={"params": {"target": "https://127.0.0.1/"}},
        ).status_code
        == 422
    )
    assert (
        c.post("/v1/actions/bilibili/execute", headers=headers, json={"params": {}}).status_code
        == 403
    )


@pytest.mark.asyncio
async def test_gate_waits_for_queries_and_releases_on_cancel():
    gate = AccessGate()
    async with gate.query():

        async def manage():
            async with gate.manage():
                pytest.fail("查询完成前不能开始写入")

        task = asyncio.create_task(manage())
        await asyncio.sleep(0)
        with pytest.raises(BridgeError, match="管理员"):
            async with gate.query():
                pass
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not gate.writer
    async with gate.manage():
        assert gate.readers == 0


@pytest.mark.asyncio
async def test_runner_stdin_and_management_secrets_removed(settings, tmp_path, monkeypatch):
    script = tmp_path / "stdin.py"
    script.write_text(
        "import sys,json,os; value=sys.stdin.read(); "
        'print(json.dumps({"schema_version":1,"ok":True,"result":{"received":value,'
        '"leaked":[k for k in os.environ if k.startswith("REACH_BRIDGE_")]}}))'
    )
    monkeypatch.setenv("REACH_BRIDGE_ADMIN_TOKEN", ADMIN)
    runner = Runner(settings)
    try:
        result = await runner.run(
            [str(script)],
            executable=sys.executable,
            input_data=b"private-stdin",
            execution_timeout=30,
        )
        assert result["result"] == {"received": "private-stdin", "leaked": []}
    finally:
        await runner.close()


def test_admin_settings_require_explicit_security_choice(settings):
    with pytest.raises(ValueError, match="不同"):
        replace(settings, admin_token=TOKEN, admin_origin="https://reach.example.com")
    with pytest.raises(ValueError, match="HTTPS"):
        replace(settings, admin_token=ADMIN, admin_origin=ORIGIN)
    with pytest.raises(ValueError, match="ORIGIN"):
        replace(settings, admin_token=ADMIN, admin_origin="https://reach.example.com/path")


@pytest.mark.asyncio
async def test_cancel_before_job_coroutine_starts(settings):
    from reach_bridge.catalog import Catalog

    manager = Manager(settings, Runner(settings), Catalog(settings), AccessGate())

    async def operation(job):
        pytest.fail("已取消的任务不应开始执行")

    job = manager.start("owner", "status", "-", operation)
    await manager.cancel_owner("owner")
    assert manager.active is None
    assert manager.jobs[job["id"]]["state"] == "cancelled"
    await manager.close()
    await manager.runner.close()


@pytest.mark.asyncio
async def test_login_cancellation_drains_active_browser_input(settings, monkeypatch):
    from reach_bridge.catalog import Catalog

    async def helper(self, operation, platform="-", data=None):
        return {"target_id": "test-target"}

    async def cli(self, args, *, secret=None):
        return {}

    monkeypatch.setattr(Manager, "helper", helper)
    monkeypatch.setattr(Manager, "cli", cli)
    gate = AccessGate()
    manager = Manager(settings, Runner(settings), Catalog(settings), gate)
    manager.start("owner", "login", "bilibili", manager.login)
    await asyncio.sleep(0)
    assert manager.active["state"] == "waiting_user"
    async with manager.browser_lock:
        cancellation = asyncio.create_task(manager.cancel_owner("owner"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert gate.writer is True
        assert not cancellation.done()
        with pytest.raises(BridgeError):
            manager.login_job("owner")
    await cancellation
    assert not gate.writer and manager.active is None
    await manager.close()
    await manager.runner.close()
