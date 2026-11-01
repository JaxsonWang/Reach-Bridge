#!/usr/bin/env python3
"""管理员维护入口：解析正式 release、构建镜像、备份数据和显式回滚。"""

import argparse
import datetime
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = "Panniantong/Agent-Reach"
ROOT = Path(__file__).resolve().parents[1]


def version(value: str) -> str:
    if not re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value):
        raise ValueError("版本须为正式 release，例如 1.6.1 或 v1.6.1")
    return value.removeprefix("v")


def release(value: str) -> dict:
    suffix = "latest" if value == "latest" else "tags/v" + version(value)
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases/{suffix}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "Reach-Bridge"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.load(response)
    resolved = version(data["tag_name"])
    if data.get("draft") or data.get("prerelease"):
        raise ValueError("只使用正式 release")
    expected = f"https://github.com/{REPO}/releases/download/v{resolved}/agent-reach.zip"
    if not any(a.get("browser_download_url") == expected for a in data.get("assets", [])):
        raise ValueError("该 release 缺少预期的 agent-reach.zip 资产")
    return {"version": resolved, "url": data["html_url"]}


def atomic_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def preserve_owner(source: Path, destination: Path) -> None:
    original = source.stat(follow_symlinks=False)
    copied = destination.stat(follow_symlinks=False)
    if (original.st_uid, original.st_gid) != (copied.st_uid, copied.st_gid):
        os.chown(destination, original.st_uid, original.st_gid, follow_symlinks=False)


def copy_home(source: Path, destination: Path) -> None:
    """复制数据与属主，不跟随工具环境的符号链接。"""
    shutil.copytree(source, destination, symlinks=True)
    for directory, dirs, files in os.walk(source, followlinks=False):
        original = Path(directory)
        target = destination / original.relative_to(source)
        for name in dirs + files:
            preserve_owner(original / name, target / name)
        preserve_owner(original, target)


class Maintenance:
    def __init__(self, root: Path):
        self.root = root
        self.state = root / ".upstream-state"
        self.home = root / "data/home"

    def current(self) -> str:
        text = (self.root / "upstream.env").read_text(encoding="utf-8").strip()
        if not text.startswith("AGENT_REACH_VERSION="):
            raise ValueError("upstream.env 缺少 AGENT_REACH_VERSION")
        return version(text.split("=", 1)[1])

    def write_version(self, value: str) -> None:
        temporary = self.root / "upstream.env.tmp"
        temporary.write_text(f"AGENT_REACH_VERSION={version(value)}\n", encoding="utf-8")
        temporary.replace(self.root / "upstream.env")

    def compose(self, value: str, *args: str, capture: bool = False) -> str:
        env = dict(os.environ, AGENT_REACH_VERSION=version(value))
        build_config = ["-f", str(self.root / "compose.build.yaml")] if args[0] == "build" else []
        completed = subprocess.run(
            [
                "docker",
                "compose",
                "--project-directory",
                str(self.root),
                "--env-file",
                str(self.root / ".env"),
                "--env-file",
                str(self.root / "upstream.env"),
                "-f",
                str(self.root / "compose.yaml"),
                *build_config,
                *args,
            ],
            cwd=self.root,
            env=env,
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else None,
        )
        return completed.stdout or ""

    def image_id(self, value: str) -> str:
        images = self.compose(value, "config", "--images", capture=True).split()
        if len(images) != 1:
            raise RuntimeError("Compose 配置应解析为一个服务镜像")
        return subprocess.check_output(
            ["docker", "image", "inspect", "--format={{.Id}}", images[0]],
            text=True,
        ).strip()

    def deployed_image(self, value: str) -> str:
        containers = self.compose(
            value, "ps", "--all", "--quiet", "reach-bridge", capture=True
        ).split()
        if len(containers) != 1:
            raise RuntimeError("应有一个已部署的服务容器，请先按 README 完成部署")
        return subprocess.check_output(
            ["docker", "container", "inspect", "--format={{.Image}}", containers[0]], text=True
        ).strip()

    def update(self, target: str) -> None:
        pending = self.state / "pending.json"
        if pending.exists():
            raise RuntimeError("上次维护未完成，请先运行 rollback；不要继续覆盖恢复点")
        old = self.current()
        if target == old:
            print(f"已固定为 {target}；未构建或修改数据")
            return
        if not self.home.is_dir() or self.home.is_symlink():
            raise RuntimeError("data/home 必须是已初始化的真实目录")
        if not (self.home / ".agent-reach/state.json").is_file():
            raise RuntimeError("先完成首次 agent-reach setup，再执行版本升级")
        old_image = self.image_id(old)
        if self.deployed_image(old) != old_image:
            raise RuntimeError("实际容器镜像与 upstream.env 不一致，停止升级，请先核对部署版本")
        self.compose(target, "build", "--pull", "reach-bridge")
        candidate_image = self.image_id(target)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup = self.root / "backups" / stamp
        backup.mkdir(parents=True, mode=0o700)
        backup.parent.chmod(0o700)
        record = {
            "old": old,
            "new": target,
            "old_image": old_image,
            "new_image": candidate_image,
            "backup": str(backup.relative_to(self.root)),
            "phase": "backup",
        }
        atomic_json(pending, record)
        try:
            self.compose(old, "stop", "reach-bridge")
            copy_home(self.home, backup / "home")
            record["phase"] = "upgrade"
            atomic_json(pending, record)
            output = self.compose(
                target,
                "run",
                "--rm",
                "--no-deps",
                "reach-bridge",
                "agent-reach",
                "update",
                "--sync-only",
                capture=True,
            )
            result = json.loads(output)
            atomic_json(backup / "sync.json", result)
            if not result.get("ok") or not isinstance(result.get("result"), dict):
                raise RuntimeError("上游状态同步失败，详见备份目录的 sync.json")
            sync = result["result"]
            if sync.get("problems"):
                raise RuntimeError("上游报告渠道工具升级失败，详见 sync.json")
            self.compose(
                target, "up", "-d", "--no-build", "--wait", "--wait-timeout", "90", "reach-bridge"
            )
            self.write_version(target)
            atomic_json(self.state / "rollback.json", record)
            pending.unlink()
            print(f"已切换到 Agent-Reach {target}；备份：{record['backup']}")
            for action in sync.get("user_actions", []):
                print("需要处理：", action)
        except BaseException:
            print(
                "升级未完成，恢复点已保留。请执行 python3 scripts/upstream.py rollback。",
                file=sys.stderr,
            )
            raise

    def rollback(self) -> None:
        pending = self.state / "pending.json"
        source = pending if pending.exists() else self.state / "rollback.json"
        record = json.loads(source.read_text(encoding="utf-8"))
        old, new = version(record["old"]), version(record["new"])
        if self.current() not in (old, new):
            raise RuntimeError("当前版本已另行修改，不应用此恢复点")
        backup = (self.root / record["backup"]).resolve()
        if not backup.is_relative_to((self.root / "backups").resolve()) or (
            record["phase"] != "backup" and not (backup / "home").is_dir()
        ):
            raise RuntimeError("恢复点路径无效或 HOME 快照缺失")
        if self.image_id(old) != record["old_image"]:
            raise RuntimeError(
                "旧镜像已被替换，停止回滚；请先恢复记录中的镜像，不使用同名新镜像代替"
            )
        if record["phase"] == "backup":
            # 备份失败时原 HOME 未改动，不得套用半份快照或上一次升级的记录。
            self.compose(
                old, "up", "-d", "--no-build", "--wait", "--wait-timeout", "90", "reach-bridge"
            )
            source.replace(backup / "cancelled.json")
            print("已恢复原服务；备份未完成，原 HOME 未替换")
            return
        self.compose(new, "stop", "reach-bridge")
        staging = self.home.with_name("home.restore-" + backup.name)
        displaced = self.home.with_name("home.displaced-" + backup.name)
        stage = record.get("restore_stage", "copying")
        if stage == "copying":
            if staging.is_symlink():
                raise RuntimeError("恢复暂存目录不得是软链接")
            if staging.exists():
                # 只清理本恢复点生成、可由快照重新复制的半成品，不动当前 HOME。
                shutil.rmtree(staging)
            copy_home(backup / "home", staging)
            record["restore_stage"] = stage = "prepared"
            atomic_json(source, record)
        if stage == "prepared":
            if not displaced.exists():
                self.home.rename(displaced)
            elif self.home.exists():
                raise RuntimeError("恢复路径状态冲突，请检查当前 HOME 与 displaced 目录")
            record["restore_stage"] = stage = "displaced"
            atomic_json(source, record)
        if stage == "displaced":
            if staging.exists() and not self.home.exists():
                staging.rename(self.home)
            elif staging.exists() or not self.home.exists():
                raise RuntimeError("恢复暂存目录与 HOME 状态不一致")
            record["restore_stage"] = "installed"
            atomic_json(source, record)
        self.write_version(old)
        self.compose(
            old, "up", "-d", "--no-build", "--wait", "--wait-timeout", "90", "reach-bridge"
        )
        source.replace(backup / "restored.json")
        # pending 的失败升级不能留下指向更早升级的快捷回滚记录。
        if pending == source:
            previous = self.state / "rollback.json"
            if previous.exists():
                previous.replace(backup / "previous-rollback.json")
        print(f"已恢复 Agent-Reach {old} 及其 HOME 快照；升级后数据保留在 {displaced}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "update", "rollback"])
    parser.add_argument("version", nargs="?", default="latest")
    args = parser.parse_args()
    os.umask(0o077)
    maintenance = Maintenance(ROOT)
    maintenance.state.mkdir(exist_ok=True, mode=0o700)
    try:
        with (maintenance.state / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.action == "check":
                latest = release("latest")
                print(
                    json.dumps(
                        {
                            "current": maintenance.current(),
                            "latest": latest["version"],
                            "release": latest["url"],
                        },
                        ensure_ascii=False,
                    )
                )
            elif args.action == "update":
                maintenance.update(release(args.version)["version"])
            else:
                maintenance.rollback()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        # 不打印子进程捕获的 stdout；它可能包含账号状态。
        print(f"维护失败：{exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
