"""原生 Cookie 模式回归：保留默认合同，禁用浏览器时不执行浏览器 CLI。"""

import json
from contextlib import contextmanager
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from reach_bridge.admin import Manager
from reach_bridge.app import create_app
from reach_bridge.catalog import DEFAULT_HOSTS, Catalog
from reach_bridge.config import Settings
from reach_bridge.runner import Runner

from .conftest import TOKEN
from .test_admin import ADMIN, ORIGIN, login, wait_job


def action(name, parameter=None, *, backends=None):
    params = []
    if parameter:
        params.append({"name": parameter, "required": True, "type": "str"})
    if name in {"search", "hot", "hot_posts", "hot_stocks"}:
        params.append({"name": "limit", "required": False, "type": "int", "default": 5})
    return {"name": name, "params": params, "backends": backends or ["fixture-native"]}


def channel_metadata():
    # 夹具声明所有待验渠道；不依赖默认 conftest 中只有 Bili 的目录。
    actions = {
        "bilibili": [
            action("search", "query"),
            action("read", "target"),
            action("hot"),
            action("subtitle", "target"),
        ],
        "github": [action("search", "query"), action("read", "target")],
        "youtube": [
            action("search", "query"),
            action("read", "target"),
            action("subtitle", "target"),
            action("transcribe", "target"),
        ],
        "v2ex": [action("hot"), action("read", "target")],
        "web": [action("search", "query"), action("read", "target")],
        "xiaoyuzhou": [action("transcribe", "target")],
        "xueqiu": [
            action("quote", "symbol", backends=["tencent", "xueqiu-api"]),
            action("search", "query", backends=["tencent", "xueqiu-api"]),
            action("hot_posts", backends=["xueqiu-api"]),
            action("hot_stocks", backends=["xueqiu-api"]),
        ],
    }
    for name in ("xiaohongshu", "twitter", "reddit", "instagram", "facebook", "linkedin", "boss"):
        actions[name] = [
            action("search", "query", backends=["opencli"]),
            action("read", "target", backends=["opencli"]),
        ]
    return [
        {"name": name, "description": "原生模式测试渠道", "actions": rows}
        for name, rows in actions.items()
    ]


@pytest.fixture
def clients(settings, tmp_path, monkeypatch):
    calls = []
    original_load = Catalog.load
    original_run = Runner.run
    policies_file = tmp_path / "all-channels.json"
    policies_file.write_text(
        json.dumps(
            {
                row["name"]: {
                    "hosts": DEFAULT_HOSTS[row["name"]],
                    "enabled": True,
                    "actions": [item["name"] for item in row["actions"]],
                }
                for row in channel_metadata()
            }
        ),
        encoding="utf-8",
    )

    def load(self, version, rows):
        original_load(self, version, channel_metadata())

    async def run(self, args, **kwargs):
        calls.append(("query_cli", list(args)))
        return await original_run(self, args, **kwargs)

    async def helper(self, operation, platform="-", data=None):
        calls.append(("helper", operation, platform, data))
        if operation == "describe":
            return {
                "channels": {
                    row["name"]: {
                        "login": True,
                        "cookie_import": True,
                        "cookie_delete": row["name"] == "youtube",
                        "credential_keys": ["github-token"] if row["name"] == "github" else [],
                    }
                    for row in channel_metadata()
                },
                "credential_keys": ["github-token", "groq-key", "openai-key"],
            }
        if operation == "credentials_status":
            return {
                "keys": {"github-token": False, "groq-key": False, "openai-key": False},
                "channels": {},
            }
        if operation == "browser_login":
            return {"target_id": "fixture-page", "url": "https://www.bilibili.com"}
        return {"saved": True}

    async def cli(self, args, *, secret=None):
        calls.append(("admin_cli", list(args), secret))
        return {"ok": True, "saved": True}

    monkeypatch.setattr(Catalog, "load", load)
    monkeypatch.setattr(Runner, "run", run)
    monkeypatch.setattr(Manager, "helper", helper)
    monkeypatch.setattr(Manager, "cli", cli)

    @contextmanager
    def create(enabled, *, admin=False):
        configured = replace(settings, channels_file=policies_file, browser_enabled=enabled)
        if admin:
            configured = replace(
                configured,
                admin_token=ADMIN,
                admin_origin=ORIGIN,
                allow_insecure_http=True,
                state_dir=tmp_path / "admin-state",
            )
        with TestClient(create_app(configured)) as client:
            calls.clear()
            client.recorded_calls = calls
            if admin:
                login(client)
            yield client

    return create


def test_browser_setting_defaults_to_original_enabled_contract(settings, monkeypatch):
    assert settings.browser_enabled is True
    monkeypatch.delenv("REACH_BRIDGE_BROWSER_ENABLED", raising=False)
    assert Settings.from_env().browser_enabled is True
    monkeypatch.setenv("REACH_BRIDGE_BROWSER_ENABLED", "false")
    assert Settings.from_env().browser_enabled is False


def test_default_mode_still_executes_configured_browser_action(clients):
    with clients(True) as client:
        response = client.post(
            "/v1/actions/xiaohongshu/search",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"params": {"query": "fixture"}},
        )
        assert response.status_code == 200
        assert any(
            row[:1] == ("query_cli",) and row[1][:2] == ["xiaohongshu", "search"]
            for row in client.recorded_calls
        )


@pytest.mark.parametrize(
    "platform,name,params",
    [
        ("bilibili", "search", {"query": "fixture"}),
        ("bilibili", "subtitle", {"target": "BV1GJ411x7h7"}),
        ("github", "read", {"target": "owner/repo"}),
        ("youtube", "read", {"target": "https://www.youtube.com/watch?v=jNQXAC9IVRw"}),
        ("youtube", "transcribe", {"target": "https://www.youtube.com/watch?v=jNQXAC9IVRw"}),
        ("v2ex", "hot", {"limit": 2}),
        ("web", "search", {"query": "fixture"}),
        ("web", "read", {"target": "https://github.com/owner/repo"}),
        ("xiaoyuzhou", "transcribe", {"target": "https://www.xiaoyuzhoufm.com/episode/fixture"}),
        ("xueqiu", "quote", {"symbol": "SH600519"}),
        ("xueqiu", "search", {"query": "fixture"}),
    ],
)
def test_native_mode_keeps_authorized_native_queries(clients, platform, name, params):
    with clients(False) as client:
        response = client.post(
            f"/v1/actions/{platform}/{name}",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"params": params},
        )
        assert response.status_code == 200, response.text
        assert any(
            row[0] == "query_cli" and row[1][:2] == [platform, name]
            for row in client.recorded_calls
        )


@pytest.mark.parametrize(
    "platform,name,params",
    [
        ("xiaohongshu", "search", {"query": "fixture"}),
        ("twitter", "read", {"target": "https://x.com/fixture/status/123"}),
        ("reddit", "search", {"query": "fixture"}),
        ("instagram", "search", {"query": "fixture"}),
        ("facebook", "search", {"query": "fixture"}),
        ("linkedin", "search", {"query": "fixture"}),
        ("boss", "search", {"query": "fixture"}),
        ("xueqiu", "hot_posts", {"limit": 2}),
        ("xueqiu", "hot_stocks", {"limit": 2}),
    ],
)
def test_existing_enabled_policy_cannot_bypass_browser_disabled(clients, platform, name, params):
    with clients(False) as client:
        response = client.post(
            f"/v1/actions/{platform}/{name}",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"params": params},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "BROWSER_DISABLED"
        assert client.recorded_calls == []


def test_legacy_search_and_read_also_reject_browser_actions(clients):
    with clients(False) as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        search = client.post(
            "/v1/search", headers=headers, json={"platform": "xiaohongshu", "query": "fixture"}
        )
        read = client.post(
            "/v1/read",
            headers=headers,
            json={"platform": "twitter", "url": "https://x.com/fixture/status/123"},
        )
        assert search.status_code == read.status_code == 503
        assert search.json()["error"]["code"] == read.json()["error"]["code"] == "BROWSER_DISABLED"
        assert not client.recorded_calls


def test_native_credentials_capabilities_are_truthful(clients):
    with clients(False, admin=True) as client:
        response = client.get("/admin/api/credentials")
        assert response.status_code == 200
        body = response.json()
        assert body["capabilities"]["browser_enabled"] is False
        channels = body["capabilities"]["channels"]
        assert channels["bilibili"]["cookie_import"] is True
        assert channels["youtube"]["cookie_import"] is True
        assert all(row["login"] is False for row in channels.values())
        assert channels["xiaohongshu"]["cookie_import"] is False
        assert channels["twitter"]["cookie_import"] is False


@pytest.mark.parametrize("platform", ["bilibili", "youtube"])
def test_manual_native_cookies_remain_available(clients, platform):
    with clients(False, admin=True) as client:
        response = client.post(
            "/admin/api/cookies", json={"platform": platform, "cookies": "SID=fixture-cookie"}
        )
        assert response.status_code == 200
        wait_job(client, response.json()["id"])
        assert (
            "helper",
            "cookie_import",
            platform,
            {"cookies": "SID=fixture-cookie"},
        ) in client.recorded_calls
        assert not any(
            row[0] == "helper" and row[1].startswith("browser_") for row in client.recorded_calls
        )


@pytest.mark.parametrize("key", ["github-token", "groq-key", "openai-key"])
def test_native_mode_keeps_api_key_management(clients, key):
    with clients(False, admin=True) as client:
        response = client.post("/admin/api/credentials", json={"key": key, "value": "fixture-key"})
        assert response.status_code == 200
        wait_job(client, response.json()["id"])
        assert ("admin_cli", ["configure", key, "--stdin"], "fixture-key") in client.recorded_calls


@pytest.mark.parametrize("platform", ["bilibili", "youtube"])
def test_native_install_skips_browser_prepare_even_with_login_capability(clients, platform):
    with clients(False, admin=True) as client:
        response = client.post("/admin/api/install", json={"platform": platform})
        assert response.status_code == 200
        wait_job(client, response.json()["id"])
        assert ("admin_cli", ["add", platform], None) in client.recorded_calls
        assert not any(
            row[0] == "helper" and row[1] == "browser_prepare" for row in client.recorded_calls
        )


@pytest.mark.parametrize(
    "route,body",
    [
        ("/admin/api/install", {"platform": "xueqiu"}),
        ("/admin/api/install", {"platform": "xiaohongshu"}),
        ("/admin/api/login", {"platform": "bilibili"}),
        ("/admin/api/login", {"platform": "youtube"}),
        ("/admin/api/cookies", {"platform": "twitter", "cookies": "auth_token=fixture"}),
        ("/admin/api/cookies", {"platform": "xiaohongshu", "cookies": "web_session=fixture"}),
        ("/admin/api/verify", {"platform": "twitter"}),
        ("/admin/api/logout-platform", {"platform": "twitter"}),
        ("/admin/api/logout-platform", {"platform": "xueqiu"}),
    ],
)
def test_browser_management_is_blocked_before_upstream_execution(clients, route, body):
    with clients(False, admin=True) as client:
        response = client.post(route, json=body)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "BROWSER_DISABLED"
        assert not client.recorded_calls


def test_native_doctor_and_xueqiu_search_check_have_bounded_scope(clients):
    with clients(False, admin=True) as client:
        status = client.post("/admin/api/status", json={})
        wait_job(client, status.json()["id"])
        assert ("admin_cli", ["doctor"], None) in client.recorded_calls
        verify = client.post("/admin/api/verify", json={"platform": "xueqiu"})
        wait_job(client, verify.json()["id"])
        checks = [
            row[1]
            for row in client.recorded_calls
            if row[0] == "admin_cli" and row[1][0] == "check"
        ]
        assert len(checks) == 1 and checks[0][1] == "xueqiu" and "--search" in checks[0]


@pytest.mark.parametrize("body", [{}, {"platform": "-"}, {"platform": ""}])
def test_native_verify_cannot_run_an_all_channel_check(clients, body):
    with clients(False, admin=True) as client:
        response = client.post("/admin/api/verify", json=body)
        assert response.status_code >= 400
        assert not client.recorded_calls


@pytest.mark.parametrize(
    "method,route,body",
    [
        ("get", "/admin/api/login/frame", None),
        ("post", "/admin/api/login/input", {"action": "click", "x": 10, "y": 20}),
        ("post", "/admin/api/login/finish", {}),
    ],
)
def test_browser_controls_reject_native_mode_without_running_helper(clients, method, route, body):
    with clients(False, admin=True) as client:
        kwargs = {"json": body} if body is not None else {}
        response = client.request(method, route, **kwargs)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "BROWSER_DISABLED"
        assert not client.recorded_calls


def test_native_catalog_hides_browser_actions_but_keeps_native_xueqiu(clients):
    with clients(False) as client:
        response = client.get("/v1/channels", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 200
        body = response.json()
        assert body["browser_enabled"] is False
        channels = {row["name"]: row for row in body["channels"]}
        assert set(channels["xueqiu"]["actions"]) == {"quote", "search"}
        assert channels["xiaohongshu"]["actions"] == []
        assert channels["twitter"]["actions"] == []
        assert channels["bilibili"]["actions"]
        assert not client.recorded_calls


def test_native_credential_deletion_remains_available(clients):
    with clients(False, admin=True) as client:
        key = client.post("/admin/api/credentials", json={"key": "github-token", "value": None})
        wait_job(client, key.json()["id"])
        assert (
            "helper",
            "credential_delete",
            "-",
            {"key": "github-token"},
        ) in client.recorded_calls
        youtube = client.post("/admin/api/cookies/delete", json={"platform": "youtube"})
        wait_job(client, youtube.json()["id"])
        assert ("helper", "cookie_delete", "youtube", None) in client.recorded_calls
        bilibili = client.post("/admin/api/logout-platform", json={"platform": "bilibili"})
        wait_job(client, bilibili.json()["id"])
        assert ("admin_cli", ["logout", "bilibili"], None) in client.recorded_calls
        assert not any(
            row[0] == "helper" and row[1].startswith("browser_") for row in client.recorded_calls
        )
