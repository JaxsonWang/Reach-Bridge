"""管理 helper 的协议、权限边界与凭据替换测试，不连接真实平台或浏览器。"""

import base64
import http.cookiejar
import io
import json
import os
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from reach_bridge import upstream_admin as admin


def channel(name="example"):
    return SimpleNamespace(name=name, login_url="https://www.example.com/login", session="")


def png_header(width=1280, height=800):
    return base64.b64encode(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + struct.pack(">II", width, height)
    ).decode()


@pytest.fixture
def upstream(tmp_path):
    saved = {}
    state_data = {"checks": {"example": {"previous": "old"}}}
    calls = []
    pages = [
        {
            "id": "PAGE1",
            "type": "page",
            "url": "https://www.example.com/login?token=private-fragment#private",
        }
    ]
    cookies = [
        {"name": "old", "value": "old-secret", "domain": ".example.com", "path": "/"},
        {"name": "other", "value": "unrelated-secret", "domain": ".other.com", "path": "/"},
    ]

    class Config:
        def __init__(self, *, read_only=False):
            self.read_only = read_only

        def get(self, key):
            return saved.get(key)

        def delete(self, key):
            saved.pop(key, None)

    def cdp(method, params=None, *, target_id=None):
        calls.append((method, params, target_id))
        if method == "Storage.getCookies":
            return {"cookies": cookies}
        if method == "Page.captureScreenshot":
            return {"data": png_header()}
        return {}

    def mutate(callback):
        callback(state_data)

    fixture = SimpleNamespace(
        Config=Config,
        channels=[
            channel(),
            SimpleNamespace(name="bilibili", login_url="", session="bili-cli"),
            SimpleNamespace(name="github", login_url="", session=""),
        ],
        manage=SimpleNamespace(
            site_domains=lambda item: ["example.com"] if item.name == "example" else []
        ),
        cdp=SimpleNamespace(call=cdp, _json=lambda path: pages),
        browser=SimpleNamespace(
            browser_root=lambda: tmp_path / "browser",
            profile_dir=lambda: tmp_path / "browser" / "profile",
            clear_site=lambda domains: calls.append(("clear_site", domains, None)),
            is_running=lambda: True,
            _require_owned=lambda: None,
            touch=lambda: None,
            save_session=lambda: None,
            _load_state=lambda: {},
            update_state=lambda **kwargs: calls.append(("state", kwargs)),
            setup=lambda **kwargs: {},
            stop=lambda: True,
            start=lambda url: None,
            wait_for=lambda predicate, timeout: predicate(),
        ),
        sessions=SimpleNamespace(
            has_login_state=lambda name: False,
            session_env=lambda name: {"HOME": str(tmp_path / name)},
        ),
        paths=SimpleNamespace(
            read_small_text_no_follow=lambda *args, **kwargs: None,
            ensure_no_symlink_path=lambda *args: None,
        ),
        state=SimpleNamespace(
            load=lambda: state_data, mutate=mutate, now=lambda: "2026-11-01T10:00:00Z"
        ),
        tools=SimpleNamespace(resolve=lambda executable: str(tmp_path / executable)),
        calls=calls,
        pages=pages,
        cookies=cookies,
        values=saved,
        data=state_data,
    )
    return fixture


def invoke(monkeypatch, upstream, op, name="example", payload=None, raw=None):
    monkeypatch.setattr(admin, "load_upstream", lambda: upstream)
    output = io.StringIO()
    code = admin.main(
        [op, name], io.StringIO(raw if raw is not None else json.dumps(payload or {})), output
    )
    return code, json.loads(output.getvalue()), output.getvalue()


@pytest.mark.parametrize(
    "raw", ["{broken-secret", "[]", "null", '"secret"', "x" * (admin.MAX_INPUT + 1)]
)
def test_protocol_rejects_invalid_json_without_echo(monkeypatch, upstream, raw):
    code, result, text = invoke(monkeypatch, upstream, "describe", raw=raw)
    assert code == 1
    assert result["schema_version"] == 1 and result["ok"] is False
    assert result["error"]["code"] == "BAD_INPUT"
    assert "broken-secret" not in text
    assert result["result"] == {}


@pytest.mark.parametrize(
    "operation", ["exec", "eval", "configure", "Runtime.evaluate", "logout;id"]
)
def test_only_fixed_operations_are_callable(monkeypatch, upstream, operation):
    code, result, _ = invoke(monkeypatch, upstream, operation)
    assert code == 1 and result["error"]["code"] == "BAD_INPUT"
    assert not upstream.calls


def test_version_gate_precedes_runtime_import(monkeypatch):
    imported = []

    def fake_import(name):
        imported.append(name)
        assert name == "agent_reach"
        return SimpleNamespace(__version__="1.7.0")

    monkeypatch.setattr(admin.importlib, "import_module", fake_import)
    output = io.StringIO()
    assert admin.main(["describe", "-"], io.StringIO("{}"), output) == 1
    assert json.loads(output.getvalue())["error"]["code"] == "UNSUPPORTED_UPSTREAM"
    assert imported == ["agent_reach"]


def test_metadata_and_status_never_return_credentials(monkeypatch, upstream):
    upstream.values["github_token"] = "private-github-token"
    code, result, text = invoke(monkeypatch, upstream, "credentials_status", name="-")
    assert code == 0 and result["result"]["keys"]["github-token"] is True
    assert "private-github-token" not in text
    _, result, _ = invoke(monkeypatch, upstream, "describe", name="bilibili")
    info = result["result"]["channels"]["bilibili"]
    assert info["login_url"] == admin.BILI_LOGIN_URL
    assert info["domains"] == ["bilibili.com"]


def test_delete_restricts_key_and_channel(monkeypatch, upstream):
    upstream.values.update(github_token="secret", groq_api_key="other-secret")
    code, _, _ = invoke(
        monkeypatch, upstream, "credential_delete", name="example", payload={"key": "github-token"}
    )
    assert code == 1 and upstream.values["github_token"] == "secret"
    code, result, _ = invoke(
        monkeypatch, upstream, "credential_delete", name="github", payload={"key": "github-token"}
    )
    assert code == 0 and result["result"]["configured"] is False
    assert upstream.values == {"groq_api_key": "other-secret"}


@pytest.mark.parametrize(
    "domain",
    [
        "example.com.evil.com",
        "evil-example.com",
        "localhost",
        "127.0.0.1",
        "other.com",
        "example.com@evil.com",
        "*.example.com",
    ],
)
def test_cookie_domain_must_belong_to_selected_channel(domain):
    with pytest.raises(admin.AdminError, match="管理操作"):
        admin.parse_cookies(
            [{"name": "session", "value": "secret", "domain": domain}], ["example.com"]
        )


def test_cookie_header_and_browser_json_preserve_value():
    rows = admin.parse_cookies("session=a=b; token=encoded%2Fvalue", ["example.com"])
    assert rows[0]["value"] == "a=b" and rows[1]["value"] == "encoded%2Fvalue"
    assert rows[0]["domain"] == ".example.com"
    rows = admin.parse_cookies(
        json.dumps(
            [
                {
                    "name": "session",
                    "value": "secret",
                    "domain": ".www.example.com",
                    "sameSite": "lax",
                    "session": True,
                    "hostOnly": False,
                }
            ]
        ),
        ["example.com"],
    )
    assert rows[0]["sameSite"] == "Lax"
    pretty = '[\n  {"name":"SID","value":"secret","domain":".example.com"}\n]'
    assert admin.parse_cookies(pretty, ["example.com"])[0]["value"] == "secret"


@pytest.mark.parametrize(
    "value",
    [
        "session=secret\nHeader=bad",
        "HttpOnly",
        "a=x;a=y",
        [],
        [{"name": "a", "value": "x", "domain": ".example.com", "expires": float("nan")}],
        [{"name": "a", "value": "x", "domain": ".example.com", "expires": 1}],
        [{"name": "a", "value": "x", "domain": ".example.com", "secure": "false"}],
    ],
)
def test_bad_cookies_are_rejected(value):
    with pytest.raises(admin.AdminError):
        admin.parse_cookies(value, ["example.com"])


def test_browser_import_replaces_site_snapshot_and_marks_manual(monkeypatch, upstream):
    monkeypatch.setattr(admin, "prepare_browser", lambda *_args, **_kwargs: None)
    code, result, text = invoke(
        monkeypatch, upstream, "cookie_import", payload={"cookies": "new=new-secret"}
    )
    assert code == 0 and result["result"]["verified"] is False
    applied = next(
        args["cookies"] for method, args, _ in upstream.calls if method == "Storage.setCookies"
    )
    assert any(row["name"] == "old" and row["expires"] == 1 for row in applied)
    assert all(row["domain"] == ".example.com" for row in applied)
    assert upstream.data["session_reuse"]["example"]["source"] == "manual"
    assert "example" not in upstream.data["checks"]
    assert "new-secret" not in text and "old-secret" not in text


def test_failed_browser_import_restores_original_cookies(upstream):
    saves = []

    def save():
        saves.append(1)
        if len(saves) == 1:
            raise OSError("secret")

    upstream.browser.save_session = save
    with pytest.raises(OSError):
        admin.replace_browser_cookies(
            upstream, admin.parse_cookies("new=secret", ["example.com"]), ["example.com"]
        )
    writes = [
        params["cookies"] for method, params, _ in upstream.calls if method == "Storage.setCookies"
    ]
    assert len(writes) == 2
    assert any(row["name"] == "old" and row["value"] == "old-secret" for row in writes[1])
    assert any(row["name"] == "new" and row["expires"] == 1 for row in writes[1])


def test_bili_import_does_not_start_browser(monkeypatch, upstream):
    written = []
    monkeypatch.setattr(admin, "save_bili_native", lambda _up, rows: written.extend(rows))
    monkeypatch.setattr(
        admin, "prepare_browser", lambda *args, **kwargs: pytest.fail("不应启动浏览器")
    )
    code, result, text = invoke(
        monkeypatch,
        upstream,
        "cookie_import",
        name="bilibili",
        payload={"cookies": "SESSDATA=private-session"},
    )
    assert code == 0 and result["result"]["storage"] == "native"
    assert result["result"]["verified"] is True
    assert written[0]["value"] == "private-session"
    assert "private-session" not in text


def test_bili_writer_uses_stdin_and_excludes_bridge_secret(monkeypatch, upstream, tmp_path):
    (tmp_path / "python").touch()
    monkeypatch.setenv("REACH_BRIDGE_ADMIN_TOKEN", "private-admin-token")
    invoked = []

    def run(args, **kwargs):
        invoked.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"saved":true}')

    monkeypatch.setattr(admin.subprocess, "run", run)
    admin.save_bili_native(
        upstream, admin.parse_cookies("SESSDATA=private-cookie", ["bilibili.com"])
    )
    args, kwargs = invoked[0]
    assert args[:2] == [str(tmp_path / "python"), "-c"]
    assert "private-cookie" not in " ".join(args)
    assert json.loads(kwargs["input"])["SESSDATA"] == "private-cookie"
    assert "REACH_BRIDGE_ADMIN_TOKEN" not in kwargs["env"]
    assert kwargs["env"]["HOME"] == str(tmp_path / "bili-cli")


def test_bili_invalid_session_fails_before_writing(upstream):
    with pytest.raises(admin.AdminError) as caught:
        admin.save_bili_native(upstream, [{"name": "bili_jct", "value": "secret"}])
    assert caught.value.code == "NEED_LOGIN"


def test_browser_frame_omits_url_query_and_fragment(monkeypatch, upstream):
    code, result, text = invoke(
        monkeypatch, upstream, "browser_frame", payload={"target_id": "PAGE1"}
    )
    assert code == 0 and result["result"]["image"] == png_header()
    assert result["result"]["url"] == "https://www.example.com/login"
    assert "private-fragment" not in text


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.com/login",
        "http://www.example.com/login",
        "https://www.example.com:444/login",
        "file:///private/file",
        "https://user:password@www.example.com/login",
    ],
)
def test_browser_target_cannot_escape_platform(monkeypatch, upstream, url):
    upstream.pages[0]["url"] = url
    code, result, _ = invoke(monkeypatch, upstream, "browser_frame", payload={"target_id": "PAGE1"})
    assert code == 1 and result["error"]["code"] == "TARGET_UNAVAILABLE"
    assert not upstream.calls


def test_browser_input_is_fixed_and_does_not_echo_text(monkeypatch, upstream):
    code, _, text = invoke(
        monkeypatch,
        upstream,
        "browser_input",
        payload={"target_id": "PAGE1", "action": "type", "text": "private-password"},
    )
    assert code == 0 and "private-password" not in text
    assert upstream.calls == [("Input.insertText", {"text": "private-password"}, "PAGE1")]
    code, result, _ = invoke(
        monkeypatch,
        upstream,
        "browser_input",
        payload={"target_id": "PAGE1", "action": "Runtime.evaluate", "expression": "1"},
    )
    assert code == 1 and result["error"]["code"] == "BAD_INPUT"


def test_compatibility_selects_configured_launcher(monkeypatch, upstream, tmp_path):
    old, launcher = tmp_path / "chromium", tmp_path / "reach-bridge-chromium"
    for executable in (old, launcher):
        executable.touch()
        executable.chmod(0o755)
    upstream.browser._load_state = lambda: {"backend": "chrome", "executable": str(old)}
    monkeypatch.setenv("REACH_BRIDGE_BROWSER_EXECUTABLE", str(launcher))
    monkeypatch.setenv("REACH_BRIDGE_BROWSER_SANDBOX", "false")
    stopped = []
    upstream.browser.stop = lambda: stopped.append(True) or True
    admin.select_system_browser(upstream)
    assert stopped == [True]
    assert upstream.calls == [
        (
            "state",
            {
                "backend": "chrome",
                "executable": str(launcher),
                "launcher_python": None,
                "fallback_reason": None,
            },
        )
    ]
    upstream.calls.clear()
    upstream.browser._load_state = lambda: {"backend": "chrome", "executable": str(launcher)}
    admin.select_system_browser(upstream)
    assert not upstream.calls and stopped == [True]


def test_selected_system_browser_is_preserved(monkeypatch, upstream, tmp_path):
    executable = tmp_path / "chromium"
    executable.touch()
    executable.chmod(0o755)
    upstream.browser._load_state = lambda: {"backend": "chrome", "executable": str(executable)}
    monkeypatch.setenv("REACH_BRIDGE_BROWSER_EXECUTABLE", "/another/chromium")
    admin.select_system_browser(upstream)
    assert not upstream.calls


def test_old_official_chromium_uses_on_demand_launcher_in_standard_mode(
    monkeypatch, upstream, tmp_path
):
    launcher = tmp_path / "reach-bridge-chromium"
    launcher.touch()
    launcher.chmod(0o755)
    upstream.browser._load_state = lambda: {"backend": "chrome", "executable": "/usr/bin/chromium"}
    monkeypatch.setenv("REACH_BRIDGE_BROWSER_EXECUTABLE", str(launcher))
    monkeypatch.setenv("REACH_BRIDGE_BROWSER_SANDBOX", "true")
    is_file, access = Path.is_file, os.access
    monkeypatch.setattr(
        Path, "is_file", lambda path: str(path) == "/usr/bin/chromium" or is_file(path)
    )
    monkeypatch.setattr(
        os, "access", lambda path, mode: path == "/usr/bin/chromium" or access(path, mode)
    )
    stopped = []
    upstream.browser.stop = lambda: stopped.append(True) or True
    admin.select_system_browser(upstream)
    assert stopped == [True]
    assert upstream.calls[0] == (
        "state",
        {
            "backend": "chrome",
            "executable": str(launcher),
            "launcher_python": None,
            "fallback_reason": None,
        },
    )


def test_error_envelope_does_not_echo_exception_or_upstream_output(monkeypatch, upstream):
    def broken(*args):
        print("stdout-secret")
        raise RuntimeError("private-cookie=secret")

    monkeypatch.setattr(admin, "describe", broken)
    code, result, text = invoke(monkeypatch, upstream, "describe")
    assert code == 1 and result["error"]["code"] == "UPSTREAM_ERROR"
    assert "stdout-secret" not in text and "private-cookie" not in text


@pytest.mark.parametrize("new_cookie,valid", [("valid-cookie", True), ("invalid-cookie", False)])
def test_native_writer_replaces_only_after_authentication(tmp_path, new_cookie, valid):
    # 用真实子进程执行固定 writer，仅将上游包替换成本机夹具，验证保存与失败边界。
    packages = tmp_path / "packages"
    bili = packages / "bili_cli"
    api = packages / "bilibili_api"
    bili.mkdir(parents=True)
    api.mkdir()
    (bili / "__init__.py").write_text("", encoding="utf-8")
    (bili / "auth.py").write_text(
        "import json\nfrom pathlib import Path\n"
        'CONFIG_DIR = Path.home() / ".bilibili-cli"\n'
        'CREDENTIAL_FILE = CONFIG_DIR / "credential.json"\n'
        "def save_credential(value):\n"
        '    CREDENTIAL_FILE.write_text(json.dumps({"sessdata": value.sessdata}))\n'
        "    CREDENTIAL_FILE.chmod(0o600)\n",
        encoding="utf-8",
    )
    (api / "__init__.py").write_text(
        "class Credential:\n"
        "    def __init__(self, **values):\n"
        "        self.__dict__.update(values)\n"
        "class user:\n"
        "    @staticmethod\n"
        "    async def get_self_info(value):\n"
        '        if value.sessdata != "valid-cookie":\n'
        '            raise ValueError("not authenticated")\n'
        '        return {"mid": 123}\n',
        encoding="utf-8",
    )
    home = tmp_path / "home"
    destination = home / ".bilibili-cli" / "credential.json"
    destination.parent.mkdir(parents=True)
    destination.write_text('{"sessdata":"previous-cookie"}', encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-c", admin.BILI_WRITER],
        input=json.dumps({"SESSDATA": new_cookie}),
        text=True,
        capture_output=True,
        env={**os.environ, "HOME": str(home), "PYTHONPATH": str(packages)},
        check=False,
    )
    if valid:
        assert completed.returncode == 0
        assert json.loads(completed.stdout) == {"saved": True}
        assert json.loads(destination.read_text())["sessdata"] == new_cookie
        assert destination.stat().st_mode & 0o777 == 0o600
    else:
        assert completed.returncode != 0
        assert json.loads(destination.read_text())["sessdata"] == "previous-cookie"
    assert new_cookie not in completed.stdout
    assert not list(destination.parent.glob(".bridge-*"))


@pytest.mark.parametrize(
    "action,extra,method",
    [
        ("click", {"x": 123, "y": 456}, "Input.dispatchMouseEvent"),
        ("key", {"key": "Control+A"}, "Input.dispatchKeyEvent"),
        ("scroll", {"x": 10, "y": 20, "delta": 100}, "Input.dispatchMouseEvent"),
        ("reload", {}, "Page.reload"),
    ],
)
def test_fixed_browser_controls(monkeypatch, upstream, action, extra, method):
    code, result, _ = invoke(
        monkeypatch,
        upstream,
        "browser_input",
        payload={"target_id": "PAGE1", "action": action, **extra},
    )
    assert code == 0 and result["result"] == {"applied": True}
    assert all(call[0] == method and call[2] == "PAGE1" for call in upstream.calls)


def test_browser_finish_never_claims_verification_for_cookie_presence(monkeypatch, upstream):
    code, result, text = invoke(
        monkeypatch, upstream, "browser_finish", payload={"target_id": "PAGE1"}
    )
    assert code == 0 and result["result"]["verified"] is False
    assert result["result"]["cookie_count"] == 1
    assert "old-secret" not in text and "unrelated-secret" not in text


@pytest.fixture
def youtube(monkeypatch, upstream, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    upstream.channels.append(SimpleNamespace(name="youtube", login_url="", session=""))
    upstream.paths.home_dir = lambda: tmp_path
    upstream.state.root = lambda: tmp_path / ".agent-reach"
    owned = set()
    upstream.state.owned_path = lambda path: path in owned
    upstream.state.remember_path = lambda path, purpose: owned.add(path)
    upstream.paths.make_private_dir = lambda path: path.mkdir(
        parents=True, exist_ok=True, mode=0o700
    )

    def read(path, **kwargs):
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def write(path, text):
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)

    upstream.paths.read_small_text_no_follow = read
    upstream.paths.atomic_write_private_text = write
    config = tmp_path / ".config" / "yt-dlp" / "config"
    monkeypatch.setattr(
        admin,
        "youtube_config_candidates",
        lambda _up: {
            config,
            tmp_path / "yt-dlp.conf",
            tmp_path / ".config" / "yt-dlp.conf",
        },
    )
    return upstream


def test_youtube_cookie_import_is_native_and_standard_netscape(monkeypatch, youtube):
    monkeypatch.setattr(
        admin, "prepare_browser", lambda *args, **kwargs: pytest.fail("不启动浏览器")
    )
    code, result, text = invoke(
        monkeypatch,
        youtube,
        "cookie_import",
        name="youtube",
        payload={"cookies": "SID=private-youtube-session"},
    )
    assert code == 0 and result["result"]["storage"] == "yt-dlp"
    assert result["result"]["verified"] is False
    config, jar, expected = admin.youtube_layout(youtube)
    assert config.read_text() == expected
    loaded = http.cookiejar.MozillaCookieJar(str(jar))
    loaded.load(ignore_discard=True, ignore_expires=True)
    assert [(cookie.domain, cookie.name, cookie.value) for cookie in loaded] == [
        (".youtube.com", "SID", "private-youtube-session")
    ]
    assert jar.stat().st_mode & 0o777 == 0o600
    assert "private-youtube-session" not in text
    _, result, _ = invoke(monkeypatch, youtube, "credentials_status", name="youtube")
    assert result["result"]["channels"]["youtube"]["native_saved"] is True
    assert result["result"]["channels"]["youtube"]["cookie_count"] == 1


@pytest.mark.parametrize("location", ["user", "home", "other-user"])
def test_youtube_import_never_overwrites_existing_configuration(monkeypatch, youtube, location):
    config, jar, _ = admin.youtube_layout(youtube)
    destination = {
        "user": config,
        "home": youtube.paths.home_dir() / "yt-dlp.conf",
        "other-user": config.parent.parent / "yt-dlp.conf",
    }[location]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("--cookies /private/custom-cookie-jar\n", encoding="utf-8")
    code, result, text = invoke(
        monkeypatch,
        youtube,
        "cookie_import",
        name="youtube",
        payload={"cookies": "SID=another-secret"},
    )
    assert code == 1 and result["error"]["code"] == "CONFIG_CONFLICT"
    assert destination.read_text() == "--cookies /private/custom-cookie-jar\n"
    assert not jar.exists() and "custom-cookie-jar" not in text


def test_youtube_jar_failure_removes_only_new_managed_configuration(monkeypatch, youtube):
    config, jar, _ = admin.youtube_layout(youtube)
    original = youtube.paths.atomic_write_private_text

    def failing(path, text):
        if path == jar:
            raise OSError("fixture failure")
        return original(path, text)

    youtube.paths.atomic_write_private_text = failing
    code, _, _ = invoke(
        monkeypatch, youtube, "cookie_import", name="youtube", payload={"cookies": "SID=secret"}
    )
    assert code == 1 and not config.exists() and not jar.exists()


def test_youtube_delete_removes_only_managed_jar_and_config(monkeypatch, youtube):
    invoke(monkeypatch, youtube, "cookie_import", name="youtube", payload={"cookies": "SID=secret"})
    config, jar, _ = admin.youtube_layout(youtube)
    code, result, _ = invoke(monkeypatch, youtube, "cookie_delete", name="youtube")
    assert code == 0 and result["result"] == {"deleted": True, "saved": False}
    assert not config.exists() and not jar.exists()


def test_youtube_browser_uses_google_login_but_exports_only_youtube(monkeypatch, youtube):
    youtube.pages[0]["url"] = "https://accounts.google.com/v3/signin?private=1"
    youtube.cookies.extend(
        [
            {"name": "SID", "value": "youtube-secret", "domain": ".youtube.com", "path": "/"},
            {"name": "SID", "value": "google-secret", "domain": ".google.com", "path": "/"},
        ]
    )
    code, result, _ = invoke(
        monkeypatch, youtube, "browser_finish", name="youtube", payload={"target_id": "PAGE1"}
    )
    assert code == 0 and result["result"]["cookie_count"] == 1
    _, jar, _ = admin.youtube_layout(youtube)
    assert "youtube-secret" in jar.read_text() and "google-secret" not in jar.read_text()


@pytest.mark.parametrize("name", ["example", "bilibili", "youtube"])
def test_explicit_browser_prepare_selects_system_before_install(monkeypatch, youtube, name):
    sequence = []
    monkeypatch.setattr(admin, "select_system_browser", lambda _up: sequence.append("system"))

    def ensure(tool):
        sequence.append(("ensure", tool))

    def setup(**kwargs):
        sequence.append(("setup", kwargs))
        return {"running": True}

    youtube.tools.ensure = ensure
    youtube.browser.setup = setup
    code, result, _ = invoke(monkeypatch, youtube, "browser_prepare", name=name)
    assert code == 0 and result["result"]["prepared"] is True
    assert sequence == ["system", ("ensure", "opencli"), ("setup", {"headless": True})]


def test_browser_prepare_failure_is_visible_and_not_retried(monkeypatch, upstream):
    sequence = []
    monkeypatch.setattr(admin, "select_system_browser", lambda _up: sequence.append("system"))

    def fail(tool):
        sequence.append(tool)
        raise RuntimeError("private-cookie installation-log")

    upstream.tools.ensure = fail
    upstream.browser.setup = lambda **kwargs: pytest.fail("安装失败后不能继续启动")
    code, result, text = invoke(monkeypatch, upstream, "browser_prepare")
    assert code == 1 and result["error"]["code"] == "BROWSER_PREPARE_FAILED"
    assert sequence == ["system", "opencli"]
    assert "private-cookie" not in text and "installation-log" not in text


def test_regular_browser_use_does_not_install_dependencies(monkeypatch, upstream):
    monkeypatch.setattr(admin, "select_system_browser", lambda _up: None)
    upstream.tools.ensure = lambda tool: pytest.fail("非准备动作不安装依赖")
    upstream.tools.resolve = lambda tool: None
    with pytest.raises(admin.AdminError) as caught:
        admin.prepare_browser(upstream, visible=True)
    assert caught.value.code == "NEED_SETUP"


def test_root_is_rejected_only_for_browser_management(monkeypatch, upstream):
    monkeypatch.setattr(admin.os, "getuid", lambda: 0)
    upstream.tools.ensure = lambda tool: pytest.fail("root 不应进入安装或启动")
    code, result, _ = invoke(monkeypatch, upstream, "browser_prepare")
    assert code == 1 and result["error"]["code"] == "BROWSER_PREPARE_FAILED"
    code, _, _ = invoke(monkeypatch, upstream, "describe")
    assert code == 0
    code, _, _ = invoke(monkeypatch, upstream, "credentials_status")
    assert code == 0


def test_loaded_channel_registry_is_an_independent_list(monkeypatch):
    registered = [channel()]

    def module(name):
        if name == "agent_reach":
            return SimpleNamespace(__version__="1.6.1")
        if name == "agent_reach.channels":
            return SimpleNamespace(get_all_channels=lambda: registered)
        if name == "agent_reach.config":
            return SimpleNamespace(Config=object)
        return SimpleNamespace()

    monkeypatch.setattr(admin.importlib, "import_module", module)
    loaded = admin.load_upstream()
    loaded.channels.append(channel("fixture"))
    assert len(registered) == 1 and len(loaded.channels) == 2


def test_youtube_delete_uses_official_running_browser_cleanup(monkeypatch, youtube):
    invoke(monkeypatch, youtube, "cookie_import", name="youtube", payload={"cookies": "SID=secret"})
    code, _, _ = invoke(monkeypatch, youtube, "cookie_delete", name="youtube")
    assert code == 0 and ("clear_site", ["youtube.com"], None) in youtube.calls


@pytest.mark.parametrize("database_path", ["Cookies", "Network/Cookies"])
def test_youtube_delete_cleans_offline_profile_and_snapshot_without_starting(
    monkeypatch, youtube, database_path
):
    invoke(monkeypatch, youtube, "cookie_import", name="youtube", payload={"cookies": "SID=secret"})
    youtube.browser.is_running = lambda: False
    youtube.browser.setup = lambda **kwargs: pytest.fail("退出不能启动浏览器")
    youtube.tools.ensure = lambda tool: pytest.fail("退出不能安装工具")
    database = youtube.browser.profile_dir() / "Default" / database_path
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE cookies (host_key TEXT, value TEXT)")
        connection.executemany(
            "INSERT INTO cookies VALUES (?, ?)",
            [
                (".youtube.com", "fixture-youtube-secret"),
                ("www.youtube.com", "fixture-subdomain-secret"),
                (".google.com", "fixture-google-secret"),
                ("youtube.com.evil.example", "fixture-unrelated-secret"),
            ],
        )
    snapshot = youtube.browser.browser_root() / "session.json"
    snapshot.write_text(
        json.dumps(
            {
                "cookies": [
                    {"domain": ".youtube.com", "name": "SID", "value": "fixture-youtube-secret"},
                    {"domain": ".google.com", "name": "SID", "value": "fixture-google-secret"},
                ]
            }
        ),
        encoding="utf-8",
    )
    code, result, text = invoke(monkeypatch, youtube, "cookie_delete", name="youtube")
    assert code == 0 and result["result"]["deleted"] is True
    with sqlite3.connect(database) as connection:
        hosts = {row[0] for row in connection.execute("SELECT host_key FROM cookies")}
    assert hosts == {".google.com", "youtube.com.evil.example"}
    assert [row["domain"] for row in json.loads(snapshot.read_text())["cookies"]] == [".google.com"]
    assert "fixture-youtube-secret" not in text and "fixture-google-secret" not in text


def test_youtube_offline_cleanup_failure_is_visible_and_keeps_native_files(monkeypatch, youtube):
    invoke(monkeypatch, youtube, "cookie_import", name="youtube", payload={"cookies": "SID=secret"})
    youtube.browser.is_running = lambda: False
    database = youtube.browser.profile_dir() / "Default" / "Cookies"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"invalid database fixture")
    code, result, _ = invoke(monkeypatch, youtube, "cookie_delete", name="youtube")
    config, jar, _ = admin.youtube_layout(youtube)
    assert code == 1 and result["error"]["code"] == "UPSTREAM_ERROR"
    assert config.exists() and jar.exists()


def test_real_window_size_persists_across_cdp_connections(upstream):
    size = [1050, 713]
    methods = []

    def independent_connection(method, params=None, *, target_id=None):
        methods.append(method)
        if method == "Browser.getWindowForTarget":
            return {"windowId": 17, "bounds": {"windowState": "normal"}}
        if method == "Browser.setContentsSize":
            assert params["windowId"] == 17
            size[:] = [params["width"], params["height"]]
            return {}
        if method == "Runtime.evaluate":
            return {"result": {"value": size == [1280, 800]}}
        raise AssertionError("不应使用随连接关闭失效的 Emulation 覆盖")

    upstream.cdp.call = independent_connection
    admin.set_browser_contents_size(upstream, "PAGE1")
    assert size == [1280, 800]
    assert methods == ["Browser.getWindowForTarget", "Browser.setContentsSize", "Runtime.evaluate"]


@pytest.mark.parametrize(
    "data,expected",
    [
        (png_header(1050, 713), "FRAME_SIZE_MISMATCH"),
        ("not-base64", "BROWSER_FAILED"),
        (base64.b64encode(b"not-png").decode(), "BROWSER_FAILED"),
    ],
)
def test_frame_rejects_wrong_dimensions_or_format(monkeypatch, upstream, data, expected):
    upstream.cdp.call = lambda *args, **kwargs: {"data": data}
    code, result, _ = invoke(monkeypatch, upstream, "browser_frame", payload={"target_id": "PAGE1"})
    assert code == 1 and result["error"]["code"] == expected
    assert result["result"] == {}


QR_KEY = "a" * 32
QR_URL = "https://account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=" + QR_KEY


def test_bili_qr_start_returns_validated_challenge(monkeypatch, upstream, tmp_path):
    (tmp_path / "python").touch()
    requested = []

    def http(url, **kwargs):
        requested.append(url)
        return {"qrcode_key": QR_KEY, "url": QR_URL}, {}

    monkeypatch.setattr(admin, "bili_qr_http", http)
    code, result, _ = invoke(monkeypatch, upstream, "bilibili_qr_start", "bilibili")
    assert code == 0
    assert result["result"] == {"qrcode_key": QR_KEY, "url": QR_URL, "expires_in": 180}
    assert requested == [admin.BILI_QR_ROOT + "generate"]
    assert not upstream.calls


@pytest.mark.parametrize(
    "state,expected", [(86101, "waiting_scan"), (86090, "waiting_confirm"), (86038, "expired")]
)
def test_bili_qr_poll_waiting_never_saves(monkeypatch, upstream, state, expected):
    monkeypatch.setattr(admin, "bili_qr_http", lambda *a, **k: ({"code": state}, {}))
    monkeypatch.setattr(admin, "save_bili_native", lambda *a: pytest.fail("等待时不能保存凭据"))
    code, result, text = invoke(
        monkeypatch, upstream, "bilibili_qr_poll", "bilibili", {"qrcode_key": QR_KEY}
    )
    assert code == 0 and result["result"] == {"state": expected}
    assert QR_KEY not in text and not upstream.calls


@pytest.mark.parametrize(
    "payload",
    [{}, {"qrcode_key": "https://evil.test"}, {"qrcode_key": QR_KEY, "url": "https://evil.test"}],
)
def test_bili_qr_poll_rejects_untrusted_context_before_network(monkeypatch, upstream, payload):
    monkeypatch.setattr(admin, "bili_qr_http", lambda *a, **k: pytest.fail("无效输入不能联网"))
    code, result, _ = invoke(monkeypatch, upstream, "bilibili_qr_poll", "bilibili", payload)
    assert code == 1 and result["error"]["code"] == "BAD_INPUT"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/scan?qrcode_key=" + QR_KEY,
        QR_URL.replace(QR_KEY, "b" * 32),
        QR_URL + "&qrcode_key=second",
    ],
)
def test_bili_qr_start_rejects_wrong_origin_and_key(monkeypatch, upstream, tmp_path, url):
    (tmp_path / "python").touch()
    monkeypatch.setattr(
        admin, "bili_qr_http", lambda *a, **k: ({"qrcode_key": QR_KEY, "url": url}, {})
    )
    code, result, _ = invoke(monkeypatch, upstream, "bilibili_qr_start", "bilibili")
    assert code == 1 and result["error"]["code"] == "QR_FAILED"


def test_bili_qr_start_requires_installed_native_writer(monkeypatch, upstream):
    monkeypatch.setattr(
        admin, "bili_qr_http", lambda *a, **k: pytest.fail("未安装时不能创建二维码")
    )
    code, result, _ = invoke(monkeypatch, upstream, "bilibili_qr_start", "bilibili")
    assert code == 1 and result["error"]["code"] == "NEED_SETUP"


@pytest.mark.parametrize("operation", ["bilibili_qr_start", "bilibili_qr_poll"])
def test_bili_qr_not_available_for_other_channel(monkeypatch, upstream, operation):
    monkeypatch.setattr(
        admin, "bili_qr_http", lambda *a, **k: pytest.fail("其他渠道不能创建B站二维码")
    )
    code, result, _ = invoke(
        monkeypatch,
        upstream,
        operation,
        "github",
        {"qrcode_key": QR_KEY} if operation.endswith("poll") else {},
    )
    assert code == 1 and result["error"]["code"] == "BAD_INPUT"


@pytest.mark.parametrize("direct_cookies", [False, True])
def test_bili_qr_success_verifies_saves_and_marks_native_without_secret_output(
    monkeypatch, upstream, direct_cookies
):
    requested, saved = [], []
    ticket = "https://passport.biligame.com/x/passport-login/web/crossDomain?ticket=private-ticket"
    cookies = {"SESSDATA": "private-session", "bili_jct": "private-csrf", "DedeUserID": "123"}

    def http(url, **kwargs):
        requested.append((url, kwargs))
        if kwargs.get("ticket"):
            return {}, cookies.copy()
        return {
            "code": 0,
            "url": ticket,
            "refresh_token": "private-refresh",
        }, cookies.copy() if direct_cookies else {}

    monkeypatch.setattr(admin, "bili_qr_http", http)
    monkeypatch.setattr(admin, "save_bili_native", lambda _up, rows: saved.extend(rows))
    code, result, text = invoke(
        monkeypatch, upstream, "bilibili_qr_poll", "bilibili", {"qrcode_key": QR_KEY}
    )
    assert code == 0 and result["result"] == {
        "state": "authenticated",
        "verified": True,
        "saved": True,
    }
    assert {row["name"]: row["value"] for row in saved} == {
        **cookies,
        "ac_time_value": "private-refresh",
    }
    assert len(requested) == (1 if direct_cookies else 2)
    assert upstream.data["session_reuse"]["bilibili"]["source"] == "manual"
    assert not upstream.calls
    assert all(
        secret not in text
        for secret in [
            QR_KEY,
            "private-session",
            "private-csrf",
            "private-refresh",
            "private-ticket",
        ]
    )


@pytest.mark.parametrize("state", [None, True, 99999, -1])
def test_bili_qr_unknown_state_is_failure_not_success(monkeypatch, upstream, state):
    monkeypatch.setattr(admin, "bili_qr_http", lambda *a, **k: ({"code": state}, {}))
    monkeypatch.setattr(admin, "save_bili_native", lambda *a: pytest.fail("未知状态不能保存"))
    code, result, _ = invoke(
        monkeypatch, upstream, "bilibili_qr_poll", "bilibili", {"qrcode_key": QR_KEY}
    )
    assert code == 1 and result["error"]["code"] == "QR_FAILED"


def test_bili_qr_validation_failure_does_not_mark_or_leak(monkeypatch, upstream):
    monkeypatch.setattr(
        admin, "bili_qr_http", lambda *a, **k: ({"code": 0}, {"SESSDATA": "private-session"})
    )

    def fail(*args):
        raise admin.AdminError("VERIFICATION_FAILED")

    monkeypatch.setattr(admin, "save_bili_native", fail)
    code, result, text = invoke(
        monkeypatch, upstream, "bilibili_qr_poll", "bilibili", {"qrcode_key": QR_KEY}
    )
    assert code == 1 and result["error"]["code"] == "VERIFICATION_FAILED"
    assert "bilibili" not in upstream.data.get("session_reuse", {})
    assert "private-session" not in text


@pytest.mark.parametrize(
    "url",
    [
        "http://passport.biligame.com/x/passport-login/web/crossDomain",
        "https://evil.test/x/passport-login/web/crossDomain",
        "https://passport.biligame.com/other",
        "https://user@passport.biligame.com/x/passport-login/web/crossDomain",
        "https://passport.biligame.com:444/x/passport-login/web/crossDomain",
    ],
)
def test_bili_qr_ticket_restricts_exact_https_endpoints(monkeypatch, url):
    monkeypatch.setattr(admin, "build_opener", lambda *a: pytest.fail("域越界不能联网"))
    with pytest.raises(admin.AdminError) as caught:
        admin.bili_qr_http(url, ticket=True)
    assert caught.value.code == "QR_FAILED"


def test_bili_qr_http_reads_ticket_headers_without_following_redirect(monkeypatch):
    from email.message import Message
    from urllib.error import HTTPError

    headers = Message()
    headers.add_header(
        "Set-Cookie", "SESSDATA=private-session; Domain=.biligame.com; Secure; HttpOnly"
    )
    headers.add_header("Set-Cookie", "bili_jct=private-csrf; Path=/")
    headers.add_header("Location", "https://evil.test/steal")
    calls = []

    def open_request(request, timeout):
        calls.append(request.full_url)
        assert timeout == 15
        raise HTTPError(request.full_url, 302, "Found", headers, io.BytesIO())

    monkeypatch.setattr(admin, "build_opener", lambda handler: SimpleNamespace(open=open_request))
    data, cookies = admin.bili_qr_http(
        "https://passport.biligame.com/x/passport-login/web/crossDomain?ticket=private-ticket",
        ticket=True,
    )
    assert data == {} and cookies == {"SESSDATA": "private-session", "bili_jct": "private-csrf"}
    assert len(calls) == 1
    assert (
        admin.NoQrRedirect().redirect_request(None, None, 302, "", headers, "https://evil.test")
        is None
    )


def test_bili_qr_network_errors_are_fixed_and_secret_free(monkeypatch, upstream):
    def fail(request, timeout):
        raise OSError("private-ticket private-cookie")

    monkeypatch.setattr(admin, "build_opener", lambda *a: SimpleNamespace(open=fail))
    code, result, text = invoke(
        monkeypatch, upstream, "bilibili_qr_poll", "bilibili", {"qrcode_key": QR_KEY}
    )
    assert code == 1 and result["error"]["code"] == "QR_FAILED"
    assert "private-ticket" not in text and "private-cookie" not in text and QR_KEY not in text
