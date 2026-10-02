"""Sync HappyFox's REST API help articles into this directory as Markdown.

Fetches the API index article and every article it links to from the public knowledge-base
API, converts each HTML body to Markdown, writes `<id>-<slug>.md` and deletes any other file
here with a name of that form. Run `uv run Docs/sync.py`, then review what changed upstream
with `git diff Docs/`.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

BASE = "https://support.happyfox.com"
INDEX_ID = 360
OUT = Path(__file__).resolve().parent
OWNED = re.compile(r"^\d+-[a-z0-9-]+\.md$")
LINK = re.compile(r'href="(?:https?://support\.happyfox\.com)?/kb/article/(\d+)')

VOID = {"area", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
BLOCK = HEADINGS | {
    "blockquote", "body", "colgroup", "dd", "div", "dl", "dt", "head", "hr", "html", "li",
    "ol", "p", "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}
SKIP = {"colgroup", "head", "script", "style"}
# First line of a quoted or paragraph block that marks it as a code sample.
CODE_START = re.compile(r"[{}\[\]]|import\s|from\s+\S+\s+import\s|<\?php|curl\s")


class Node:
    def __init__(self, tag: str, attrs: dict[str, str | None], parent: Node | None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[Node | str] = []

    def find_all(self, tag: str) -> list[Node]:
        found = []
        for child in self.children:
            if isinstance(child, Node):
                if child.tag == tag:
                    found.append(child)
                found.extend(child.find_all(tag))
        return found

    def text(self) -> str:
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)


class Tree(HTMLParser):
    """Lenient DOM builder; unknown tags stay in the tree and render as their children."""

    def __init__(self, markup: str) -> None:
        super().__init__(convert_charrefs=True)
        self.root = self.cur = Node("root", {}, None)
        self.feed(markup)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs), self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, dict(attrs), self.cur))

    def handle_endtag(self, tag):
        node = self.cur
        while node.parent is not None and node.tag != tag:
            node = node.parent
        if node.parent is not None:
            self.cur = node.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def escape(text: str) -> str:
    """Escape prose so placeholders like `<module>` survive Markdown rendering."""
    return text.replace("`", "\\`").replace("*", "\\*").replace("<", "\\<")


def escape_line(line: str) -> str:
    """Keep a prose line that starts like list, heading, or quote syntax literal."""
    line = re.sub(r"^(\d+)([.)])(?=\s|$)", r"\1\\\2", line)
    line = re.sub(r"^([-+]|#{1,6})(?=\s|$)", r"\\\1", line)
    line = re.sub(r"^(>|={2,}\s*$|-{2,}\s*$)", r"\\\1", line)
    return line


def url(href: str) -> str:
    return re.sub(r"[ <>()]", lambda m: f"%{ord(m.group(0)):02X}", href.strip())


def inline(nodes: list[Node | str], mode: str = "prose") -> str:
    """Render inline content. `prose` escapes and collapses whitespace; `code` collapses it
    like a browser; `pre` keeps it. `<br>` always becomes a newline."""
    parts = []
    for node in nodes:
        if isinstance(node, str):
            if mode == "pre":
                parts.append(node)
            else:
                text = re.sub(r"[ \t\r\n]+", " ", node)
                parts.append(escape(text) if mode == "prose" else text)
            continue
        tag = node.tag
        if tag == "br":
            parts.append("\n")
        elif tag == "img":
            if src := node.attrs.get("src"):
                alt = (node.attrs.get("alt") or "").replace("]", "\\]")
                parts.append(f"![{alt}]({url(src)})")
        elif tag == "a" and mode == "prose":
            label = inline(node.children, mode)
            href = (node.attrs.get("href") or "").strip()
            if not label.strip():
                continue
            if href.startswith(("http://", "https://")):
                plain = " ".join(node.text().split())
                parts.append(url(href) if plain == href else f"[{label.strip()}]({url(href)})")
            else:
                parts.append(label)
        elif tag == "code" and mode == "prose":
            code = " ".join(node.text().replace("\xa0", " ").split())
            if code:
                tick = "``" if "`" in code else "`"
                parts.append(f"{tick}{code}{tick}")
        elif tag in BLOCK:
            parts.append("\n" + inline(node.children, mode) + "\n")
        else:
            parts.append(inline(node.children, mode))
    return "".join(parts)


def paragraphs(nodes: list[Node | str]) -> list[str]:
    lines = inline(nodes).replace("\xa0", " ").split("\n")
    out: list[str] = []
    current: list[str] = []
    for line in lines:
        line = re.sub(r" {2,}", " ", line).strip()
        if line:
            current.append(escape_line(line))
        elif current:
            out.append("\n".join(current))
            current = []
    if current:
        out.append("\n".join(current))
    return out


def code_lines(node: Node) -> list[str]:
    """Visible lines of a code sample, whether it is one `<p>` per line, `<br>`-separated,
    or a `<pre>`."""
    lines: list[str] = []
    run: list[Node | str] = []

    def flush() -> None:
        text = inline(run, "code")
        if text.strip() or "\n" in text:
            for line in text.split("\n"):
                lines.append(line.lstrip(" ").replace("\xa0", " ").rstrip())
        run.clear()

    for child in node.children:
        if isinstance(child, Node) and child.tag == "pre":
            flush()
            lines.extend(pre_lines(child))
        elif isinstance(child, Node) and child.tag in BLOCK:
            flush()
            lines.extend(code_lines(child))
        else:
            run.append(child)
    flush()
    return lines


def pre_lines(node: Node) -> list[str]:
    text = inline(node.children, "pre").replace("\xa0", " ")
    return undouble([line.rstrip() for line in text.removeprefix("\n").split("\n")])


def undouble(lines: list[str]) -> list[str]:
    """Trim blank edges and drop the blank line some samples put after every line."""
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    if len(lines) >= 3 and all(not lines[i].strip() for i in range(1, len(lines), 2)):
        lines = lines[::2]
    return lines


def is_code(node: Node) -> bool:
    first = next((line.strip() for line in code_lines(node) if line.strip()), "")
    return bool(CODE_START.match(first))


def fence(lines: list[str]) -> str:
    squeezed: list[str] = []
    for line in undouble(lines):
        if line.strip() or (squeezed and squeezed[-1].strip()):
            squeezed.append(line)
    first = squeezed[0].strip() if squeezed else ""
    if first[:1] in "{}[]":
        lang = "json"
    elif first.startswith(("import ", "from ", "#")):
        lang = "python"
    elif first.startswith("<?php"):
        lang = "php"
    elif first.startswith("curl"):
        lang = "sh"
    else:
        lang = ""
    body = "\n".join(squeezed)
    mark = "~~~" if "```" in body else "```"
    return f"{mark}{lang}\n{body}\n{mark}"


def one_line(node: Node) -> str:
    return " ".join(inline(node.children).replace("\xa0", " ").split())


def all_bold(node: Node, bold: bool = False) -> bool:
    bold = bold or node.tag in ("b", "strong")
    for child in node.children:
        if isinstance(child, str):
            if child.strip(" \t\r\n\xa0") and not bold:
                return False
        elif not all_bold(child, bold):
            return False
    return True


def section_title(node: Node) -> str | None:
    """Title of a `<p>` that opens a section: a named anchor the page's TOC links to, or a
    numbered line set entirely in bold."""
    links = node.find_all("a")
    for a in links:
        if (a.attrs.get("id") or a.attrs.get("name")) and not a.attrs.get("href"):
            if title := one_line(a):
                return title
    if any(a.attrs.get("href") for a in links):
        return None
    title = one_line(node)
    numbered = re.match(r"\d+(\.\d+)*\.?\s*[A-Za-z]", title)
    return title if numbered and len(title) <= 100 and all_bold(node) else None


def quote(parts: list[str]) -> list[str]:
    if not parts:
        return []
    lines = "\n\n".join(parts).split("\n")
    return ["\n".join(f"> {line}" if line else ">" for line in lines)]


def listing(node: Node, ordered: bool, in_cell: bool) -> list[str]:
    items: list[str] = []
    number = int(node.attrs.get("start") or 1)
    for child in node.children:
        if not isinstance(child, Node):
            continue
        if child.tag in ("ul", "ol") and items:
            nested = listing(child, child.tag == "ol", in_cell)
            if nested:
                items[-1] += "\n" + "\n".join("  " + line for line in nested[0].split("\n"))
            continue
        if child.tag != "li":
            continue
        parts = blocks(child, in_cell)
        if not parts:
            continue
        marker = f"{number}." if ordered else "-"
        pad = " " * (len(marker) + 1)
        lines = "\n".join(parts).split("\n")
        rest = "".join("\n" + (pad + line if line else "") for line in lines[1:])
        items.append(f"{marker} {lines[0]}{rest}")
        number += 1
    return ["\n".join(items)] if items else []


def table(node: Node) -> list[str]:
    rows = []
    for tr in node.find_all("tr"):
        cells = [c for c in tr.children if isinstance(c, Node) and c.tag in ("td", "th")]
        row = []
        for cell in cells:
            lines = [ln for part in blocks(cell, in_cell=True) for ln in part.split("\n")]
            row.append("<br>".join(ln for ln in lines if ln.strip()).replace("|", "\\|"))
        if any(row):
            rows.append(row)
    if not rows:
        return []
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(row) + " |" for row in rows[1:]]
    return ["\n".join(out)]


def block(node: Node, in_cell: bool) -> list[str]:
    tag = node.tag
    if tag in SKIP:
        return []
    if tag in HEADINGS:
        text = one_line(node)
        return [f"{'#' * int(tag[1])} {text}"] if text else []
    if tag == "hr":
        return [] if in_cell else ["---"]
    if tag in ("ul", "ol"):
        return listing(node, tag == "ol", in_cell)
    if tag == "table":
        return table(node)
    if tag == "pre" and not in_cell:
        return [fence(pre_lines(node))]
    if tag in ("blockquote", "p", "div") and not in_cell and is_code(node):
        return [fence(code_lines(node))]
    if tag == "blockquote" or (tag == "div" and "alert" in (node.attrs.get("class") or "")):
        return quote(blocks(node, in_cell))
    parts = blocks(node, in_cell)
    if tag == "p" and not in_cell and parts and (title := section_title(node)):
        first, _, rest = parts[0].partition("\n")
        if first.replace("\\", "") == title.replace("\\", ""):
            parts[:1] = [f"## {title}"] + ([rest] if rest else [])
    return [p for p in parts if p != "Back to top"]


def blocks(node: Node, in_cell: bool = False) -> list[str]:
    out: list[str] = []
    run: list[Node | str] = []
    for child in node.children:
        if isinstance(child, str) or child.tag not in BLOCK:
            run.append(child)
            continue
        out.extend(paragraphs(run))
        run = []
        for part in block(child, in_cell):
            append(out, part)
    out.extend(paragraphs(run))
    return out


def append(out: list[str], part: str) -> None:
    """Add a block, joining a code sample the page split across adjacent quotes."""
    if out and part.startswith(("```", "~~~")):
        opener, _, body = part.partition("\n")
        prev_opener, _, prev_body = out[-1].partition("\n")
        if opener == prev_opener:
            out[-1] = f"{prev_opener}\n{prev_body[:-4]}\n{body}"
            return
    out.append(part)


def render(article: dict) -> str:
    body = "\n\n".join(blocks(Tree(article["contents"]).root))
    source = f"{BASE}/kb/article/{article['id']}-{article['slug']}/"
    updated = article["last_updated_at"]
    head = f"# {article['title'].strip()}\n\nSource: {source} (last updated {updated} UTC)"
    return re.sub(r"\n{3,}", "\n\n", f"{head}\n\n{body}\n")


def fetch(article_id: int) -> dict | None:
    """Return one article from the public KB API, or None if it no longer exists."""
    request = urllib.request.Request(
        f"{BASE}/api/1.1/json/kb/article/{article_id}/",
        headers={"Accept": "application/json", "User-Agent": "hfox-docs-sync"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def main() -> int:
    index = fetch(INDEX_ID)
    if index is None:
        print(f"error: index article {INDEX_ID} not found", file=sys.stderr)
        return 1
    linked = dict.fromkeys(int(i) for i in LINK.findall(index["contents"]))
    linked.pop(INDEX_ID, None)
    articles = [index]
    for article_id in linked:
        article = fetch(article_id)
        if article is None:
            print(f"warning: linked article {article_id} no longer exists", file=sys.stderr)
        else:
            articles.append(article)
    written = set()
    for article in articles:
        name = f"{article['id']}-{article['slug']}.md"
        (OUT / name).write_text(render(article), encoding="utf-8")
        written.add(name)
        print(f"wrote {name}", file=sys.stderr)
    for path in sorted(OUT.glob("*.md")):
        if OWNED.match(path.name) and path.name not in written:
            path.unlink()
            print(f"removed {path.name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
