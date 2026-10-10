#!/usr/bin/env python3
"""End-to-end tests for scripts/server-setup.sh and `install.sh remote`.

Every run uses a local bare origin as DEV_SPEC_REPO_URL, throwaway DEV_SPEC_DIR / CLAUDE_CONFIG_DIR / HOME
(all with spaces in the path), and an allow-listed PATH that has bash, python3, git and a few POSIX tools but
no node, no shasum and no claude. `ssh` and `sudo` are fake shims; nothing here touches the real ~/.claude
or opens a network connection. Run: python3 scripts/test_server_setup.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETUP = ROOT / "scripts/server-setup.sh"
INSTALL_SH = ROOT / "install.sh"
BASELINE = "e9a1c7cc834421eebd9d4d4a94593ebe58b28755"   # v1.1.1: last release without --device / server-setup
RULE = "global/rules/dev-spec/01-core.md"
INSTALLED_RULE = "rules/dev-spec/01-core.md"
APT_HINT = "sudo apt-get install -y git python3"
# What a minimal Debian install has besides git + python3. Deliberately no node, shasum, perl or claude.
TOOLS = ("bash", "sh", "env", "python3", "git", "cat", "cp", "mv", "rm", "mkdir", "rmdir", "mktemp", "find", "sort",
         "cksum", "diff", "grep", "cut", "dirname", "basename", "head", "tail", "sed", "tr", "wc", "ls", "ln",
         "chmod", "touch", "tar", "uname", "date", "sleep", "true", "false")
results: list[tuple[str, bool, str]] = []

FAKE_SUDO = """#!/bin/sh
echo "$@" >> "$FAKE_SUDO_LOG"
exit 0
"""

# Runs the "remote" command locally the way sshd would: one string handed to a POSIX shell, stdin passed through.
# DEV_SPEC_REPO_URL is dropped from the environment because a real ssh does not forward it; the sandbox
# directories must be present so a bug can never reach the real home.
FAKE_SSH = """#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
open(os.environ["FAKE_SSH_LOG"], "a").write(json.dumps(a) + "\\n")
if os.environ.get("FAKE_SSH_RC"):
    sys.exit(int(os.environ["FAKE_SSH_RC"]))
i = 0
if a[i] == "-p":
    i += 2
if a[i] == "--":
    i += 1
env = {k: v for k, v in os.environ.items() if k != "DEV_SPEC_REPO_URL"}
assert env.get("DEV_SPEC_DIR") and env.get("CLAUDE_CONFIG_DIR"), "sandbox directories missing"
os.execvpe("sh", ["sh", "-c", " ".join(a[i + 1:])], env)
"""


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def minimal_path(dst: Path, exclude: tuple[str, ...] = ()) -> Path:
    """A directory holding symlinks to the allow-listed tools only; use it as the whole PATH."""
    dst.mkdir(parents=True)
    for tool in TOOLS:
        real = os.path.realpath(sys.executable) if tool == "python3" else shutil.which(tool)
        if real and tool not in exclude:
            (dst / tool).symlink_to(real)
    return dst


def script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def run(args: list[str], env: dict, cwd: Path | None = None, stdin=None) -> subprocess.CompletedProcess:
    return subprocess.run(args, env=env, cwd=cwd, stdin=stdin if stdin is not None else subprocess.DEVNULL,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)


def worktree_files(dst: Path) -> None:
    """The version under test: tracked plus new files of this checkout."""
    files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                           capture_output=True, text=True, check=True).stdout.split("\n")
    for f in filter(None, files):
        if (ROOT / f).is_file():
            (dst / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / f, dst / f)


def pre_feature_files(dst: Path) -> str:
    """A release that predates this feature. The real v1.1.1 tree when history has it (its install.sh needs
    shasum and has no --device); otherwise today's tree with install.sh / validate.sh replaced by scripts
    that fail, which is the property the scenario depends on."""
    if subprocess.run(["git", "cat-file", "-e", BASELINE + "^{commit}"], cwd=ROOT, capture_output=True).returncode == 0:
        archive = subprocess.run(["git", "archive", BASELINE], cwd=ROOT, capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(dst)], input=archive, check=True)
        return "v1.1.1 原始内容"
    worktree_files(dst)
    for rel in ("install.sh", "scripts/validate.sh"):
        script(dst / rel, "#!/usr/bin/env bash\necho '旧版入口不应被调用' >&2\nexit 97\n")
    return "当前内容 + 必然失败的 install.sh/validate.sh（历史中没有基线提交）"


class Origin:
    """A bare origin plus the author's clone that publishes to it."""

    def __init__(self, tmp: Path, name: str, fill, genv: dict):
        self.bare, self.dev, self.genv = tmp / f"{name}.git", tmp / f"{name}-dev", genv
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.bare)], check=True, env=genv)
        self.dev.mkdir()
        self.note = fill(self.dev)
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-qm", "initial")
        self.git("remote", "add", "origin", str(self.bare))
        self.git("push", "-q", "-u", "origin", "main")

    def git(self, *args: str) -> str:
        p = subprocess.run(["git", *args], cwd=self.dev, env=self.genv, capture_output=True, text=True)
        assert p.returncode == 0, f"git {args}: {p.stderr}"
        return p.stdout.strip()

    def publish(self, marker: str) -> str:
        p = self.dev / RULE
        p.write_text(p.read_text() + f"\n<!-- {marker} -->\n")
        self.git("commit", "-qam", f"rule {marker}")
        self.git("push", "-q", "origin", "main")
        return self.git("rev-parse", "HEAD")

    def tag(self, name: str) -> str:
        self.git("tag", "-a", name, "-m", name)
        self.git("push", "-q", "origin", name)
        return self.git("rev-parse", "HEAD")

    def replace_tree(self, fill, msg: str) -> str:
        for p in self.dev.iterdir():
            if p.name != ".git":
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        fill(self.dev)
        self.git("add", "-A")
        self.git("commit", "-qm", msg)
        self.git("push", "-q", "origin", "main")
        return self.git("rev-parse", "HEAD")


class Box:
    """One simulated server: its own HOME, source directory and Claude config home."""

    def __init__(self, tmp: Path, name: str, origin: Origin, path: str, logs: Path):
        self.root = tmp / name
        self.home = self.root / "home"
        self.home.mkdir(parents=True)
        self.src = self.root / "spec dir" / "claude-config"       # parent does not exist yet; path has a space
        self.conf = self.root / "claude home"
        self.origin, self.path, self.logs = origin, path, logs

    def env(self, path: str | None = None, **extra: str) -> dict:
        e = {"PATH": path or self.path, "HOME": str(self.home), "DEV_SPEC_REPO_URL": str(self.origin.bare),
             "DEV_SPEC_DIR": str(self.src), "CLAUDE_CONFIG_DIR": str(self.conf),
             "DEV_SPEC_UPDATE_VALIDATE_CMD": "true",       # the real default is exercised in test_self_update.py
             "FAKE_SUDO_LOG": str(self.logs / "sudo.log"), "FAKE_SSH_LOG": str(self.logs / "ssh.log"),
             "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C.UTF-8"}      # servers run a UTF-8 locale
        if os.environ.get("TMPDIR"):
            e["TMPDIR"] = os.environ["TMPDIR"]
        e.update(extra)
        return e

    def setup(self, path: str | None = None, **extra: str) -> subprocess.CompletedProcess:
        """Run the script the way `install.sh remote` delivers it: on stdin of `bash -s`."""
        with SETUP.open() as fh:
            return run(["bash", "-s"], self.env(path, **extra), cwd=self.root, stdin=fh)

    def git(self, *args: str) -> subprocess.CompletedProcess:
        return run(["git", "-C", str(self.src), *args], self.env(os.environ["PATH"]))

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").stdout.strip()

    def manifest(self) -> dict:
        p = self.conf / ".dev-spec-manifest.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def state(self) -> dict:
        p = self.conf / "dev-spec-update.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def rule(self) -> str:
        p = self.conf / INSTALLED_RULE
        return p.read_text() if p.exists() else ""

    def backups(self) -> list[str]:
        d = self.conf / "dev-spec-backups"
        return sorted(str(p.relative_to(d)) for p in d.rglob("*")) if d.exists() else []


def out(r: subprocess.CompletedProcess) -> str:
    return f"rc={r.returncode}\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}"


def doctor_line(r: subprocess.CompletedProcess) -> str:
    return next((ln for ln in r.stdout.splitlines() if ln.startswith("doctor:")), "")


def test_stable_channel(tmp: Path, path: str, logs: Path, genv: dict) -> None:
    """Cases 5 and 8: first install pins main to the highest release tag; later runs only update."""
    o = Origin(tmp, "stable", worktree_files, genv)
    o.tag("v0.2.0")
    o.publish("rel-10")
    t10 = o.tag("v0.10.0")
    o.publish("rc-11")
    o.tag("v0.11.0-rc1")
    o.publish("unreleased")

    box = Box(tmp, "box-stable", o, path, logs)
    (box.conf / "rules").mkdir(parents=True)
    (box.conf / "rules/legacy.md").write_text("legacy\n")           # retired on the first install only
    r = box.setup()
    m = box.manifest()
    check("首次安装成功并输出 doctor 结果", r.returncode == 0 and doctor_line(r) == "doctor: 正常", out(r))
    check("D4: HEAD 位于语义版本最高的发布 tag（v0.10.0 > v0.2.0，忽略 rc）", box.head() == t10, box.head())
    check("D4: 仍在 main 分支且有上游",
          box.git("symbolic-ref", "-q", "--short", "HEAD").stdout.strip() == "main"
          and box.git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").stdout.strip() == "origin/main",
          box.git("status", "-sb").stdout)
    check("已装规则不含 tag 之后的内容", "rel-10" in box.rule() and "rc-11" not in box.rule()
          and "unreleased" not in box.rule(), box.rule()[-200:])
    check("manifest: copy 模式、stable 通道、source 指向含空格的克隆目录",
          m.get("mode") == "copy" and m.get("options", {}).get("update_channel") == "stable"
          and m.get("options", {}).get("auto_update") is True and m.get("source") == os.path.realpath(box.src), str(m)[:400])
    check("首次安装退役旧规则并接管 CLAUDE.md", "rules/legacy.md" in m.get("retired", {})
          and not (box.conf / "rules/legacy.md").exists() and (box.conf / "CLAUDE.md").exists(), str(m.get("retired")))

    # 8a: an unchanged second run takes the update path and changes nothing
    (box.src / ".git/sentinel").write_text("same clone\n")
    before = ((box.conf / ".dev-spec-manifest.json").read_text(), box.backups())
    r = box.setup()
    check("重跑: 退出 0 且走更新路径（已是最新）", r.returncode == 0 and "已是最新" in r.stdout
          and doctor_line(r) == "doctor: 正常", out(r))
    check("重跑: 不重新克隆、不 reset", (box.src / ".git/sentinel").exists() and box.head() == t10)
    check("重跑: 不重装（manifest 与 dev-spec-backups 不变）",
          ((box.conf / ".dev-spec-manifest.json").read_text(), box.backups()) == before, str(box.backups()))

    # 8c: a rule file the user adds afterwards is theirs
    (box.conf / "rules/mine.md").write_text("user rule\n")
    r = box.setup()
    check("重跑: 首次之后放入 rules/ 的用户文件不被退役", r.returncode == 0 and (box.conf / "rules/mine.md").exists()
          and box.manifest().get("retired") == m.get("retired"), out(r))

    # 8d: uncommitted changes in the source survive
    readme = box.src / "README.md"
    readme.write_text(readme.read_text() + "\nlocal edit\n")
    r = box.setup()
    check("重跑: 源仓库未提交改动仍在", r.returncode == 0 and readme.read_text().endswith("\nlocal edit\n")
          and box.head() == t10, out(r))
    box.git("checkout", "--", "README.md")

    # 5 (second half): a new release is picked up by the installed stable updater
    t11 = o.tag("v0.11.0")
    r = box.setup()
    check("发布 v0.11.0 后再跑：更新到 v0.11.0", r.returncode == 0 and box.head() == t11
          and box.state().get("last_result") == "已更新" and box.state().get("version") == "v0.11.0"
          and "unreleased" in box.rule(), out(r) + str(box.state()))
    check("更新后的重装也不退役用户规则", (box.conf / "rules/mine.md").exists()
          and box.manifest().get("retired") == m.get("retired"), str(box.manifest().get("retired")))

    # 8b: a device rolled back to an older release stays there
    rb = run([sys.executable, str(box.src / "scripts/dev_spec_install.py"), "rollback", "v0.10.0",
              "--claude-home", str(box.conf)], box.env())
    r = box.setup()
    check("回滚后再跑：仍停在该 tag、detached、自动更新保持关闭",
          rb.returncode == 0 and r.returncode == 0 and box.head() == t10
          and box.git("symbolic-ref", "-q", "HEAD").returncode != 0
          and box.manifest().get("options", {}).get("auto_update") is False and "rel-10" in box.rule()
          and "unreleased" not in box.rule(), out(rb) + out(r))
    check("回滚后再跑：doctor 仍报告回滚状态", "已回滚到 v0.10.0" in r.stdout, r.stdout[-600:])


def test_no_release_tag(tmp: Path, path: str, logs: Path, genv: dict) -> None:
    o = Origin(tmp, "untagged", worktree_files, genv)
    head = o.publish("only-main")
    box = Box(tmp, "box-untagged", o, path, logs)
    r = box.setup()
    check("无发布 tag：安装 main", r.returncode == 0 and box.head() == head and "only-main" in box.rule()
          and doctor_line(r) == "doctor: 正常", out(r))
    r = box.setup()
    check("无发布 tag：更新器报告还没有发布 tag", r.returncode == 0 and "还没有发布 tag" in r.stdout
          and box.head() == head, out(r))


def test_review_followups(tmp: Path, path: str, logs: Path, genv: dict) -> None:
    """Regressions from the package review: trailing slash, exit code on a rejected update, opt-out of takeover."""
    o = Origin(tmp, "followup", worktree_files, genv)
    o.publish("base")
    o.tag("v0.1.0")

    # trailing slash on DEV_SPEC_DIR / CLAUDE_CONFIG_DIR must not break the first install
    box = Box(tmp, "box-slash", o, path, logs)
    r = box.setup(DEV_SPEC_DIR=str(box.src) + "/", CLAUDE_CONFIG_DIR=str(box.conf) + "//")
    check("回归: 目录带尾部斜杠时首次安装成功", r.returncode == 0 and doctor_line(r) == "doctor: 正常"
          and box.manifest().get("source") == str(box.src), out(r))

    # a rejected update must surface as a non-zero exit (batch deployments read the exit code)
    o.publish("next")
    o.tag("v0.2.0")
    r = box.setup(DEV_SPEC_UPDATE_VALIDATE_CMD="false")
    check("回归: 更新被拒绝时脚本非 0", r.returncode != 0 and "拒绝" in r.stdout and "next" not in box.rule(), out(r))
    r = box.setup()
    check("回归: 随后正常更新退出 0", r.returncode == 0 and "next" in box.rule(), out(r))

    # DEV_SPEC_KEEP_EXISTING=1: do not take over CLAUDE.md or retire existing rules on first install
    keep = Box(tmp, "box-keep", o, path, logs)
    (keep.conf / "rules").mkdir(parents=True)
    (keep.conf / "rules/mine.md").write_text("mine\n")
    (keep.conf / "CLAUDE.md").write_text("# my own\n")
    r = keep.setup(DEV_SPEC_KEEP_EXISTING="1")
    check("回归: KEEP_EXISTING 时保留用户的 CLAUDE.md 与规则", r.returncode == 0
          and (keep.conf / "CLAUDE.md").read_text() == "# my own\n" and (keep.conf / "rules/mine.md").exists()
          and (keep.conf / INSTALLED_RULE).exists(), out(r))
    take = Box(tmp, "box-take", o, path, logs)
    (take.conf / "rules").mkdir(parents=True)
    (take.conf / "rules/mine.md").write_text("mine\n")
    r = take.setup()
    check("默认: 首次安装退役原有规则并留有备份", r.returncode == 0 and not (take.conf / "rules/mine.md").exists()
          and any(b.endswith("rules/mine.md") for b in take.backups()), out(r) + str(take.backups()))


def test_pre_feature_release(tmp: Path, path: str, logs: Path, genv: dict) -> None:
    """Case 2 (D1): the latest release predates --device; its install.sh cannot run here (no node, no shasum)."""
    o = Origin(tmp, "old", pre_feature_files, genv)
    told = o.tag("v1.1.1")
    o.replace_tree(worktree_files, "feat: server deployment (unreleased)")
    box = Box(tmp, "box-old", o, path, logs)
    r = box.setup()
    m = box.manifest()
    entries = [e["path"] for e in m.get("entries", [])]
    check(f"旧发布（{o.note}）：不经克隆内 install.sh 也能装上", r.returncode == 0 and box.head() == told
          and doctor_line(r) == "doctor: 正常", out(r))
    check("旧发布：不是半安装（清单条目齐全且都存在，装的是 tag 的内容）",
          {"rules/dev-spec", "agents/dev-spec", "hooks/dev-spec", "CLAUDE.md"} <= set(entries)
          and all((box.conf / e).exists() for e in entries)
          and (box.conf / "hooks/dev-spec/dev_spec_update.py").read_bytes() == (box.src / "hooks/dev_spec_update.py").read_bytes(),
          str(entries))
    before = (box.conf / ".dev-spec-manifest.json").read_text()
    r = box.setup()
    check("旧发布：重跑结果一致", r.returncode == 0 and box.head() == told and doctor_line(r) == "doctor: 正常"
          and (box.conf / ".dev-spec-manifest.json").read_text() == before, out(r))


def test_leftover_targets(tmp: Path, path: str, logs: Path, genv: dict) -> None:
    """Case 9: whatever is already at DEV_SPEC_DIR is never deleted or re-cloned."""
    o = Origin(tmp, "left", worktree_files, genv)
    o.tag("v0.1.0")
    head = o.publish("after-tag")

    box = Box(tmp, "box-notgit", o, path, logs)
    box.src.mkdir(parents=True)
    (box.src / "precious.txt").write_text("keep me\n")
    r = box.setup()
    check("目标目录非空且不是 git 仓库：非 0、不删除、不安装", r.returncode != 0
          and (box.src / "precious.txt").read_text() == "keep me\n" and sorted(p.name for p in box.src.iterdir()) == ["precious.txt"]
          and not box.conf.exists(), out(r))

    box = Box(tmp, "box-clone-only", o, path, logs)
    box.src.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", str(o.bare), str(box.src)], check=True, env=genv)
    (box.src / ".git/sentinel").write_text("same clone\n")
    r = box.setup()
    check("已有完整克隆但未安装：不重新克隆、不 reset、完成安装", r.returncode == 0
          and (box.src / ".git/sentinel").exists() and box.head() == head and "after-tag" in box.rule()
          and box.manifest().get("source") == os.path.realpath(box.src) and doctor_line(r) == "doctor: 正常", out(r))

    elsewhere = box.root / "elsewhere"
    r = box.setup(DEV_SPEC_DIR=str(elsewhere))
    check("已有安装来自另一个克隆目录：非 0、不克隆、不接管", r.returncode != 0 and not elsewhere.exists()
          and box.manifest().get("source") == os.path.realpath(box.src), out(r))

    box = Box(tmp, "box-empty-dir", o, path, logs)
    box.src.mkdir(parents=True)
    r = box.setup()
    check("目标目录存在但为空：正常克隆安装", r.returncode == 0 and doctor_line(r) == "doctor: 正常"
          and box.manifest().get("source") == os.path.realpath(box.src), out(r))

    r = run(["bash", str(SETUP)], Box(tmp, "box-direct", o, path, logs).env(), cwd=tmp)
    check("直接执行脚本文件（非 stdin）同样可用", r.returncode == 0 and doctor_line(r) == "doctor: 正常", out(r))


def test_dependencies(tmp: Path, bins: Path, fake: Path, logs: Path, genv: dict) -> None:
    """Case 10 (D5): missing prerequisites stop the script before it creates anything; it never calls sudo."""
    o = Origin(tmp, "deps", worktree_files, genv)
    real_py = os.path.realpath(sys.executable)

    def shim(version: str) -> Path:
        d = tmp / f"py-{version}"
        script(d / "python3", f'#!/bin/sh\ncase "$1" in --version|-V) echo "Python {version}"; exit 0;; esac\n'
                              f'exec "{real_py}" "$@"\n')
        return d

    def untouched(box: Box, r: subprocess.CompletedProcess) -> bool:
        return (r.returncode != 0 and APT_HINT in r.stdout + r.stderr and not box.src.exists()
                and not box.src.parent.exists() and not box.conf.exists())

    nogit = minimal_path(tmp / "bin-nogit", exclude=("git",))
    box = Box(tmp, "box-nogit", o, "", logs)
    r = box.setup(f"{fake}{os.pathsep}{nogit}")
    check("无 git：非 0、给出 apt 提示、不创建任何目录", untouched(box, r) and "git" in r.stderr, out(r))

    box = Box(tmp, "box-py38", o, "", logs)
    r = box.setup(f"{shim('3.8.10')}{os.pathsep}{fake}{os.pathsep}{bins}")
    check("python3 为 3.8：非 0、给出 apt 提示、不创建任何目录", untouched(box, r) and "3.8" in r.stderr, out(r))

    nopy = minimal_path(tmp / "bin-nopy", exclude=("python3",))
    box = Box(tmp, "box-nopy", o, "", logs)
    r = box.setup(f"{fake}{os.pathsep}{nopy}")
    check("无 python3：非 0、给出 apt 提示、不创建任何目录", untouched(box, r), out(r))

    for version in ("3.9.2", "3.10.12", "3.11.2"):
        box = Box(tmp, f"box-py{version}", o, "", logs)
        r = box.setup(f"{shim(version)}{os.pathsep}{fake}{os.pathsep}{bins}")
        check(f"python3 {version} 满足 ≥3.9（按整数比较，不是字符串）", r.returncode == 0
              and doctor_line(r) == "doctor: 正常", out(r))


def test_remote(tmp: Path, path: str, logs: Path, genv: dict) -> None:
    """Cases 6 and 7: `install.sh remote` only pipes server-setup.sh into ssh."""
    # a local checkout whose validation and installer must not be reached by `remote`
    local = tmp / "local copy"
    (local / "scripts").mkdir(parents=True)
    (local / "hooks").mkdir()
    shutil.copy2(INSTALL_SH, local / "install.sh")
    shutil.copy2(SETUP, local / "scripts/server-setup.sh")
    script(local / "scripts/validate.sh", '#!/usr/bin/env bash\ntouch "$(dirname "$0")/validate.invoked"\nexit 97\n')
    for rel in ("scripts/dev_spec_install.py", "hooks/dev_spec_update.py"):
        script(local / rel, "import sys\nopen(__file__ + '.invoked', 'w').close()\nsys.exit(97)\n")
    ssh_log = logs / "ssh.log"

    def calls() -> list[list[str]]:
        return [json.loads(ln) for ln in ssh_log.read_text().splitlines()] if ssh_log.exists() else []

    def remote(box: Box, *args: str, **extra: str) -> subprocess.CompletedProcess:
        ssh_log.unlink(missing_ok=True)
        return run(["bash", str(local / "install.sh"), "remote", *args], box.env(**extra), cwd=tmp)

    o = Origin(tmp, "o'rig in", worktree_files, genv)       # space and single quote must survive the remote shell
    o.tag("v0.1.0")
    box = Box(tmp, "box-remote", o, path, logs)
    r = remote(box, "user@host")
    c = calls()
    check("remote: 脚本经 stdin 在远端跑到最后（输出 doctor 结果）", r.returncode == 0 and doctor_line(r) == "doctor: 正常"
          and box.manifest().get("mode") == "copy", out(r))
    check("remote: 调用形如 ssh -- <host> '… bash -s'", len(c) == 1 and c[0][:2] == ["--", "user@host"]
          and len(c[0]) == 3 and c[0][2].endswith("bash -s"), str(c))
    check("remote: DEV_SPEC_REPO_URL（含空格与单引号）原样到达远端",
          box.git("remote", "get-url", "origin").stdout.strip() == str(o.bare), box.git("remote", "-v").stdout)
    check("remote: 不运行本地 validate，也不进入本地安装器/更新器",
          not list(local.rglob("*.invoked")), str(list(local.rglob("*.invoked"))))

    box2 = Box(tmp, "box-remote-rc", o, path, logs)
    e = box2.env(FAKE_SSH_RC="7")
    del e["DEV_SPEC_REPO_URL"]
    ssh_log.unlink(missing_ok=True)
    r = run(["bash", str(local / "install.sh"), "remote", "user@host", "--port", "2222"], e, cwd=tmp)
    check("remote: ssh 的退出码原样返回；--port 变成 -p；未设 URL 时远端命令就是 bash -s",
          r.returncode == 7 and calls() == [["-p", "2222", "--", "user@host", "bash -s"]], out(r) + str(calls()))
    ssh_log.unlink(missing_ok=True)
    r = run(["bash", str(local / "install.sh"), "remote", "--port", "2222", "user@host"], e, cwd=tmp)
    check("remote: --port 可写在主机之前", r.returncode == 7 and calls() == [["-p", "2222", "--", "user@host", "bash -s"]],
          out(r) + str(calls()))

    box3 = Box(tmp, "box-remote-fail", o, path, logs)
    box3.src.mkdir(parents=True)
    (box3.src / "precious.txt").write_text("x\n")
    r = remote(box3, "user@host")
    check("remote: 远端失败时 install.sh 也非 0", r.returncode != 0 and len(calls()) == 1
          and (box3.src / "precious.txt").exists(), out(r))

    for args, why in ((), "无主机"), (("--port", "22"), "只有端口"), (("-oProxyCommand=touch pwned",), "主机以 - 开头"), \
            (("user@host", "--port"), "--port 缺值"), (("user@host", "--port", "22;id"), "端口非数字"), \
            (("user@host", "other@host"), "多余参数"):
        r = remote(box2, *args)
        check(f"remote: {why}时退出 2、给出用法、不调用 ssh", r.returncode == 2 and "remote" in r.stderr
              and "--port" in r.stderr and not calls(), out(r) + str(calls()))
    check("remote: 参数错误同样不触碰本地校验与安装器", not list(local.rglob("*.invoked")) and not (tmp / "pwned").exists())

    # DEV_SPEC_KEEP_EXISTING is forwarded only as the fixed value 1; anything else never reaches the remote command
    kb = Box(tmp, "box-remote-keep", o, path, logs)
    r = remote(kb, "user@host", DEV_SPEC_KEEP_EXISTING="1")
    check("remote: DEV_SPEC_KEEP_EXISTING=1 被带到远端命令", r.returncode == 0 and "DEV_SPEC_KEEP_EXISTING=1 " in calls()[0][2], str(calls()))
    kb2 = Box(tmp, "box-remote-keep2", o, path, logs)
    r = remote(kb2, "user@host", DEV_SPEC_KEEP_EXISTING="1; touch /tmp/x")
    check("remote: 其他取值不进入远端命令", "KEEP_EXISTING" not in calls()[0][2] and "touch" not in calls()[0][2], str(calls()))


def test_local_install_without_node(tmp: Path, path: str, logs: Path, genv: dict) -> None:
    """Case 11 (last part): the pre-install check of install.sh is the device subset and needs no node."""
    home = tmp / "local home" / "claude"
    env = {"PATH": path, "HOME": str(tmp / "local home"), "GIT_CONFIG_NOSYSTEM": "1", "DEV_SPEC_REQUIRE_NODE": "1",
           **({"TMPDIR": os.environ["TMPDIR"]} if os.environ.get("TMPDIR") else {})}
    (tmp / "local home").mkdir()
    r = run(["bash", str(INSTALL_SH), "--apply", "--skip-version-check", "--claude-home", str(home)], env, cwd=tmp)
    check("无 node 时 install.sh --apply 成功（安装前校验用 --device）", r.returncode == 0
          and (home / ".dev-spec-manifest.json").exists() and (home / INSTALLED_RULE).exists(), out(r))


def main() -> int:
    tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="dev-spec-srv-")))
    try:
        bins = minimal_path(tmp / "bin")
        fake = tmp / "fake-bin"
        script(fake / "sudo", FAKE_SUDO)
        script(fake / "ssh", FAKE_SSH)
        logs = tmp / "logs"
        logs.mkdir()
        (tmp / "git-home").mkdir()
        path = f"{fake}{os.pathsep}{bins}"
        genv = {**os.environ, "HOME": str(tmp / "git-home"), "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        genv.pop("GIT_CONFIG_GLOBAL", None)
        check("测试环境：PATH 中没有 node、shasum、claude",
              not any(shutil.which(t, path=path) for t in ("node", "shasum", "claude")))
        # `$name` directly followed by a CJK character is read as part of the name under a UTF-8 locale
        # (which servers use, and this sandbox does not): bash then aborts on an "unbound variable".
        bare = [f"{p.name}:{i}" for p in (SETUP, INSTALL_SH, ROOT / "scripts/validate.sh")
                for i, ln in enumerate(p.read_text().splitlines(), 1) if re.search(r"\$[A-Za-z_]\w*[^\x00-\x7f]", ln, re.A)]
        check("shell 脚本中变量名后紧跟非 ASCII 字符时都加了花括号", not bare, str(bare))

        test_stable_channel(tmp, path, logs, genv)
        test_no_release_tag(tmp, path, logs, genv)
        test_pre_feature_release(tmp, path, logs, genv)
        test_leftover_targets(tmp, path, logs, genv)
        test_dependencies(tmp, bins, fake, logs, genv)
        test_remote(tmp, path, logs, genv)
        test_local_install_without_node(tmp, path, logs, genv)
        test_review_followups(tmp, path, logs, genv)
        check("全程没有调用 sudo", not (logs / "sudo.log").exists(),
              (logs / "sudo.log").read_text() if (logs / "sudo.log").exists() else "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in results if not r[1]]
    for name, _, detail in failed:
        print(f"FAIL {name}: {detail[:1800]}")
    print(f"server-setup: {len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
