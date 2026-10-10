#!/usr/bin/env python3
"""ui_lint: objectively detectable UI problems. Taste is not judged here - only things a script can be sure about.

  ui_lint.py <file-or-dir>...        lint these UI source files
  ui_lint.py --changed [<base>]      lint UI files changed relative to base (default HEAD) plus untracked ones
  options: --json   machine output      --strict   exit 1 when anything is reported (default: always 0)

Two kinds of findings:
  per line   accessibility and interaction defects with a precise location (missing alt, removed focus ring,
             click handler on a non-interactive element, input without a label, emoji used as an icon, ...)
  per set    signs that values were not taken from one system across the linted files (too many distinct
             colours / font sizes / radii) and decoration used as a default (gradients, hover-scale, eyebrow labels)

A line containing `ui-lint: ignore` is skipped. Findings are hints for the author and for ui-reviewer.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

UI_EXT = {".tsx", ".jsx", ".vue", ".svelte", ".html", ".htm", ".css", ".scss", ".less", ".astro"}
MARKUP_EXT = {".tsx", ".jsx", ".vue", ".svelte", ".html", ".htm", ".astro"}
SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".next", "coverage", ".claude", "vendor",
             "__tests__", "__mocks__", "__snapshots__", "fixtures"}
TEST_FILE = re.compile(r"\.(?:test|spec|stories|story)\.[a-z]+$|(?:^|/)tests?/", re.I)   # not shipped UI
IGNORE = "ui-lint: ignore"

# pictographic emoji only: the emoji planes, a few colourful dingbats, and anything forced to emoji presentation.
# Plain text symbols (arrows, check marks, stars, box drawing, CJK punctuation) are not emoji.
EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2705\u2728\u274C\u2764\u2B50\u2B55\u26A1]|.\uFE0F")
TAG_OPEN = re.compile(r"<([A-Za-z][\w.-]*)(?=[\s/>])")
HEX = re.compile(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b")
FONT_SIZE = re.compile(r"font-size\s*:\s*([\d.]+(?:px|rem|em))|\btext-\[([\d.]+(?:px|rem|em))\]")
RADIUS = re.compile(r"border-radius\s*:\s*([\d.]+(?:px|rem|em|%))|\brounded-\[([\d.]+(?:px|rem|em|%))\]")
PLACEHOLDER_TEXT = re.compile(r"lorem ipsum|dolor sit amet|\bJohn Doe\b|\bJane Doe\b|\bAcme (?:Inc|Corp)\b", re.I)

THRESHOLDS = {"colors": 8, "font_sizes": 6, "radii": 3, "gradients": 3, "hover_scale": 3, "eyebrow": 3, "important": 3}


def collect(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files += [f for f in sorted(p.rglob("*")) if f.is_file() and f.suffix in UI_EXT
                      and not (set(f.parts) & SKIP_DIRS) and not TEST_FILE.search(f.as_posix())]
        elif p.is_file() and p.suffix in UI_EXT and not TEST_FILE.search(p.as_posix()):
            files.append(p)
    return files


def changed(base: str) -> list[str]:
    def git(*a: str) -> str:
        r = subprocess.run(["git", *a], capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else ""
    names = git("diff", "--name-only", "--no-renames", base, "--") + git("ls-files", "--others", "--exclude-standard")
    return sorted({n for n in names.splitlines() if n and Path(n).suffix in UI_EXT and Path(n).is_file()
                   and not (set(Path(n).parts) & SKIP_DIRS)})


def iter_tags(text: str):
    """Yield (name, attrs, start) for opening tags. Attribute values may contain `>` (JSX arrow functions,
    comparisons) and nested braces, so the end of a tag is found by tracking {} depth and quotes."""
    for m in TAG_OPEN.finditer(text):
        i, depth, quote = m.end(), 0, ""
        while i < len(text):
            c = text[i]
            if quote:
                if c == quote:
                    quote = ""
            elif c in "\"'`":
                quote = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth = max(0, depth - 1)
            elif c == ">" and depth == 0:
                yield m.group(1), text[m.end():i], m.start()
                break
            elif c == "<" and depth == 0:
                break                       # not a tag after all (e.g. a generic or a comparison)
            i += 1


def line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def attr(attrs: str, name: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(name)}\s*=", attrs) is not None


def lint_file(path: Path, text: str) -> list[dict]:
    out: list[dict] = []
    lines = text.splitlines()
    skip = {i + 1 for i, l in enumerate(lines) if IGNORE in l}

    def add(line: int, rule: str, msg: str) -> None:
        if line not in skip:
            out.append({"file": str(path), "line": line, "rule": rule, "message": msg})

    markup = path.suffix in MARKUP_EXT
    for i, l in enumerate(lines, 1):
        stripped = l.strip()
        is_comment = stripped.startswith(("//", "/*", "*", "<!--", "{/*"))
        if markup and not is_comment and EMOJI.search(l) and re.search(r"<[A-Za-z/]|>[^<>{}]*$|^[^<>{}]*<", l):
            add(i, "emoji-icon", "界面里出现 emoji：用项目的图标集，文案里也不用 emoji 充当装饰")
        if PLACEHOLDER_TEXT.search(l) and not is_comment:
            add(i, "placeholder-content", "占位内容（lorem / 假人名假公司）：换成真实长度的真实内容")
        if re.search(r"transition\s*:\s*all\b|\btransition-all\b", l):
            add(i, "transition-all", "transition: all 会让无关属性也动起来：只过渡需要的属性")
        if re.search(r"background-clip\s*:\s*text|\bbg-clip-text\b", l):
            add(i, "gradient-text", "渐变文字是常见的生成味默认值：没有品牌依据就用纯色")
        if re.search(r"\bfrom-(?:purple|violet|indigo|fuchsia)-\d+\b.*\bto-(?:pink|purple|blue|indigo|fuchsia)-\d+\b", l):
            add(i, "default-gradient", "紫/蓝/粉渐变是默认值而不是选择：沿用项目配色")

    # focus ring removed without a visible replacement anywhere in the same file
    if not re.search(r":focus-visible|focus-visible:|:focus-within|focus-within:|:focus\s*\{[^}]*(?:outline|box-shadow|border)[^}]*\}|focus:(?:ring|outline-(?!none)|border)", text, re.S):
        for m in re.finditer(r"outline\s*:\s*(?:none|0)\b|\bfocus:outline-none\b|\boutline-none\b", text):
            add(line_of(text, m.start()), "focus-removed", "去掉了焦点样式却没有提供替代：键盘用户会看不到焦点（加 :focus-visible 样式）")

    if markup:
        comment_lines = {i for i, l in enumerate(lines, 1) if l.strip().startswith(("//", "/*", "*", "<!--", "{/*"))}
        for tag, attrs, start in iter_tags(text):
            line = line_of(text, start)
            if line in comment_lines:
                continue
            low = tag            # native elements only: <Input>, <Image> … are components with their own contract
            clickable = re.search(r"(?<![\w-])(?:onClick|@click|v-on:click|on:click|onclick)\s*=", attrs)
            keyboard_ok = attr(attrs, "role") and (attr(attrs, "tabIndex") or attr(attrs, "tabindex"))
            # a dialog/backdrop that closes on outside click is a normal pattern (Esc handles the keyboard path)
            backdrop = re.search(r'role\s*=\s*["\'](?:dialog|presentation|none)["\']|aria-hidden|backdrop|overlay|\binset-0\b'
                                 r'|target\s*===?\s*\w+\.currentTarget', attrs, re.I)
            # spread props may carry role/tabIndex (headless widgets); a handler that only stops propagation is not an action
            spread = "{..." in attrs or "v-bind=" in attrs
            passive = re.search(r"(?:onClick|@click)\s*=\s*[{\"]\s*\(?\w*\)?\s*=>\s*\w+\.(?:stopPropagation|preventDefault)\(\)\s*[}\"]"
                                r"|@click\.stop\s*(?:=\s*\"\"|(?=[\s/>]|$))", attrs)
            if low in {"div", "span", "li", "p", "section", "img"} and clickable and not (keyboard_ok or backdrop or spread or passive):
                add(line, "click-on-noninteractive", f"<{tag}> 上绑定点击却不是按钮/链接：键盘无法操作（改用 <button>/<a>，或补 role 与 tabIndex 和键盘事件）")
            if low == "img" and not attr(attrs, "alt") and "{..." not in attrs:
                add(line, "img-alt", "<img> 缺少 alt：装饰图写 alt=\"\"，有意义的图写出含义")
            if low in {"input", "textarea"} and attr(attrs, "placeholder") and not re.search(r'type\s*=\s*["\'](?:hidden|submit|button|checkbox|radio)', attrs):
                labelled = attr(attrs, "aria-label") or attr(attrs, "aria-labelledby") or attr(attrs, "id") or "{..." in attrs
                wrapped = re.search(r"<label\b[^>]*>(?:(?!</label>).)*$", text[max(0, start - 400):start], re.S)
                if not labelled and not wrapped:
                    add(line, "placeholder-as-label", f"<{tag}> 只有占位符没有标签：占位符在输入后消失，补可见 <label> 或 aria-label")
            if low == "a" and re.search(r'href\s*=\s*["\']#["\']', attrs):
                add(line, "href-hash", "<a href=\"#\"> 不是导航：要么给真实地址，要么用 <button>")
    return out


def lint_set(texts: dict[Path, str]) -> list[dict]:
    """Signals across all linted files: values not taken from one system, decoration used by default."""
    out: list[dict] = []
    joined = "\n".join(l for t in texts.values() for l in t.splitlines() if IGNORE not in l)

    def add(rule: str, msg: str) -> None:
        out.append({"file": "(所检查的文件合计)", "line": 0, "rule": rule, "message": msg})

    colors = {c.lower() for c in HEX.findall(joined)}
    if len(colors) > THRESHOLDS["colors"]:
        add("too-many-colors", f"硬编码了 {len(colors)} 种颜色值（>{THRESHOLDS['colors']}）：收敛到设计 token（中性色 + 一个强调色 + 语义色）")
    sizes = {a or b for a, b in FONT_SIZE.findall(joined)}
    if len(sizes) > THRESHOLDS["font_sizes"]:
        add("too-many-font-sizes", f"出现 {len(sizes)} 种字号（>{THRESHOLDS['font_sizes']}）：定一套 5–6 级的字号阶梯")
    radii = {a or b for a, b in RADIUS.findall(joined)}
    if len(radii) > THRESHOLDS["radii"]:
        add("too-many-radii", f"出现 {len(radii)} 种圆角值（>{THRESHOLDS['radii']}）：圆角应只有一套")
    gradients = len(re.findall(r"(?:linear|radial|conic)-gradient\(|\bbg-gradient-to-", joined))
    if gradients >= THRESHOLDS["gradients"]:
        add("gradients", f"用了 {gradients} 处渐变：渐变作为装饰是生成味的默认值，确认每一处都有理由")
    scale = len(re.findall(r"\bhover:scale-|:hover\s*\{[^}]*transform\s*:\s*scale", joined, re.S))
    if scale >= THRESHOLDS["hover_scale"]:
        add("hover-scale", f"{scale} 处悬停放大：到处悬停放大是默认动效，只保留能说明'可点击'的一处，或都去掉")
    eyebrow = len(re.findall(r"text-transform\s*:\s*uppercase[^}]*letter-spacing|\buppercase\b[^\"'\n]*\btracking-", joined, re.S))
    if eyebrow >= THRESHOLDS["eyebrow"]:
        add("eyebrow-labels", f"{eyebrow} 处全大写加字距的小标签：每个标题上都放它是模板痕迹")
    important = len(re.findall(r"!important", joined))
    if important > THRESHOLDS["important"]:
        add("important", f"{important} 处 !important：通常说明选择器在互相覆盖，理顺层级而不是加权")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="可客观检出的界面问题（不判断审美）")
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--changed", nargs="?", const="HEAD", metavar="BASE", help="检查相对 BASE（默认 HEAD）改动的 UI 文件")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="有提示时退出码为 1")
    a = ap.parse_args()
    if a.changed is None and not a.paths:
        ap.error("给出文件/目录，或使用 --changed")
    missing = [x for x in a.paths if not Path(x).exists()]
    if missing:
        print(f"路径不存在：{', '.join(missing)}", file=sys.stderr)   # "0 个文件" must not look like a clean result
        return 2
    files = collect(changed(a.changed) if a.changed is not None else a.paths)
    texts = {f: f.read_text(errors="replace") for f in files}
    findings = [x for f, t in texts.items() for x in lint_file(f, t)] + (lint_set(texts) if texts else [])
    if a.json:
        print(json.dumps({"files": len(files), "findings": findings}, ensure_ascii=False, indent=2))
    else:
        for x in findings:
            loc = f"{x['file']}:{x['line']}" if x["line"] else x["file"]
            print(f"{loc}  [{x['rule']}] {x['message']}")
        print(f"ui_lint: {len(files)} 个文件，{len(findings)} 条提示" + ("" if findings or not files else "（未发现可客观检出的问题；审美与交互仍需真实渲染验证）"))
    return 1 if a.strict and findings else 0


if __name__ == "__main__":
    sys.exit(main())
