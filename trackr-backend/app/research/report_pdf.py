"""A minimal, bounded, text-searchable PDF writer for report artifacts (RP 06). Pure.

WHY THIS EXISTS INSTEAD OF A LIBRARY. No PDF-writing dependency is installed and adding one is
not authorised in this session. A headless browser renderer would also be the riskiest option
(remote fetches, script execution). This writer is deliberately tiny and closed:

* standard Type 1 fonts only (Helvetica, Helvetica-Bold, Courier) -- nothing embedded, nothing
  fetched; WinAnsi (cp1252) text, so the output is text-searchable and copyable;
* no images, no links to external resources, no JavaScript, no forms, no attachments;
* input is a list of typed BLOCKS built by ``report_render`` from the validated document; there
  is no HTML or markup parsing, so untrusted text cannot become structure;
* hard bounds on pages and bytes; exceeding them raises ``RenderLimit`` (the job fails with a
  stated reason rather than emitting a clipped report).

Characters outside cp1252 are replaced with "?" and counted; the renderer states the count in
the artifact manifest. Helvetica widths are the standard AFM metrics for the printable ASCII
range; other characters use the average width, which only affects wrapping, never content.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from typing import Any

PAGE_W, PAGE_H = 595.28, 841.89  # A4 in points
MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 48.0, 56.0, 56.0
MAX_PAGES = 400
MAX_BYTES = 12 * 1024 * 1024

# Helvetica AFM widths (1/1000 em) for chars 32..126.
_HELV = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556,
         556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667,
         667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722,
         667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500,
         556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278,
         556, 500, 722, 500, 500, 500, 334, 260, 334, 584]
_HELV_BOLD = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
              556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
              975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
              667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
              333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
              611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584]
FONTS = {"F1": ("Helvetica", _HELV), "F2": ("Helvetica-Bold", _HELV_BOLD),
         "F3": ("Courier", None)}


class RenderLimit(Exception):
    """Output would exceed a hard bound. Never truncated silently."""


def text_width(text: str, font: str, size: float) -> float:
    _, widths = FONTS[font]
    if widths is None:
        return len(text) * 600 * size / 1000
    total = 0
    for ch in text:
        o = ord(ch)
        total += widths[o - 32] if 32 <= o <= 126 else 556
    return total * size / 1000


def wrap(text: str, font: str, size: float, width: float) -> list[str]:
    """Greedy word wrap; a word longer than the line is broken by characters, never clipped."""
    out: list[str] = []
    for para in text.split("\n"):
        line = ""
        for word in para.split(" "):
            candidate = word if not line else f"{line} {word}"
            if text_width(candidate, font, size) <= width:
                line = candidate
                continue
            if line:
                out.append(line)
            while text_width(word, font, size) > width:
                cut = len(word)
                while cut > 1 and text_width(word[:cut], font, size) > width:
                    cut -= 1
                out.append(word[:cut])
                word = word[cut:]
            line = word
        out.append(line)
    return out


@dataclass
class Block:
    """One typed layout block. kind: TITLE | HEADING | PARA | SMALL | TABLE | RULE | GAP."""

    kind: str
    text: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    widths: list[float] = field(default_factory=list)  # fractions summing to 1


class _Page:
    def __init__(self) -> None:
        self.ops: list[str] = []


def _esc(text: str, counter: list[int]) -> bytes:
    raw = text.encode("cp1252", errors="replace")
    counter[0] += raw.count(b"?") - text.count("?")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


class Writer:
    def __init__(self, header: str, footer: str) -> None:
        self.header = header
        self.footer = footer
        self.pages: list[_Page] = []
        self.y = 0.0
        self.replaced = [0]
        self.width = PAGE_W - 2 * MARGIN_X
        self._new_page()

    # -- primitives ------------------------------------------------------------------------
    def _new_page(self) -> None:
        if len(self.pages) >= MAX_PAGES:
            raise RenderLimit("PAGE_LIMIT")
        self.pages.append(_Page())
        self.y = PAGE_H - MARGIN_TOP

    def _ensure(self, height: float) -> bool:
        """True when a new page was started."""
        if self.y - height < MARGIN_BOTTOM:
            self._new_page()
            return True
        return False

    def _text(self, x: float, y: float, text: str, font: str, size: float) -> None:
        s = _esc(text, self.replaced).decode("latin-1")
        self.pages[-1].ops.append(f"BT /{font} {size:.1f} Tf {x:.2f} {y:.2f} Td ({s}) Tj ET")

    def _line(self, x1: float, y1: float, x2: float, y2: float, grey: float = 0.6) -> None:
        self.pages[-1].ops.append(f"{grey:.2f} G 0.5 w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")

    # -- blocks ----------------------------------------------------------------------------
    def para(self, text: str, font: str = "F1", size: float = 9.5, lead: float = 1.35,
             indent: float = 0.0) -> None:
        for line in wrap(text, font, size, self.width - indent):
            self._ensure(size * lead)
            self.y -= size * lead
            self._text(MARGIN_X + indent, self.y, line, font, size)

    def gap(self, h: float) -> None:
        if not self._ensure(h):
            self.y -= h

    def rule(self) -> None:
        self._ensure(6)
        self.y -= 4
        self._line(MARGIN_X, self.y, PAGE_W - MARGIN_X, self.y)
        self.y -= 4

    def table(self, columns: list[str], rows: list[list[str]], widths: list[float]) -> None:
        """Header repeated on every page the table spans; cells wrap, rows never split."""
        size, lead, pad = 8.0, 1.3, 3.0
        cols = [w * self.width for w in widths]

        def cell_lines(values: list[str], font: str) -> list[list[str]]:
            return [wrap(v, font, size, c - 2 * pad) for v, c in zip(values, cols, strict=True)]

        def draw(values: list[str], font: str) -> None:
            lines = cell_lines(values, font)
            h = max(len(x) for x in lines) * size * lead + 2 * pad
            top = self.y
            x = MARGIN_X
            for col_lines, w in zip(lines, cols, strict=True):
                yy = top - pad
                for ln in col_lines:
                    yy -= size * lead
                    self._text(x + pad, yy + 2, ln, font, size)
                x += w
            self.y = top - h
            self._line(MARGIN_X, self.y, PAGE_W - MARGIN_X, self.y, 0.8)

        def height(values: list[str], font: str) -> float:
            return max(len(x) for x in cell_lines(values, font)) * size * lead + 2 * pad

        head_h = height(columns, "F2")
        self._ensure(head_h + (height(rows[0], "F1") if rows else 0))
        draw(columns, "F2")
        for r in rows:
            h = height(r, "F1")
            if h > PAGE_H - MARGIN_TOP - MARGIN_BOTTOM - head_h:
                raise RenderLimit("ROW_TALLER_THAN_PAGE")
            if self._ensure(h):
                draw(columns, "F2")
            draw(r, "F1")
        self.gap(6)

    def render(self, blocks: list[Block]) -> bytes:
        for b in blocks:
            if b.kind == "TITLE":
                self.para(b.text, "F2", 15, 1.3)
                self.gap(4)
            elif b.kind == "HEADING":
                self.gap(8)
                self._ensure(40)  # keep a heading with at least a few lines after it
                self.para(b.text, "F2", 11.5, 1.3)
                self.gap(2)
            elif b.kind == "PARA":
                self.para(b.text)
                self.gap(3)
            elif b.kind == "SMALL":
                self.para(b.text, "F1", 7.8)
                self.gap(2)
            elif b.kind == "MONO":
                self.para(b.text, "F3", 7.8)
            elif b.kind == "TABLE":
                self.table(b.columns, b.rows, b.widths)
            elif b.kind == "RULE":
                self.rule()
            elif b.kind == "GAP":
                self.gap(8)
        return self._assemble()

    # -- file ------------------------------------------------------------------------------
    def _assemble(self) -> bytes:
        n = len(self.pages)
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        catalog = add(b"")  # placeholder, filled below
        pages_id = add(b"")
        font_ids = {k: add(f"<< /Type /Font /Subtype /Type1 /BaseFont /{name} "
                           f"/Encoding /WinAnsiEncoding >>".encode())
                    for k, (name, _) in FONTS.items()}
        fonts = " ".join(f"/{k} {v} 0 R" for k, v in font_ids.items())
        page_ids = []
        for i, page in enumerate(self.pages, 1):
            ops = list(page.ops)
            ops.append(f"BT /F1 7.5 Tf {MARGIN_X:.2f} {PAGE_H - 34:.2f} Td "
                       f"({_esc(self.header, self.replaced).decode('latin-1')}) Tj ET")
            foot = f"{self.footer}  |  Page {i} of {n}"
            ops.append(f"BT /F1 7.5 Tf {MARGIN_X:.2f} 30 Td "
                       f"({_esc(foot, self.replaced).decode('latin-1')}) Tj ET")
            ops.append(f"0.8 G 0.5 w {MARGIN_X:.2f} {PAGE_H - 40:.2f} m "
                       f"{PAGE_W - MARGIN_X:.2f} {PAGE_H - 40:.2f} l S")
            stream = zlib.compress("\n".join(ops).encode("latin-1"))
            content = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream)
                          + stream + b"\nendstream")
            page_ids.append(add(
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                f"/Resources << /Font << {fonts} >> >> /Contents {content} 0 R >>".encode()))
        kids = " ".join(f"{p} 0 R" for p in page_ids)
        objects[pages_id - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode()
        objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objects, 1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
        for off in offsets:
            out += f"{off:010d} 00000 n \n".encode()
        out += (f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\n"
                f"startxref\n{xref}\n%%EOF\n").encode()
        if len(out) > MAX_BYTES:
            raise RenderLimit("BYTE_LIMIT")
        return bytes(out)


def render_pdf(blocks: list[Block], header: str, footer: str) -> tuple[bytes, int, int]:
    """(pdf bytes, page count, characters replaced for encoding)."""
    w = Writer(header, footer)
    data = w.render(blocks)
    return data, len(w.pages), w.replaced[0]


def meta(value: Any) -> str:
    return str(value)
