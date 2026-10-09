#!/usr/bin/env python3
"""Install / uninstall / doctor for dev-spec in a Claude config home.

Modes
  --link   symlink each component to this repo (single source, edits apply live)
  --copy   copy components (default; for devices without this repo mounted)

Default is a dry run. Only paths under --claude-home are touched. Anything
replaced or retired is moved into <home>/dev-spec-backups/<stamp>/ and recorded
in <home>/.dev-spec-manifest.json, so uninstall restores the previous state.
"""

from __future__ import annotations

import argparse
import filecmp
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
MANIFEST = ".dev-spec-manifest.json"
HOOK_MARK = "dev-spec/policy-guard.py"
UPDATE_MARK = "dev-spec/dev_spec_update.py"
MIN_VERSION = (2, 1, 212)   # isolation: worktree, worktree.baseRef, effort, agent hooks


# ---------- helpers ----------

def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"错误：{path} 不是合法 JSON（{e}），不修改。请先修复。")
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def remove(p: Path) -> None:
    if p.is_symlink() or p.is_file():
        p.unlink()
    elif p.is_dir():
        shutil.rmtree(p)


def same_tree(a: Path, b: Path) -> bool:
    if a.is_file() or b.is_file():
        return a.is_file() and b.is_file() and filecmp.cmp(a, b, shallow=False)
    cmp = filecmp.dircmp(a, b, ignore=[".DS_Store", "__pycache__"])
    if cmp.left_only or cmp.right_only or cmp.funny_files:
        return False
    _, mismatch, errors = filecmp.cmpfiles(a, b, cmp.common_files, shallow=False)
    return not mismatch and not errors and all(same_tree(a / d, b / d) for d in cmp.common_dirs)


def claude_version() -> tuple[int, ...] | None:
    exe = shutil.which("claude")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in m.groups()) if m else None


def script_command(home: Path, name: str) -> str:
    script = home / "hooks/dev-spec" / name
    if home.resolve() == (Path.home() / ".claude").resolve():
        path = f'"$HOME/.claude/hooks/dev-spec/{name}"'
    else:
        path = shlex.quote(str(script))
    # Missing script (e.g. unmounted source volume) must exit 0: exit 2 from PreToolUse would block every tool call.
    return f"f={path}; [ -f \"$f\" ] || exit 0; exec python3 \"$f\""


def hook_group(home: Path) -> dict:
    group = json.loads((SRC / "hooks/hooks.json").read_text())["hooks"]["PreToolUse"][0]
    group["hooks"][0]["command"] = script_command(home, "policy-guard.py")
    return group


def update_group(home: Path) -> dict:
    return {"hooks": [{"type": "command", "command": script_command(home, "dev_spec_update.py"),
                       "async": True, "timeout": 900, "statusMessage": "dev-spec 自动更新检查"}]}


# (event, marker in command) for every hook entry this installer manages
MANAGED_HOOKS = [("PreToolUse", HOOK_MARK), ("SessionStart", UPDATE_MARK)]


def desired_hooks(home: Path, opts: dict) -> dict[str, dict | None]:
    return {"PreToolUse": None if opts.get("no_hooks") else hook_group(home),
            "SessionStart": update_group(home) if opts.get("auto_update") else None}


def is_ours(g: dict, mark: str) -> bool:
    return any(mark in h.get("command", "") for h in g.get("hooks", []))


def apply_hooks(settings: dict, want: dict[str, dict | None]) -> dict:
    """Return a copy of settings where each managed event holds exactly the desired dev-spec group (or none)."""
    s = json.loads(json.dumps(settings))
    hooks = s.setdefault("hooks", {})
    for event, mark in MANAGED_HOOKS:
        groups = [g for g in hooks.get(event, []) if not is_ours(g, mark)]
        if want.get(event) is not None:
            groups.append(want[event])
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not hooks:
        s.pop("hooks")
    return s


# ---------- plan ----------

def components(manage_claude_md: bool) -> list[tuple[str, Path]]:
    comps = [("rules/dev-spec", SRC / "global/rules/dev-spec"),
             ("agents/dev-spec", SRC / "agents"),
             ("hooks/dev-spec", SRC / "hooks")]
    comps += [(f"skills/{p.name}", p) for p in sorted((SRC / "skills").iterdir()) if p.is_dir()]
    comps += [(f"workflows/{p.name}", p) for p in sorted((SRC / "workflows").glob("*.js"))]
    if manage_claude_md:
        comps.append(("CLAUDE.md", SRC / "global/CLAUDE.md"))
    return comps


def state_of(dst: Path, src: Path, mode: str) -> str:
    if not dst.exists() and not dst.is_symlink():
        return "absent"
    if mode == "link":
        return "same" if dst.is_symlink() and dst.resolve() == src.resolve() else "differs"
    return "same" if not dst.is_symlink() and dst.exists() and same_tree(src, dst) else "differs"


def source_version() -> str:
    p = subprocess.run(["git", "-C", str(SRC), "describe", "--tags", "--always", "--dirty", "--match", "v[0-9]*"],
                       capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else "unknown"


# ---------- install ----------

def install(home: Path, a: argparse.Namespace) -> int:
    old = load_json(home / MANIFEST)
    mode = "link" if a.link else "copy" if a.copy else old.get("mode", "copy")
    prev_opts = old.get("options", {})
    opts = {
        "no_hooks": a.no_hooks if a.no_hooks else prev_opts.get("no_hooks", False) if not a.hooks else False,
        "auto_update": (False if a.no_auto_update else True if a.auto_update else prev_opts.get("auto_update", True)),
        "update_require_signed": a.require_signed or prev_opts.get("update_require_signed", False),
        "update_interval_hours": prev_opts.get("update_interval_hours", 6),
        "update_channel": a.channel or prev_opts.get("update_channel", "stable"),
    }
    owned = {e["path"]: e for e in old.get("entries", [])}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_root = home / "dev-spec-backups" / stamp
    manifest = {"version": 2, "mode": mode, "source": str(SRC), "installed_at": stamp,
                "spec_version": source_version(),
                "entries": [], "retired": dict(old.get("retired", {})),
                "options": opts,
                "settings_hook": old.get("settings_hook", False),
                "worktree_head": old.get("worktree_head", False)}
    steps: list[tuple[str, str, Path, Path, bool]] = []   # (label, rel, src, dst, backup_needed)
    conflicts = []

    claude_md = home / "CLAUDE.md"
    manage_md = a.manage_claude_md or "CLAUDE.md" in owned or not (claude_md.exists() or claude_md.is_symlink())
    for rel, src in components(manage_md):
        dst = home / rel
        prev = owned.get(rel)
        st = state_of(dst, src, mode)
        entry = {"path": rel, "kind": mode}
        if prev and prev.get("backup"):
            entry["backup"] = prev["backup"]
        if st == "same":
            steps.append(("不变", rel, src, dst, False))
        elif st == "absent":
            steps.append(("新增", rel, src, dst, False))
        elif prev:
            steps.append(("更新", rel, src, dst, False))
        elif a.force or rel == "CLAUDE.md":
            steps.append(("替换(备份)", rel, src, dst, True))
            entry["backup"] = str(backup_root / rel)
        else:
            conflicts.append(rel)
            continue
        manifest["entries"].append(entry)

    retire = []
    if a.retire_legacy_rules and (home / "rules").is_dir():
        retire = sorted(p for p in (home / "rules").iterdir()
                        if p.name not in {"dev-spec", ".DS_Store"})

    settings_path = home / "settings.json"
    settings = load_json(settings_path)
    new_settings = apply_hooks(settings, desired_hooks(home, opts))
    hook_change = new_settings != settings
    # worktree subagents must branch from local HEAD to see contract checkpoints; set only if the user has no value
    set_wt_head = "baseRef" not in (settings.get("worktree") or {})

    print(f"模式: {'软链接' if mode == 'link' else '复制'}")
    for label, rel, *_ in steps:
        print(f"  {label:10} {rel}")
    for rel in conflicts:
        print(f"  冲突跳过   {rel}（已存在且非本规范管理；--force 备份后替换）")
    for p in retire:
        print(f"  退役旧规则 rules/{p.name} → 备份")
    print(f"  settings.json: hooks {'更新' if hook_change else '已是最新'}"
          f"（守卫{'关' if opts['no_hooks'] else '开'}，自动更新{'开' if opts['auto_update'] else '关'}）")
    wt_now = (settings.get("worktree") or {}).get("baseRef")
    print(f"  settings.json: worktree.baseRef {'设为 head' if set_wt_head else f'保持用户值 {wt_now!r}'}")
    stale = [r for r in owned if r not in {e['path'] for e in manifest['entries']} and r not in conflicts]
    for rel in stale:
        print(f"  移除过期   {rel}")

    if not a.apply:
        print("\n预览完成，未写入任何文件。加 --apply 执行。")
        return 0

    home.mkdir(parents=True, exist_ok=True)
    for label, rel, src, dst, backup in steps:
        if label == "不变":
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if backup:
            target = backup_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(target))
        elif dst.exists() or dst.is_symlink():
            remove(dst)
        if mode == "link":
            dst.symlink_to(src)
        elif src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".DS_Store", "__pycache__"))
        else:
            shutil.copy2(src, dst)

    for p in retire:
        target = backup_root / "rules" / p.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(target))
        manifest["retired"][f"rules/{p.name}"] = str(target)

    for rel in stale:
        prev = owned[rel]
        dst = home / rel
        if dst.exists() or dst.is_symlink():
            remove(dst)
        if prev.get("backup") and Path(prev["backup"]).exists():
            shutil.move(prev["backup"], str(dst))

    if (hook_change or set_wt_head) and settings_path.exists():
        backup_root.mkdir(parents=True, exist_ok=True)
        if not (backup_root / "settings.json").exists():
            shutil.copy2(settings_path, backup_root / "settings.json")
    if hook_change:
        settings = new_settings
    if set_wt_head:
        settings.setdefault("worktree", {})["baseRef"] = "head"
        manifest["worktree_head"] = True
    manifest["settings_hook"] = True     # managed hooks are tracked by marker; uninstall removes them
    if hook_change or set_wt_head:
        write_json(settings_path, settings)

    write_json(home / MANIFEST, manifest)
    print("\n安装完成。新开会话后规则生效；技能与 hook 立即生效。运行 doctor 复查。")
    return 0


def drop_hook(settings_path: Path) -> None:
    s = load_json(settings_path)
    write_json(settings_path, apply_hooks(s, {event: None for event, _ in MANAGED_HOOKS}))


# ---------- uninstall ----------

def uninstall(home: Path, apply: bool) -> int:
    m = load_json(home / MANIFEST)
    if not m:
        print("未找到安装清单，无需卸载。")
        return 0
    for e in m.get("entries", []):
        print(f"  {'删除并恢复备份' if e.get('backup') else '删除'} {e['path']}")
    for rel in m.get("retired", {}):
        print(f"  恢复旧规则 {rel}")
    if m.get("settings_hook"):
        print("  settings.json: 移除 dev-spec 守卫与自动更新 hook")
    if m.get("worktree_head"):
        print("  settings.json: 移除安装器设置的 worktree.baseRef")
    if not apply:
        print("\n预览完成，未修改。加 --apply 执行。")
        return 0
    for e in m.get("entries", []):
        p = home / e["path"]
        if p.exists() or p.is_symlink():
            remove(p)
        if e.get("backup") and Path(e["backup"]).exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(e["backup"], str(p))
    for rel, backup in m.get("retired", {}).items():
        if Path(backup).exists() and not (home / rel).exists():
            (home / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(backup, str(home / rel))
    if m.get("settings_hook"):
        drop_hook(home / "settings.json")
    if m.get("worktree_head"):
        sp = home / "settings.json"
        s = load_json(sp)
        wt = s.get("worktree") or {}
        if wt.get("baseRef") == "head":
            wt.pop("baseRef")
            if not wt:
                s.pop("worktree", None)
            write_json(sp, s)
    for d in ("hooks", "agents", "skills", "rules", "workflows"):
        p = home / d
        if p.is_dir() and not any(x for x in p.iterdir() if x.name != ".DS_Store"):
            shutil.rmtree(p)
    for name in ("dev-spec-update.json", "dev-spec-update.log"):
        (home / name).unlink(missing_ok=True)
    shutil.rmtree(home / ".dev-spec-update.lock", ignore_errors=True)
    (home / MANIFEST).unlink()
    print("\n卸载完成。备份目录 dev-spec-backups/ 保留，确认无误后可手动删除。")
    return 0


# ---------- doctor ----------

def doctor(home: Path, check_version: bool) -> int:
    problems = 0
    if check_version:
        v = claude_version()
        if v is None:
            print("  提示 未找到 claude CLI（仅用桌面端时可忽略）")
        elif v < MIN_VERSION:
            print(f"  问题 CLI 版本 {'.'.join(map(str, v))} 低于要求 {'.'.join(map(str, MIN_VERSION))}，运行 claude update")
            problems += 1
        else:
            print(f"  正常 CLI 版本 {'.'.join(map(str, v))}")
    m = load_json(home / MANIFEST)
    if not m:
        print("  未安装（无清单）")
        return 1
    src_root = Path(m.get("source", SRC))
    if not src_root.exists():
        print(f"  问题 规范源不可达：{src_root}（外接盘未挂载？软链接模式下规范已失效）")
        problems += 1
    comps = dict(components(True))
    for e in m.get("entries", []):
        p, src = home / e["path"], comps.get(e["path"])
        if e["kind"] == "link":
            if not p.is_symlink():
                print(f"  问题 {e['path']} 不是软链接"); problems += 1
            elif not p.exists():
                print(f"  问题 {e['path']} 软链接失效 → {os.readlink(p)}"); problems += 1
            elif src and p.resolve() != src.resolve():
                print(f"  提示 {e['path']} 指向其他源 {p.resolve()}")
        elif not p.exists():
            print(f"  问题 {e['path']} 缺失"); problems += 1
        elif src and not same_tree(src, p):
            print(f"  提示 {e['path']} 与源不同（重新 --apply 同步）")
    settings = load_json(home / "settings.json")
    if apply_hooks(settings, desired_hooks(home, m.get("options", {}))) != settings:
        print("  问题 settings.json 中 dev-spec hooks 缺失或过期（重新 --apply）"); problems += 1
    opts = m.get("options", {})
    state = load_json(home / "dev-spec-update.json")
    print(f"  版本 {m.get('spec_version', 'unknown')}（安装时）；当前源 {source_version()}")
    rb = load_json(home / ROLLBACK_FILE)
    if rb:
        print(f"  已回滚到 {rb.get('tag')}（{rb.get('at')}，原 {rb.get('from_branch') or rb.get('from_sha', '')[:10]}），"
              "自动更新关闭；恢复：bash install.sh resume")
    if opts.get("auto_update"):
        print(f"  自动更新 开（通道 {opts.get('update_channel', 'stable')}，间隔 {opts.get('update_interval_hours', 6)}h）；最近检查 {state.get('last_check', '从未')}："
              f"{state.get('last_result', '-')}{'，版本 ' + state['version'] if state.get('version') else ''}")
        if str(state.get("last_result", "")).startswith(("拒绝", "失败")):
            print("  提示 最近一次自动更新未成功，详见 dev-spec-update.log")
    else:
        print("  自动更新 关")
    print("doctor:", "正常" if not problems else f"{problems} 个问题")
    return 1 if problems else 0


# ---------- rollback / resume ----------

ROLLBACK_FILE = "dev-spec-rollback.json"
RELEASE_TAG = re.compile(r"^v\d+\.\d+\.\d+$")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)


def _reinstall(repo: Path, home: Path, mode: str, *flags: str) -> subprocess.CompletedProcess:
    # run the installer of whatever version is now checked out (it may be older than this one)
    return subprocess.run([sys.executable, str(repo / "scripts/dev_spec_install.py"), "install", "--apply",
                           "--claude-home", str(home), f"--{mode}", *flags], capture_output=True, text=True)


def rollback(home: Path, tag: str) -> int:
    """Pin a device to an earlier release. Two independent guards keep it there: auto-update is switched
    off with a flag every installer since v1.0.0 understands, and the updater skips a detached checkout."""
    m = load_json(home / MANIFEST)
    if not m:
        return print("未安装（无清单）", file=sys.stderr) or 2
    repo, mode = Path(m.get("source", "")), m.get("mode", "copy")
    if mode == "link":
        return print("link 模式（开发机）的源就是开发仓库，请直接用 git 切换版本，不做回滚", file=sys.stderr) or 2
    if not RELEASE_TAG.match(tag):
        return print(f"只能回滚到发布 tag（vX.Y.Z）：{tag}", file=sys.stderr) or 2
    _git(repo, "fetch", "--quiet", "--tags", "origin")
    if _git(repo, "rev-parse", "--verify", "-q", f"refs/tags/{tag}").returncode != 0:
        return print(f"tag {tag} 不存在", file=sys.stderr) or 2
    if _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return print("规范源仓库有未提交改动，不回滚", file=sys.stderr) or 2
    branch = _git(repo, "symbolic-ref", "-q", "--short", "HEAD").stdout.strip()
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if _git(repo, "checkout", "--quiet", "--detach", f"refs/tags/{tag}").returncode != 0:
        return print(f"检出 {tag} 失败", file=sys.stderr) or 2
    r = _reinstall(repo, home, mode, "--no-auto-update")
    if r.returncode != 0:
        _git(repo, "checkout", "--quiet", branch or sha)
        _reinstall(repo, home, mode)
        print(f"回滚失败，已恢复原版本：{(r.stdout + r.stderr).strip()[-400:]}", file=sys.stderr)
        return 1
    write_json(home / ROLLBACK_FILE, {"tag": tag, "from_branch": branch, "from_sha": sha,
                                      "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    resume = f"git -C {shlex.quote(str(repo))} switch {branch or '<分支>'} && bash {shlex.quote(str(repo / 'install.sh'))} --apply --auto-update"
    print(f"已回滚到 {tag}，自动更新已关闭。\n恢复：bash install.sh resume（目标版本不支持时运行：{resume}）")
    return 0


def resume(home: Path) -> int:
    m, info = load_json(home / MANIFEST), load_json(home / ROLLBACK_FILE)
    if not info:
        return print("当前没有处于回滚状态", file=sys.stderr) or 2
    repo, mode = Path(m.get("source", "")), m.get("mode", "copy")
    target = info.get("from_branch") or info.get("from_sha")
    if _git(repo, "checkout", "--quiet", target).returncode != 0:
        return print(f"切回 {target} 失败", file=sys.stderr) or 2
    r = _reinstall(repo, home, mode, "--auto-update")
    if r.returncode != 0:
        return print(f"重新安装失败：{(r.stdout + r.stderr).strip()[-400:]}", file=sys.stderr) or 1
    (home / ROLLBACK_FILE).unlink(missing_ok=True)
    print(f"已恢复到 {target} 并重新开启自动更新；下次会话启动时按通道检查新版本")
    return 0


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser(description="dev-spec 安装器（默认仅预览）")
    ap.add_argument("command", nargs="?", default="install", choices=["install", "uninstall", "doctor", "rollback", "resume"])
    ap.add_argument("tag", nargs="?", help="rollback 的目标发布 tag（vX.Y.Z）")
    ap.add_argument("--apply", action="store_true", help="实际执行（默认只预览）")
    ap.add_argument("--link", action="store_true", help="软链接到本仓库（单一真源，修改即时生效）")
    ap.add_argument("--copy", action="store_true", help="复制文件（默认）")
    ap.add_argument("--force", action="store_true", help="同名且非本规范管理的条目备份后替换")
    ap.add_argument("--manage-claude-md", action="store_true", help="由本规范接管 ~/.claude/CLAUDE.md（旧文件备份）")
    ap.add_argument("--retire-legacy-rules", action="store_true", help="把 rules/ 下其他旧规则移入备份")
    ap.add_argument("--no-hooks", action="store_true", help="不安装 PreToolUse 守卫（记住该选择）")
    ap.add_argument("--hooks", action="store_true", help="重新启用 PreToolUse 守卫")
    ap.add_argument("--auto-update", action="store_true", help="开启自动更新（默认开启并记住选择）")
    ap.add_argument("--no-auto-update", action="store_true", help="关闭自动更新")
    ap.add_argument("--require-signed", action="store_true", help="自动更新只接受有有效签名的提交/tag")
    ap.add_argument("--channel", choices=["stable", "main"], help="自动更新通道：stable=发布 tag（默认），main=跟随主分支")
    ap.add_argument("--skip-version-check", action="store_true")
    ap.add_argument("--claude-home", default=os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    a = ap.parse_args()
    if a.link and a.copy:
        ap.error("--link 与 --copy 只能选一个")
    home = Path(a.claude_home).expanduser()
    print(f"目标: {home}\n源:   {SRC}\n")

    if a.command == "doctor":
        return doctor(home, not a.skip_version_check)
    if a.command == "rollback":
        return rollback(home, a.tag or "")
    if a.command == "resume":
        return resume(home)
    if a.command == "uninstall":
        return uninstall(home, a.apply)
    if not a.skip_version_check:
        v = claude_version()
        if v is not None and v < MIN_VERSION:
            sys.exit(f"CLI 版本 {'.'.join(map(str, v))} 低于要求 {'.'.join(map(str, MIN_VERSION))}："
                     "worktree 隔离等字段会被静默忽略。先运行 claude update，或加 --skip-version-check。")
        if v is None:
            print("提示：未找到 claude CLI，跳过版本检查（桌面端请确认已更新）。\n")
    return install(home, a)


if __name__ == "__main__":
    sys.exit(main())
