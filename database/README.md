# Versioned index definitions

database/mappings is the only schema source. Run python -m database.migrate with
explicit ES credentials and ES_INDEX_PREFIX. Handlers/processors never create
indices or mappings. Migration is additive; use a new prefix for incompatible
schema/identity changes and reprocess raw records.

The JSON files in queries are standalone query examples. Average model polarity
is distinct from the API's label-count net sentiment. The production API uses
backend/api/queries.py, which validates date windows, versions and data cohorts.

See ../docs/OPERATIONS.md for migration and rollback. Legacy shell helpers now
delegate to root commands. Broad project-wide automatic deletion is retired.
