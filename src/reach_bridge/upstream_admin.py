"""固定 Agent-Reach 1.6.1 管理适配器；仅通过 stdin JSON 接收敏感内容。"""

import contextlib
import importlib
import ipaddress
import json
import math
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

SUPPORTED_VERSION = "1.6.1"
MAX_INPUT = 256 * 1024
CREDENTIAL_KEYS = {
    "github-token": "github_token",
    "groq-key": "groq_api_key",
    "openai-key": "openai_api_key",
}
OPERATIONS = {
    "describe",
    "credentials_status",
    "credential_delete",
    "browser_prepare",
    "browser_login",
    "cookie_import",
    "cookie_delete",
    "browser_finish",
    "browser_frame",
    "browser_input",
}
BILI_LOGIN_URL = "https://passport.bilibili.com/login"
MESSAGES = {
    "BAD_INPUT": "管理操作或参数不符合要求",
    "UNSUPPORTED_UPSTREAM": "管理适配器仅支持 Agent-Reach 1.6.1",
    "NEED_SETUP": "所需上游工具或专用浏览器尚未准备好",
    "NEED_LOGIN": "未取得有效登录凭据，请完成登录后再保存",
    "VERIFICATION_FAILED": "新凭据验证或保存未完成，未确认有效登录",
    "BROWSER_FAILED": "专用浏览器操作未完成",
    "BROWSER_PREPARE_FAILED": "系统 Chromium、OpenCLI 或浏览器扩展准备失败",
    "TARGET_UNAVAILABLE": "登录页面已关闭或进入未授权域，请重新打开登录页面",
    "BUSY": "上游浏览器正在执行其他操作",
    "TIMEOUT": "管理操作超过时限",
    "UPSTREAM_ERROR": "上游管理操作失败，未确认完成",
    "CONFIG_CONFLICT": "已有自定义 yt-dlp 配置，请管理员处理后再管理 YouTube Cookie",
}


class AdminError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(MESSAGES[code])


def require(condition, code="BAD_INPUT"):
    if not condition:
        raise AdminError(code)


def load_upstream():
    package = importlib.import_module("agent_reach")
    require(getattr(package, "__version__", None) == SUPPORTED_VERSION, "UNSUPPORTED_UPSTREAM")
    modules = {
        name: importlib.import_module("agent_reach.runtime." + name)
        for name in ("browser", "cdp", "manage", "sessions", "state", "tools")
    }
    modules["Config"] = importlib.import_module("agent_reach.config").Config
    modules["channels"] = list(importlib.import_module("agent_reach.channels").get_all_channels())
    modules["paths"] = importlib.import_module("agent_reach.utils.paths")
    return SimpleNamespace(**modules)


def channel_for(upstream, name):
    channel = next((item for item in upstream.channels if item.name == name), None)
    require(channel is not None)
    return channel


def cookie_domains(upstream, channel):
    if channel.name == "bilibili":
        return ["bilibili.com"]
    if channel.name == "youtube":
        return ["youtube.com"]
    return list(upstream.manage.site_domains(channel))


def login_url(channel):
    if channel.name == "bilibili":
        return BILI_LOGIN_URL
    if channel.name == "youtube":
        return "https://www.youtube.com/account"
    return channel.login_url


def login_domains(upstream, channel):
    domains = cookie_domains(upstream, channel)
    return domains + ["accounts.google.com"] if channel.name == "youtube" else domains


def domain_allowed(domain, allowed):
    if not isinstance(domain, str):
        return False
    host = domain.lstrip(".").lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
        return False
    return any(host == base or host.endswith("." + base) for base in allowed)


def parse_cookies(value, domains):
    """接受 Cookie 请求头或浏览器 JSON 导出，拒绝丢失归属和跨站条目。"""
    require(bool(domains))
    if isinstance(value, str):
        require(bool(value))
        if value.lstrip().startswith("["):
            try:
                value = json.loads(value)
            except ValueError as exc:
                raise AdminError("BAD_INPUT") from exc
        else:
            require(not any(ord(c) < 32 or ord(c) == 127 for c in value))
            rows = []
            for part in value.split(";"):
                name, sep, content = part.strip().partition("=")
                require(bool(sep))
                rows.append({"name": name, "value": content, "domain": "." + domains[0]})
            value = rows
    require(isinstance(value, list) and 0 < len(value) <= 500)
    result, seen = [], set()
    fields = {
        "name",
        "value",
        "domain",
        "path",
        "secure",
        "httpOnly",
        "expires",
        "expirationDate",
        "sameSite",
        "hostOnly",
        "session",
        "storeId",
        "id",
    }
    for item in value:
        require(isinstance(item, dict) and set(item) <= fields)
        name, content = item.get("name"), item.get("value")
        require(isinstance(name, str) and bool(re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)))
        require(isinstance(content, str) and len(content) <= 16384)
        require(not any(ord(c) < 32 or ord(c) == 127 for c in content))
        domain = item.get("domain")
        require(domain_allowed(domain, domains))
        path = item.get("path", "/")
        require(isinstance(path, str) and path.startswith("/") and len(path) <= 2048)
        require(not any(ord(c) < 32 or ord(c) == 127 for c in path))
        key = (name, domain.lower(), path)
        require(key not in seen)
        seen.add(key)
        cookie = {"name": name, "value": content, "domain": domain.lower(), "path": path}
        for flag in ("secure", "httpOnly"):
            require(type(item.get(flag, True)) is bool)
            cookie[flag] = item.get(flag, True)
        expiry = item.get("expires", item.get("expirationDate", -1))
        require(type(expiry) in (int, float) and math.isfinite(expiry))
        require(expiry <= 0 or expiry > time.time())
        if expiry > 0:
            cookie["expires"] = expiry
        if "sameSite" in item:
            same = {
                "no_restriction": "None",
                "unspecified": None,
                "lax": "Lax",
                "strict": "Strict",
                "None": "None",
                "Lax": "Lax",
                "Strict": "Strict",
            }
            require(isinstance(item["sameSite"], str) and item["sameSite"] in same)
            if same[item["sameSite"]]:
                cookie["sameSite"] = same[item["sameSite"]]
                require(cookie["sameSite"] != "None" or cookie["secure"])
        result.append(cookie)
    return result


def describe(upstream, name):
    channels = upstream.channels if name == "-" else [channel_for(upstream, name)]
    return {
        "channels": {
            channel.name: {
                "login_url": login_url(channel) or None,
                "domains": cookie_domains(upstream, channel),
                "cookie_import": bool(cookie_domains(upstream, channel)),
                "cookie_delete": channel.name == "youtube",
                "login": bool(login_url(channel)),
                "native_session": channel.session or None,
                "credential_keys": (
                    ["github-token"]
                    if channel.name == "github"
                    else ["groq-key", "openai-key"]
                    if channel.name == "youtube"
                    else ["groq-key"]
                    if channel.name == "xiaoyuzhou"
                    else []
                ),
            }
            for channel in channels
        },
        "credential_keys": list(CREDENTIAL_KEYS),
    }


def saved_browser_cookies(upstream):
    path = upstream.browser.browser_root() / "session.json"
    text = upstream.paths.read_small_text_no_follow(path, max_bytes=2 * 1024 * 1024)
    if text is None:
        return []
    data = json.loads(text)
    require(isinstance(data, dict) and isinstance(data.get("cookies", []), list), "UPSTREAM_ERROR")
    return data.get("cookies", [])


def youtube_layout(upstream):
    home = upstream.paths.home_dir().absolute()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config").absolute()
    require(config_home.is_relative_to(home), "CONFIG_CONFLICT")
    config = config_home / "yt-dlp" / "config"
    jar = upstream.state.root() / "sessions" / "bridge-youtube" / "cookies.txt"
    text = "# Reach-Bridge managed YouTube cookies\n--cookies " + shlex.quote(str(jar)) + "\n"
    return config, jar, text


def youtube_config_candidates(upstream):
    # 与固定 yt-dlp 的 portable/home/user/system 搜索位置保持一致，不执行配置内容。
    package = importlib.import_module("yt_dlp")
    utils = importlib.import_module("yt_dlp.utils")
    home = upstream.paths.home_dir()
    files = {
        Path(package.__file__).parent / "yt-dlp.conf",
        home / "yt-dlp.conf",
        Path.cwd() / "yt-dlp.conf",
    }
    directories = list(utils.get_user_config_dirs("yt-dlp"))
    directories += list(utils.get_system_config_dirs("yt-dlp"))
    for directory in map(Path, directories):
        files.update(
            {directory.parent / "yt-dlp.conf", directory / "config", directory / "config.txt"}
        )
        if directory.name.startswith("."):
            files.add(directory.parent / "yt-dlp.conf.txt")
    return files


def check_youtube_configuration(upstream):
    config, jar, expected = youtube_layout(upstream)
    for candidate in youtube_config_candidates(upstream) | {config}:
        if not candidate.exists() and not candidate.is_symlink():
            continue
        require(candidate == config and not candidate.is_symlink(), "CONFIG_CONFLICT")
        current = upstream.paths.read_small_text_no_follow(candidate, max_bytes=65536)
        require(current == expected, "CONFIG_CONFLICT")
    if jar.exists() or jar.is_symlink():
        require(not jar.is_symlink() and upstream.state.owned_path(jar), "CONFIG_CONFLICT")
    return config, jar, expected


def youtube_cookie_count(upstream):
    _, jar, _ = youtube_layout(upstream)
    text = upstream.paths.read_small_text_no_follow(jar, max_bytes=2 * 1024 * 1024)
    if text is None:
        return 0
    count = 0
    for line in text.splitlines():
        if line.startswith("#HttpOnly_"):
            line = line.removeprefix("#HttpOnly_")
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7 or not domain_allowed(parts[0], ["youtube.com"]):
            continue
        require(parts[4].isdigit(), "UPSTREAM_ERROR")
        if int(parts[4]) == 0 or int(parts[4]) > time.time():
            count += 1
    return count


def save_youtube_cookies(upstream, cookies):
    config, jar, expected = check_youtube_configuration(upstream)
    require(bool(cookies), "NEED_LOGIN")
    lines = ["# Netscape HTTP Cookie File", "# Reach-Bridge managed YouTube cookies"]
    for cookie in cookies:
        require(domain_allowed(cookie.get("domain"), ["youtube.com"]))
        domain = cookie["domain"]
        fields = [
            domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            cookie.get("path", "/"),
            "TRUE" if cookie.get("secure", True) else "FALSE",
            str(max(0, int(cookie.get("expires", 0)))),
            cookie["name"],
            cookie["value"],
        ]
        require(
            all(
                isinstance(value, str) and not any(c in value for c in "\r\n\t") for value in fields
            )
        )
        if cookie.get("httpOnly"):
            fields[0] = "#HttpOnly_" + fields[0]
        lines.append("\t".join(fields))
    upstream.paths.ensure_no_symlink_path(config, "yt-dlp 配置")
    upstream.paths.ensure_no_symlink_path(jar, "YouTube Cookie")
    upstream.paths.make_private_dir(config.parent)
    upstream.paths.make_private_dir(jar.parent)
    upstream.state.remember_path(jar, "Reach-Bridge YouTube cookies")
    created = not config.exists()
    if created:
        upstream.paths.atomic_write_private_text(config, expected)
    try:
        upstream.paths.atomic_write_private_text(jar, "\n".join(lines) + "\n")
    except Exception:
        # 首次导入未写入 jar 时撤回本次新配置；已有配置和旧 jar 均保持原样。
        if created:
            config.unlink()
        raise


def cookie_delete(upstream, channel, payload):
    require(channel.name == "youtube" and not payload)
    config, jar, _ = check_youtube_configuration(upstream)
    upstream.paths.ensure_no_symlink_path(jar, "YouTube Cookie")
    upstream.paths.ensure_no_symlink_path(config, "yt-dlp 配置")
    clear_youtube_browser_session(upstream)
    jar.unlink(missing_ok=True)
    config.unlink(missing_ok=True)
    mark_manual(upstream, channel, 0)
    return {"deleted": True, "saved": False}


def clear_youtube_browser_session(upstream):
    """清理现有专用 profile，不为退出操作安装工具或启动浏览器。"""
    if upstream.browser.is_running():
        upstream.browser.clear_site(["youtube.com"])
        return
    # Chromium 在不同发行构建中使用这两个 Cookie 数据库位置；只处理已存在的文件。
    default = upstream.browser.profile_dir() / "Default"
    where = "host_key = ? OR host_key = ? OR host_key LIKE ?"
    domains = ("youtube.com", ".youtube.com", "%.youtube.com")
    for database in (default / "Cookies", default / "Network" / "Cookies"):
        upstream.paths.ensure_no_symlink_path(database, "专用浏览器 Cookie 数据库")
        if not database.exists():
            continue
        require(database.is_file(), "UPSTREAM_ERROR")
        with contextlib.closing(
            sqlite3.connect(database.absolute().as_uri() + "?mode=rw", uri=True, timeout=5)
        ) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                count = connection.execute(
                    "SELECT count(*) FROM cookies WHERE " + where, domains
                ).fetchone()[0]
                deleted = connection.execute("DELETE FROM cookies WHERE " + where, domains)
                require(deleted.rowcount == count, "UPSTREAM_ERROR")
    snapshot = upstream.browser.browser_root() / "session.json"
    text = upstream.paths.read_small_text_no_follow(snapshot, max_bytes=2 * 1024 * 1024)
    if text is not None:
        saved = json.loads(text)
        require(
            isinstance(saved, dict) and isinstance(saved.get("cookies"), list), "UPSTREAM_ERROR"
        )
        saved["cookies"] = [
            row
            for row in saved["cookies"]
            if not isinstance(row, dict) or not domain_allowed(row.get("domain"), ["youtube.com"])
        ]
        upstream.paths.atomic_write_private_text(snapshot, json.dumps(saved, ensure_ascii=False))


def credentials_status(upstream, name):
    config = upstream.Config(read_only=True)
    keys = {key: bool(config.get(field)) for key, field in CREDENTIAL_KEYS.items()}
    channels = upstream.channels if name == "-" else [channel_for(upstream, name)]
    cookies = saved_browser_cookies(upstream)
    records = upstream.state.load().get("session_reuse", {})
    items = {}
    for channel in channels:
        domains = cookie_domains(upstream, channel)
        count = sum(
            1
            for row in cookies
            if isinstance(row, dict)
            and domain_allowed(row.get("domain"), domains)
            and isinstance(row.get("expires", -1), (float, int))
            and (row.get("expires", -1) <= 0 or row["expires"] > time.time())
        )
        record = records.get(channel.name, {})
        updated = record.get("at") if isinstance(record, dict) else None
        # 只接受上游时间格式，不把损坏状态里的任意字符串传给浏览器。
        if not isinstance(updated, str) or not re.fullmatch(
            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", updated
        ):
            updated = None
        native = bool(channel.session and upstream.sessions.has_login_state(channel.session))
        if channel.name == "youtube":
            native_count = youtube_cookie_count(upstream)
            native = native_count > 0
            count = native_count
        verified = bool(native and isinstance(record, dict) and record.get("verified") is True)
        items[channel.name] = {
            "cookie_count": count,
            "native_saved": native,
            "saved": native or count > 0,
            "updated_at": updated,
            "verified": verified,
            "verified_at": updated if verified else None,
        }
    return {"keys": keys, "channels": items}


def select_system_browser(upstream):
    # 只有新增浏览器管理要求非 root；查询、CLI 和凭据状态读取沿用既有运行条件。
    require(not hasattr(os, "getuid") or os.getuid() != 0, "NEED_SETUP")
    current = upstream.browser._load_state()
    previous = current.get("executable")
    if (
        current.get("backend") == "chrome"
        and isinstance(previous, str)
        and Path(previous).is_file()
        and os.access(previous, os.X_OK)
    ):
        return
    selected = os.environ.get("REACH_BRIDGE_BROWSER_EXECUTABLE", "")
    require(
        bool(selected)
        and Path(selected).is_absolute()
        and Path(selected).is_file()
        and os.access(selected, os.X_OK),
        "NEED_SETUP",
    )
    if upstream.browser.is_running():
        require(upstream.browser.stop(), "BROWSER_FAILED")
    upstream.browser.update_state(
        backend="chrome", executable=selected, launcher_python=None, fallback_reason=None
    )


def prepare_browser(upstream, *, visible):
    select_system_browser(upstream)
    require(bool(upstream.tools.resolve("opencli")), "NEED_SETUP")
    result = upstream.browser.setup(headless=not visible)
    require(isinstance(result, dict), "BROWSER_FAILED")


def browser_prepare(upstream, channel, payload):
    """显式管理员准备动作；必须在任何渠道 add 和登录前选择系统浏览器。"""
    require(not payload and bool(login_url(channel)))
    try:
        select_system_browser(upstream)
        # 官方 ensure 会先处理 OpenCLI 的 Node 依赖；不升级用户的不兼容已有版本。
        upstream.tools.ensure("opencli")
        require(bool(upstream.tools.resolve("opencli")), "NEED_SETUP")
        result = upstream.browser.setup(headless=True)
        require(isinstance(result, dict), "BROWSER_FAILED")
    except Exception as exc:
        raise AdminError("BROWSER_PREPARE_FAILED") from exc
    return {"prepared": True, "browser": "system", "opencli": True}


def mark_manual(upstream, channel, count, *, verified=False):
    now = upstream.state.now()

    def update(data):
        data.setdefault("session_reuse", {})[channel.name] = {
            "source": "manual",
            "at": now,
            "verified": verified,
            "copied_cookies": count,
            "cookie_snapshot": 1,
            "source_changed": False,
        }
        data.setdefault("session_revisions", {})[channel.name] = now
        data.setdefault("checks", {}).pop(channel.name, None)

    upstream.state.mutate(update)


def cdp_call(upstream, method, params=None, *, target_id=None):
    reply = upstream.cdp.call(method, params, target_id=target_id)
    require(isinstance(reply, dict), "BROWSER_FAILED")
    return reply


def safe_page_url(raw, domains):
    require(isinstance(raw, str), "TARGET_UNAVAILABLE")
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname or ""
        allowed = (
            parsed.scheme == "https"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and domain_allowed(host, domains)
        )
        try:
            ipaddress.ip_address(host)
            allowed = False
        except ValueError:
            pass
    except ValueError as exc:
        raise AdminError("TARGET_UNAVAILABLE") from exc
    require(allowed, "TARGET_UNAVAILABLE")
    return urlunsplit(("https", host, parsed.path or "/", "", ""))


def page_target(upstream, channel, target_id):
    require(isinstance(target_id, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", target_id)))
    require(upstream.browser.is_running(), "TARGET_UNAVAILABLE")
    upstream.browser._require_owned()
    pages = upstream.cdp._json("/json/list")
    require(isinstance(pages, list), "BROWSER_FAILED")
    page = next((p for p in pages if p.get("id") == target_id and p.get("type") == "page"), None)
    require(page is not None, "TARGET_UNAVAILABLE")
    safe = safe_page_url(page.get("url"), login_domains(upstream, channel))
    upstream.browser.touch()
    return page, safe


def browser_login(upstream, channel):
    url = login_url(channel)
    require(bool(url))
    prepare_browser(upstream, visible=True)
    previous_ids = {page.get("id") for page in (upstream.cdp._json("/json/list") or [])}
    upstream.browser.start(url)
    chosen = []

    def ready():
        pages = upstream.cdp._json("/json/list") or []
        # 官方 start 通常新开标签；优先绑定本次创建的页面，而不是同平台遗留标签。
        pages = sorted(pages, key=lambda page: page.get("id") in previous_ids)
        for page in pages:
            if page.get("type") != "page":
                continue
            try:
                safe = safe_page_url(page.get("url"), login_domains(upstream, channel))
            except AdminError:
                continue
            if page.get("id") in previous_ids and safe != url:
                continue
            chosen[:] = [page]
            return True
        return False

    require(upstream.browser.wait_for(ready, 5), "TARGET_UNAVAILABLE")
    target_id = chosen[0]["id"]
    cdp_call(
        upstream,
        "Emulation.setDeviceMetricsOverride",
        {
            "width": 1280,
            "height": 800,
            "deviceScaleFactor": 1,
            "mobile": False,
        },
        target_id=target_id,
    )
    return {
        "target_id": target_id,
        "url": safe_page_url(chosen[0]["url"], login_domains(upstream, channel)),
        "width": 1280,
        "height": 800,
    }


# 固定程序只在已安装的 bili-cli 环境运行；先验证，再由官方 writer 暂存并原子替换。
BILI_WRITER = """
import asyncio, contextlib, json, os, pathlib, sys, tempfile
from bili_cli import auth
from bilibili_api import Credential, user
data = json.load(sys.stdin)
credential = Credential(sessdata=data.get("SESSDATA", ""), bili_jct=data.get("bili_jct", ""),
    ac_time_value=data.get("ac_time_value", ""), buvid3=data.get("buvid3", ""),
    buvid4=data.get("buvid4", ""), dedeuserid=data.get("DedeUserID", ""))
async def validate():
    if not credential.sessdata:
        raise ValueError()
    info = await user.get_self_info(credential)
    if not isinstance(info, dict) or not info.get("mid"):
        raise ValueError()
with (open(os.devnull, "w") as silent, contextlib.redirect_stdout(silent),
      contextlib.redirect_stderr(silent)):
    asyncio.run(validate())
    destination = auth.CREDENTIAL_FILE
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.is_symlink() or destination.parent.is_symlink():
        raise ValueError()
    with tempfile.TemporaryDirectory(prefix=".bridge-", dir=destination.parent) as folder:
        auth.CONFIG_DIR = pathlib.Path(folder)
        auth.CREDENTIAL_FILE = auth.CONFIG_DIR / "credential.json"
        auth.save_credential(credential)
        os.replace(auth.CREDENTIAL_FILE, destination)
print('{"saved":true}')
"""


def save_bili_native(upstream, cookies):
    jar = {}
    for row in cookies:
        if row["name"] in jar:
            require(jar[row["name"]] == row["value"])
        jar[row["name"]] = row["value"]
    require(bool(jar.get("SESSDATA", "").strip()), "NEED_LOGIN")
    executable = upstream.tools.resolve("bili")
    require(bool(executable), "NEED_SETUP")
    python = Path(executable).with_name("python.exe" if os.name == "nt" else "python")
    require(python.is_file(), "NEED_SETUP")
    env = {key: value for key, value in os.environ.items() if not key.startswith("REACH_BRIDGE_")}
    env.update(upstream.sessions.session_env("bili-cli"))
    target = Path(env["HOME"]) / ".bilibili-cli" / "credential.json"
    upstream.paths.ensure_no_symlink_path(target, "原生凭据")
    try:
        result = subprocess.run(
            [str(python), "-c", BILI_WRITER],
            input=json.dumps(jar),
            text=True,
            capture_output=True,
            env=env,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdminError("VERIFICATION_FAILED") from exc
    require(result.returncode == 0, "VERIFICATION_FAILED")
    require(json.loads(result.stdout) == {"saved": True}, "UPSTREAM_ERROR")


def browser_cookies(upstream, domains):
    rows = cdp_call(upstream, "Storage.getCookies").get("cookies")
    require(isinstance(rows, list), "BROWSER_FAILED")
    return [
        row for row in rows if isinstance(row, dict) and domain_allowed(row.get("domain"), domains)
    ]


def replace_browser_cookies(upstream, cookies, domains):
    previous = browser_cookies(upstream, domains)
    fields = {"name", "value", "domain", "path", "secure", "httpOnly", "sameSite", "expires"}
    previous = [{k: v for k, v in row.items() if k in fields} for row in previous]
    incoming = {(row["name"], row["domain"], row.get("path", "/")) for row in cookies}
    stale = [
        {**row, "value": "", "expires": 1}
        for row in previous
        if (row["name"], row["domain"], row.get("path", "/")) not in incoming
    ]
    try:
        cdp_call(upstream, "Storage.setCookies", {"cookies": stale + cookies})
        upstream.browser.save_session()
    except Exception:
        # 站点快照写入失败时恢复原 Cookie，不把一次失败导入变成两个账号的混合状态。
        old = {(row["name"], row["domain"], row.get("path", "/")) for row in previous}
        undo = [
            {**row, "value": "", "expires": 1}
            for row in cookies
            if (row["name"], row["domain"], row.get("path", "/")) not in old
        ] + previous
        cdp_call(upstream, "Storage.setCookies", {"cookies": undo})
        upstream.browser.save_session()
        raise


def cookie_import(upstream, channel, payload):
    require(set(payload) == {"cookies"})
    domains = cookie_domains(upstream, channel)
    cookies = parse_cookies(payload["cookies"], domains)
    if channel.name == "bilibili":
        save_bili_native(upstream, cookies)
    elif channel.name == "youtube":
        save_youtube_cookies(upstream, cookies)
    else:
        prepare_browser(upstream, visible=False)
        replace_browser_cookies(upstream, cookies, domains)
    verified = channel.name == "bilibili"
    mark_manual(upstream, channel, len(cookies), verified=verified)
    return {
        "saved": True,
        "cookie_count": len(cookies),
        "verified": verified,
        "storage": "native" if verified else "yt-dlp" if channel.name == "youtube" else "browser",
    }


def browser_finish(upstream, channel, payload):
    require(set(payload) == {"target_id"})
    page_target(upstream, channel, payload["target_id"])
    cookies = browser_cookies(upstream, cookie_domains(upstream, channel))
    require(bool(cookies), "NEED_LOGIN")
    if channel.name == "bilibili":
        save_bili_native(upstream, cookies)
    elif channel.name == "youtube":
        save_youtube_cookies(upstream, cookies)
    if channel.name != "youtube":
        upstream.browser.save_session()
    verified = channel.name == "bilibili"
    mark_manual(upstream, channel, len(cookies), verified=verified)
    return {
        "saved": True,
        "cookie_count": len(cookies),
        "verified": verified,
        "storage": "native" if verified else "yt-dlp" if channel.name == "youtube" else "browser",
    }


def browser_frame(upstream, channel, payload):
    require(set(payload) == {"target_id"})
    _, safe = page_target(upstream, channel, payload["target_id"])
    screenshot = cdp_call(
        upstream,
        "Page.captureScreenshot",
        {
            "format": "png",
            "fromSurface": True,
            "captureBeyondViewport": False,
        },
        target_id=payload["target_id"],
    )
    image = screenshot.get("data")
    require(isinstance(image, str) and 0 < len(image) <= 8 * 1024 * 1024, "BROWSER_FAILED")
    return {
        "image": image,
        "url": safe,
        "width": 1280,
        "height": 800,
    }


def number(value, minimum, maximum):
    require(type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum)
    return value


def browser_input(upstream, channel, payload):
    action, target = payload.get("action"), payload.get("target_id")
    page_target(upstream, channel, target)
    base = {"action", "target_id"}
    if action == "click":
        require(set(payload) == base | {"x", "y"})
        point = {
            "x": number(payload["x"], 0, 1279),
            "y": number(payload["y"], 0, 799),
            "button": "left",
            "clickCount": 1,
        }
        for kind in ("mousePressed", "mouseReleased"):
            cdp_call(
                upstream, "Input.dispatchMouseEvent", {"type": kind, **point}, target_id=target
            )
    elif action == "type":
        require(set(payload) == base | {"text"})
        text = payload["text"]
        require(isinstance(text, str) and 0 < len(text) <= 4096 and "\x00" not in text)
        cdp_call(upstream, "Input.insertText", {"text": text}, target_id=target)
    elif action == "key":
        require(set(payload) == base | {"key"})
        keys = {
            "Enter": 13,
            "Tab": 9,
            "Escape": 27,
            "Backspace": 8,
            "Delete": 46,
            "ArrowLeft": 37,
            "ArrowUp": 38,
            "ArrowRight": 39,
            "ArrowDown": 40,
            "Home": 36,
            "End": 35,
            "PageUp": 33,
            "PageDown": 34,
            "Space": 32,
            "Control+A": 65,
        }
        key = payload["key"]
        require(isinstance(key, str) and key in keys)
        values = {
            "key": "a" if key == "Control+A" else " " if key == "Space" else key,
            "windowsVirtualKeyCode": keys[key],
            "nativeVirtualKeyCode": keys[key],
            "modifiers": 2 if key == "Control+A" else 0,
        }
        for kind in ("keyDown", "keyUp"):
            cdp_call(upstream, "Input.dispatchKeyEvent", {"type": kind, **values}, target_id=target)
    elif action == "scroll":
        require(base | {"delta"} <= set(payload) <= base | {"x", "y", "delta"})
        cdp_call(
            upstream,
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": number(payload.get("x", 640), 0, 1279),
                "y": number(payload.get("y", 400), 0, 799),
                "deltaX": 0,
                "deltaY": number(payload["delta"], -2000, 2000),
            },
            target_id=target,
        )
    elif action == "reload":
        require(set(payload) == base)
        cdp_call(upstream, "Page.reload", {}, target_id=target)
    else:
        raise AdminError("BAD_INPUT")
    return {"applied": True}


def execute(operation, channel_name, payload, upstream):
    require(operation in OPERATIONS and isinstance(payload, dict))
    if operation in {"describe", "credentials_status"}:
        require(not payload)
        return globals()[operation](upstream, channel_name)
    if operation == "credential_delete":
        require(
            set(payload) == {"key"}
            and isinstance(payload["key"], str)
            and payload["key"] in CREDENTIAL_KEYS
        )
        if channel_name != "-":
            allowed = describe(upstream, channel_name)["channels"][channel_name]["credential_keys"]
            require(payload["key"] in allowed)
        config = upstream.Config()
        config.delete(CREDENTIAL_KEYS[payload["key"]])
        return {"deleted": True, "configured": bool(config.get(CREDENTIAL_KEYS[payload["key"]]))}
    channel = channel_for(upstream, channel_name)
    if operation == "browser_login":
        require(not payload)
        return browser_login(upstream, channel)
    return globals()[operation](upstream, channel, payload)


def main(argv=None, stdin=None, stdout=None):
    argv = sys.argv[1:] if argv is None else argv
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    try:
        require(len(argv) == 2 and argv[0] in OPERATIONS)
        raw = stdin.read(MAX_INPUT + 1)
        require(len(raw) <= MAX_INPUT)
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except (ValueError, UnicodeError) as exc:
            raise AdminError("BAD_INPUT") from exc
        require(isinstance(payload, dict))
        # 上游日志可能含账号状态；协议 stdout 和错误正文只由此文件输出。
        with (
            open(os.devnull, "w") as silent,
            contextlib.redirect_stdout(silent),
            contextlib.redirect_stderr(silent),
        ):
            upstream = load_upstream()
            result = execute(argv[0], argv[1], payload, upstream)
        envelope = {"schema_version": 1, "ok": True, "result": result, "error": None}
    except Exception as exc:
        code = exc.code if isinstance(exc, AdminError) else getattr(exc, "code", "UPSTREAM_ERROR")
        if not isinstance(code, str) or code not in MESSAGES:
            code = "UPSTREAM_ERROR"
        if isinstance(exc, ModuleNotFoundError):
            code = "NEED_SETUP"
        envelope = {
            "schema_version": 1,
            "ok": False,
            "result": {},
            "error": {"code": code, "message": MESSAGES[code]},
        }
    stdout.write(json.dumps(envelope, ensure_ascii=False) + "\n")
    return 0 if envelope["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
