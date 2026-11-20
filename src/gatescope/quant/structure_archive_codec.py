"""Lossless structure evidence packing; searchable projections are not evidence.

The database stores compressed bytes in BYTEA, never base64 in JSONB. Base64 is
used only by the SQL read adapter. Old journal rows remain readable unchanged.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import zlib

FORMAT = "structure-zlib-1"
MAX_RAW_BYTES = 4 * 1024 * 1024
PROJECT_FIELDS = (
    "id",
    "pattern",
    "phase",
    "sequence",
    "reason",
    "observed_time",
    "first_observed_time",
    "deadline_time",
    "snapshot_at",
    "first_snapshot_at",
    "source_mode",
)
LATEST_FIELDS = ("name", "stage", "evidence_id")


def forecast_comparison(value: dict | None, sequence: int) -> dict | None:
    """Keep comparison dimensions once; full prose/geometry lives in the journal.

    Context/candidate are the stage row's own keys. Never use this summary in
    place of the frozen original forecast when rendering historical evidence.
    """
    if not isinstance(value, dict) or "_evidence" in value:
        return value
    keys = (
        "id",
        "issued_at",
        "deadline_time",
        "basis_end",
        "kind",
        "source",
        "history_count",
        "history_cutoff",
        "status",
        "confirmation_closes",
        "version",
        "guideline_fit",
        "confirmation_basis_time",
    )
    result = {key: copy.deepcopy(value[key]) for key in keys if key in value}
    target = value.get("target")
    result["target"] = (
        {key: target[key] for key in ("price", "lower", "upper", "theory_price", "index") if key in target}
        if isinstance(target, dict)
        else None
    )
    result["confirmation"] = [
        {key: trigger[key] for key in ("side", "price") if key in trigger}
        for trigger in value.get("confirmation", [])
    ]
    result["_evidence"] = {
        "sequence": sequence,
        "path": "first.forecast",
        "sha256": hashlib.sha256(canonical_record(value)).hexdigest(),
    }
    return result if len(canonical_record(result)) < len(canonical_record(value)) else value


def canonical_record(record: dict) -> bytes:
    return json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def structure_projection(record: dict) -> dict:
    result = {key: copy.deepcopy(record[key]) for key in PROJECT_FIELDS if key in record}
    latest = record.get("latest", {})
    result["latest"] = {key: copy.deepcopy(latest[key]) for key in LATEST_FIELDS if key in latest}
    return result


def pack_structure_record(record: dict) -> tuple[dict, bytes | None]:
    if not isinstance(record, dict) or "_archive" in record or "_archive_data" in record:
        raise ValueError("STRUCTURE_ARCHIVE_INPUT_INVALID")
    raw = canonical_record(record)
    # Tiny/exceptionally large legacy-compatible rows need no alternate format.
    if len(raw) < 768 or len(raw) > MAX_RAW_BYTES:
        return record, None
    packed = zlib.compress(raw, level=9)
    header = structure_projection(record)
    header["_archive"] = {"format": FORMAT, "sha256": hashlib.sha256(raw).hexdigest(), "raw_bytes": len(raw)}
    if len(packed) + len(canonical_record(header)) >= len(raw) * 0.85:
        return record, None
    return header, packed


def unpack_structure_record(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("STRUCTURE_ARCHIVE_RECORD_INVALID")
    if "_archive" not in value:
        return value
    try:
        meta = value["_archive"]
        size = meta["raw_bytes"]
        if (
            meta["format"] != FORMAT
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 < size <= MAX_RAW_BYTES
        ):
            raise ValueError()
        encoded = value["_archive_data"]
        if not isinstance(encoded, str) or len(encoded) > MAX_RAW_BYTES * 2:
            raise ValueError()
        packed = base64.b64decode(encoded, validate=True)
        inflater = zlib.decompressobj()
        raw = inflater.decompress(packed, size + 1)
        if len(raw) != size or not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
            raise ValueError()
        if hashlib.sha256(raw).hexdigest() != meta["sha256"]:
            raise ValueError()
        result = json.loads(raw)
        if not isinstance(result, dict) or "_archive" in result or "_archive_data" in result:
            raise ValueError()
        projection = {key: item for key, item in value.items() if key not in ("_archive", "_archive_data")}
        if structure_projection(result) != projection:
            raise ValueError()
        return result
    except (KeyError, TypeError, ValueError, zlib.error, UnicodeDecodeError) as exc:
        raise ValueError("STRUCTURE_ARCHIVE_INTEGRITY_FAILED") from exc
