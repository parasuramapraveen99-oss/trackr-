"""The public concept catalogue: every answer Phase 1 can give.

Versioned public reference text, nothing derived from any investor. Each entry is an
approved topic (``topic_id``), the phrases that resolve to it (``aliases``), and the
answer shown for a definition question (``body``). A topic with a ``search`` spec can also
answer "what is the latest from ..." by searching that body's OWN website and nothing
else (Phase 2); its query and domains are fixed here, never built from the user's words.

REVIEW STATUS. The bodies are general financial-education text written by the
implementer on 11 October 2026 and reviewed by the owner the same day (version
2026-10-11.2). Changing an entry is a new ``CATALOGUE_VERSION`` and needs a fresh
review. 2026-10-11.3 adds one alias ("rights entitlements") and no body text, on the
owner's instruction of the same day. 2026-10-11.4 changes only the NSE and BSE search
policy (dates read from the circular text, 90-day window); no body text. Tax topics are
left out on purpose: rates and rules change with each Finance Act, and stale tax text is
worse than none.
"""

from __future__ import annotations

from dataclasses import dataclass

CATALOGUE_VERSION = "2026-10-11.4"
REVIEW_STATUS = "OWNER_REVIEWED_2026-10-11"
SOURCE_LABEL = "Trackr+ concept library"


@dataclass(frozen=True)
class SearchSpec:
    """A reviewed, fixed public search. ``publisher`` is what the user sees as the source."""

    query: str
    domains: tuple[str, ...]
    publisher: str
    #: How far back "latest" looks. Older documents are not "latest".
    max_age_days: int
    #: True for sources whose pages carry no publish date in the search index (exchange
    #: circulars, live check 11 Oct 2026: zero results with a date filter, all undated
    #: without one). Then the search sends no date filter, and a document is admitted only
    #: if a date printed in its own text ("Date : October 07, 2026", "Notice Date 24 Jul
    #: 2026") falls inside the window. An undated document is dropped, never guessed.
    dates_from_text: bool = False


@dataclass(frozen=True)
class Concept:
    topic_id: str
    title: str
    aliases: tuple[str, ...]
    body: str
    search: SearchSpec | None = None


_CONCEPTS: tuple[Concept, ...] = (
    Concept(
        "xirr",
        "XIRR",
        ("xirr", "extended internal rate of return"),
        "XIRR is your yearly return when money goes in and out at different times. It "
        "counts every buy, sale and dividend on the date it happened, so a rupee invested "
        "last month weighs less than one invested years ago. Use it to see how your own "
        "money has grown.",
    ),
    Concept(
        "cagr",
        "CAGR",
        ("cagr", "compound annual growth rate"),
        "CAGR is the steady yearly growth rate that takes a starting value to an ending "
        "value. It ignores money added or taken out in between, so it suits a single "
        "investment held over time. Trackr+ shows it for one holding at a time.",
    ),
    Concept(
        "twrr",
        "TWRR",
        ("twrr", "time weighted return", "time weighted rate of return"),
        "TWRR measures how the investments themselves performed, with the effect of when "
        "money was added or withdrawn removed. It is the fair way to compare a portfolio "
        "with an index or with another manager.",
    ),
    Concept(
        "absolute_return",
        "Absolute return",
        ("absolute return",),
        "Absolute return is the total gain or loss as a percentage of the money invested. "
        "It does not care how long the money was invested, so a return over five years "
        "and one over five months look the same.",
    ),
    Concept(
        "stock_split",
        "Stock split",
        ("stock split", "share split", "split"),
        "In a stock split a company divides each share into more shares with a lower face "
        "value. In a 2 for 1 split, one share becomes two, each priced at about half. The "
        "number of shares changes, but the total value of what you hold does not.",
    ),
    Concept(
        "bonus_issue",
        "Bonus issue",
        ("bonus issue", "bonus shares", "bonus share", "bonus"),
        "A bonus issue gives existing shareholders free extra shares in a fixed ratio, "
        "paid out of the company's reserves. The share price adjusts down to match, so "
        "the total value of your holding stays the same on the day.",
    ),
    Concept(
        "dividend",
        "Dividend",
        ("dividend", "dividends"),
        "A dividend is part of a company's profit paid to shareholders, a fixed amount "
        "per share. You receive it if you hold the shares on the record date. The company "
        "announces the amount and the dates in advance.",
    ),
    Concept(
        "ex_date",
        "Ex-date and record date",
        ("ex date", "record date", "ex dividend date"),
        "The record date is the day a company checks who its shareholders are for a "
        "dividend, bonus or split. The ex-date is the first day on which buying the share "
        "no longer gets you that benefit. In India, with next-day settlement, the two are "
        "usually the same day.",
    ),
    Concept(
        "rights_issue",
        "Rights issue",
        ("rights issue", "rights shares", "rights entitlement", "rights entitlements"),
        "In a rights issue a company offers its existing shareholders new shares, usually "
        "below the market price, in proportion to what they already hold. You can apply "
        "for them, sell the right to someone else, or let it lapse.",
    ),
    Concept(
        "buyback",
        "Buyback",
        ("buyback", "buy back", "share buyback"),
        "In a buyback a company buys its own shares from shareholders, either through a "
        "tender offer at a fixed price or on the open market. Fewer shares remain after "
        "it, so each remaining share is a larger part of the company.",
    ),
    Concept(
        "demerger",
        "Demerger",
        ("demerger", "spin off", "spinoff"),
        "In a demerger a company separates one of its businesses into a new listed "
        "company. Shareholders receive shares of the new company in a set ratio, and the "
        "original cost is divided between the two companies as the company announces.",
    ),
    Concept(
        "isin",
        "ISIN",
        ("isin", "international securities identification number"),
        "An ISIN is a 12 character code that identifies one security. Indian company "
        "shares have ISINs starting with INE. A company can change its name or trading "
        "symbol, but the ISIN of a share usually stays the same.",
    ),
    Concept(
        "demat_account",
        "Demat account",
        ("demat account", "demat", "depository participant"),
        "A demat account holds your shares electronically. It is kept with a depository, "
        "NSDL or CDSL, through a depository participant such as your broker. Shares you "
        "buy land here, and shares you sell leave from here.",
    ),
    Concept(
        "contract_note",
        "Contract note",
        ("contract note", "contract notes"),
        "A contract note is the document your broker sends after each day you trade. It "
        "lists every trade with its price, quantity, charges and taxes, and it is the "
        "legal record of those trades.",
    ),
    Concept(
        "cas",
        "CAS statement",
        ("cas", "consolidated account statement"),
        "A CAS, or Consolidated Account Statement, is sent by NSDL or CDSL. It lists the "
        "shares and mutual fund units you hold across all your demat accounts in one "
        "statement. It shows what you hold, not the prices you paid.",
    ),
    Concept(
        "etf",
        "ETF",
        ("etf", "etfs", "exchange traded fund", "exchange traded funds"),
        "An ETF is a fund that holds a basket of investments, often the shares of an "
        "index, and trades on the stock exchange like a share. You buy and sell it during "
        "market hours at the live price.",
    ),
    Concept(
        "index_fund",
        "Index fund",
        ("index fund", "index funds"),
        "An index fund is a mutual fund that copies an index such as the Nifty 50. You "
        "buy and sell it through the fund house at the end-of-day price, not on the "
        "exchange during the day.",
    ),
    Concept(
        "nifty_50",
        "Nifty 50",
        ("nifty 50", "nifty50", "nifty"),
        "The Nifty 50 is the National Stock Exchange's index of 50 large Indian companies. "
        "Each company's weight depends on the value of its shares available for trading. "
        "It is the most common benchmark for Indian large-company shares.",
    ),
    Concept(
        "sensex",
        "Sensex",
        ("sensex", "bse sensex"),
        "The Sensex is the BSE's index of 30 large, actively traded Indian companies, "
        "weighted by the value of their shares available for trading. It is one of the "
        "oldest measures of the Indian stock market.",
    ),
    Concept(
        "market_cap",
        "Market capitalisation",
        (
            "market cap",
            "market capitalisation",
            "market capitalization",
            "large cap",
            "mid cap",
            "small cap",
        ),
        "Market capitalisation is a company's share price multiplied by its total number "
        "of shares. SEBI groups companies by it: the top 100 are large cap, the next 150 "
        "are mid cap, and the rest are small cap.",
    ),
    Concept(
        "pe_ratio",
        "P/E ratio",
        ("pe ratio", "p/e ratio", "p/e", "price to earnings", "price earnings ratio"),
        "The P/E ratio is the share price divided by the company's yearly earnings per "
        "share. It shows how much investors pay for each rupee of profit. It is most "
        "useful when comparing companies in the same industry.",
    ),
    Concept(
        "rbi",
        "RBI",
        ("rbi", "reserve bank of india", "reserve bank", "repo rate", "monetary policy"),
        "The Reserve Bank of India is India's central bank. It sets the repo rate, the "
        "rate at which it lends to banks, through its Monetary Policy Committee, and it "
        "regulates banks and the money supply.",
        SearchSpec(
            query="Reserve Bank of India press release monetary policy statement",
            domains=("rbi.org.in",),
            publisher="Reserve Bank of India",
            max_age_days=90,
        ),
    ),
    Concept(
        "sebi",
        "SEBI",
        ("sebi", "securities and exchange board of india"),
        "SEBI, the Securities and Exchange Board of India, regulates India's stock "
        "markets, brokers, mutual funds and listed companies. It issues its rules as "
        "circulars and works to protect investors.",
        SearchSpec(
            query="SEBI circular press release for investors and market participants",
            domains=("sebi.gov.in",),
            publisher="SEBI",
            max_age_days=60,
        ),
    ),
    Concept(
        "nse",
        "NSE",
        ("nse", "national stock exchange"),
        "The National Stock Exchange of India is the country's largest stock exchange "
        "by trading volume. It runs the Nifty indices and publishes circulars on "
        "trading rules and listed companies.",
        SearchSpec(
            query="National Stock Exchange of India circular",
            domains=("nseindia.com",),
            publisher="NSE",
            max_age_days=90,
            dates_from_text=True,
        ),
    ),
    Concept(
        "bse",
        "BSE",
        ("bse", "bombay stock exchange"),
        "BSE, formerly the Bombay Stock Exchange, is Asia's oldest stock exchange, "
        "founded in 1875. It runs the Sensex and publishes notices on trading and "
        "listed companies.",
        SearchSpec(
            query="BSE notice circular",
            domains=("bseindia.com",),
            publisher="BSE",
            max_age_days=90,
            dates_from_text=True,
        ),
    ),
    Concept(
        "benchmark",
        "Benchmark",
        ("benchmark", "benchmarks"),
        "A benchmark is an index used as a yardstick, such as the Nifty 50. Comparing a "
        "portfolio's return with its benchmark shows whether it did better or worse than "
        "the market it invests in.",
    ),
)

CONCEPTS: dict[str, Concept] = {c.topic_id: c for c in _CONCEPTS}

#: Official sources: regulators, exchanges, the fund industry body and government. The
#: catalogue's own "latest" searches use only the first four (``_check``).
OFFICIAL_PUBLISHERS: dict[str, str] = {
    "rbi.org.in": "Reserve Bank of India",
    "sebi.gov.in": "SEBI",
    "nseindia.com": "NSE",
    "bseindia.com": "BSE",
    "amfiindia.com": "AMFI",
    "mca.gov.in": "Ministry of Corporate Affairs",
    "incometax.gov.in": "Income Tax Department",
    "incometaxindia.gov.in": "Income Tax Department",
    "pib.gov.in": "Press Information Bureau",
    "irdai.gov.in": "IRDAI",
    "pfrda.org.in": "PFRDA",
    "mospi.gov.in": "Ministry of Statistics",
    "indiabudget.gov.in": "Union Budget",
    "mcxindia.com": "MCX",
    "sec.gov": "US Securities and Exchange Commission",
    "federalreserve.gov": "US Federal Reserve",
}
#: Reputable financial news, owner decision 11 Oct 2026. Shown as "reported by", never as
#: official confirmation. An exact host, not a parent domain: economictimes.indiatimes.com
#: is admitted, other indiatimes.com sites are not.
NEWS_PUBLISHERS: dict[str, str] = {
    "economictimes.indiatimes.com": "The Economic Times",
    "livemint.com": "Mint",
    "business-standard.com": "Business Standard",
    "moneycontrol.com": "Moneycontrol",
    "reuters.com": "Reuters",
    "financialexpress.com": "Financial Express",
    "thehindubusinessline.com": "BusinessLine",
    "cnbctv18.com": "CNBC-TV18",
    "ndtvprofit.com": "NDTV Profit",
    "apnews.com": "Associated Press",
}
PUBLISHERS: dict[str, str] = {**OFFICIAL_PUBLISHERS, **NEWS_PUBLISHERS}
#: Every domain any search may touch. Anything else is refused before sending and
#: re-refused on every returned URL (``sources.admit_url``).
SOURCE_DOMAINS: frozenset[str] = frozenset(PUBLISHERS)
#: The catalogue's own fixed "latest" searches stay on regulators and exchanges.
OFFICIAL_DOMAINS: frozenset[str] = frozenset(
    {"rbi.org.in", "sebi.gov.in", "nseindia.com", "bseindia.com"}
)

#: Shown when nothing else fits. Order is the order the chips appear in.
DEFAULT_SUGGESTIONS: tuple[str, ...] = (
    "xirr",
    "stock_split",
    "bonus_issue",
    "dividend",
    "etf",
    "pe_ratio",
)


def _check() -> None:
    """Fail at import, not at request time, if two topics claim the same phrase: that
    would make resolution depend on dictionary order."""
    seen: dict[str, str] = {}
    for concept in _CONCEPTS:
        for alias in concept.aliases:
            owner = seen.setdefault(alias, concept.topic_id)
            if owner != concept.topic_id:
                raise ValueError(f"alias {alias!r} claimed by {owner} and {concept.topic_id}")
    for concept in _CONCEPTS:
        if concept.search and not set(concept.search.domains) <= OFFICIAL_DOMAINS:
            raise ValueError(f"{concept.topic_id} searches outside the official domains")
    for topic_id in DEFAULT_SUGGESTIONS:
        if topic_id not in CONCEPTS:
            raise ValueError(f"default suggestion {topic_id!r} is not in the catalogue")


_check()
