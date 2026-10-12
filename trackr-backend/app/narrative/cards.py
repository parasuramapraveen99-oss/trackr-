"""Definition cards: what a metric MEANS, written by us, chosen by the model.

THIS IS THE OTHER HALF OF THE ANSWER TO "DO WE NEED RAG". The numbers are addressable, so
they need no retriever. The definitions are a small, bounded, hand-maintained corpus, so
they need no retriever either -- a card is keyed by the thing it defines, and a lookup by
key cannot return the wrong neighbour the way a nearest-vector match can.

The model selects a card id. It never writes or paraphrases a definition, because a
paraphrased definition is an unreviewed one: "Sharpe measures return per unit of risk"
and "Sharpe measures how much you are paid for the risk you take" are not the same claim
about the risk-free rate, and only one of them is ours.

Card bodies contain no portfolio figures on purpose. A card explains the measure; the
claim it accompanies supplies this book's reading of it.
"""

from __future__ import annotations

#: Card id -> (title, body). Bodies avoid digits so they need no lexicon entry; where a
#: digit is unavoidable the phrase lives in ``lexicon.py`` and the card names it.
CARDS: dict[str, tuple[str, str]] = {
    "CARD.hhi": (
        "Effective holdings",
        "One divided by the Herfindahl index -- the number of equally sized positions "
        "that would be as concentrated as this book actually is. It measures "
        "concentration by weight alone, before any question of whether the holdings "
        "move together.",
    ),
    "CARD.correlation": (
        "Average pairwise correlation",
        "How closely the holdings move with each other, day to day, on returns rather "
        "than on price levels. Weights are diversification's appearance; this is its "
        "substance. Holdings that all move together are one position wearing several "
        "names, and the weight column cannot show that.",
    ),
    "CARD.sharpe": (
        "Sharpe ratio",
        "Return above the risk-free rate, divided by volatility -- how much the book is "
        "paid for the variability it accepts. The risk-free rate used here is an "
        "assumption, not a sourced figure, and is disclosed as one.",
    ),
    "CARD.beta": (
        "Beta",
        "Covariance with the benchmark divided by the index's variance, computed from "
        "daily returns. Calculated here rather than taken from the fundamentals vendor, "
        "whose beta field is unusable for Indian scrips.",
    ),
    "CARD.var": (
        "Value at risk",
        "The loss a bad-but-not-extreme day would bring, read from the actual history of "
        "daily moves rather than assumed from a normal distribution. It says nothing "
        "about the worst day, only about where the ordinary tail begins.",
    ),
    "CARD.max_drawdown": (
        "Maximum drawdown",
        "The deepest fall from a previous peak over the window. The number that tests "
        "whether a plan survives contact, because it is the loss that had to be sat "
        "through rather than the one that was averaged away.",
    ),
    "CARD.harmonic_pe": (
        "Portfolio P/E",
        "The weighted harmonic mean of the holdings' P/Es -- equivalently, weight the "
        "earnings yields and invert. An arithmetic mean is dominated by whichever "
        "holding is most expensive. Loss-makers have no meaningful P/E, so they are "
        "excluded and the excluded weight is disclosed rather than hidden inside the "
        "average.",
    ),
    "CARD.factor_percentile": (
        "Factor score",
        "A percentile against a named universe, not an absolute rating. A score says "
        "where this book sits among those companies on that characteristic; it makes no "
        "claim that the characteristic is desirable.",
    ),
    "CARD.capital_state": (
        "Capital state",
        "Each holding's profit or loss set against its recent price trend, which sorts "
        "the book into in-profit-and-rising, in-profit-and-falling, in-loss-and-falling "
        "and in-loss-and-rising. A description of where capital currently sits, not a "
        "view on what to do about it.",
    ),
    "CARD.coverage": (
        "Coverage",
        "The share of the book, by weight, that a figure could actually be computed for. "
        "A figure covering part of the book is reported with its coverage rather than "
        "presented as though it covered all of it.",
    ),
    "CARD.grandfathering": (
        "Grandfathered cost",
        "For equity bought before the long-term capital gains regime began, the cost of "
        "acquisition is stepped up to the higher of actual cost and the market value on "
        "the cut-off date, capped at the sale price. It reduces taxable gain; it does "
        "not change what was paid.",
    ),
}


class UnknownCard(Exception):
    """A card id that does not exist. Fail closed, same as the lexicon."""


def card(card_id: str) -> tuple[str, str]:
    try:
        return CARDS[card_id]
    except KeyError:
        raise UnknownCard(
            f"{card_id!r} is not a definition card. Definitions are written here and "
            "selected by id; the model never authors one."
        ) from None
