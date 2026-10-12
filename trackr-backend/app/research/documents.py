"""Bounded, deterministic text extraction for research source documents (session 12).

SEPARATE FROM FINANCIAL INGESTION, on purpose. Nothing here calls a broker parser, stages a
row, interprets a number or produces a Transaction (CLAUDE.md rule 5). The output is plain
text with provenance -- what the document says, verbatim, for a person to read and cite.

LOCAL AND DETERMINISTIC ONLY. PDF text comes from pdfminer.six (a declared dependency via
pdfplumber); plain text is decoded as strict UTF-8. No OCR, no model, no network. An
encrypted, malformed or image-only PDF yields an explicit UNAVAILABLE reason, never
invented text.

THE PIN. ``EXTRACTOR_ID``/``extractor_version()``/``NORMALIZATION`` identify exactly how text
was produced, and migration 048 stores them with the text's SHA-256. Content reads serve the
RETAINED text after re-verifying both digests; they never re-extract under whatever library
is installed later.

PAGES. The retained artifact is the pages' text joined by a form feed (``\\f``), the same
convention ``pdftotext`` uses. Page numbers in the API are 1-based. A plain-text upload is a
single page 1.
"""

from __future__ import annotations

import hashlib
import io
import unicodedata
from dataclasses import dataclass

#: Upload bound (BLUEPRINT_BACKEND.md section 7.1).
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
#: Largest text a single content read returns (BLUEPRINT_BACKEND.md section 7.3).
MAX_CONTENT_BYTES = 256 * 1024
#: Parser resource bounds. A 2,000-page PDF is refused rather than parsed unboundedly.
MAX_PAGES = 2000
MAX_TEXT_BYTES = 16 * 1024 * 1024

PDF_MEDIA = "application/pdf"
TEXT_MEDIA = "text/plain"

EXTRACTOR_PDF = "pdfminer.six"
EXTRACTOR_TEXT = "utf8-text"
#: Normalisation policy, versioned: NFC, CRLF/CR -> LF, pages joined by \f, no trimming.
NORMALIZATION = "nfc-lf-ff/1"
PAGE_SEPARATOR = "\f"


@dataclass(frozen=True)
class Extraction:
    status: str  # EXTRACTED | UNAVAILABLE
    extractor_id: str
    extractor_version: str
    normalization: str
    text: str | None
    page_count: int | None
    unavailable_reason: str | None

    @property
    def text_bytes(self) -> bytes | None:
        return self.text.encode("utf-8") if self.text is not None else None

    @property
    def text_sha256(self) -> str | None:
        data = self.text_bytes
        return hashlib.sha256(data).hexdigest() if data is not None else None


class UnsupportedDocumentError(ValueError):
    """The bytes are not a PDF or UTF-8 text document matching their declared type."""


def sniff_media_type(content: bytes, declared: str | None, filename: str | None) -> str:
    """Decide PDF vs text from the SIGNATURE, and require the declaration to agree."""
    declared = (declared or "").split(";")[0].strip().lower()
    is_pdf = content.startswith(b"%PDF-")
    if declared == PDF_MEDIA or (filename or "").lower().endswith(".pdf"):
        if not is_pdf:
            raise UnsupportedDocumentError("declared PDF but the file has no PDF signature")
        return PDF_MEDIA
    if declared in (TEXT_MEDIA, "text/markdown") or (filename or "").lower().endswith(
        (".txt", ".md")
    ):
        if is_pdf:
            raise UnsupportedDocumentError("declared text but the file is a PDF")
        return TEXT_MEDIA
    raise UnsupportedDocumentError("only PDF and UTF-8 text documents are accepted")


def extractor_version(extractor_id: str) -> str:
    if extractor_id == EXTRACTOR_PDF:
        import pdfminer

        return str(pdfminer.__version__)
    if extractor_id == EXTRACTOR_TEXT:
        return "1"
    raise ValueError(f"unknown extractor {extractor_id!r}")


def _normalise(text: str) -> str:
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def extract(content: bytes, media_type: str) -> Extraction:
    """Deterministic text for ``content``. Never raises for bad document CONTENT -- that is
    an UNAVAILABLE extraction with a reason; it raises only for an unknown media type."""
    if media_type == TEXT_MEDIA:
        version = extractor_version(EXTRACTOR_TEXT)
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return Extraction("UNAVAILABLE", EXTRACTOR_TEXT, version, NORMALIZATION, None,
                              None, "DECODE_ERROR")
        text = _normalise(text).replace(PAGE_SEPARATOR, "\n")
        return Extraction("EXTRACTED", EXTRACTOR_TEXT, version, NORMALIZATION, text, 1, None)
    if media_type != PDF_MEDIA:
        raise UnsupportedDocumentError(media_type)
    return _extract_pdf(content)


def _extract_pdf(content: bytes) -> Extraction:
    version = extractor_version(EXTRACTOR_PDF)

    def unavailable(reason: str) -> Extraction:
        return Extraction("UNAVAILABLE", EXTRACTOR_PDF, version, NORMALIZATION, None, None,
                          reason)

    from pdfminer.converter import TextConverter
    from pdfminer.layout import LAParams
    from pdfminer.pdfdocument import PDFDocument, PDFEncryptionError, PDFPasswordIncorrect
    from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdfparser import PDFParser, PDFSyntaxError
    from pdfminer.psparser import PSException

    try:
        parser = PDFParser(io.BytesIO(content))
        document = PDFDocument(parser)
        if not document.is_extractable:
            return unavailable("ENCRYPTED")
        manager = PDFResourceManager()
        params = LAParams()
        pages: list[str] = []
        total = 0
        for index, page in enumerate(PDFPage.create_pages(document)):
            if index >= MAX_PAGES:
                return unavailable("PAGE_LIMIT")
            buffer = io.StringIO()
            device = TextConverter(manager, buffer, laparams=params)
            PDFPageInterpreter(manager, device).process_page(page)
            device.close()
            page_text = _normalise(buffer.getvalue()).replace(PAGE_SEPARATOR, "\n")
            total += len(page_text.encode("utf-8"))
            if total > MAX_TEXT_BYTES:
                return unavailable("OUTPUT_LIMIT")
            pages.append(page_text)
    except (PDFPasswordIncorrect, PDFEncryptionError):
        return unavailable("ENCRYPTED")
    except (PDFSyntaxError, PSException, ValueError, KeyError, TypeError, AttributeError,
            IndexError, AssertionError):
        return unavailable("MALFORMED")
    except Exception as exc:  # pdfminer raises assorted internal types on corrupt input
        if "encrypt" in type(exc).__name__.lower():
            return unavailable("ENCRYPTED")
        return unavailable("MALFORMED")
    if not pages or not any(p.strip() for p in pages):
        return unavailable("NO_TEXT")
    return Extraction("EXTRACTED", EXTRACTOR_PDF, version, NORMALIZATION,
                      PAGE_SEPARATOR.join(pages), len(pages), None)


def page_text(text: str, page: int | None) -> tuple[str, int]:
    """One 1-based page of retained text, or the whole document when ``page`` is None.

    Raises ``IndexError`` for a page outside the document and ``OverflowError`` when the
    result would exceed MAX_CONTENT_BYTES -- a citation is never silently truncated.
    """
    pages = text.split(PAGE_SEPARATOR)
    if page is None:
        chosen = text
    else:
        if page < 1 or page > len(pages):
            raise IndexError(page)
        chosen = pages[page - 1]
    if len(chosen.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise OverflowError(len(chosen.encode("utf-8")))
    return chosen, len(pages)
