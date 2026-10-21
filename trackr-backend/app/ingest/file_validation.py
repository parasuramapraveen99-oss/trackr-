"""Two-tier upload validation: what the FILENAME claims, then what the BYTES actually are.

The two tiers answer different questions and neither one can answer the other's.

**Tier 1 -- extension.** Is this even a shape we accept (``.pdf``, ``.csv``, ``.xlsx``)?
Cheap, and the only check a browser can do before an upload starts. It is also trivially
wrong: renaming ``note.pdf`` to ``note.csv`` passes it, which is exactly why the client's
copy of this check is a courtesy and this module's copy is the enforcement.

**Tier 2 -- content.** Open the bytes and say what the document IS. That splits into two
separate findings, and conflating them produced the unhelpful errors this module exists to
replace:

  * the *container* -- PDF / XLSX / OLE2 / text -- read from magic bytes, so a contract
    note renamed ``.csv`` is caught before any parser sees it;
  * the *document* -- tradebook, contract note, CAS -- read from the structure inside the
    container: a CSV's header row, a workbook's sheet names, a PDF's header text.

A tradebook CSV and a contract-note CSV export are both well-formed CSVs of the right
extension. Only the second finding can tell them apart, and only the second finding can
say the sentence a person can act on: *this is a contract note, and contract notes go in
the other slot.*

WHAT THIS MODULE MAY NOT DO. It classifies and stops -- no ledger, no staging, no storage,
not even a full parse. It is the ingestion layer's front door, and CLAUDE.md rule 5 keeps
the ingestion layer clear of everything downstream of it. Nothing here raises for bad
input either: an unreadable file is a verdict with an explanation, not an exception. A
person who picked the wrong file deserves a sentence, not a traceback.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

from app.ingest.cas_table import holdings_header
from app.ingest.tabular import csv_rows, workbook_sheets
from app.ingest.zerodha_contract_note import CONTRACT_NOTE_MARKER
from app.ingest.zerodha_contract_note_xlsx import CLEAN_SHEET_NAMES
from app.ingest.zerodha_tradebook import (
    MAX_HEADER_SCAN_LINES,
    REQUIRED_COLUMNS,
    _norm_key,
    _split_line,
)

#: Tier 1. Everything else is refused by name alone, before a byte is inspected.
ALLOWED_EXTENSIONS = ("csv", "xlsx", "pdf")

#: Containers this module can name from magic bytes.
CONTAINER_PDF = "pdf"
CONTAINER_XLSX = "xlsx"
CONTAINER_XLS = "xls"
CONTAINER_ZIP = "zip"
CONTAINER_TEXT = "text"
CONTAINER_BINARY = "binary"
CONTAINER_EMPTY = "empty"

#: Documents this module can name from structure. "unknown" is a real answer, not a
#: failure to try: it means the bytes opened cleanly and are none of the things we parse.
DOC_TRADEBOOK_CSV = "tradebook_csv"
DOC_CONTRACT_NOTE_CSV = "contract_note_csv"
DOC_CONTRACT_NOTE_XLSX = "contract_note_xlsx"
DOC_CONTRACT_NOTE_PDF = "contract_note_pdf"
DOC_TRADEBOOK_XLSX = "tradebook_xlsx"
DOC_CAS_PDF = "cas_pdf"
DOC_CAS_CSV = "cas_csv"
DOC_CAS_XLSX = "cas_xlsx"
DOC_UNKNOWN = "unknown"

#: document kind -> (adapter name, the upload route that owns it). The single source of
#: truth for "where does this file go", shared by the router's per-file dispatch and by
#: the guidance text a mismatch produces -- so the two can never disagree about which
#: slot a document belongs in.
ROUTING: dict[str, tuple[str, str]] = {
    DOC_TRADEBOOK_CSV: ("zerodha_tradebook", "/ingest/csv"),
    DOC_TRADEBOOK_XLSX: ("zerodha_tradebook_xlsx", "/ingest/xlsx"),
    DOC_CONTRACT_NOTE_CSV: ("zerodha_contract_note_csv", "/ingest/csv"),
    DOC_CONTRACT_NOTE_XLSX: ("zerodha_contract_note_xlsx", "/ingest/xlsx"),
    DOC_CONTRACT_NOTE_PDF: ("zerodha_contract_note", "/ingest/files"),
    # A CAS is recognised from its own markers (see _CAS_MARKERS) and routed to the
    # holdings-only adapter. Routed rather than requiring the uploader to name the adapter,
    # because "which of these is a CAS" is a question the bytes answer better than a person
    # does -- and because a CAS misfiled as a contract note would otherwise be refused with
    # a puzzling message rather than simply working.
    DOC_CAS_PDF: ("cas_pdf", "/ingest/files"),
    DOC_CAS_CSV: ("cas_csv", "/ingest/files"),
    DOC_CAS_XLSX: ("cas_xlsx", "/ingest/files"),
}

#: A human name per document kind, for the one place these are read aloud: the error a
#: person sees when their file went in the wrong slot.
LABELS: dict[str, str] = {
    DOC_TRADEBOOK_CSV: "a Zerodha tradebook (CSV)",
    DOC_CONTRACT_NOTE_CSV: "a contract note saved as CSV",
    DOC_CONTRACT_NOTE_XLSX: "a Zerodha contract note (XLSX)",
    DOC_CONTRACT_NOTE_PDF: "a Zerodha contract note (PDF)",
    DOC_TRADEBOOK_XLSX: "a Zerodha tradebook (XLSX)",
    DOC_CAS_PDF: "a CAS / holding statement",
    DOC_CAS_CSV: "a CAS holdings table (CSV)",
    DOC_CAS_XLSX: "a CAS holdings table (XLSX)",
    DOC_UNKNOWN: "a file we don't recognise",
}

#: How much of a PDF's raw bytes to scan for the encryption marker. ``/Encrypt`` lives in
#: the trailer dictionary, which is never inside a compressed stream, so a bounded scan of
#: the tail is enough and costs nothing on a 40-page note.
_PDF_TRAILER_SCAN = 4096

#: A CAS can quote "contract note" in its narration, so its markers take precedence.
#: Original PDFs and structured holdings tables route to separate CAS readers.
_CAS_MARKERS = (
    "CONSOLIDATED ACCOUNT STATEMENT",
    "STATEMENT OF ACCOUNT",
    "STATEMENT OF HOLDING",
    "STATEMENT OF TRANSACTIONS",
    "NSDL CONSOLIDATED",
    "CDSL CONSOLIDATED",
    "DEPOSITORY PARTICIPANT",
)

#: A bulk contract-note workbook names each sheet for its trading day. Anchored, because
#: a sheet legitimately called "Trades 01-04-2024" is not a date sheet.
_DATE_SHEET = re.compile(r"^\d{2}[-/]\d{2}[-/]\d{4}$")

@dataclass(frozen=True, slots=True)
class FileVerdict:
    """What one uploaded file is, and what to do about it.

    ``ok`` means "we can parse this": the container matched its extension AND the document
    inside is one an adapter handles. Everything else carries ``reason`` (what is wrong,
    in one sentence) and ``hint`` (what to do instead) -- both written for the person who
    picked the file, never for the developer who wrote the parser.
    """

    filename: str
    extension: str
    container: str
    document: str
    ok: bool
    adapter: str | None = None
    reason: str | None = None
    hint: str | None = None
    #: True only for a PDF we could not open because it is locked. Distinct from a plain
    #: unreadable file: the fix is a password, not a different file.
    encrypted: bool = False
    notes: list[str] = field(default_factory=list)


def extension_of(filename: str) -> str:
    """Lowercased final extension, without the dot. ``""`` when there is none."""
    name = (filename or "").strip().rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].strip().lower()


def check_extension(filename: str) -> str | None:
    """Tier 1. ``None`` when the name is acceptable, otherwise the sentence to show.

    Enforced server-side even though the file input already filters by ``accept``: the
    ``accept`` attribute is a picker hint, not a constraint, and drag-and-drop bypasses it
    entirely.
    """
    ext = extension_of(filename)
    if not ext:
        return f"{filename or 'This file'} has no file extension, so we can't tell what it is."
    if ext not in ALLOWED_EXTENSIONS:
        return f"We can't read .{ext} files. Upload a PDF, CSV or XLSX downloaded from your broker."
    return None


def sniff_container(content: bytes) -> str:
    """What the bytes ARE, from magic numbers -- never from the filename.

    This is the check a renamed file cannot pass. Order matters only in that XLSX is a ZIP
    and must be tested as the more specific of the two.
    """
    if not content:
        return CONTAINER_EMPTY
    if content[:5] == b"%PDF-":
        return CONTAINER_PDF
    if content[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        # OLE2 compound file: a pre-2007 .xls, or a .doc. openpyxl reads neither.
        return CONTAINER_XLS
    if content[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = set(archive.namelist())
        except (zipfile.BadZipFile, OSError):
            return CONTAINER_BINARY
        return CONTAINER_XLSX if "xl/workbook.xml" in names else CONTAINER_ZIP
    if b"\x00" in content[:8192]:
        # A NUL byte inside the first pages is the cheapest reliable "this is not text"
        # signal; UTF-16 exports are the one false positive and Console produces none.
        return CONTAINER_BINARY
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            content.decode("cp1252")
        except UnicodeDecodeError:
            return CONTAINER_BINARY
    return CONTAINER_TEXT


def decode_text(content: bytes) -> str:
    """Best-effort text out of a byte string that ``sniff_container`` called text.

    ``utf-8-sig`` first because Console's CSV exports carry a BOM, and a BOM left on the
    first header cell turns ``symbol`` into ``\\ufeffsymbol`` and silently fails the
    header match.
    """
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _classify_text(content: bytes) -> tuple[str, list[str]]:
    """Which document a decodable text file is. Returns (document kind, notes)."""
    text = decode_text(content)
    lines = text.splitlines()
    try:
        if holdings_header(csv_rows(content, max_rows=MAX_HEADER_SCAN_LINES)) is not None:
            return DOC_CAS_CSV, []
    except (ValueError, csv.Error):
        pass

    # The tradebook test is the adapter's own header scan, not a re-implementation of it:
    # if this says yes, ZerodhaTradebookAdapter will find the same header on the same line.
    for line in lines[:MAX_HEADER_SCAN_LINES]:
        if not line.strip():
            continue
        _, cells = _split_line(line)
        keys = {_norm_key(c) for c in cells}
        if REQUIRED_COLUMNS <= keys:
            return DOC_TRADEBOOK_CSV, []

    upper = text[:20000].upper()
    if any(marker in upper for marker in _CAS_MARKERS):
        return DOC_UNKNOWN, ["CAS spreadsheets need a structured holdings table; "
                             "otherwise upload the original CAS PDF."]
    if CONTRACT_NOTE_MARKER in upper:
        return DOC_CONTRACT_NOTE_CSV, []

    # Say which columns are missing rather than "invalid file". A tradebook whose header
    # is one renamed column away from correct is the single most common real failure, and
    # naming the gap turns a dead end into a fix.
    best_missing: set[str] | None = None
    for line in lines[:MAX_HEADER_SCAN_LINES]:
        if not line.strip():
            continue
        _, cells = _split_line(line)
        keys = {_norm_key(c) for c in cells}
        missing = REQUIRED_COLUMNS - keys
        if len(keys) > 2 and (best_missing is None or len(missing) < len(best_missing)):
            best_missing = missing
    notes = []
    if best_missing and len(best_missing) < len(REQUIRED_COLUMNS):
        notes.append("missing columns: " + ", ".join(sorted(best_missing)))
    return DOC_UNKNOWN, notes


def _classify_xlsx(content: bytes) -> tuple[str, list[str]]:
    """Which document a workbook is, from its sheet names and first header row.

    Mirrors ``ZerodhaContractNoteXlsxAdapter.parse``'s own shape test (clean == exactly
    the three named sheets, otherwise bulk) so a file this function calls a contract note
    is one that adapter will actually accept.
    """
    try:
        sheets = workbook_sheets(content, max_rows=MAX_HEADER_SCAN_LINES)
    except Exception as exc:  # noqa: BLE001 - openpyxl raises assorted types
        return DOC_UNKNOWN, [f"could not open workbook: {exc}"]

    if sheets:
        sheetnames = [sheet.name for sheet in sheets]
        for sheet in sheets:
            if holdings_header(sheet.rows) is not None:
                return DOC_CAS_XLSX, []
            for row in sheet.rows:
                keys = {_norm_key(str(c)) for c in row if c is not None}
                if REQUIRED_COLUMNS <= keys:
                    return DOC_TRADEBOOK_XLSX, []
        if frozenset(sheetnames) == CLEAN_SHEET_NAMES:
            return DOC_CONTRACT_NOTE_XLSX, []
        if any(_DATE_SHEET.match(name.strip()) for name in sheetnames):
            return DOC_CONTRACT_NOTE_XLSX, []
        for sheet in sheets:
            for row in sheet.rows:
                joined = " ".join(str(c) for c in row if c is not None).upper()
                if CONTRACT_NOTE_MARKER in joined:
                    return DOC_CONTRACT_NOTE_XLSX, []
    return DOC_UNKNOWN, []


def _pdf_is_encrypted(content: bytes) -> bool:
    """Whether the trailer declares encryption. A heuristic, and deliberately one: it only
    decides which sentence to show, and ``_classify_pdf`` still confirms by trying to open
    the file."""
    return b"/Encrypt" in content[-_PDF_TRAILER_SCAN:] or b"/Encrypt" in content[:_PDF_TRAILER_SCAN]


#: The three things that can happen when we try to open a PDF. Decided from the PDF
#: library's own exception, never from a heuristic: pdfminer raises
#: ``PDFPasswordIncorrect`` for a password that does not open the file, and anything else
#: means the file itself is broken -- where a password cannot help and must not be asked for.
UNLOCK_OPENED = "OPENED"
UNLOCK_WRONG_PASSWORD = "WRONG_PASSWORD"
UNLOCK_DAMAGED = "DAMAGED"


@dataclass(frozen=True, slots=True)
class Unlock:
    """Result of ``unlock_pdf``. ``password`` is the one that opened the file ("" when it
    needs none); ``source`` is the saved source it came from, None for no password or one
    typed just now."""

    outcome: str
    password: str = ""
    source: str | None = None


def _is_wrong_password(exc: BaseException) -> bool:
    """pdfplumber wraps pdfminer's exceptions; walk the chain for PDFPasswordIncorrect."""
    from pdfminer.pdfdocument import PDFPasswordIncorrect

    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, PDFPasswordIncorrect):
            return True
        stack.extend(arg for arg in current.args if isinstance(arg, BaseException))
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                stack.append(linked)
    return False


def _try_open(content: bytes, password: str) -> str:
    """One attempt with ``password`` exactly as given. Reads page 1, so a file that only
    fails once decryption actually starts is caught here rather than in the parser."""
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(content), password=password) as pdf:
            if pdf.pages:
                pdf.pages[0].extract_text()
        return UNLOCK_OPENED
    except Exception as exc:  # noqa: BLE001 - pdfplumber raises assorted types
        return UNLOCK_WRONG_PASSWORD if _is_wrong_password(exc) else UNLOCK_DAMAGED


def unlock_pdf(content: bytes, candidates: list[tuple[str | None, str]]) -> Unlock:
    """Open a PDF: first with no password, then each ``(source, password)`` EXACTLY as
    given -- no case variants, no PAN derivation. The first that opens wins.

    A file that opens with no password never touches the candidates. A file that fails for
    any reason other than a wrong password is DAMAGED at once: trying more passwords on a
    broken file would only turn "this file is damaged" into a misleading "locked".
    """
    first = _try_open(content, "")
    if first == UNLOCK_OPENED:
        return Unlock(UNLOCK_OPENED)
    if first == UNLOCK_DAMAGED:
        return Unlock(UNLOCK_DAMAGED)
    for source, password in candidates:
        if not password:
            continue
        if _try_open(content, password) == UNLOCK_OPENED:
            return Unlock(UNLOCK_OPENED, password=password, source=source)
    return Unlock(UNLOCK_WRONG_PASSWORD)


def _classify_pdf(content: bytes, password: str = "") -> tuple[str, bool, list[str]]:
    """Which document a PDF is, from the text on its first page.

    Returns (document kind, encrypted, notes). Only the first page is read: every marker
    that distinguishes these documents is in the header block, and a 60-page bulk note
    costs seconds to extract in full for an answer the first page already gives.
    """
    try:
        import pdfplumber
    except ImportError:  # pragma: no cover - dependency guard
        return DOC_CONTRACT_NOTE_PDF, False, ["PDF not inspected: pdfplumber unavailable"]

    error: Exception | None = None
    # Some regenerated CAS PDFs retain /Encrypt but only accept the empty password.
    # Try the supplied password exactly, then no password; never invent a case variant.
    for candidate in (password, "") if password else ("",):
        try:
            with pdfplumber.open(io.BytesIO(content), password=candidate) as pdf:
                pages = pdf.pages[:2]
                text = "\n".join(page.extract_text() or "" for page in pages)
            break
        except Exception as exc:  # noqa: BLE001 - pdfplumber raises assorted types
            error = exc
    else:
        if _pdf_is_encrypted(content):
            return DOC_UNKNOWN, True, [str(error)]
        return DOC_UNKNOWN, False, [f"could not read PDF: {error}"]

    upper = text.upper()
    if not upper.strip():
        # A scan, or a note printed to image. Nothing to classify and nothing to parse:
        # this codebase reads text, it does not OCR.
        return DOC_UNKNOWN, False, ["no readable text on the first pages (scanned image?)"]
    if any(marker in upper for marker in _CAS_MARKERS):
        return DOC_CAS_PDF, False, []
    if CONTRACT_NOTE_MARKER in upper:
        return DOC_CONTRACT_NOTE_PDF, False, []
    return DOC_UNKNOWN, False, []


def _mismatch(filename: str, extension: str, container: str, document: str) -> FileVerdict:
    """The verdict for a file whose bytes are not what its name claims."""
    named = {
        CONTAINER_PDF: "a PDF",
        CONTAINER_XLSX: "an Excel workbook",
        CONTAINER_XLS: "an old-style Excel file (.xls)",
        CONTAINER_ZIP: "a ZIP archive",
        CONTAINER_TEXT: "a plain text file",
        CONTAINER_BINARY: "not a document we can read",
        CONTAINER_EMPTY: "empty",
    }[container]
    hint = {
        CONTAINER_PDF: "Rename it back to .pdf and upload it again — we read contract-note PDFs.",
        CONTAINER_XLSX: "Rename it back to .xlsx and upload it again.",
        CONTAINER_XLS: "Open it in Excel and save it as .xlsx, then upload that.",
        CONTAINER_ZIP: "Unzip it first and upload the files inside.",
        CONTAINER_TEXT: "If this is a tradebook, save it as .csv and upload it again.",
        CONTAINER_BINARY: "Download the statement again from your broker and upload that file.",
        CONTAINER_EMPTY: "The file is 0 bytes — download it again from your broker.",
    }[container]
    return FileVerdict(
        filename=filename,
        extension=extension,
        container=container,
        document=document,
        ok=False,
        reason=f"This is named .{extension} but it's actually {named}.",
        hint=hint,
    )


def classify(content: bytes, filename: str, pdf_password: str = "") -> FileVerdict:
    """Both tiers, on one file. Never raises.

    ``pdf_password`` is the password that opens this file, exactly (``unlock_pdf`` found it
    among the investor's saved ones, or they just typed it). Passing nothing is correct for
    every other caller and simply produces the "locked" verdict for a locked file.
    """
    extension = extension_of(filename)
    tier1 = check_extension(filename)
    if tier1 is not None:
        return FileVerdict(
            filename=filename,
            extension=extension,
            container=CONTAINER_BINARY,
            document=DOC_UNKNOWN,
            ok=False,
            reason=tier1,
            hint="Accepted files: contract notes (.pdf or .xlsx) and tradebooks (.csv).",
        )

    container = sniff_container(content)

    if container == CONTAINER_EMPTY:
        return _mismatch(filename, extension, container, DOC_UNKNOWN)

    # Tier 2a -- the container has to match what the name claimed. A CSV that is really a
    # PDF is the case in the brief, and it is caught here, before any parser is chosen.
    expected = {"csv": CONTAINER_TEXT, "xlsx": CONTAINER_XLSX, "pdf": CONTAINER_PDF}[extension]
    if container != expected:
        return _mismatch(filename, extension, container, DOC_UNKNOWN)

    # Tier 2b -- what the document inside actually is.
    encrypted = False
    if container == CONTAINER_PDF:
        # Open it first, with the library's own verdict: a damaged file and a locked one
        # need opposite answers ("send another copy" vs "type its password").
        unlock = unlock_pdf(content, [(None, pdf_password)] if pdf_password else [])
        if unlock.outcome == UNLOCK_DAMAGED:
            return FileVerdict(
                filename=filename,
                extension=extension,
                container=container,
                document=DOC_UNKNOWN,
                ok=False,
                reason="This PDF is damaged or not a complete PDF, so we couldn't open it.",
                hint="Download it again from where you got it and upload that copy.",
            )
        if unlock.outcome == UNLOCK_WRONG_PASSWORD:
            encrypted, document, notes = True, DOC_UNKNOWN, []
        else:
            document, encrypted, notes = _classify_pdf(content, unlock.password)
    elif container == CONTAINER_XLSX:
        document, notes = _classify_xlsx(content)
    else:
        document, notes = _classify_text(content)

    if encrypted:
        return FileVerdict(
            filename=filename,
            extension=extension,
            container=container,
            document=DOC_UNKNOWN,
            ok=False,
            encrypted=True,
            reason="This file is locked with a password.",
            hint="Enter its password below. We'll remember it for this kind of statement.",
            notes=notes,
        )

    routed = ROUTING.get(document)
    if routed is not None:
        return FileVerdict(
            filename=filename,
            extension=extension,
            container=container,
            document=document,
            ok=True,
            adapter=routed[0],
            notes=notes,
        )

    return FileVerdict(
        filename=filename,
        extension=extension,
        container=container,
        document=document,
        ok=False,
        reason=_unsupported_reason(document),
        hint=_unsupported_hint(document),
        notes=notes,
    )


def _unsupported_reason(document: str) -> str:
    if document == DOC_CAS_PDF:
        return "This looks like a CAS or holding statement, not a trade document."
    if document == DOC_TRADEBOOK_XLSX:
        return "This looks like a tradebook saved as a spreadsheet."
    if document == DOC_CONTRACT_NOTE_CSV:
        return "This looks like a contract note saved as CSV."
    return (
        "We opened this file but couldn't recognise it as a tradebook, contract note "
        "or CAS holdings table."
    )


def _unsupported_hint(document: str) -> str:
    if document == DOC_CAS_PDF:
        return (
            "We build your portfolio from trades, not from a holding snapshot. "
            "Upload a tradebook (Console → Reports → Tradebook) or a contract note instead."
        )
    if document == DOC_TRADEBOOK_XLSX:
        return "Export the tradebook as CSV from Zerodha Console and upload that."
    if document == DOC_CONTRACT_NOTE_CSV:
        return "Download the contract note as PDF or XLSX from Console and upload that."
    return (
        "Download the file again from Zerodha Console — Reports → Tradebook for trades, "
        "or Reports → Contract notes for notes with charges."
    )


def verdict_for_adapter(verdict: FileVerdict, adapter: str) -> FileVerdict:
    """Narrow a verdict to one declared adapter -- the check the single-adapter upload
    routes need on top of ``classify``.

    ``classify`` answers "what is this?". This answers "does what this is match the slot it
    was dropped into?", which is the question ``/ingest/csv`` and ``/ingest/xlsx`` ask,
    since each accepts exactly one document kind. A right-shaped file in the wrong slot
    fails here with the slot named, not with a parse error 200 rows later.
    """
    if not verdict.ok:
        return verdict
    if verdict.adapter == adapter:
        return verdict
    expected = next((d for d, (a, _) in ROUTING.items() if a == adapter), None)
    return FileVerdict(
        filename=verdict.filename,
        extension=verdict.extension,
        container=verdict.container,
        document=verdict.document,
        ok=False,
        reason=(
            f"This is {LABELS.get(verdict.document, 'a different kind of file')}, "
            f"but this slot expects {LABELS.get(expected or '', 'another file type')}."
        ),
        hint=_slot_hint(verdict.document),
        notes=verdict.notes,
    )


def _slot_hint(document: str) -> str:
    routed = ROUTING.get(document)
    if routed is None:
        return (
            "Choose the matching upload type, or use the onboarding wizard, "
            "which sorts files for you."
        )
    return (
        f"Upload it under {LABELS[document]} instead — or use the onboarding wizard, "
        "which sends each file to the right place for you."
    )


def summarise(verdicts: list[FileVerdict]) -> dict[str, Any]:
    """Counts a caller can render without walking the list. ``ok``/``rejected`` only --
    what happened to an accepted file afterwards is staging's story, not validation's."""
    return {
        "ok": sum(1 for v in verdicts if v.ok),
        "rejected": sum(1 for v in verdicts if not v.ok),
    }
