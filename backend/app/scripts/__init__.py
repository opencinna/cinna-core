"""Operational scripts (data backfills, maintenance tasks).

These are standalone modules invoked via
``docker compose exec backend python -m app.scripts.<module>``. Each one
is idempotent and safe to rerun. Schema-only migrations live in
``app/alembic/versions/``; this directory hosts data-only operations
that don't fit cleanly into Alembic (e.g. backfills that call AI
functions or talk to external services).
"""
