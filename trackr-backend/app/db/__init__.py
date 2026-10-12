"""Persistence and compliance scaffolding.

May import ``engine``; ``engine`` must never import this. The engine stays pure functions
over plain data, which is what keeps the fixtures runnable with no infrastructure and the
stack swappable. ``make lint`` enforces the direction.
"""
