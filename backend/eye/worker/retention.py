"""The checked retention transition (Package 5): one coordinator, off by default.

Retention prunes the raw evidence **bytes** of one partition (one source,
one layer, one UTC day of capture batches by requested start). Rows are
never deleted: each ``eye.raw_evidence`` row keeps its id, checksum, size
and media type and records the decision that pruned it. Observations,
receipts, event claims, media items, coverage and derivations are untouched,
and no code path here or in the database deletes them.

A partition is eligible only when every check passes:

1. its lateness window has closed (day end + ``lateness_hours``);
2. it holds no pending batch, and every batch's stored bytes still match
   their checksum;
3. every required derivation of every day its batches touch has a current
   manifest that re-derives exactly from current evidence (``valid``);
4. the most recent backup verification did not fail, and the latest
   verified synthetic proof holds every batch's exact bytes and every
   current manifest; and
5. that backup copy, re-read just before pruning, is present and intact.

A check that fails blocks the partition. A check that cannot run (database
error, unreachable backup directory) is UNVERIFIED and also blocks it.

``plan`` evaluates read-only and records nothing. ``execute`` is refused
unless deletion is explicitly enabled in configuration (default off), the
process is in demo mode and the partition's source is synthetic. It locks
the tables a late write or recomputation would touch, re-runs every check
inside that lock, records the decision (refusals included) and prunes only
when the recheck passed. The database independently refuses any pruning
that lacks a matching decision and verified proof in the same transaction
(migration 0007).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pg8000.exceptions import DatabaseError, InterfaceError

from eye.ingest import capture
from eye.ingest.capture import iso
from eye.storage.db import Connection, transaction
from eye.worker import backup, rollups
from eye.worker.rollups import ManifestCheck, Partition

LOCKED_TABLES = (
    "eye.capture_batch",
    "eye.raw_evidence",
    "eye.derivation_run",
    "eye.derivation_manifest",
    "eye.backup_proof",
)


class RetentionRefused(RuntimeError):
    """Execution is not permitted in this configuration or for this partition."""


@dataclass(frozen=True)
class Batch:
    batch_id: str
    status: str
    sha256: str
    pruned: bool
    intact: bool | None  # None once pruned


@dataclass
class Verdict:
    partition: Partition
    verdict: str  # eligible | blocked | unverified
    reasons: list[str] = field(default_factory=list)
    batches: list[Batch] = field(default_factory=list)
    checks: list[ManifestCheck] = field(default_factory=list)
    proof_id: str | None = None
    evaluated_at: datetime | None = None

    @property
    def batch_ids(self) -> list[str]:
        return [b.batch_id for b in self.batches if not b.pruned]

    @property
    def manifest_ids(self) -> list[str]:
        return sorted({c.manifest_id for c in self.checks if c.manifest_id})

    def as_dict(self) -> dict:
        return {
            "partition": self.partition.key,
            "verdict": self.verdict,
            "reasons": self.reasons,
            "batches": len(self.batches),
            "unpruned_batches": len(self.batch_ids),
            "manifests": [
                {"check": c.label, "state": c.state, "reason": c.reason} for c in self.checks
            ],
            "backup_proof_id": self.proof_id,
        }


def _batches(conn: Connection, partition: Partition) -> list[Batch]:
    return [
        Batch(bid, status, sha, content_null, intact)
        for bid, status, sha, content_null, intact in conn.run(
            "SELECT b.batch_id::text, b.status::text, e.sha256, e.content IS NULL, "
            "CASE WHEN e.content IS NULL THEN NULL "
            "     ELSE encode(sha256(e.content), 'hex') = e.sha256 END "
            "FROM eye.capture_batch b JOIN eye.raw_evidence e USING (batch_id) "
            "WHERE b.source_id = :s AND b.layer = :l "
            "AND (b.requested_start AT TIME ZONE 'UTC')::date = :d ORDER BY b.batch_id",
            s=partition.source_id,
            l=partition.layer,
            d=partition.day,
        )
    ]


def _covering_proof(conn: Connection, batches: list[Batch], manifest_ids: list[str]):
    """Latest verified proof holding these exact bytes and manifests, or a reason."""
    latest = conn.run(
        "SELECT state FROM eye.backup_proof ORDER BY verified_at DESC, proof_id DESC LIMIT 1"
    )
    if not latest:
        return None, "no backup has been verified"
    if latest[0][0] != "verified":
        return None, "the most recent backup verification failed"
    wanted = {(b.batch_id, b.sha256) for b in batches}
    for proof_id, generation_id, batch_ids, shas, manifests in conn.run(
        "SELECT proof_id::text, generation_id, batch_ids::text[], evidence_sha256s, "
        "manifest_ids::text[] FROM eye.backup_proof WHERE state = 'verified' AND synthetic "
        "ORDER BY verified_at DESC, proof_id DESC LIMIT 20"
    ):
        held = set(zip(batch_ids, shas, strict=True))
        if wanted <= held and set(manifest_ids) <= set(manifests):
            return (proof_id, generation_id), None
    return None, (
        "no verified backup proof holds every batch's exact bytes and every current "
        "manifest of this partition"
    )


def evaluate(
    conn: Connection,
    partition: Partition,
    *,
    now: datetime,
    lateness_hours: int,
    lines: list,
    backup_dir: Path | None,
    in_transaction: bool = False,
) -> Verdict:
    """Every retention check for one partition. Reads only."""
    verdict = Verdict(partition, "blocked", evaluated_at=now)
    blocked: list[str] = []
    unverified: list[str] = []
    try:
        verdict.batches = _batches(conn, partition)
        if not verdict.batches:
            blocked.append("no capture batches in this partition")
        elif not verdict.batch_ids:
            blocked.append("already pruned; nothing to prune")
        deadline = partition.end + timedelta(hours=lateness_hours)
        if now < deadline:
            blocked.append(f"lateness window open until {iso(deadline)}")
        pending = [b.batch_id for b in verdict.batches if b.status == "pending"]
        if pending:
            blocked.append(f"{len(pending)} batch(es) still pending, e.g. {pending[0]}")
        for b in verdict.batches:
            if b.intact is False:
                blocked.append(f"batch {b.batch_id}: stored bytes do not match their checksum")
        for day in rollups.days_touched(conn, partition):
            part = Partition(partition.source_id, partition.layer, day)
            verdict.checks += rollups.validate(
                conn, part, lines, own_snapshot=not in_transaction, ledger=day == partition.day
            )
        for c in verdict.checks:
            if c.state == "unverified":
                unverified.append(f"{c.label}: UNVERIFIED: {c.reason}")
            elif c.state != "valid":
                blocked.append(f"{c.label}: {c.state}: {c.reason}")
        live = [b for b in verdict.batches if not b.pruned]
        found, why = _covering_proof(conn, live, verdict.manifest_ids)
        if found is None:
            blocked.append(why)
        else:
            verdict.proof_id, generation_id = found
            if backup_dir is None:
                unverified.append(
                    "UNVERIFIED: no backup directory is configured, so the copy cannot be re-read"
                )
            else:
                try:
                    problems = backup.recheck(
                        backup_dir, generation_id, [(b.batch_id, b.sha256) for b in live]
                    )
                except (OSError, ValueError, backup.BackupError) as exc:
                    unverified.append(f"UNVERIFIED: backup copy could not be re-read ({exc})")
                else:
                    blocked += problems
    except (DatabaseError, InterfaceError) as exc:
        unverified.append(f"UNVERIFIED: a check could not run ({type(exc).__name__})")
    verdict.reasons = blocked + unverified
    verdict.verdict = "blocked" if blocked else "unverified" if unverified else "eligible"
    return verdict


def plan(conn, *, now=None, lateness_hours, lines, backup_dir) -> list[Verdict]:
    """Evaluate every partition. Read-only: nothing is recorded or changed."""
    now = now or datetime.now(UTC)
    return [
        evaluate(
            conn, p, now=now, lateness_hours=lateness_hours, lines=lines, backup_dir=backup_dir
        )
        for p in rollups.partitions(conn)
    ]


@dataclass(frozen=True)
class Execution:
    decision_id: str
    verdict: Verdict
    pruned: int

    def as_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "pruned_batches": self.pruned,
            **self.verdict.as_dict(),
        }


def execute(
    conn: Connection,
    partition: Partition,
    *,
    allow_deletion: bool,
    mode: str,
    lateness_hours: int,
    lines: list,
    backup_dir: Path | None,
    now: datetime | None = None,
) -> Execution:
    """Lock, recheck and prune one explicitly selected synthetic partition."""
    if not allow_deletion:
        raise RetentionRefused(
            "deletion is disabled (retention.allow_deletion = false); nothing was changed"
        )
    if mode != "demo":
        raise RetentionRefused("this package prunes synthetic demo data only, never production")
    if (
        not partition.source_id.startswith("synthetic-")
        or partition.source_id not in capture.APPROVED_SOURCES
    ):
        raise RetentionRefused(f"{partition.source_id} is not an approved synthetic source")
    with transaction(conn):
        conn.run(f"LOCK TABLE {', '.join(LOCKED_TABLES)} IN SHARE ROW EXCLUSIVE MODE")
        verdict = evaluate(
            conn,
            partition,
            now=now or datetime.now(UTC),
            lateness_hours=lateness_hours,
            lines=lines,
            backup_dir=backup_dir,
            in_transaction=True,
        )
        rollups.record_checks(conn, verdict.checks)
        eligible = verdict.verdict == "eligible"
        decision_id = conn.run(
            "INSERT INTO eye.retention_decision (source_id, layer, partition_day, verdict, "
            "reasons, batch_ids, manifest_ids, backup_proof_id, evaluated_at, lateness_hours) "
            "VALUES (:s, :l, :d, :v, CAST(:r AS jsonb), CAST(:b AS uuid[]), CAST(:m AS uuid[]), "
            "CAST(:p AS uuid), :at, :late) RETURNING decision_id::text",
            s=partition.source_id,
            l=partition.layer,
            d=partition.day,
            v="pruned" if eligible else verdict.verdict,
            r=_json([] if eligible else verdict.reasons),
            b=verdict.batch_ids,
            m=verdict.manifest_ids,
            p=verdict.proof_id,
            at=verdict.evaluated_at,
            late=lateness_hours,
        )[0][0]
        pruned = 0
        if eligible:
            pruned = len(
                conn.run(
                    "UPDATE eye.raw_evidence SET content = NULL, pruned_by_decision = :d "
                    "WHERE batch_id = ANY(CAST(:b AS uuid[])) AND content IS NOT NULL "
                    "RETURNING batch_id",
                    d=decision_id,
                    b=verdict.batch_ids,
                )
            )
            if pruned != len(verdict.batch_ids):
                raise RetentionRefused("pruned a different number of batches than checked")
    return Execution(decision_id, verdict, pruned)


def _json(value) -> str:
    import json

    return json.dumps(value)
