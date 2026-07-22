from __future__ import annotations

import re

from apps.api.models import CandidateProposal

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class SQLSafetyError(ValueError):
    pass


def require_identifier(value: str, label: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise SQLSafetyError(f"Unsafe {label}: {value!r}")
    return value


def require_relation(value: str) -> str:
    parts = value.split(".")
    if not 1 <= len(parts) <= 3:
        raise SQLSafetyError(f"Unsafe relation: {value!r}")
    return ".".join(require_identifier(part, "relation component") for part in parts)


def require_revenue_alias(value: str) -> str:
    alias = require_identifier(value, "target alias")
    if alias != "revenue":
        raise SQLSafetyError("The v1 remediation renderer only permits target_alias='revenue'")
    return alias


def render_candidate_sql(
    proposal: CandidateProposal, *, relation: str = "payments_raw"
) -> str:
    """Render the only SQL shape the executor accepts.

    Metadata and model output supply identifiers, never executable SQL. This strict
    renderer makes comments, additional statements, functions and DDL impossible.
    """

    source_field = require_identifier(proposal.source_field, "source field")
    target_alias = require_revenue_alias(proposal.target_alias)
    relation = require_relation(relation)
    return (
        "SELECT\n"
        "    payment_id,\n"
        f"    {source_field} AS {target_alias}\n"
        f"FROM {relation}"
    )


def validate_candidate_sql(sql: str, *, relation: str = "payments_raw") -> str:
    relation = require_relation(relation)
    if ";" in sql or "--" in sql or "/*" in sql or "*/" in sql:
        raise SQLSafetyError("Comments and multiple statements are not allowed")
    identifier = r"([A-Za-z_][A-Za-z0-9_]{0,127})"
    pattern = re.compile(
        rf"\A\s*SELECT\s+payment_id\s*,\s*{identifier}\s+AS\s+{identifier}"
        rf"\s+FROM\s+{re.escape(relation)}\s*\Z",
        re.IGNORECASE,
    )
    match = pattern.fullmatch(sql)
    if not match:
        raise SQLSafetyError("Candidate SQL does not match the read-only allowlisted shape")
    # Re-validate the captured identifiers so this independent gate is exactly as
    # strict as render_candidate_sql: the alias must be the pinned 'revenue'.
    require_identifier(match.group(1), "source field")
    require_revenue_alias(match.group(2))
    return sql


def render_dbt_patch(proposal: CandidateProposal, *, existing_sql: str) -> str:
    source_field = require_identifier(proposal.source_field, "source field")
    require_revenue_alias(proposal.target_alias)
    before = '{% set revenue_column = env_var("DATARESCUE_REVENUE_COLUMN", "amount") %}'
    desired = (
        '{% set revenue_column = env_var('
        f'"DATARESCUE_REVENUE_COLUMN", "{source_field}") %}}'
    )
    before_count = existing_sql.count(before)
    desired_count = existing_sql.count(desired)
    if before_count == 0 and desired_count == 1:
        # CI and post-merge verification can evaluate the already-applied,
        # byte-identical model without manufacturing a second change.
        return existing_sql
    if before_count != 1 or desired_count != 0:
        raise SQLSafetyError(
            "The staging model does not contain one unambiguous allowlisted revenue default"
        )
    patched = existing_sql.replace(before, desired, 1)
    if patched == existing_sql:
        raise SQLSafetyError("The staging model patch made no change")
    return patched
