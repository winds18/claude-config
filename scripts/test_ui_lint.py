#!/usr/bin/env python3
"""Tests for skills/ui-quality/scripts/ui_lint.py: every rule has a detection case and a must-not-flag case.
Run: python3 scripts/test_ui_lint.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "skills/ui-quality/scripts/ui_lint.py"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def lint(tmp: Path, files: dict[str, str], *args: str) -> tuple[int, dict]:
    d = Path(tempfile.mkdtemp(dir=tmp))
    for name, body in files.items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(body)
    p = subprocess.run([sys.executable, str(TOOL), str(d), "--json", *args], capture_output=True, text=True)
    return p.returncode, json.loads(p.stdout or "{}")


def rules(report: dict) -> list[str]:
    return [f["rule"] for f in report.get("findings", [])]


def case(tmp: Path, name: str, rule: str, bad: dict[str, str], good: dict[str, str]) -> None:
    _, r = lint(tmp, bad)
    check(f"{name}: 检出", rule in rules(r), json.dumps(r, ensure_ascii=False)[:300])
    _, r = lint(tmp, good)
    check(f"{name}: 合理写法不误报", rule not in rules(r), json.dumps(r, ensure_ascii=False)[:300])


def main() -> int:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(os.path.realpath(t))

        case(tmp, "div 绑点击", "click-on-noninteractive",
             {"a.tsx": "export const A = () => <div onClick={() => go()}>打开</div>\n"},
             {"a.tsx": "export const A = () => <><button onClick={() => go()}>打开</button>\n"
                       "<div onClick={() => a > b ? go() : stop()} role=\"button\" tabIndex={0} onKeyDown={k}>x</div>\n"
                       "<div className=\"card\">静态内容</div></>\n"})
        case(tmp, "Vue 的 @click", "click-on-noninteractive",
             {"a.vue": "<template><span @click=\"open\">打开</span></template>\n"},
             {"a.vue": "<template><button @click=\"open\">打开</button></template>\n"})
        case(tmp, "图片缺 alt", "img-alt",
             {"a.html": "<img src=\"a.png\">\n"},
             {"a.html": "<img src=\"a.png\" alt=\"\">\n<img src=\"b.png\" alt=\"订单流程图\">\n", "b.tsx": "const I = (p) => <img {...p} />\n"})
        case(tmp, "占位符当标签", "placeholder-as-label",
             {"a.html": "<form><input type=\"text\" placeholder=\"邮箱\"></form>\n"},
             {"a.html": "<label>邮箱 <input type=\"text\" placeholder=\"name@example.com\"></label>\n"
                        "<label for=\"e\">邮箱</label><input id=\"e\" placeholder=\"name@example.com\">\n"
                        "<input aria-label=\"搜索\" placeholder=\"搜索\">\n<input type=\"submit\" placeholder=\"x\">\n"})
        case(tmp, "去掉焦点样式", "focus-removed",
             {"a.css": "button { outline: none; }\n"},
             {"a.css": "button { outline: none; }\nbutton:focus-visible { outline: 2px solid #06c; }\n",
              "b.tsx": "const B = () => <button className=\"focus:outline-none focus-visible:ring-2\">x</button>\n"})
        case(tmp, "emoji 当图标", "emoji-icon",
             {"a.tsx": "export const A = () => <><button>🚀 开始</button><p>完成 ✅</p><p>注意 ⚠️</p></>\n"},
             {"a.tsx": "// 🚀 注释里的 emoji 不算\nexport const A = () => <button>开始 → 下一步 ✓ ★ ← ↑ 中文标点，。「」</button>\n",
              "a.css": ".x::before { content: '🚀'; } /* css 不查 */\n"})
        case(tmp, "占位内容", "placeholder-content",
             {"a.html": "<p>Lorem ipsum dolor sit amet</p>\n"},
             {"a.html": "<p>本月订单 128 笔，比上月多 12 笔</p>\n"})
        case(tmp, "transition: all", "transition-all",
             {"a.css": ".b { transition: all .2s; }\n"},
             {"a.css": ".b { transition: opacity .2s, transform .2s; }\n"})
        case(tmp, "渐变文字", "gradient-text",
             {"a.css": ".t { background-clip: text; }\n"},
             {"a.css": ".t { color: #111; }\n"})
        case(tmp, "默认紫色渐变", "default-gradient",
             {"a.tsx": "const H = () => <div className=\"bg-gradient-to-r from-purple-500 to-pink-500\" />\n"},
             {"a.tsx": "const H = () => <div className=\"bg-white text-purple-700\" />\n"})
        case(tmp, "href=#", "href-hash",
             {"a.html": "<a href=\"#\">删除</a>\n"},
             {"a.html": "<a href=\"/orders\">订单</a><a href=\"#section-2\">跳到第二节</a>\n"})

        # set-level thresholds: just above flags, at the limit does not
        many = "".join(f".c{i} {{ color: #{i:02x}{i:02x}{i:02x}; }}\n" for i in range(1, 10))
        few = "".join(f".c{i} {{ color: #{i:02x}{i:02x}{i:02x}; }}\n" for i in range(1, 9))
        case(tmp, "颜色未收敛（9 种 > 8）", "too-many-colors", {"a.css": many}, {"a.css": few})
        case(tmp, "字号未收敛", "too-many-font-sizes",
             {"a.css": "".join(f".f{i} {{ font-size: {10 + i}px; }}\n" for i in range(7))},
             {"a.css": "".join(f".f{i} {{ font-size: {10 + i}px; }}\n" for i in range(6))})
        case(tmp, "圆角未收敛", "too-many-radii",
             {"a.css": "".join(f".r{i} {{ border-radius: {2 * i + 2}px; }}\n" for i in range(4))},
             {"a.css": "".join(f".r{i} {{ border-radius: {2 * i + 2}px; }}\n" for i in range(3))})
        case(tmp, "渐变泛滥", "gradients",
             {"a.css": ".a{background:linear-gradient(red,blue)}\n.b{background:radial-gradient(red,blue)}\n.c{background:linear-gradient(1deg,red,blue)}\n"},
             {"a.css": ".a{background:linear-gradient(red,blue)}\n"})
        case(tmp, "到处悬停放大", "hover-scale",
             {"a.tsx": "const A = () => <><i className=\"hover:scale-105\"/><i className=\"hover:scale-110\"/><i className=\"hover:scale-105\"/></>\n"},
             {"a.tsx": "const A = () => <i className=\"hover:scale-105\"/>\n"})
        case(tmp, "全大写小标签泛滥", "eyebrow-labels",
             {"a.tsx": "const A = () => <>" + "<p className=\"uppercase tracking-widest\">x</p>" * 3 + "</>\n"},
             {"a.tsx": "const A = () => <p className=\"uppercase tracking-widest\">x</p>\n"})
        case(tmp, "!important 过多", "important",
             {"a.css": ".a{color:red !important}\n" * 4}, {"a.css": ".a{color:red !important}\n" * 3})

        # noise found on a real 1100-file project: each must NOT be reported
        _, r = lint(tmp, {"a.test.tsx": "const A = () => <><img src=\"a.png\" /><button>👍</button><div onClick={f}>x</div></>\n",
                          "__tests__/b.tsx": "const B = () => <img src=\"a.png\" />\n", "c.stories.tsx": "const C = () => <a href=\"#\">x</a>\n"})
        check("测试与 story 文件不检查", r.get("files") == 0, str(r))
        _, r = lint(tmp, {"a.tsx": "// Photos still use <img> here\n/* legacy: <div onClick={x}> */\nconst A = () => <p>ok</p>\n"})
        check("注释里提到的标签不报", not r.get("findings"), str(r))
        _, r = lint(tmp, {"a.tsx": "const A = () => <><Input placeholder=\"搜索\" /><Image src={s} /><Link href=\"#\">x</Link></>\n"})
        check("自定义组件（大写）不按原生元素检查", not r.get("findings"), str(r))
        _, r = lint(tmp, {"a.tsx": "const M = () => <>\n<div className=\"fixed inset-0 bg-black/50\" onClick={close} />\n"
                                   "<div role=\"dialog\" aria-modal=\"true\" onClick={(e) => e.target === e.currentTarget && close()}>x</div>\n"
                                   "<div className={styles.backdrop} onClick={() => setOpen(false)} /></>\n"})
        check("弹窗遮罩点击关闭不报", "click-on-noninteractive" not in rules(r), str(r))
        _, r = lint(tmp, {"a.tsx": "const T = () => <>\n<div {...rowProps} onClick={select}>行</div>\n"
                                   "<div className=\"grid\" onClick={e => e.stopPropagation()}>内容</div></>\n"})
        check("带属性展开的控件与只阻止冒泡的处理不报", "click-on-noninteractive" not in rules(r), str(r))
        _, r = lint(tmp, {"a.tsx": "const reactions = ['👍', '❤️']\nconst msg = 'Message from 🤖 bot'\nconst A = () => <p>{msg}</p>\n"})
        check("数据里的 emoji 不报（只查标记所在的行）", "emoji-icon" not in rules(r), str(r))
        _, r = lint(tmp, {"a.tsx": "const F = () => <div className=\"focus-within:ring-2\"><input aria-label=\"x\" className=\"outline-none\" /></div>\n"})
        check("外层 focus-within 提供焦点样式时不报", "focus-removed" not in rules(r), str(r))

        # mechanics
        _, r = lint(tmp, {"a.html": "<img src=\"a.png\"> <!-- ui-lint: ignore -->\n"})
        check("ui-lint: ignore 跳过该行", "img-alt" not in rules(r), str(r))
        _, r = lint(tmp, {"a.py": "<img src='a.png'>\n", "node_modules/x/a.html": "<img src=\"a.png\">\n"})
        check("只检查 UI 源文件并跳过 node_modules", r.get("files") == 0 and not r.get("findings"), str(r))
        _, r = lint(tmp, {"a.html": "<p>\n\n<img src=\"a.png\">\n"})
        f = [x for x in r["findings"] if x["rule"] == "img-alt"]
        check("报告准确的文件与行号", f and f[0]["line"] == 3 and f[0]["file"].endswith("a.html"), str(f))
        code, _ = lint(tmp, {"a.html": "<img src=\"a.png\">\n"})
        strict, _ = lint(tmp, {"a.html": "<img src=\"a.png\">\n"}, "--strict")
        clean, _ = lint(tmp, {"a.html": "<img src=\"a.png\" alt=\"\">\n"}, "--strict")
        check("默认退出 0，--strict 有提示时为 1、无提示为 0", (code, strict, clean) == (0, 1, 0), f"{code},{strict},{clean}")

        # --changed in a git repo: only changed and untracked UI files
        repo = tmp / "repo"
        repo.mkdir()
        g = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True)
        g("init", "-q", "-b", "main"); g("config", "user.email", "t@example.com"); g("config", "user.name", "t")
        (repo / "old.html").write_text("<img src=\"old.png\">\n")
        (repo / "keep.html").write_text("<p>ok</p>\n")
        g("add", "-A"); g("commit", "-qm", "init")
        (repo / "keep.html").write_text("<a href=\"#\">x</a>\n")
        (repo / "new.tsx").write_text("const A = () => <div onClick={() => 1}>x</div>\n")
        p = subprocess.run([sys.executable, str(TOOL), "--changed", "--json"], cwd=repo, capture_output=True, text=True)
        r = json.loads(p.stdout or "{}")
        check("--changed 只看改动与未跟踪的 UI 文件", r.get("files") == 2 and set(rules(r)) == {"href-hash", "click-on-noninteractive"}, p.stdout[:400])
        p = subprocess.run([sys.executable, str(TOOL)], cwd=repo, capture_output=True, text=True)
        check("无参数时报用法错误", p.returncode == 2)
        p = subprocess.run([sys.executable, str(TOOL), "no/such/dir"], cwd=repo, capture_output=True, text=True)
        check("路径不存在时报错而不是报告 0 个文件", p.returncode == 2 and "不存在" in p.stderr, p.stdout + p.stderr)

    failed = [r for r in results if not r[1]]
    for name, _, detail in failed:
        print(f"FAIL {name}: {detail[:400]}")
    print(f"ui-lint: {len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
