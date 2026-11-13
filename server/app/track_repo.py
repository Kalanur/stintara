"""PostgreSQL persistence of closed track geometry. Pure validation lives in trackmap; this module only does I/O.

PostgreSQL is the durable source, TrackStore stays the hot cache. A geometry row is canonical and immutable once
written (first valid closed lap wins); only the optional sector data may be refreshed afterwards.
"""
import json
import logging

from .trackmap import GEOMETRY_SCHEMA_VERSION, geometry_record, validate_record
from .uuid7 import uuid7

logger = logging.getLogger("stintara.trackmap")

_COLUMNS = ("id, schema_version, length_m, points, distances, sector_observations, sector_boundaries_m, updated_at")


def _json(value):
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def row_to_raw(row):
    """Decode a stored row into the shape validate_record expects. Undecodable JSON yields an invalid record."""
    try:
        return dict(schema_version=row["schema_version"], length_m=row["length_m"], points=_json(row["points"]),
                    distances=_json(row["distances"]), sector_observations=_json(row["sector_observations"]))
    except (TypeError, ValueError):
        return {}


async def _fetch(conn, key):
    return await conn.fetchrow(f"""SELECT {_COLUMNS} FROM track_geometries
        WHERE provider=$1 AND track_name=$2 AND track_length_key=$3""", *key)


async def load_geometry(conn, key):
    """Return a validated record, or None when absent / corrupt / of an unknown schema version."""
    row = await _fetch(conn, key)
    if row is None:
        return None
    record = validate_record(row_to_raw(row), key)
    if record is None:
        logger.warning(json.dumps({"event": "track_geometry_rejected", "provider": key[0],
                                   "schema_version": row["schema_version"]}))
    return record


SAVED, EXISTING, INCOMPATIBLE, INVALID, RETRY = "saved", "existing", "incompatible", "invalid", "retry"


async def save_geometry(conn, key, model):
    """Persist a closed model. Returns (outcome, record); record is the canonical geometry for SAVED / EXISTING.

    - SAVED: this model's geometry is now the canonical row.
    - EXISTING: another valid row won; the caller adopts `record`.
    - INCOMPATIBLE: a row of another (newer) schema version exists. Stable: it is never overwritten or downgraded.
    - INVALID: the model itself would not survive reload validation. Stable: it is never written.
    - RETRY: transient (row vanished or was changed concurrently); trying again later may succeed.

    Concurrency: INSERT .. ON CONFLICT DO NOTHING makes the first writer win. A corrupt row of the current schema
    version is replaced optimistically (by updated_at).
    """
    record = geometry_record(model)
    if record is None or validate_record(json.loads(json.dumps(record)), key) is None:
        return INVALID, None
    provider, name, length_key = key
    inserted = await conn.fetchval("""INSERT INTO track_geometries(id, provider, track_name, track_length_key,
            schema_version, length_m, points, distances, sector_observations, sector_boundaries_m)
        VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb,$9::jsonb,$10::jsonb)
        ON CONFLICT (provider, track_name, track_length_key) DO NOTHING RETURNING id""",
        uuid7(), provider, name, length_key, GEOMETRY_SCHEMA_VERSION, record["length_m"],
        json.dumps(record["points"]), json.dumps(record["distances"]),
        json.dumps(record["sector_observations"]), json.dumps(record["sector_boundaries_m"]))
    if inserted is not None:
        return SAVED, record
    row = await _fetch(conn, key)
    if row is None:
        return RETRY, None
    existing = validate_record(row_to_raw(row), key)
    if existing is not None:
        return EXISTING, existing
    if row["schema_version"] != GEOMETRY_SCHEMA_VERSION:
        return INCOMPATIBLE, None
    replaced = await conn.fetchval("""UPDATE track_geometries SET length_m=$3, points=$4::jsonb, distances=$5::jsonb,
            sector_observations=$6::jsonb, sector_boundaries_m=$7::jsonb, learned_at=now(), updated_at=now()
        WHERE id=$1 AND updated_at=$2 RETURNING id""", row["id"], row["updated_at"], record["length_m"],
        json.dumps(record["points"]), json.dumps(record["distances"]),
        json.dumps(record["sector_observations"]), json.dumps(record["sector_boundaries_m"]))
    return (SAVED, record) if replaced is not None else (RETRY, None)


async def save_sectors(conn, key, model):
    """Refresh only the sector data of an existing current-version row; geometry is never touched here."""
    record = geometry_record(model)
    if record is None:
        return False
    updated = await conn.fetchval("""UPDATE track_geometries SET sector_observations=$4::jsonb,
            sector_boundaries_m=$5::jsonb, updated_at=now()
        WHERE provider=$1 AND track_name=$2 AND track_length_key=$3 AND schema_version=$6 RETURNING id""",
        *key, json.dumps(record["sector_observations"]), json.dumps(record["sector_boundaries_m"]),
        GEOMETRY_SCHEMA_VERSION)
    return updated is not None
