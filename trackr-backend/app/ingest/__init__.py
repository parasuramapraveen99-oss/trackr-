"""Layer 1: ingestion. "Get the rows in."

Every adapter here has the same job and the same output contract: turn some source into
``Transaction`` objects, or into raw documents for a parser, and stop. Adapters may
import from ``engine``; nothing in ``engine`` may import from here. That one-way rule is
what makes adding a source in 2027 a matter of writing one file rather than touching the
calculation layer.

See ``docs/ARCHITECTURE.md`` for the dependency rule and ``docs/INGESTION.md`` for the
legal boundary around each source.
"""
