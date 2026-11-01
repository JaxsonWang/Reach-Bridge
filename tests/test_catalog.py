"""动态动作的声明、显式授权和命令行参数契约。"""

from dataclasses import replace

import pytest

from reach_bridge.catalog import Catalog
from reach_bridge.config import ChannelPolicy
from reach_bridge.runner import BridgeError


def catalog(settings):
    value = Catalog(settings)
    value.load(
        "1.6.1",
        [
            {
                "name": "bilibili",
                "description": "测试",
                "actions": [
                    {
                        "name": "hot",
                        "params": [{"name": "limit", "type": "int", "required": False}],
                    },
                    {
                        "name": "read",
                        "params": [{"name": "target", "type": "str", "required": True}],
                    },
                    {
                        "name": "comments",
                        "params": [{"name": "target", "type": "str", "required": True}],
                    },
                    {"name": "execute", "params": []},
                ],
            }
        ],
    )
    return value


def test_no_separator_without_positional_args(settings):
    value = catalog(settings)
    assert value.build_args("bilibili", "hot", {"limit": 3}) == [
        "bilibili",
        "hot",
        "--timeout",
        "60",
        "--limit=3",
    ]
    assert value.build_args("bilibili", "hot", {})[-1] == "60"
    for limit in (True, "3", 0, 51):
        with pytest.raises(BridgeError):
            value.build_args("bilibili", "hot", {"limit": limit})


def test_comments_read_allowed_unknown_actions_not_implicitly_authorized(settings):
    value = catalog(settings)
    assert value.action("bilibili", "comments")["name"] == "comments"
    with pytest.raises(BridgeError):
        value.action("bilibili", "execute")
    value.set_policy("bilibili", ChannelPolicy(hosts=["www.bilibili.com"], actions=["read"]))
    with pytest.raises(BridgeError):
        value.action("bilibili", "comments")


def test_canonical_policy_seeds_once_and_disabled_admin_uses_legacy(settings, tmp_path):
    configured = replace(
        settings,
        admin_token="placeholder-admin-token-longer-than-32-characters",
        admin_origin="https://reach.example.com",
        state_dir=tmp_path / "private",
    )
    value = catalog(configured)
    value.set_policy("bilibili", ChannelPolicy(hosts=["www.bilibili.com"], enabled=False))
    assert Catalog(configured).policies["bilibili"].enabled is False
    assert Catalog(settings).policies["bilibili"].enabled is True
