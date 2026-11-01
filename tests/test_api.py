import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from reach_bridge.app import create_app

from .conftest import TOKEN


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        yield client


def test_auth_and_catalog(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    data = client.get("/v1/channels").json()
    assert data["agent_reach_version"] == "1.6.1"
    assert data["channels"][0]["actions"] == ["search", "read"]
    assert "securitySchemes" in client.get("/openapi.json").json()["components"]
    schema = client.get("/openapi.json").json()
    assert schema["paths"]["/v1/search"]["post"]["responses"]["422"]["content"]["application/json"][
        "schema"
    ]["$ref"].endswith("ErrorResponse")
    client.headers.pop("Authorization")
    assert client.get("/openapi.json").status_code == 401
    assert client.get("/v1/channels").status_code == 401
    client.headers["Authorization"] = "Bearer wrong-token"
    assert (
        client.post("/v1/search", json={"platform": "bilibili", "query": "hello"}).status_code
        == 401
    )


def test_search_args_and_source(client):
    query = '--timeout 999; $(touch /tmp/not-a-real-command) 中文 & "'
    result = client.post("/v1/search", json={"platform": "bilibili", "query": query, "limit": 3})
    assert result.status_code == 200
    data = result.json()
    assert data["items"][0]["title"] == query
    assert "raw" not in data["items"][0]
    assert data["backend"] == "fixture"
    assert data["meta"]["argv"] == [
        "bilibili",
        "search",
        "--timeout",
        "60",
        "--limit",
        "3",
        "--",
        query,
    ]
    assert data["meta"]["supervised"] == "1"
    assert data["meta"]["token_in_child"] is False


def test_read_preserves_query_but_drops_fragment(client):
    result = client.post(
        "/v1/read",
        json={
            "platform": "bilibili",
            "url": "https://www.bilibili.com:443/video/BV1GJ411x7h7?a=1&b=2#part",
        },
    )
    assert result.status_code == 200
    assert (
        result.json()["meta"]["argv"][-1] == "https://www.bilibili.com/video/BV1GJ411x7h7?a=1&b=2"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://www.bilibili.com/video/1",
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://www.bilibili.com.evil.invalid/",
        "https://www.bilibili.com@evil.invalid/",
        "https://user:password@www.bilibili.com/",
        "https://www.bilibili.com:8080/",
        "https://b23.tv/example",
        "file:///etc/passwd",
        "https://www.bilibili.com\\@127.0.0.1/",
        "https://www.bilibili.com/\nsecret",
        "https://www.bilibili.com:bad/",
    ],
)
def test_url_boundary(client, url):
    response = client.post("/v1/read", json={"platform": "bilibili", "url": url})
    assert response.status_code == 422
    assert url not in response.text


@pytest.mark.parametrize(
    "payload",
    [
        {"platform": "bilibili", "query": " "},
        {"platform": "bilibili", "query": "x" * 301},
        {"platform": "bilibili", "query": "secret-fixture", "limit": True},
        {"platform": "bilibili", "query": "secret-fixture", "limit": 21},
        {"platform": "bilibili", "query": "secret-fixture", "backend": "anything"},
    ],
)
def test_validation_does_not_echo_inputs(client, payload):
    response = client.post("/v1/search", json=payload)
    assert response.status_code == 422
    assert "secret-fixture" not in response.text


def test_unconfigured_platform_and_management_not_exposed(client):
    assert client.post("/v1/search", json={"platform": "web", "query": "hello"}).status_code == 403
    assert client.post("/v1/update", json={}).status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.post("/v1/search", content="not-json").status_code == 422
    assert client.post("/v1/search", content=b"x" * 16385).status_code == 413
    chunks = (b"x" * 4096 for _ in range(5))
    assert client.post("/v1/search", content=chunks).status_code == 413


def test_upstream_failure_is_not_fabricated_success(client):
    response = client.post("/v1/search", json={"platform": "bilibili", "query": "need-login"})
    assert response.status_code == 424
    assert response.json()["ok"] is False
    assert response.json()["error"]["code"] == "NEED_LOGIN"


@pytest.mark.parametrize("query", ["invalid", "schema", "exit-mismatch"])
def test_invalid_upstream(client, query):
    response = client.post("/v1/search", json={"platform": "bilibili", "query": query})
    assert response.status_code == 502
    assert "private-invalid-json" not in response.text


def test_configuration_and_startup_fail_loudly(settings):
    with pytest.raises(ValueError):
        replace(settings, token="short-secret")
    settings.channels_file.write_text(json.dumps({"missing": {"hosts": ["example.com"]}}))
    with pytest.raises(RuntimeError, match="配置的渠道"):
        with TestClient(create_app(settings)):
            pass
