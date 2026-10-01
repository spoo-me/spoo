"""Idempotent startup helpers — data migrations + reference-row seeding.

Kept separate from ``repositories.indexes`` (which only does index ops) so
schema changes and data changes can be reverted independently.
"""
