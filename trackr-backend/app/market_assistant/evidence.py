"""From search results to quotable official excerpts. docs/AI_ASSISTANT_PRIVACY_PLAN.md 6-7.

Every result is re-admitted here (``sources.admit_url``) whatever the search service
claimed. Text is cleaned, split at paragraph and sentence boundaries, and ranked locally
against the catalogue's own query: word overlap by default, or BM25 / hybrid BM25 + a
LOCAL embedding model when configured (``retrieval.py``, Phase 5); never a remote
embedding service. Each
excerpt keeps its document hash and exact offsets into the cleaned text, so a quote always
resolves to the span it came from and is never paraphrased.

Dates are kept honest: a missing date stays missing (shown as such), a document dated in
the future is dropped as unreliable, and one older than the topic's window is not "latest".
A passage carrying a personal identifier (an email, phone number, PAN or Aadhaar) is
dropped rather than masked: public availability is not a reason to republish it, and
masking would break the exact-quote guarantee.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from app.ingest.redaction import BARE_PAN, EMAIL, PHONE
from app.market_assistant.catalogue import OFFICIAL_PUBLISHERS, SearchSpec
from app.market_assistant.retrieval import Embedder, RetrievalMode, rank
from app.market_assistant.sources import (
    admit_public_url,
    admit_url,
    public_domain,
    publisher_domain,
)

MAX_EXCERPTS = 3
MAX_EXCERPT_CHARS = 600
_CHUNK_TARGET = 900
_MIN_CHUNK_CHARS = 80

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_AADHAAR = re.compile(r"(?<!\d)\d{4}\s?\d{4}\s?\d{4}(?!\d)")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[a-z]{3,}")
_STOPWORDS = frozenset({"the", "and", "for", "with", "from", "of", "india", "indian"})


@dataclass(frozen=True)
class RawResult:
    """One search result as received, before any admission check. Defined here, not in
    the search adapter, so this module and the checker built on it import no network code."""

    url: str
    title: str
    published: str | None
    text: str


@dataclass(frozen=True)
class Excerpt:
    doc_id: str
    chunk_id: str
    url: str
    domain: str
    publisher: str
    title: str
    published: date | None
    text: str
    start: int
    end: int
    #: The server template that introduces the quote: "states" or "announced_on".
    template: str = "states"
    #: "official" (regulator, exchange, government) or "news" (reported, not confirmed).
    source_kind: str = "official"
    verification_url: str | None = None


@dataclass(frozen=True)
class Doc:
    url: str
    title: str
    published: date | None
    text: str
    doc_id: str
    source_kind: str | None = None
    verification_url: str | None = None


_MONTHS = {
    m: i
    for i, names in enumerate(
        (("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
         ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
         ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
         ("dec", "december")),
        start=1,
    )
    for m in names
}
#: A date the document prints next to a "Date" label, near its top. Indian circulars
#: write day-first numerals (24/07/2026), so d/m/y is the only numeric reading.
_TEXT_DATE = re.compile(
    r"\b(?:notice\s+)?dated?\s*[:\-]?\s*(?:"
    r"(?P<mname>[a-z]{3,9})\.?\s+(?P<mday>\d{1,2}),?\s+(?P<myear>\d{4})"
    r"|(?P<dday>\d{1,2})\s+(?P<dname>[a-z]{3,9})\.?,?\s+(?P<dyear>\d{4})"
    r"|(?P<nday>\d{1,2})[/.-](?P<nmon>\d{1,2})[/.-](?P<nyear>\d{4})"
    r")",
    re.I,
)
TEXT_DATE_SCAN_CHARS = 2000


def text_date(text: str) -> date | None:
    """The first labelled date in the top of a document, or None. Never guessed: an
    unlabelled or unparseable date is None."""
    for m in _TEXT_DATE.finditer(text[:TEXT_DATE_SCAN_CHARS]):
        try:
            if m.group("mname"):
                month = _MONTHS.get(m.group("mname").lower())
                day, year = int(m.group("mday")), int(m.group("myear"))
            elif m.group("dname"):
                month = _MONTHS.get(m.group("dname").lower())
                day, year = int(m.group("dday")), int(m.group("dyear"))
            else:
                month = int(m.group("nmon"))
                day, year = int(m.group("nday")), int(m.group("nyear"))
            if month:
                return date(year, month, day)
        except ValueError:
            continue
    return None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def clean(text: str) -> str:
    """Control characters out, runs of spaces folded, paragraph breaks kept."""
    text = _CONTROL.sub(" ", text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def admit_documents(results: list[RawResult], spec: SearchSpec, today: date) -> list[Doc]:
    oldest = today - timedelta(days=spec.max_age_days)
    seen: set[str] = set()
    docs: list[Doc] = []
    for r in results:
        if not admit_url(r.url, spec.domains):
            continue
        text = clean(r.text)
        if spec.dates_from_text:
            published = text_date(text)
            if published is None:  # undated: cannot be "latest", so not admitted
                continue
        else:
            published = _parse_date(r.published)
        if published and (published > today + timedelta(days=1) or published < oldest):
            continue
        if len(text) < _MIN_CHUNK_CHARS:
            continue
        digest = hashlib.sha256(text.encode()).hexdigest()
        if digest in seen:  # the same document syndicated under two URLs
            continue
        seen.add(digest)
        docs.append(Doc(r.url, clean(r.title) or spec.publisher, published, text, digest[:16]))
    return docs


_ISSUER_WEBSITE = re.compile(
    r"\b(?:company|issuer|fund|corporate)\s+(?:web\s*site|website)\s*[:=]\s*"
    r"(https://[^\s<>\"'\)]+|www\.[a-z0-9.-]+)", re.I,
)


def admit_open_documents(results: list[RawResult], spec: SearchSpec, today: date) -> list[Doc]:
    """Trust known publishers and issuer hosts vouched for by a regulator/exchange.

    A broad Exa search may discover any site. An unknown site is admitted only when an
    admitted official document explicitly labels its company/issuer/fund website. News
    links, arbitrary hyperlinks and the model's opinion cannot grant source trust. The
    verification reference is carried with each issuer excerpt for review.
    """
    docs = admit_documents(results, spec, today)
    docs = [Doc(d.url, d.title, d.published or text_date(d.text), d.text, d.doc_id)
            for d in docs if not _has_personal_data(d.title)]
    oldest = today - timedelta(days=spec.max_age_days)
    docs = [d for d in docs if d.published is None or oldest <= d.published <= today]
    verified: dict[str, str] = {}
    # Website identity references can be older than the news being researched. They
    # verify the host only and are never relabelled as current evidence.
    anchors = admit_documents(results, replace(spec, max_age_days=3650), today)
    for doc in anchors:
        if publisher_domain(doc.url) not in OFFICIAL_PUBLISHERS:
            continue
        if not is_safe_passage(doc.text):
            continue
        for match in _ISSUER_WEBSITE.finditer(doc.text):
            url = match[1].rstrip(".,;")
            if url.startswith("www."):
                url = "https://" + url
            if admit_public_url(url):
                verified[public_domain(url)] = doc.url
    seen = {d.doc_id for d in docs}
    for result in results:
        if not admit_public_url(result.url):
            continue
        host = public_domain(result.url)
        # Exact host only: an issuer reference cannot authorize every tenant/subdomain.
        if host not in verified:
            continue
        text = clean(result.text)
        published = _parse_date(result.published) or text_date(text)
        if published and not oldest <= published <= today:
            continue
        if len(text) < _MIN_CHUNK_CHARS:
            continue
        if _has_personal_data(result.title):
            continue
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        if digest not in seen:
            seen.add(digest)
            docs.append(Doc(result.url, clean(result.title), published, text, digest,
                            "issuer", verified[host]))
    return docs


def chunks(text: str) -> list[tuple[int, int]]:
    """Offsets of passages: paragraphs merged up to a target size, long ones split at
    sentence ends. Offsets index ``text`` exactly."""
    spans: list[tuple[int, int]] = []
    for para in re.finditer(r"[^\n]+(?:\n(?!\n)[^\n]+)*", text):
        start, end = para.span()
        if end - start <= _CHUNK_TARGET:
            spans.append((start, end))
            continue
        cursor = start
        for boundary in _SENTENCE_END.finditer(text, start, end):
            if boundary.start() - cursor >= _CHUNK_TARGET:
                spans.append((cursor, boundary.start()))
                cursor = boundary.end()
        spans.append((cursor, end))
    merged: list[tuple[int, int]] = []
    for span in spans:
        if (
            merged
            and span[0] - merged[-1][0] < _CHUNK_TARGET
            and span[1] - merged[-1][0] <= _CHUNK_TARGET
        ):
            merged[-1] = (merged[-1][0], span[1])
        else:
            merged.append(span)
    return [s for s in merged if s[1] - s[0] >= _MIN_CHUNK_CHARS]


#: Site chrome that official pages repeat around their content: menus, accessibility
#: controls, breadcrumbs. Found in live rbi.org.in / nseindia.com / bseindia.com text on
#: 11 Oct 2026 ("Press Releases | Official Website of ...", "Font Size A+ | Reset | A-").
_CHROME_PHRASES = re.compile(
    r"font size|skip to (?:main )?content|high contrast|a\+\s*\|?\s*(?:a|reset)|view all\s*-",
    re.I,
)
_SEPARATOR = re.compile(r"\s[|\u2022]\s|\s-\s")


def is_navigation(passage: str) -> bool:
    """True for a passage that is page chrome rather than content: accessibility controls,
    or a run of menu items (three or more ``|``/bullet/`` - `` separators with short items
    between them). Conservative: a sentence with one pipe is kept. Chrome repeats the
    query's words ("Press Releases", "Circulars"), so left in it outranks the real text in
    every ranking mode (docs/AI_ASSISTANT_PRIVACY_PLAN.md section 7.2, extraction quality)."""
    if _CHROME_PHRASES.search(passage):
        return True
    separators = len(_SEPARATOR.findall(passage))
    if separators < 3:
        return False
    items = [i for i in re.split(r"\s[|\u2022-]\s", passage) if i.strip()]
    return sum(len(i.split()) for i in items) / max(len(items), 1) <= 6


def _has_personal_data(passage: str) -> bool:
    return bool(
        EMAIL.search(passage)
        or PHONE.search(passage)
        or BARE_PAN.search(passage.upper())
        or _AADHAAR.search(passage)
    )


_INJECTION = re.compile(
    r"ignore\s+(?:(?:all|the|your|previous|prior)\s+)*(?:instructions|rules|prompt)|"
    r"system\s+(?:message|prompt)|(?:reveal|send|upload)\s+(?:the\s+)?(?:secret|password|"
    r"portfolio|credential|api\s+key)|(?:assistant|developer)\s*:", re.I,
)


def is_safe_passage(text: str) -> bool:
    return not (_has_personal_data(text) or _INJECTION.search(text) or is_navigation(text))


def _terms(text: str) -> set[str]:
    return set(_WORD.findall(text.casefold())) - _STOPWORDS


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    """Cut a passage to the excerpt cap at a sentence end, keeping exact offsets."""
    if end - start <= MAX_EXCERPT_CHARS:
        return start, end
    cut = start
    for boundary in _SENTENCE_END.finditer(text, start, start + MAX_EXCERPT_CHARS):
        cut = boundary.start()
    return (start, cut) if cut > start else (start, start + MAX_EXCERPT_CHARS)


def select_excerpts(docs: list[Doc], spec: SearchSpec) -> list[Excerpt]:
    query_terms = _terms(spec.query)
    picked: list[tuple[int, Excerpt]] = []
    for doc in docs:
        best: tuple[int, int, int] | None = None
        for index, (start, end) in enumerate(chunks(doc.text)):
            passage = doc.text[start:end]
            if not is_safe_passage(passage):
                continue
            score = len(query_terms & _terms(passage)) * 10 - index
            if best is None or score > best[0]:
                best = (score, start, end)
        if best is None:
            continue
        start, end = _trim(doc.text, best[1], best[2])
        picked.append(
            (
                best[0],
                Excerpt(
                    doc_id=doc.doc_id,
                    chunk_id=f"{doc.doc_id}:{start}-{end}",
                    url=doc.url,
                    domain=publisher_domain(doc.url),
                    publisher=spec.publisher,
                    title=doc.title[:300],
                    published=doc.published,
                    text=doc.text[start:end],
                    start=start,
                    end=end,
                ),
            )
        )
    # Newest dated first; undated after dated, since "latest" cannot be claimed for them.
    picked.sort(
        key=lambda p: (p[1].published is None, -(p[1].published or date.min).toordinal(), -p[0])
    )
    return [excerpt for _, excerpt in picked[:MAX_EXCERPTS]]


#: docs/AI_ASSISTANT_PRIVACY_PLAN.md section 10: up to 8 passages offered to the selector.
MAX_CANDIDATES = 8
MAX_CANDIDATES_PER_DOC = 3


@dataclass(frozen=True)
class Candidate:
    """A passage offered to the selector, under a short local ID (``c1``..``c8``). The ID
    means nothing outside this one request, so the model never sees a URL or a hash."""

    candidate_id: str
    doc: Doc
    start: int
    end: int

    @property
    def text(self) -> str:
        return self.doc.text[self.start : self.end]


def candidate_chunks(docs: list[Doc], spec: SearchSpec) -> list[Candidate]:
    """The best passages across all admitted documents, ranked locally, at most three per
    document so one page cannot crowd out the rest. Passages with personal data are out."""
    query_terms = _terms(spec.query)
    scored: list[tuple[int, Doc, int, int]] = []
    for doc in docs:
        per_doc: list[tuple[int, Doc, int, int]] = []
        for index, (start, end) in enumerate(chunks(doc.text)):
            passage = doc.text[start:end]
            if not is_safe_passage(passage):
                continue
            per_doc.append((len(query_terms & _terms(passage)) * 10 - index, doc, start, end))
        per_doc.sort(key=lambda item: -item[0])
        scored.extend(per_doc[:MAX_CANDIDATES_PER_DOC])
    scored.sort(key=lambda item: -item[0])
    return [
        Candidate(f"c{n}", doc, start, end)
        for n, (_, doc, start, end) in enumerate(scored[:MAX_CANDIDATES], start=1)
    ]


@dataclass(frozen=True)
class Passage:
    doc: Doc
    start: int
    end: int

    @property
    def text(self) -> str:
        return self.doc.text[self.start : self.end]


def rank_passages(
    docs: list[Doc], spec: SearchSpec, mode: RetrievalMode, embedder: Embedder | None = None
) -> tuple[list[Passage], RetrievalMode]:
    """Every eligible passage of every admitted document, ranked once across documents.
    Passages carrying personal data are out before ranking, so they are never embedded."""
    passages = [
        Passage(doc, start, end)
        for doc in docs
        for start, end in chunks(doc.text)
        if is_safe_passage(doc.text[start:end])
    ]
    result = rank(spec.query, [p.text for p in passages], mode, embedder)
    return [passages[i] for i in result.order], result.mode


def candidates_from_ranking(ranked: list[Passage]) -> list[Candidate]:
    """The ranked passages under the same caps as ``candidate_chunks``: three per page,
    eight in all."""
    per_doc: dict[str, int] = {}
    picked: list[Passage] = []
    for p in ranked:
        if per_doc.get(p.doc.doc_id, 0) >= MAX_CANDIDATES_PER_DOC:
            continue
        per_doc[p.doc.doc_id] = per_doc.get(p.doc.doc_id, 0) + 1
        picked.append(p)
        if len(picked) == MAX_CANDIDATES:
            break
    return [Candidate(f"c{n}", p.doc, p.start, p.end) for n, p in enumerate(picked, start=1)]


def excerpts_from_ranking(ranked: list[Passage], spec: SearchSpec) -> list[Excerpt]:
    """The best-ranked passage of each page, newest page first, as ``select_excerpts``."""
    seen: set[str] = set()
    picked: list[tuple[int, Excerpt]] = []
    for position, p in enumerate(ranked):
        if p.doc.doc_id in seen:
            continue
        seen.add(p.doc.doc_id)
        start, end = _trim(p.doc.text, p.start, p.end)
        picked.append(
            (
                -position,
                Excerpt(
                    doc_id=p.doc.doc_id,
                    chunk_id=f"{p.doc.doc_id}:{start}-{end}",
                    url=p.doc.url,
                    domain=publisher_domain(p.doc.url),
                    publisher=spec.publisher,
                    title=p.doc.title[:300],
                    published=p.doc.published,
                    text=p.doc.text[start:end],
                    start=start,
                    end=end,
                ),
            )
        )
    picked.sort(
        key=lambda x: (x[1].published is None, -(x[1].published or date.min).toordinal(), -x[0])
    )
    return [excerpt for _, excerpt in picked[:MAX_EXCERPTS]]


def excerpt_from_span(
    candidate: Candidate,
    start: int,
    end: int,
    template: str,
    publisher: str,
    source_kind: str = "official",
) -> Excerpt:
    """A checked selection back into an Excerpt. ``start``/``end`` index the document."""
    doc = candidate.doc
    return Excerpt(
        doc_id=doc.doc_id,
        chunk_id=f"{doc.doc_id}:{start}-{end}",
        url=doc.url,
        domain=public_domain(doc.url) if source_kind == "issuer" else publisher_domain(doc.url),
        publisher=publisher,
        title=doc.title[:300],
        published=doc.published,
        text=doc.text[start:end],
        start=start,
        end=end,
        template=template,
        source_kind=source_kind,
        verification_url=doc.verification_url,
    )
