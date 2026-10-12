"""JWT authentication -- login to this application, not to a broker.

Genuinely new: nothing in the root package had a login layer at all. Kept deliberately
small and separate from ``app/db/`` -- this is the one place in the schema that is not
about an investor's holdings or the compliance trail around them, and it must not grow
into a place other modules reach into directly. Route through ``service.py``.
"""
