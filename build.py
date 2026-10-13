"""Build the DevOps course PDFs from Markdown in src/<course>/NN-*.md.

Usage:  python build.py [docker linux git networking]
Needs:  pip install markdown pygments pypdf reportlab   +  Google Chrome
"""

import html
import io
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import markdown
from pygments.formatters import HtmlFormatter
from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

COURSES = {
    "docker": ("Docker", "Containers from zero to production", "#1d63ed"),
    "linux": ("Linux", "The operating system behind everything you deploy", "#c2410c"),
    "git": ("Git", "Version control, collaboration and GitOps", "#d1361f"),
    "networking": ("Networking", "From packets and subnets to cloud and Kubernetes", "#0f766e"),
}

CALLOUTS = {"tip": "Tip", "warning": "Warning", "note": "Note", "interview": "Interview"}


def slug(text, seen):
    s = re.sub(r"[^a-z0-9]+", "-", html.unescape(re.sub(r"<[^>]+>", "", text)).lower()).strip("-") or "s"
    base, n = s, 2
    while s in seen:
        s, n = f"{base}-{n}", n + 1
    seen.add(s)
    return s


def render_body(src_dir):
    """Markdown files -> (body_html, headings[(level, id, text)])."""
    md = markdown.Markdown(
        extensions=["fenced_code", "tables", "codehilite", "sane_lists", "smarty"],
        extension_configs={"codehilite": {"guess_lang": False, "css_class": "hl"},
                           "smarty": {"smart_dashes": False}},
    )
    parts = []
    for f in sorted(src_dir.glob("[0-9][0-9]-*.md")):
        md.reset()
        parts.append(f'<section class="part">{md.convert(f.read_text())}</section>')
    body = "\n".join(parts)

    # Table cells escape pipes as "\|"; inside inline code Markdown keeps the backslash, so drop it.
    body = re.sub(r"<code>(.*?)</code>", lambda m: "<code>" + m.group(1).replace("\\|", "|") + "</code>", body, flags=re.S)

    headings, seen = [], set()

    def tag_heading(m):
        level, inner = int(m.group(1)), m.group(2)
        hid = slug(inner, seen)
        if level <= 2:
            headings.append((level, hid, re.sub(r"<[^>]+>", "", inner)))
        return f'<h{level} id="{hid}">{inner}</h{level}>'

    body = re.sub(r"<h([1-4])>(.*?)</h\1>", tag_heading, body, flags=re.S)

    # Markdown merges back-to-back "> **Tip:**" / "> **Warning:**" quotes; split them into separate boxes.
    def split_quote(m):
        return re.sub(r"</p>\s*<p><strong>(Tip|Warning|Note|Interview):?</strong>",
                      r"</p>\n</blockquote>\n<blockquote>\n<p><strong>\1:</strong>", m.group(0))

    body = re.sub(r"<blockquote>.*?</blockquote>", split_quote, body, flags=re.S)

    def tag_callout(m):
        kind = m.group(1).lower()
        return f'<blockquote class="callout {kind}">\n<p><strong>{CALLOUTS[kind]}:</strong>'

    body = re.sub(r"<blockquote>\s*<p><strong>(Tip|Warning|Note|Interview):?</strong>:?", tag_callout, body)
    return body, headings


def toc_html(headings, pages):
    rows = []
    for level, hid, text in headings:
        num = pages.get(hid, "")
        rows.append(
            f'<a class="toc-{level}" href="#{hid}"><span class="t">{html.escape(text)}</span>'
            f'<span class="dots"></span><span class="n">{num}</span></a>'
        )
    return '<section class="toc"><div class="toc-title">Contents</div>' + "\n".join(rows) + "</section>"


CSS = """
@page { size: A4; margin: 18mm 17mm 20mm 17mm; }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font-family: -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 10.3pt;
       line-height: 1.55; color: #1f2328; margin: 0; }
.cover { height: 257mm; display: flex; flex-direction: column; justify-content: space-between;
         page-break-after: always; padding: 8mm 4mm; }
.cover .band { height: 6mm; background: var(--accent); border-radius: 3px; }
.cover .kicker { font-size: 12pt; letter-spacing: .18em; text-transform: uppercase; color: var(--accent);
                 font-weight: 700; margin-top: 50mm; }
.cover .ctitle { font-weight: 800; font-size: 46pt; line-height: 1.05; margin: 6mm 0 4mm; border: 0; color: #0d1117; }
.cover .sub { font-size: 16pt; color: #57606a; max-width: 140mm; }
.cover .levels { display: flex; gap: 3mm; margin-top: 12mm; }
.cover .levels span { border: 1.2px solid var(--accent); color: var(--accent); border-radius: 20px;
                      padding: 1.5mm 4.5mm; font-size: 10pt; font-weight: 600; }
.cover .foot { color: #8c959f; font-size: 9.5pt; }
.toc { page-break-after: always; }
.toc-title { font-size: 24pt; font-weight: 700; color: var(--accent); border-bottom: 3px solid var(--accent); padding-bottom: 3mm; margin-bottom: 4mm; }
.toc a { display: flex; align-items: baseline; text-decoration: none; color: inherit; }
.toc a.toc-1 { font-weight: 700; font-size: 11.5pt; margin-top: 3.2mm; color: var(--accent); }
.toc a.toc-2 { padding-left: 6mm; font-size: 9.8pt; color: #333; }
.toc .dots { flex: 1; border-bottom: 1px dotted #b6bcc3; margin: 0 2mm; transform: translateY(-3px); }
.toc .n { min-width: 8mm; text-align: right; font-variant-numeric: tabular-nums; }
section.part { page-break-before: always; }
h1 { font-size: 24pt; color: var(--accent); margin: 0 0 6mm; padding-bottom: 3mm;
     border-bottom: 3px solid var(--accent); line-height: 1.2; }
h2 { font-size: 15.5pt; margin: 9mm 0 3mm; color: #0d1117; border-bottom: 1px solid #d0d7de; padding-bottom: 1.5mm; }
h3 { font-size: 12.3pt; margin: 6mm 0 2mm; color: #0d1117; }
h4 { font-size: 10.8pt; margin: 4.5mm 0 1.5mm; color: #3d444d; }
h2, h3, h4 { page-break-after: avoid; break-after: avoid; }
p, li { orphans: 3; widows: 3; }
a { color: var(--accent); }
code { font-family: Menlo, "SF Mono", Consolas, monospace; font-size: 8.9pt; background: #eff1f3;
       padding: .1em .35em; border-radius: 4px; }
pre { background: #f6f8fa; border: 1px solid #d8dee4; border-left: 3px solid var(--accent); border-radius: 6px;
      padding: 3mm 3.5mm; white-space: pre-wrap; word-break: break-word; line-height: 1.42; margin: 3mm 0; }
pre code { background: none; padding: 0; font-size: 8.5pt; }
.hl { page-break-inside: auto; }
table { border-collapse: collapse; width: 100%; margin: 3mm 0 4mm; font-size: 9.2pt; page-break-inside: auto; }
tr { page-break-inside: avoid; }
th { background: var(--accent); color: #fff; text-align: left; font-weight: 600; }
th, td { border: 1px solid #d0d7de; padding: 1.6mm 2.4mm; vertical-align: top; }
tbody tr:nth-child(even) td { background: #f6f8fa; }
blockquote { margin: 3.5mm 0; padding: 2.2mm 4mm; border-left: 4px solid #8c959f; background: #f6f8fa;
             border-radius: 0 6px 6px 0; page-break-inside: avoid; }
blockquote p { margin: 1mm 0; }
.callout.tip { border-color: #1a7f37; background: #eef9f0; } .callout.tip strong:first-child { color: #1a7f37; }
.callout.warning { border-color: #bf8700; background: #fff8e5; } .callout.warning strong:first-child { color: #9a6700; }
.callout.note { border-color: #0969da; background: #eef5ff; } .callout.note strong:first-child { color: #0969da; }
.callout.interview { border-color: #8250df; background: #f6f0ff; } .callout.interview strong:first-child { color: #8250df; }
hr { border: 0; border-top: 1px solid #d0d7de; margin: 6mm 0; }
"""


def page_html(name, title, sub, accent, toc, body):
    hl = HtmlFormatter(style="friendly").get_style_defs(".hl")
    cover = f"""<div class="cover"><div class="band"></div><div>
      <div class="kicker">DevOps Engineer Course</div><div class="ctitle">{title} for DevOps Engineers</div>
      <div class="sub">{sub}</div>
      <div class="levels"><span>Beginner</span><span>Intermediate</span><span>Advanced</span></div></div>
      <div class="foot">Concepts &middot; hands-on labs &middot; troubleshooting &middot; interview Q&amp;A &middot; cheat sheet</div></div>"""
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{title} for DevOps Engineers</title>
<style>:root{{--accent:{accent}}}{CSS}{hl} .hl pre {{ background:#f6f8fa; }}</style></head>
<body>{cover}{toc}{body}</body></html>"""


def chrome_pdf(html_text, out_pdf):
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "course.html"
        src.write_text(html_text)
        subprocess.run(
            [CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
             "--generate-pdf-document-outline", f"--print-to-pdf={out_pdf}", src.as_uri()],
            check=True, capture_output=True, timeout=600,
        )


def heading_pages(pdf_path, headings):
    """Map heading id -> printed page number, using the PDF outline Chrome generates."""
    reader = PdfReader(pdf_path)
    found = []

    def walk(items):
        for it in items:
            if isinstance(it, list):
                walk(it)
            else:
                found.append((it.title.strip(), reader.get_destination_page_number(it) + 1))

    walk(reader.outline)
    pages, i = {}, 0
    norm = lambda s: re.sub(r"\s+", "", html.unescape(s))
    for level, hid, text in headings:
        t = norm(text)
        for j in range(i, len(found)):
            if norm(found[j][0]) == t:
                pages[hid], i = found[j][1], j + 1
                break
    return pages


def add_footer(pdf_path, label, accent, titles=None):
    reader, writer = PdfReader(pdf_path), PdfWriter()
    for n, page in enumerate(reader.pages, start=1):
        if n > 1:
            w, h = float(page.mediabox.width), float(page.mediabox.height)
            buf = io.BytesIO()
            c = canvas.Canvas(buf, pagesize=(w, h))
            c.setFont("Helvetica", 8)
            c.setFillColor(HexColor("#8c959f"))
            c.drawString(48, 26, label)
            c.setFillColor(HexColor(accent))
            c.drawRightString(w - 48, 26, str(n))
            c.save()
            buf.seek(0)
            page.merge_page(PdfReader(buf).pages[0])
        writer.add_page(page)
    # Keep Chrome's bookmarks: pypdf lists an item's children as the list right after it.
    def copy(items, parent=None):
        last = parent
        for it in items:
            if isinstance(it, list):
                copy(it, last)
            else:
                title = (titles or {}).get(re.sub(r"\s+", "", it.title), it.title)
                last = writer.add_outline_item(title, reader.get_destination_page_number(it), parent=parent)
    copy(reader.outline)
    writer.add_metadata({"/Title": label})
    # Each footer overlay copies the page's fonts; dedupe them or the file balloons to ~50 MB.
    writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
    for page in writer.pages:
        page.compress_content_streams()
    with open(pdf_path, "wb") as f:
        writer.write(f)


def build(name):
    title, sub, accent = COURSES[name]
    body, headings = render_body(ROOT / "src" / name)
    if not headings:
        print(f"skip {name}: no Markdown in src/{name}")
        return
    out = ROOT / f"{title}_for_DevOps_Engineers.pdf"
    # Pass 1 finds the page of every heading; pass 2 prints those numbers in the contents.
    chrome_pdf(page_html(name, title, sub, accent, toc_html(headings, {}), body), out)
    pages = heading_pages(out, headings)
    chrome_pdf(page_html(name, title, sub, accent, toc_html(headings, pages), body), out)
    clean = {re.sub(r"\s+", "", html.unescape(t)): html.unescape(t) for _, _, t in headings}
    add_footer(out, f"{title} for DevOps Engineers", accent, clean)
    print(f"{out.name}: {len(PdfReader(out).pages)} pages, {len(pages)}/{len(headings)} headings numbered")


if __name__ == "__main__":
    for course in sys.argv[1:] or COURSES:
        build(course)
