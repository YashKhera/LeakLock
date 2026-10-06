"""DynamoDB table definitions for LeakLock.

Used by tests (via moto) now and by deployment scripts later. Key design
(``docs/00-master-spec.md`` section 6.5 is not in the repo yet, so this
follows the ingest task text)::

    tanks    PK tankId (S)                        tank config + latest fields
    readings PK tankId (S), SK ts (N)            one item per (tank, second);
                                                 duplicate puts overwrite.
                                                 TTL attribute: expiresAt.
    alerts   PK tankId (S), SK sk (S)            alert records plus cooldown
                                                 markers at SK COOLDOWN#<type>.
                                                 TTL attribute: expiresAt.
    daily    PK tankId (S), SK date (S)          per-day aggregates (Phase 3).

All tables use on-demand billing. Every table name is ``prefix + suffix``.
"""

from typing import Any

TABLE_SUFFIXES = {
    "tanks": "tanks",
    "readings": "readings",
    "alerts": "alerts",
    "daily": "daily",
}


def table_name(prefix: str, key: str) -> str:
    """Full table name for a logical table key (e.g. ``leaklock-`` + ``tanks``)."""
    return f"{prefix}{TABLE_SUFFIXES[key]}"


def create_tables(dynamodb_resource: Any, prefix: str = "leaklock-") -> dict:
    """Create the four LeakLock tables and enable TTL where needed.

    Returns ``{"tanks": Table, "readings": Table, "alerts": Table,
    "daily": Table}``.
    """
    tables = {}
    specs = {
        "tanks": (
            [{"AttributeName": "tankId", "KeyType": "HASH"}],
            [{"AttributeName": "tankId", "AttributeType": "S"}],
        ),
        "readings": (
            [
                {"AttributeName": "tankId", "KeyType": "HASH"},
                {"AttributeName": "ts", "KeyType": "RANGE"},
            ],
            [
                {"AttributeName": "tankId", "AttributeType": "S"},
                {"AttributeName": "ts", "AttributeType": "N"},
            ],
        ),
        "alerts": (
            [
                {"AttributeName": "tankId", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            [
                {"AttributeName": "tankId", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
        ),
        "daily": (
            [
                {"AttributeName": "tankId", "KeyType": "HASH"},
                {"AttributeName": "date", "KeyType": "RANGE"},
            ],
            [
                {"AttributeName": "tankId", "AttributeType": "S"},
                {"AttributeName": "date", "AttributeType": "S"},
            ],
        ),
    }
    for key, (key_schema, attr_defs) in specs.items():
        tables[key] = dynamodb_resource.create_table(
            TableName=table_name(prefix, key),
            KeySchema=key_schema,
            AttributeDefinitions=attr_defs,
            BillingMode="PAY_PER_REQUEST",
        )

    client = dynamodb_resource.meta.client
    for key in ("readings", "alerts"):
        client.update_time_to_live(
            TableName=table_name(prefix, key),
            TimeToLiveSpecification={"Enabled": True, "AttributeName": "expiresAt"},
        )
    return tables
