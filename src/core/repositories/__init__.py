"""Repositories — cohesive per-entity DB access on top of the shared SqliteEngine.

Each repository takes an engine (providing `_connect`) and owns one table's operations,
keeping Database a thin composition root instead of a 1800-line god object.
"""
