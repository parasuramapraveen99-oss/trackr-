"""Workspace research: pure helpers shared by the research repositories and routers.

Nothing in this package is a financial record or feeds one. It holds the pieces that are
neither SQL nor HTTP -- canonical digests, pagination cursors, the anniversary calendar,
research-document text extraction and the code-owned metric/calendar registry -- so the
repositories (``app/db/workspace.py``, ``app/db/research.py``) and the routers can share
one definition of each rule instead of restating it.

Modules here import no framework and open no connection. The engine-grade pure rules
(thesis versions, trigger evaluation) live in ``app/engine/`` beside the portfolio
equations, under the same no-float lint.
"""
