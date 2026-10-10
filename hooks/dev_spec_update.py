#!/usr/bin/env python3
"""dev-spec self-update. Runs from an async SessionStart hook (silent) or `install.sh update` (verbose).

Order of operations — the new version is validated *before* anything live changes:
  1. throttle (default 6h) and a lock, so concurrent sessions don't race;
  2. `git fetch` the source repo recorded in the install manifest and pick the target:
     channel "stable" (default) = the highest release tag vX.Y.Z; channel "main" = the tracked branch;
  3. skip unless the target is a clean fast-forward of the local branch (never touches a dirty
     repo, local unpushed commits, a diverged history or another branch); a local branch that
     already contains the target counts as up to date;
  4. optionally require the new tip to carry a valid signature;
  5. check the new tip out into a temporary worktree and run its device validation there
     (`validate.sh --device`: static checks, guard tests, install round trip; no node needed);
  6. only if that passes: `merge --ff-only`, then re-run the installer with the options
     recorded at install time (one-time migrations such as retiring legacy rules are not repeated).

Every outcome is recorded in <claude-home>/dev-spec-update.json and appended to
dev-spec-update.log; failures never raise into the session.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
MANIFEST = HOME / ".dev-spec-manifest.json"
STATE = HOME / "dev-spec-update.json"
LOG = HOME / "dev-spec-update.log"
LOCK = HOME / ".dev-spec-update.lock"
LOG_MAX = 200_000


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str, verbose: bool) -> None:
    line = f"[{now()}] {msg}"
    if verbose:
        print(line)
    try:
        if LOG.exists() and LOG.stat().st_size > LOG_MAX:
            LOG.write_text(LOG.read_text()[-LOG_MAX // 2:])
        with LOG.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def save_state(**fields) -> None:
    try:
        state = json.loads(STATE.read_text()) if STATE.exists() else {}
    except ValueError:
        state = {}
    state.update(fields)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def git(repo: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout, env=env)


def out(repo: Path, *args: str) -> str:
    p = git(repo, *args)
    return p.stdout.rstrip("\n") if p.returncode == 0 else ""


def finish(result: str, verbose: bool, **extra) -> int:
    save_state(last_check=now(), last_result=result, **extra)
    log(result if not extra else f"{result} {json.dumps(extra, ensure_ascii=False)}", verbose)
    return 0


def run(force: bool, verbose: bool) -> int:
    try:
        manifest = json.loads(MANIFEST.read_text())
    except (OSError, ValueError):
        return 0                                    # not installed: nothing to do, stay silent
    opts = manifest.get("options", {})
    if not opts.get("auto_update", False) and not force:
        return 0
    interval_h = float(os.environ.get("DEV_SPEC_UPDATE_INTERVAL_HOURS", opts.get("update_interval_hours", 6)))
    try:
        last = json.loads(STATE.read_text()).get("last_check_epoch", 0) if STATE.exists() else 0
    except ValueError:
        last = 0
    if not force and time.time() - last < interval_h * 3600:
        return 0

    try:
        LOCK.mkdir()
    except FileExistsError:
        if time.time() - LOCK.stat().st_mtime < 1800:
            return 0                                # another session is updating
        shutil.rmtree(LOCK, ignore_errors=True)     # stale lock from a killed run
        LOCK.mkdir()
    try:
        save_state(last_check_epoch=time.time())
        return update(manifest, opts, verbose)
    except Exception as e:                          # never break a session
        return finish(f"失败：{type(e).__name__}: {e}", verbose)
    finally:
        shutil.rmtree(LOCK, ignore_errors=True)


SEMVER_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def latest_release_tag(repo: Path) -> str:
    """Highest vX.Y.Z tag (pre-release / malformed tags are ignored)."""
    tags = [t for t in out(repo, "tag", "--list", "v*").splitlines() if SEMVER_TAG.match(t)]
    return max(tags, key=lambda t: tuple(int(x) for x in SEMVER_TAG.match(t).groups()), default="")


def describe(repo: Path) -> str:
    return out(repo, "describe", "--tags", "--always", "--match", "v[0-9]*") or out(repo, "rev-parse", "--short", "HEAD")


def update(manifest: dict, opts: dict, verbose: bool) -> int:
    repo = Path(manifest.get("source", ""))
    if not (repo / ".git").exists():
        return finish(f"跳过：规范源不可达或不是 git 仓库（{repo}）", verbose)
    branch = out(repo, "symbolic-ref", "-q", "--short", "HEAD")
    upstream = out(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if not branch or not upstream:
        return finish("跳过：规范仓库不在有上游的分支上", verbose)
    channel = opts.get("update_channel", "stable")
    fetch = git(repo, "fetch", "--quiet", "--tags", upstream.split("/", 1)[0], timeout=60)
    if fetch.returncode != 0:
        return finish(f"跳过：fetch 失败（{fetch.stderr.strip()[:200]}）", verbose)
    local = out(repo, "rev-parse", "HEAD")
    if channel == "main":
        label, remote = upstream, out(repo, "rev-parse", "@{u}")
    else:
        label = latest_release_tag(repo)
        if not label:
            return finish("跳过：还没有发布 tag（vX.Y.Z）", verbose, channel=channel)
        remote = out(repo, "rev-parse", f"{label}^{{commit}}")
    if git(repo, "merge-base", "--is-ancestor", remote, local).returncode == 0:
        return finish("已是最新", verbose, version=describe(repo), channel=channel)
    if git(repo, "merge-base", "--is-ancestor", local, remote).returncode != 0:
        return finish("跳过：本地有未推送的提交或历史已分叉，不自动更新", verbose, local=local[:10], remote=remote[:10])
    if out(repo, "status", "--porcelain", "--untracked-files=no"):
        return finish("跳过：规范仓库有未提交改动", verbose, local=local[:10], remote=remote[:10])
    if opts.get("update_require_signed"):
        verify = git(repo, "verify-tag", label) if channel != "main" else git(repo, "verify-commit", remote)
        if verify.returncode != 0:
            return finish("拒绝：新版本没有有效签名", verbose, target=label)

    # validate the candidate in an isolated checkout before anything live changes
    tmp = Path(tempfile.mkdtemp(prefix="dev-spec-candidate-"))
    cand = tmp / "src"
    try:
        add = git(repo, "worktree", "add", "--detach", "--quiet", str(cand), remote)
        if add.returncode != 0:
            return finish(f"失败：无法检出候选版本（{add.stderr.strip()[:200]}）", verbose)
        # device subset (static + guard + install round trip): needs no node; the full suite gates the release in CI
        cmd = os.environ.get("DEV_SPEC_UPDATE_VALIDATE_CMD", "bash scripts/validate.sh --device")
        v = subprocess.run(cmd, shell=True, cwd=cand, capture_output=True, text=True, timeout=900)
        if v.returncode != 0:
            tail = (v.stdout + v.stderr).strip().splitlines()[-8:]
            return finish("拒绝：候选版本未通过校验，保持当前版本", verbose, remote=remote[:10], validation_tail=tail)
    finally:
        git(repo, "worktree", "remove", "--force", str(cand))
        shutil.rmtree(tmp, ignore_errors=True)

    merge = git(repo, "merge", "--ff-only", "--quiet", remote)
    if merge.returncode != 0:
        return finish(f"失败：fast-forward 失败（{merge.stderr.strip()[:200]}）", verbose)
    args = [sys.executable, str(repo / "scripts/dev_spec_install.py"), "install", "--apply", "--claude-home", str(HOME),
            "--link" if manifest.get("mode") == "link" else "--copy"]
    if opts.get("no_hooks"):
        args.append("--no-hooks")
    inst = subprocess.run(args, capture_output=True, text=True, timeout=300)
    if inst.returncode != 0:
        # repo moved forward but install failed: roll the source back so link-mode stays consistent
        git(repo, "reset", "--quiet", "--keep", local)
        return finish("失败：安装新版本出错，已回退源仓库", verbose,
                      install_tail=(inst.stdout + inst.stderr).strip().splitlines()[-8:])
    return finish("已更新", verbose, previous=local[:10], version=describe(repo), channel=channel, updated_at=now())


def main() -> int:
    ap = argparse.ArgumentParser(description="dev-spec 自我更新")
    ap.add_argument("--now", action="store_true", help="忽略检查间隔与自动更新开关，立即检查")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    if not a.now:
        try:
            sys.stdin.read()                         # drain the hook payload
        except OSError:
            pass
    return run(force=a.now, verbose=a.verbose)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
