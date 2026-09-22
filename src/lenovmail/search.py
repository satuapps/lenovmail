# Lenovmail — authored by satuapps (satuapps.com)
"""SQL full-text search expressions shared by the sync engine and the API.

The `'simple'` text configuration is chosen (instead of a single-language configuration)
because mailbox content is often multilingual: single-language stemming would be wrong
for half the content.

Important note: the `regconfig` parameter **must** be cast explicitly. asyncpg sends
the configuration as `varchar`, and Postgres has no `to_tsvector(varchar, varchar)` —
without the cast, every search index write fails with
`function to_tsvector(character varying, character varying) does not exist`.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, cast, func, literal
from sqlalchemy.dialects.postgresql import REGCONFIG

TEXT_SEARCH_CONFIG = "simple"


def tsvector_expr(text_value: str | ColumnElement[str]) -> ColumnElement:
    """`to_tsvector('simple', <text>)` with the configuration cast to regconfig."""
    source = literal(text_value) if isinstance(text_value, str) else text_value
    return func.to_tsvector(cast(literal(TEXT_SEARCH_CONFIG), REGCONFIG), source)


def tsquery_expr(query: str) -> ColumnElement:
    """`websearch_to_tsquery('simple', <query>)` — web-style search syntax."""
    return func.websearch_to_tsquery(cast(literal(TEXT_SEARCH_CONFIG), REGCONFIG), literal(query))
