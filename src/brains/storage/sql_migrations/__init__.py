# Disk-discovered schema migrations.
#
# Numbered files (``NNN_description.sql`` or ``NNN_description.py``) in this
# directory are applied in lexical order by
# ``brains.storage.migrations._apply_disk_migrations``. Each successfully
# applied migration is recorded in the ``schema_versions`` table using the
# filename stem as the version string.
#
# Conventions:
#   * Use a 3-digit prefix (``010_``, ``020_``, ...) so there's room to insert.
#   * Make every migration idempotent — check ``PRAGMA table_info`` before
#     ``ALTER TABLE``, use ``CREATE TABLE IF NOT EXISTS``, guard inserts with
#     ``ON CONFLICT DO NOTHING`` or explicit existence checks.
#   * Python migrations expose ``def upgrade(conn): ...`` taking a raw
#     ``sqlite3.Connection``.
