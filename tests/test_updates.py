import io
import json
import subprocess
from pathlib import Path

import pytest

from scripts.upstream import Maintenance, preserve_owner, release, version


class FakeMaintenance(Maintenance):
    def __init__(self, root: Path):
        super().__init__(root)
        self.state.mkdir()
        self.home.mkdir(parents=True)
        (self.home / ".agent-reach").mkdir()
        (self.home / ".agent-reach/state.json").write_text("original")
        (root / "upstream.env").write_text("AGENT_REACH_VERSION=1.6.1\n")
        self.calls = []
        self.fail = None
        self.problem = False
        self.replaced_image = False

    def image_id(self, value):
        return ("replacement-" if self.replaced_image else "fixture-") + value

    def deployed_image(self, value):
        return "fixture-" + value

    def compose(self, value, *args, capture=False):
        self.calls.append((value, args))
        if args[0] == self.fail:
            raise subprocess.CalledProcessError(1, ["docker", "compose", args[0]])
        if args[0] == "run":
            (self.home / ".agent-reach/state.json").write_text("changed-by-sync")
            return json.dumps(
                {
                    "ok": True,
                    "result": {
                        "problems": ["fixture-failure"] if self.problem else [],
                        "user_actions": [],
                    },
                }
            )
        return ""


def test_update_then_rollback_preserves_data_and_images(tmp_path):
    maintenance = FakeMaintenance(tmp_path)
    (maintenance.home / "tool-link").symlink_to(".agent-reach/state.json")
    maintenance.update("1.7.0")
    assert maintenance.current() == "1.7.0"
    assert maintenance.calls[0] == ("1.7.0", ("build", "--pull", "reach-bridge"))
    assert maintenance.calls[1] == ("1.6.1", ("stop", "reach-bridge"))
    assert (maintenance.home / ".agent-reach/state.json").read_text() == "changed-by-sync"
    record = json.loads((maintenance.state / "rollback.json").read_text())
    backup = tmp_path / record["backup"]
    assert (backup / "home/.agent-reach/state.json").read_text() == "original"
    assert (backup / "home/tool-link").is_symlink()
    assert not (maintenance.state / "pending.json").exists()
    maintenance.rollback()
    assert maintenance.current() == "1.6.1"
    assert (maintenance.home / ".agent-reach/state.json").read_text() == "original"
    displaced = maintenance.home.with_name("home.displaced-" + backup.name)
    assert (displaced / ".agent-reach/state.json").read_text() == "changed-by-sync"
    assert not (maintenance.state / "rollback.json").exists()


def test_failed_build_does_not_stop_service_or_change_data(tmp_path):
    maintenance = FakeMaintenance(tmp_path)
    maintenance.fail = "build"
    with pytest.raises(subprocess.CalledProcessError):
        maintenance.update("1.7.0")
    assert len(maintenance.calls) == 1
    assert maintenance.current() == "1.6.1"
    assert (maintenance.home / ".agent-reach/state.json").read_text() == "original"


def test_deployed_image_drift_does_not_mutate_deployment(tmp_path):
    maintenance = FakeMaintenance(tmp_path)
    maintenance.replaced_image = True
    with pytest.raises(RuntimeError, match="实际容器镜像"):
        maintenance.update("1.7.0")
    assert not maintenance.calls


def test_build_override_is_explicit_and_image_comes_from_compose(tmp_path, monkeypatch):
    from types import SimpleNamespace

    maintenance = Maintenance(tmp_path)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout="ghcr.io/jaxsonwang/reach-bridge:0.1.0-agent-reach-1.6.1\n")

    monkeypatch.setattr(subprocess, "run", run)
    maintenance.compose("1.6.1", "build", "reach-bridge")
    assert str(tmp_path / "compose.build.yaml") in commands[-1]
    maintenance.compose("1.6.1", "up", "-d")
    assert str(tmp_path / "compose.build.yaml") not in commands[-1]
    inspected = []
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda command, **kwargs: inspected.append(command) or "fixture-image",
    )
    assert maintenance.image_id("1.6.1") == "fixture-image"
    assert commands[-1][-2:] == ["config", "--images"]
    assert inspected[-1][-1] == "ghcr.io/jaxsonwang/reach-bridge:0.1.0-agent-reach-1.6.1"


@pytest.mark.parametrize("failure", ["sync", "startup"])
def test_failed_upgrade_leaves_explicit_recovery_point(tmp_path, failure):
    maintenance = FakeMaintenance(tmp_path)
    maintenance.problem = failure == "sync"
    maintenance.fail = "up" if failure == "startup" else None
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        maintenance.update("1.7.0")
    assert maintenance.current() == "1.6.1"
    assert (maintenance.state / "pending.json").is_file()
    with pytest.raises(RuntimeError, match="上次维护"):
        maintenance.update("1.8.0")
    maintenance.problem = False
    maintenance.fail = None
    maintenance.rollback()
    assert (maintenance.home / ".agent-reach/state.json").read_text() == "original"
    assert not (maintenance.state / "pending.json").exists()


def test_rollback_health_failure_can_be_retried_without_replacing_snapshot(tmp_path):
    maintenance = FakeMaintenance(tmp_path)
    maintenance.update("1.7.0")
    maintenance.fail = "up"
    with pytest.raises(subprocess.CalledProcessError):
        maintenance.rollback()
    assert maintenance.current() == "1.6.1"
    maintenance.fail = None
    maintenance.rollback()
    assert (maintenance.home / ".agent-reach/state.json").read_text() == "original"


def test_rollback_refuses_replaced_image(tmp_path):
    maintenance = FakeMaintenance(tmp_path)
    maintenance.update("1.7.0")
    maintenance.replaced_image = True
    before = len(maintenance.calls)
    with pytest.raises(RuntimeError, match="旧镜像"):
        maintenance.rollback()
    assert len(maintenance.calls) == before


def test_same_version_is_noop(tmp_path):
    maintenance = FakeMaintenance(tmp_path)
    maintenance.update("1.6.1")
    assert not maintenance.calls


def test_backup_failure_does_not_apply_previous_upgrade_snapshot(tmp_path, monkeypatch):
    maintenance = FakeMaintenance(tmp_path)
    maintenance.update("1.7.0")

    def fail_copy(source, destination):
        raise OSError("fixture-disk-full")

    with monkeypatch.context() as patch:
        patch.setattr("scripts.upstream.copy_home", fail_copy)
        with pytest.raises(OSError):
            maintenance.update("1.8.0")
    pending = json.loads((maintenance.state / "pending.json").read_text())
    assert pending["phase"] == "backup"
    maintenance.rollback()
    assert maintenance.current() == "1.7.0"
    assert (maintenance.home / ".agent-reach/state.json").read_text() == "changed-by-sync"
    assert (maintenance.state / "rollback.json").exists()


def test_restore_copy_failure_keeps_current_home_and_can_retry(tmp_path, monkeypatch):
    maintenance = FakeMaintenance(tmp_path)
    maintenance.update("1.7.0")

    def fail_copy(source, destination):
        destination.mkdir()
        (destination / "partial").write_text("incomplete")
        raise OSError("fixture-disk-full")

    with monkeypatch.context() as patch:
        patch.setattr("scripts.upstream.copy_home", fail_copy)
        with pytest.raises(OSError):
            maintenance.rollback()
    assert (maintenance.home / ".agent-reach/state.json").read_text() == "changed-by-sync"
    maintenance.rollback()
    assert (maintenance.home / ".agent-reach/state.json").read_text() == "original"


@pytest.mark.parametrize("point", ["displaced", "installed"])
def test_restore_can_resume_after_atomic_rename_interruption(tmp_path, monkeypatch, point):
    maintenance = FakeMaintenance(tmp_path)
    maintenance.update("1.7.0")
    original = Path.rename

    def interrupted(path, target):
        result = original(path, target)
        if (point == "displaced" and "home.displaced-" in target.name) or (
            point == "installed" and path.name.startswith("home.restore-")
        ):
            raise KeyboardInterrupt("fixture-interrupted")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", interrupted)
        with pytest.raises(KeyboardInterrupt):
            maintenance.rollback()
    maintenance.rollback()
    assert maintenance.current() == "1.6.1"
    assert (maintenance.home / ".agent-reach/state.json").read_text() == "original"


def test_owner_preservation_does_not_follow_links(monkeypatch):
    from types import SimpleNamespace

    source = SimpleNamespace(stat=lambda **kwargs: SimpleNamespace(st_uid=1000, st_gid=1001))
    target = SimpleNamespace(stat=lambda **kwargs: SimpleNamespace(st_uid=0, st_gid=0))
    calls = []
    monkeypatch.setattr("os.chown", lambda *args, **kwargs: calls.append((args, kwargs)))
    preserve_owner(source, target)
    assert calls == [((target, 1000, 1001), {"follow_symlinks": False})]


@pytest.mark.parametrize("invalid", ["../main", "latest", "1.6", "1.6.1;id", "1.6.1rc1"])
def test_version_input_rejects_non_release(invalid):
    with pytest.raises(ValueError):
        version(invalid)


def test_resolve_latest_to_fixed_release(monkeypatch):
    data = {
        "tag_name": "v1.6.1",
        "html_url": "https://github.com/Panniantong/Agent-Reach/releases/tag/v1.6.1",
        "assets": [
            {
                "browser_download_url": "https://github.com/Panniantong/Agent-Reach/releases/download/v1.6.1/agent-reach.zip"
            }
        ],
    }
    calls = []

    def open_url(request, timeout):
        calls.append(request.full_url)
        return io.BytesIO(json.dumps(data).encode())

    monkeypatch.setattr("urllib.request.urlopen", open_url)
    assert release("latest")["version"] == "1.6.1"
    assert calls == ["https://api.github.com/repos/Panniantong/Agent-Reach/releases/latest"]
    data["prerelease"] = True
    with pytest.raises(ValueError):
        release("latest")
    data["prerelease"] = False
    data["assets"] = []
    with pytest.raises(ValueError, match="资产"):
        release("1.6.1")
