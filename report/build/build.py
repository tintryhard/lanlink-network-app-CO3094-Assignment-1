"""Build the LanLink report: Markdown -> HTML (marked) -> one report.html -> PDF (headless Chrome).

Run `python build.py` from report/build/. One command does everything:

1. converts report/sections/*.md and title.md with `npx -y marked@18.2.0 --gfm`;
2. post-processes the HTML (figures, table wrappers and column widths, captions, heading
   numbers, contents and lists) and writes a self-contained report.html;
3. prints it to LanLink-Report.pdf, reads the real page of every heading, figure and table
   back from the PDF's link destinations, and reprints until the page numbers are stable;
4. runs the layout checks and exits non-zero if one fails.

Every figure must come from report/diagrams/lucid/<name>.png. If one is missing the build
stops with a non-zero exit code; it never falls back to a Mermaid SVG. The one exception is
a screenshot the team has not taken yet, written ![Figure N. Caption.](placeholder:<slug>):
it prints as a framed "TODO: screenshot" box with its normal caption and list entry.

Team-owned text: title.md (cover page) and report/sections/07-genai.md (Use of Generative
AI). Open items are a bold **TODO** plus an <!-- TODO: ... --> comment; the build counts
the visible TODOs and fails if any "[STUDENT" text is left.
"""
import base64, html, pathlib, re, struct, subprocess, sys
import pymupdf

B = pathlib.Path(__file__).resolve().parent
R = B.parent
CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe"
MARKED = "marked@18.2.0"
# Body order: numbered sections 1-6, the unnumbered "Use of Generative AI" page (07-genai.md,
# written by the team), then the appendix (90-* starts with "# Appendix", sub-headings "## A ...").
SECS = ["01-architecture", "02-protocol", "03-sequences", "04-challenges", "05-testing",
        "06-conclusion", "07-genai", "90-apx-errors", "91-apx-limits", "92-apx-threads-locks",
        "93-apx-evidence"]
PH_W, PH_H = 16.0, 7.0               # placeholder screenshot box (cm): ![Figure N. ...](placeholder:slug)
OUT = B / "LanLink-Report.pdf"

# ---- layout constants (cm) ----
TEXT_W, TEXT_H = 16.0, 24.9          # A4 minus the margins in report.css
FIG_MAX_H = 22.7                     # leaves room for a two-line caption on the same page
OWN_PAGE_MAX_H = 23.4                # a figure alone on its page (caption of up to two lines)
MIN_DPI = 150                        # never print a PNG below this (no visible upscaling)
# Smallest text in each Lucid PNG, in PNG pixels (measured by eye on native-size crops: the
# message labels, guards and edge labels). The build prints the size that text gets on paper
# and stops if it would fall below MIN_LABEL_PT, the size of the smallest label that was
# already in the report before the sequence diagrams were allowed to shrink (Figure 9).
SMALLEST_PX = {"architecture": 16, "frame-layout": 16, "state-connection": 14,
               "state-transfer": 16, "seq-login": 16, "seq-chat": 16, "seq-file": 16,
               "seq-file-failures": 16, "seq-file-failures-2": 16}
MIN_LABEL_PT = 3.2
# per-figure width caps. architecture: 15.2 cm lets Figure 1 and its caption fit under the
# opening lines of 1.2 on the first body page instead of leaving that page almost empty.
FIG_MAX_W = {"architecture": 15.2, "state-connection": 13.0, "state-transfer": 14.5,
             "seq-file-failures-2": 13.2}
# per-figure height caps (cm): Figures 5 and 6 print at 17.5 cm instead of a full page, so
# about nine lines of text share their pages (smallest label 3.5 pt).
FIG_MAX_H_FIG = {"seq-login": 16.5, "seq-chat": 16.5}
OWN_PAGE = {"seq-file"}              # figures that get a page of their own
# A tall figure cannot start in the space left on a page, so it would leave a hole.
# FLOAT[name] = n moves the figure below the n blocks (paragraphs, lists or headings) that
# follow it, like a LaTeX float: the text fills the page and the figure opens the next one.
# Wording is untouched. Applied in this order (a later float may jump over an earlier one's
# blocks, never over a figure).
#   seq-file 3: below the heading, paragraph and list that open 3.4, which fill the page in
#     front of Figure 7 (a page of its own) instead of leaving a third of it empty.
# Set FLOAT = {} to keep every figure exactly where the Markdown has it.
FLOAT = {"seq-file": 3}
# Column widths in % per table number, where the automatic layout wraps badly.
TABLE_COLS = {2: [18, 10, 17, 55], 3: [14, 14, 25, 21, 26], 4: [17, 14, 37, 16, 16], 5: [15, 13, 29, 13, 30],
              8: [17, 46, 37], 9: [27, 23, 50], 10: [16, 29, 23, 32], 11: [18, 25, 41, 16],
              12: [22, 20, 30, 28], 13: [18, 36, 46]}
KEEP_ROWS = 4                        # tables with at most this many body rows never split
KEEP_INTRO = 600                     # a paragraph (HTML chars) this short stays with such a table


def die(msg):
    print("BUILD FAILED:", msg, file=sys.stderr)
    sys.exit(1)


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def marked(src, dst):
    """Convert one Markdown file to an HTML fragment next to this script."""
    if not src.exists():
        die(f"missing input {src}")
    r = subprocess.run(f'npx -y {MARKED} -i "{src}" --gfm', shell=True, capture_output=True)
    if r.returncode != 0 or not r.stdout.strip():
        die(f"marked failed on {src.name}: {r.stderr.decode('utf-8', 'replace')[:300]}")
    text = r.stdout.decode("utf-8").replace("\r\n", "\n")
    dst.write_text(text, encoding="utf-8", newline="\n")
    return text


def png_size(p):
    with open(p, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        die(f"{p} is not a PNG")
    return struct.unpack(">II", head[16:24])


def label(cap):
    """Bold accent label for 'Figure N.' / 'Table N.' / 'Listing N.' at the start of a caption."""
    return re.sub(r"^((?:Figure|Table|Listing) \d+\.)", r'<span class="lab">\1</span>', cap)


def soften_code(h):
    """Line breaking inside inline code (not in <pre>), so a long identifier neither runs
    past the right margin nor leaves a badly stretched justified line before it: allow a
    break after '/' anywhere, and in a token of 12 characters or more also after '_' and
    '.' and before '(' (only where at least three characters follow, so 'transport.py'
    never breaks before 'py'). A command-line option such as --max-clients never breaks."""
    def long_tok(w):
        t = w.group(0)
        if t.startswith("-"):
            return t
        t = re.sub(r"(?<=[A-Za-z0-9])([_.])(?=[A-Za-z0-9_]{3})", r"\1<wbr>", t)
        return re.sub(r"(?<=[A-Za-z0-9_])\((?=[^)]{2})", "<wbr>(", t)
    def one(m):
        body = re.sub(r"[^\s<>&;/]{12,}", long_tok, m.group(1))
        body = re.sub(r"/(?=[^\s/<])", "/<wbr>", body)
        body = re.sub(r"(?<![\w&;-])(-{1,2}[A-Za-z][\w-]*)", r'<span class="nb">\1</span>', body)
        return f"<code>{body}</code>"
    parts = re.split(r"(<pre>.*?</pre>)", h, flags=re.S)
    return "".join(p if p.startswith("<pre>") else re.sub(r"<code>(.*?)</code>", one, p, flags=re.S) for p in parts)


MISSING = []


def process(h, sid):
    h = re.sub(r"<!--.*?-->", "", h, flags=re.S)
    h = soften_code(h)

    def fig(m):
        src, alt = m.group(1), m.group(2)
        num = re.match(r"Figure (\d+)", alt).group(1)
        if src.startswith("placeholder:"):
            # a screenshot the team has not taken yet: a framed box with the normal caption
            slug = html.escape(src.split(":", 1)[1])
            return (f'<figure class="fig ph" id="fig{num}" data-ph="{slug}">'
                    f'<div class="phbox" style="width:{PH_W:.1f}cm;height:{PH_H:.1f}cm"><span>TODO: screenshot <span class="dash">—</span> {slug}</span></div>'
                    f'<figcaption class="cap">{label(alt)}</figcaption></figure>')
        name = src.rsplit("/", 1)[1].rsplit(".", 1)[0]
        png = R / "diagrams" / "lucid" / f"{name}.png"
        if not png.exists():
            MISSING.append(name)
            return m.group(0)
        pw, ph = png_size(png)
        w = min(TEXT_W, pw / MIN_DPI * 2.54, FIG_MAX_W.get(name, 99))
        max_h = min(OWN_PAGE_MAX_H if name in OWN_PAGE else FIG_MAX_H, FIG_MAX_H_FIG.get(name, 99))
        if w * ph / pw > max_h:
            w = max_h * pw / ph
        hgt = w * ph / pw
        cls = "fig"
        if name in OWN_PAGE: cls += " ownpage"
        elif hgt > 0.8 * TEXT_H: cls += " full"
        data = base64.b64encode(png.read_bytes()).decode()
        lab = SMALLEST_PX.get(name, 16) * w / pw / 2.54 * 72
        if lab < MIN_LABEL_PT:
            die(f"{name}: smallest label would print at {lab:.1f} pt (< {MIN_LABEL_PT} pt)")
        return (f'<figure class="{cls}" id="fig{num}" data-name="{name}" data-cm="{w:.2f}x{hgt:.2f}" data-dpi="{pw / w * 2.54:.0f}" data-pt="{lab:.1f}">'
                f'<img src="data:image/png;base64,{data}" style="width:{w:.2f}cm;height:{hgt:.2f}cm" alt="">'
                f'<figcaption class="cap">{label(alt)}</figcaption></figure>')
    h = re.sub(r'<p><img src="([^"]+)" alt="([^"]*)"></p>', fig, h)

    for name, n in FLOAT.items():
        m = re.search(rf'<figure [^>]*data-name="{name}".*?</figure>\s*', h, re.S)
        if not m: continue
        rest = h[m.end():]
        mm = re.match(r"(?:\s*<(p|h[23]|ol|ul)(?: [^>]*)?>.*?</\1>){%d}\s*" % n, rest, re.S)
        if not mm: die(f"FLOAT[{name}]={n}: fewer than {n} paragraphs/lists/headings follow the figure")
        h = h[:m.start()] + mm.group(0) + m.group(0) + rest[mm.end():]

    def table(m):
        cap, tbl = m.group(1), m.group(2)
        num = int(re.match(r"Table (\d+)", cap).group(1))
        rows = tbl.count("<tr>") - 1
        cols = TABLE_COLS.get(num)
        tcls = f"t{num}"
        if cols:
            tcls += " fixed"
            tbl = tbl.replace("<table>", "<table>" + "<colgroup>" + "".join(f'<col style="width:{c}%">' for c in cols) + "</colgroup>", 1)
        tbl = tbl.replace("<table>", f'<table class="{tcls}">', 1)
        keep = " keep" if rows <= KEEP_ROWS else ""
        if not keep:
            # A table that may split keeps its first two and last two rows together (one
            # tbody each, break-inside: avoid), so a page never holds a single row of it.
            bm = re.search(r"<tbody>(.*?)</tbody>", tbl, re.S)
            trs = re.findall(r"<tr>.*?</tr>", bm.group(1), re.S)
            groups = [trs[:2], trs[2:-2], trs[-2:]]
            glue = ' class="glue"'
            body = "".join(f"<tbody{glue if k != 1 else ''}>{''.join(g)}</tbody>"
                           for k, g in enumerate(groups) if g)
            tbl = tbl[:bm.start()] + body + tbl[bm.end():]
        return f'<div class="tblwrap{keep}" id="tab{num}"><p class="cap tcap">{label(cap)}</p>{tbl}</div>'
    h = re.sub(r"<p>(Table \d+\..*?)</p>\s*(<table>.*?</table>)", table, h, flags=re.S)
    if "<table>" in h:
        die(f"{sid}: a table has no 'Table N.' caption paragraph directly above it")
    # A short paragraph right above an unbreakable table moves to the next page with it,
    # so a sentence such as "Table 13 links ..." is never left alone at the foot of a page.
    # (A wrapper, not break-after:avoid on the paragraph: Chrome then refuses to start the
    # group on a page where it fits.)
    h = re.sub(r'<p>((?:(?!</?p>).){1,%d})</p>\s*(<div class="tblwrap keep".*?</table></div>)' % KEEP_INTRO,
               r'<div class="keepgrp"><p>\1</p>\2</div>', h, flags=re.S)

    def precap(m):
        pre, cap = m.group(1), m.group(2)
        kind, num = re.match(r"(Figure|Listing) (\d+)", cap).groups()
        if kind == "Figure":
            return f'<div class="prefig fig2" id="fig{num}">{pre}<p class="cap pcap">{label(cap)}</p></div>'
        return f'<div class="prefig" id="lst{num}">{pre}<p class="cap pcap">{label(cap)}</p></div>'
    h = re.sub(r"(<pre>(?:(?!</pre>).)*</pre>)\s*<p>((?:Figure|Listing) \d+\..*?)</p>", precap, h, flags=re.S)
    return f'<section class="body sec" id="{sid}">{h}</section>'


def num_split(txt, apx=False):
    """('2.4.1', 'Session') from '2.4.1 Session'. In the appendix a heading number may start
    with a capital letter ('A Error codes', 'A.1 ...'); elsewhere only digits count, so a
    heading such as 'A note on ...' in the main body is never read as numbered."""
    pat = r"((?:\d+|[A-Z])(?:\.\d+)*)\s+(.*)" if apx else r"(\d+(?:\.\d+)*)\s+(.*)"
    m = re.match(pat, txt, re.S)
    return (m.group(1), m.group(2)) if m else ("", txt)


def title_page():
    t = (B / "title.html").read_text(encoding="utf-8")
    raw = [x.strip() for x in re.findall(r"<p>(.*?)</p>", t, re.S)]
    lines = [strip_tags(x) for x in raw]
    members = re.findall(r"<li>(.*?)</li>", t, re.S)
    if len(lines) < 12 or not members:
        die("title.md does not have the expected lines")
    # Instructor / Class / Group keep their inline Markdown (the bold TODO markers); the
    # repository line uses the plain text, because marked turns the URL into a link.
    val = lambda i: raw[i].split(": ", 1)[1]
    txt = lambda i: html.escape(lines[i].split(": ", 1)[1])
    rows = ""
    for i, mbr in enumerate(members):
        who, _, what = mbr.partition(" (")
        what = "(" + what if what else ""
        cls = "stu" + (" first" if i == 0 else "") + (" last" if i == len(members) - 1 else "")
        k = "Students:" if i == 0 else ""
        rows += f'<tr class="{cls}"><td class="k">{k}</td><td class="who">{who}</td><td class="what">{soften_code(what)}</td></tr>'
    kind, _, sub = lines[4].partition(" \u2013 ")
    main, _, more = lines[5].partition(" \u2014 ")
    logo = base64.b64encode((B / "hcmut-logo.png").read_bytes()).decode()
    return f'''<section class="titlepage">
<div class="uni">{html.escape(lines[0])}<br>{html.escape(lines[1])}<br>{html.escape(lines[2])}</div>
<img class="logo" src="data:image/png;base64,{logo}" alt="">
<div class="course">{html.escape(lines[3])}</div>
<div class="rule"></div>
<div class="blk"><div class="kind">{html.escape(kind)}</div><div class="sub">{html.escape(sub)}</div></div>
<div class="ptitle">{html.escape(main)}<span class="more">{html.escape(more)}</span></div>
<div class="rule"></div>
<table class="meta">
<tr><td class="k">Instructor:</td><td colspan="2">{val(6)}</td></tr>
<tr><td class="k">Class:</td><td colspan="2">{val(7)}</td></tr>
<tr><td class="k">Group:</td><td colspan="2">{val(8)}</td></tr>
{rows}
<tr><td class="k">Repository:</td><td colspan="2"><code>{txt(10)}</code></td></tr></table>
<div class="place">{html.escape(lines[11])}</div></section>'''


def build(pages, nfront):
    """Return (html, entries). entries = ordered list of (anchor id) for every contents/list line."""
    body, toc, n = [], [], 0
    for s in SECS:
        h = process((B / f"{s}.html").read_text(encoding="utf-8"), s)
        apx = s.startswith("9")
        def hid(m):
            nonlocal n; n += 1
            lvl, txt = m.group(1), m.group(2)
            num, rest = num_split(txt, apx)
            toc.append((int(lvl), strip_tags(txt), f"h{n}", strip_tags(num), strip_tags(rest), s))
            inner = f'<span class="hn">{num}</span> {rest}' if num else txt
            cls = "" if num else ' class="unnum"'
            return f'<h{lvl} id="h{n}"{cls}>{inner}</h{lvl}>'
        body.append(re.sub(r"<h([1-3])>(.*?)</h\1>", hid, h, flags=re.S))
    full = "".join(body)
    if MISSING:
        die("no Lucid PNG for: " + ", ".join(sorted(set(MISSING))) + " (report/diagrams/lucid/<name>.png); refusing to fall back to Mermaid SVG")
    if re.search(r'<img src="(?!data:image/png)', full):
        die("an image in the body is not a Lucid PNG")

    figs = [(int(m.group(1)), strip_tags(m.group(2))) for m in
            re.finditer(r'id="fig(\d+)".*?<(?:figcaption|p) class="cap(?: pcap)?">(.*?)</(?:figcaption|p)>', full, re.S)]
    figs.sort()
    tabs = [(int(m.group(1)), strip_tags(m.group(2))) for m in
            re.finditer(r'id="tab(\d+)"><p class="cap tcap">(.*?)</p>', full, re.S)]

    def pg(aid):
        return str(pages[aid] - nfront) if pages and aid in pages else ""
    def line(cls, num, txt, aid):
        return (f'<div class="tl {cls}"><a class="e" href="#{aid}"><span class="n">{html.escape(num)}</span>'
                f'<span class="t">{html.escape(txt)}</span></a><a class="pn" href="#{aid}">{pg(aid)}</a></div>')
    entries, tochtml = [], ""
    for lvl, txt, aid, num, rest, _s in toc:
        tochtml += line(f"l{lvl}" + ("" if num else " un"), num, rest, aid); entries.append(aid)
    lof = lot = ""
    def short(cap, kind):
        """List entry = the caption's first sentence (as LaTeX's short caption); the full
        caption stays under the figure or above the table."""
        c = re.sub(rf"^{kind} \d+\.\s*", "", cap)
        return re.split(r"(?<=[a-z0-9)\]])\.\s+(?=[\"'A-Z])", c, maxsplit=1)[0].rstrip(".")
    for num, cap in figs:
        lof += line("lf", str(num), short(cap, "Figure"), f"fig{num}"); entries.append(f"fig{num}")
    for num, cap in tabs:
        lot += line("lf", str(num), short(cap, "Table"), f"tab{num}"); entries.append(f"tab{num}")

    css = (B / "report.css").read_text(encoding="utf-8").replace("__NFRONT__", str(nfront))
    doc = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><title>LanLink Technical Explanation Report</title><style>{css}</style></head><body>
{title_page()}
<section class="front"><h1 class="fm">Contents</h1>{tochtml}
<div class="lst"><h1 class="fm">List of Figures</h1>{lof}</div>
<div class="lst"><h1 class="fm">List of Tables</h1>{lot}</div></section>
{full}</body></html>'''
    return doc, entries, toc, figs, tabs


def pdf():
    r = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--no-pdf-tagged",
                        f"--print-to-pdf={OUT}", (B / "report.html").as_uri()], capture_output=True)
    if r.returncode != 0 or not OUT.exists():
        die("Chrome could not print the PDF (is it open in a viewer?): " + r.stderr.decode("utf-8", "replace")[-300:])


def find_pages(entries):
    """Physical page (1-based) of every anchor, read from the link annotations Chrome wrote
    for the contents and list lines. Returns (pages, pages before section 1, page count)."""
    doc = pymupdf.open(OUT)
    res = {}
    for p in doc:
        for l in p.get_links():
            name = l.get("nameddest") or l.get("name")
            if name in entries and "page" in l and l["page"] >= 0:
                res[name] = l["page"] + 1
    lost = [e for e in entries if e not in res]
    if lost:
        die(f"no link destination found for {lost[:5]}")
    count = len(doc); doc.close()
    return res, res[entries[0]] - 1, count


def norm(s):
    return re.sub(r"\s+", " ", s).strip()


def checks(pages, nfront, count, toc, figs, tabs, nph):
    """Layout checks on the final PDF. Returns (failures, visible TODO count)."""
    doc = pymupdf.open(OUT)
    texts = [norm(p.get_text()) for p in doc]
    bad = []
    body = " ".join(texts[nfront:])
    for kind, items in (("Figure", figs), ("Table", tabs)):
        for num, cap in items:
            pat = rf"{kind} {num}\. {re.escape(norm(cap)[len(kind) + len(str(num)) + 3:][:25])}"
            if len(re.findall(pat, body)) != 1:
                bad.append(f"caption {kind} {num} not found exactly once in the body")
            # the caption is printed on the page where the figure / table starts
            elif not re.search(pat, texts[pages[("fig" if kind == "Figure" else "tab") + str(num)] - 1]):
                bad.append(f"{kind} {num}: caption is not on the page where it starts")
    # printed page numbers: the footer of physical page p in the body must read p - nfront
    for i in range(nfront, count):
        words = [w for w in doc[i].get_text("words") if w[1] > doc[i].rect.height - 56]
        if [w[4] for w in words] != [str(i + 1 - nfront)]:
            bad.append(f"physical page {i + 1}: footer reads {[w[4] for w in words]}, expected {i + 1 - nfront}")
    # every heading must actually be on the page the contents give for it
    for lvl, txt, aid, *_ in toc:
        if norm(txt) not in texts[pages[aid] - 1]:
            bad.append(f"heading '{txt}' is not on physical page {pages[aid]}")
    # section 4 fits on one page: all its headings on the page where it starts, and section 5
    # starts on that page or at the very top of the next one (nothing of 4 spills over)
    s4 = [aid for lvl, txt, aid, num, *_ in toc if num.split(".")[0] == "4"]
    h5 = next(e for e in toc if e[0] == 1 and e[3] == "5")
    p4, p5 = pages[s4[0]], pages[h5[2]]
    if len({pages[a] for a in s4}) != 1 or p5 - p4 > 1:
        bad.append(f"section 4 does not fit on one page (4 starts p.{p4}, 5 starts p.{p5})")
    elif p5 == p4 + 1:
        pg5 = doc[p5 - 1]
        hy = min(s["bbox"][1] for b in pg5.get_text("dict")["blocks"] for l in b.get("lines", [])
                 for s in l["spans"] if s["size"] > 16 and h5[4] in s["text"])
        above = [b for b in pg5.get_text("blocks") if b[1] > 75 and b[3] < hy - 1]
        if above:
            bad.append(f"section 4 spills onto physical page {p5}: {norm(above[0][4])[:60]!r}")
    # no text runs past the right margin (the text area ends at 595 - 70.9 pt), and nothing
    # but the running header is drawn above the text area (the text area starts at 65.2 pt;
    # a line clipped at the foot of the previous page would reappear there)
    for i in range(0, count):
        stray = [w[4] for w in doc[i].get_text("words") if w[1] < 64 and not 25 <= w[1] <= 42]
        if stray:
            bad.append(f"physical page {i + 1}: text above the text area {stray[:6]}")
    for i in range(nfront, count):
        p = doc[i]
        for w in p.get_text("words"):
            if w[2] > p.rect.width - 55:
                bad.append(f"physical page {i + 1}: '{w[4]}' runs past the right margin (x1 = {w[2]:.1f} pt)")
    # a figure and its caption on one page, inside the text area
    for i, p in enumerate(doc):
        for info in p.get_image_info():
            x0, y0, x1, y1 = info["bbox"]
            if x0 < 55 or x1 > p.rect.width - 55 or y0 < 55 or y1 > p.rect.height - 55:
                bad.append(f"image on physical page {i + 1} leaves the text area")
    # loose justified lines (reported, not a failure): mean word gap above 2.2 normal spaces
    loose = []
    for i in range(nfront, count):
        p = doc[i]
        lines = {}
        for w in p.get_text("words"):
            lines.setdefault((w[5], w[6]), []).append(w)
        for ws in lines.values():
            if len(ws) < 4 or max(w[2] for w in ws) < p.rect.width - 72.5 or ws[0][3] - ws[0][1] < 11.5:
                continue
            gap = (max(w[2] for w in ws) - min(w[0] for w in ws) - sum(w[2] - w[0] for w in ws)) / (len(ws) - 1)
            if gap > 8.8:
                loose.append(f"p.{i + 1 - nfront} gap {gap:.1f}pt: {' '.join(w[4] for w in ws)[:70]}")
    nimg = sum(len(p.get_image_info()) for p in doc)
    if nimg != len(figs) - nph + 1:  # every Lucid figure (placeholders are boxes), plus the logo
        bad.append(f"{nimg} images in the PDF, expected {len(figs) - nph + 1}")
    if sum(p.get_text().count("[STUDENT") for p in doc):
        bad.append("'[STUDENT' text is still in the PDF (use a bold TODO and an HTML comment)")
    todo = sum(len(re.findall(r"\bTODO\b", p.get_text())) for p in doc)
    doc.close()
    return bad, todo, loose


def main():
    marked(B / "title.md", B / "title.html")
    for s in SECS:
        marked(R / "sections" / f"{s}.md", B / f"{s}.html")
    pages, nfront = None, 2
    for attempt in range(6):
        h, entries, toc, figs, tabs = build(pages, nfront)
        (B / "report.html").write_text(h, encoding="utf-8")
        pdf()
        pages2, nfront2, count = find_pages(entries)
        if pages2 == pages and nfront2 == nfront:
            break
        pages, nfront = pages2, nfront2
    else:
        die("page numbers did not become stable")
    phs = re.findall(r'<figure [^>]*id="fig(\d+)" data-ph="([^"]+)"', h)
    bad, todo, loose = checks(pages, nfront, count, toc, figs, tabs, len(phs))
    print(f"pages {count} (title 1 + front {nfront - 1} + body {count - nfront}); visible TODO x{todo}; "
          f"figures {len(figs)} ({len(figs) - len(phs)} Lucid PNG, {len(phs)} placeholder); tables {len(tabs)}")
    for lvl, txt, aid, num, rest, s in toc:
        if lvl == 1 or (s.startswith("9") and lvl == 2):
            print(f"  {txt}: body p.{pages[aid] - nfront}")
    for m in re.finditer(r'<figure [^>]*id="fig(\d+)" data-name="([^"]+)" data-cm="([^"]+)" data-dpi="(\d+)" data-pt="([^"]+)"', h):
        print(f"  Figure {m.group(1)} {m.group(2)}: {m.group(3)} cm, {m.group(4)} dpi, smallest label {m.group(5)} pt, body p.{pages['fig' + m.group(1)] - nfront}")
    for num, slug in phs:
        print(f"  Figure {num} placeholder:{slug}: box {PH_W:.0f} x {PH_H:.0f} cm, body p.{pages['fig' + num] - nfront}")
    print(f"loose justified lines (mean word gap > 8.8 pt, warning only): {len(loose)}")
    for l in loose:
        print("  " + l)
    if bad:
        die("; ".join(bad))
    print("checks OK:", OUT)


if __name__ == "__main__":
    main()
