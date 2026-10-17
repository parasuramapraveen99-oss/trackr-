"""Content-based dispatch for email using the existing deterministic adapters."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from typing import Any

from app.db import crypto, identity
from app.ingest import file_validation as fv
from app.ingest import webhook
from app.ingest.base import DocumentIdentity
from app.ingest.cas_pdf import CasPdfAdapter
from app.ingest.zerodha_contract_note import (
    CLIENT_PAN,
    CLIENT_UCC,
    CONTRACT_NUMBER,
    TRADE_DATE,
    ZerodhaContractNoteAdapter,
    note_totals_problem,
    parse_charges,
)
from app.ingest.zerodha_contract_note_xlsx import ZerodhaContractNoteXlsxAdapter
from app.ingest.zerodha_tradebook import ZerodhaTradebookAdapter


class BlockedDocument(ValueError):
    def __init__(self, status: str, detail: str):
        super().__init__(detail)
        self.status = status


@dataclass
class Selection:
    adapter: Any
    account_id: str
    doctype: str
    content_type: str
    verified: bool
    validation_problem: str = ""


def select(conn: Any, investor_id: str, content: bytes, filename: str) -> Selection:
    container = fv.sniff_container(content)
    if content.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")):
        raise BlockedDocument("IGNORED", "Image attachment; no supported financial document")
    extension = {fv.CONTAINER_PDF: "pdf", fv.CONTAINER_XLSX: "xlsx", fv.CONTAINER_TEXT: "csv"}.get(
        container
    )
    if not extension:
        raise BlockedDocument("UNSUPPORTED", "Unsupported or empty attachment; original retained")
    password = ""
    if container == fv.CONTAINER_PDF:
        unlock = webhook.unlock_with_saved_passwords(conn, investor_id, content)
        if unlock.outcome == fv.UNLOCK_WRONG_PASSWORD:
            raise BlockedDocument("WAITING_PASSWORD", "No saved password opens this PDF")
        if unlock.outcome != fv.UNLOCK_OPENED:
            raise BlockedDocument("NEEDS_ATTENTION", "PDF is damaged or incomplete")
        password = unlock.password
    if container == fv.CONTAINER_XLSX:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 5000 or sum(e.file_size for e in entries) > 64 * 1024 * 1024:
                raise BlockedDocument(
                    "UNSUPPORTED", "Workbook expansion exceeds the processing limit"
                )
    # The filename is provenance. Dispatch uses the actual container even if it was
    # forwarded without an extension or with a misleading filename.
    verdict = fv.classify(content, f"attachment.{extension}", password)
    if not verdict.ok:
        raise BlockedDocument("UNSUPPORTED", verdict.reason or "Unrecognized document layout")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, lower(broker), upper(COALESCE(client_id, '')) "
            "FROM account WHERE investor_id = %s ORDER BY id",
            (investor_id,),
        )
        accounts = [(str(r[0]), r[1], r[2].strip()) for r in cur.fetchall()]
    if not accounts:
        raise BlockedDocument("NEEDS_ATTENTION", "No account is configured for this investor")

    problem = ""
    document_identity = DocumentIdentity()
    if verdict.adapter == CasPdfAdapter.name:
        mapping = {}
        for account_id, broker, _ in accounts:
            if broker in mapping:
                raise BlockedDocument("NEEDS_ATTENTION", "CAS account mapping is ambiguous")
            mapping[broker] = account_id
        adapter = CasPdfAdapter(password=password, broker_accounts=mapping)
        account_id = adapter._resolve_account(adapter._read_text(content))
        document_identity = adapter.identity(content)
        doctype, content_type = "CAS", "application/pdf"
    else:
        if verdict.adapter == ZerodhaContractNoteAdapter.name:
            adapter = ZerodhaContractNoteAdapter(password=password)
            text = adapter._text(content)
            if "ZERODHA" not in text.upper():
                raise BlockedDocument(
                    "UNSUPPORTED", "Contract-note issuer is not a supported broker"
                )
            if (
                len({m[1].upper() for m in CLIENT_PAN.finditer(text)}) > 1
                or len({m[1].upper() for m in CLIENT_UCC.finditer(text)}) > 1
                or len({m[1] for m in TRADE_DATE.finditer(text)}) > 1
            ):
                raise BlockedDocument(
                    "NEEDS_ATTENTION", "PDF contains multiple account or note scopes"
                )
            problem = note_totals_problem(text) or ""
            if not CONTRACT_NUMBER.search(text) or not parse_charges(text):
                problem = "Contract-note identity or required charge evidence is missing"
            doctype, content_type = "CONTRACT_NOTE", "application/pdf"
        elif verdict.adapter == ZerodhaContractNoteXlsxAdapter.name:
            # XLSX parsing remains available, but the current adapter returns the first
            # identity it finds. Multi-sheet identity certification is not yet released
            # for unattended promotion, so retain its normal staged-review behavior.
            adapter = ZerodhaContractNoteXlsxAdapter()
            problem = "Workbook requires review until every sheet's identity is certified"
            doctype = "CONTRACT_NOTE"
            content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif verdict.adapter == ZerodhaTradebookAdapter.name:
            adapter = ZerodhaTradebookAdapter()
            problem = "Tradebook has no independently verified account identity"
            doctype, content_type = "TRADEBOOK", "text/csv"
        else:
            raise BlockedDocument("UNSUPPORTED", "No released email adapter for this layout")
        if hasattr(adapter, "identity"):
            document_identity = adapter.identity(content)
        candidates = [(aid, code) for aid, broker, code in accounts if broker == "zerodha"]
        if document_identity.client_code:
            candidates = [
                (aid, code)
                for aid, code in candidates
                if code == document_identity.client_code.upper()
            ]
        if len(candidates) != 1:
            raise BlockedDocument(
                "NEEDS_ATTENTION", "Document does not identify exactly one account"
            )
        account_id = candidates[0][0]

    # Existing identity mismatch behavior is preserved. Missing proof is additionally
    # ineligible for automation; it is never converted into a successful verification.
    identity.verify(conn, document_identity, investor_id, account_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pan_hash, pan_hash_next FROM investor_secret WHERE investor_id = %s",
            (investor_id,),
        )
        row = cur.fetchone()
    pan_verified = bool(
        document_identity.pan
        and row
        and crypto.hash_pan(document_identity.pan) in {h for h in row if h}
    )
    verified = pan_verified and (doctype == "CAS" or bool(document_identity.client_code))
    if doctype == "CAS" and not verified:
        raise BlockedDocument("NEEDS_ATTENTION", "CAS owner could not be independently verified")
    return Selection(adapter, account_id, doctype, content_type, verified, problem)
