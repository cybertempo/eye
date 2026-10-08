"""Synthetic backup, independent verification and restore drill (Package 5).

SYNTHETIC CODE TEST ONLY. The single backup target here is
``synthetic-directory``: a disposable local directory. It exercises the
code path a real destination will use (content-addressed evidence copies, a
generation manifest, verification by an independent read-back, restore
into a clean database and comparison of named metrics). It does not prove
an off-site Google Drive backup, the private installation, restic, or any
recovery point or restore-time objective; those belong to the private
integration (Package 8).

Store layout (all files written atomically, never edited in place):

* ``evidence/<sha256>.json``: the exact raw bytes of one capture batch.
* ``lines/<sha256>.json``: one count-line definition file.
* ``generations/<generation_id>.json``: one generation manifest (canonical
  JSON; its id is the SHA-256 of these bytes). It lists every settled batch
  with its checksum, the line files, the current derivation manifests, the
  transit intervals and the named metrics a restore must reproduce.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from eye.ingest import capture
from eye.storage.db import Connection, transaction
from eye.worker import rollups, transits

TARGET_CLASS = "synthetic-directory"
FORMAT = "eye.synthetic-backup-generation/1"
VERIFIER_VERSION = "synthetic-backup-verifier/1"
LABEL = (
    "SYNTHETIC CODE TEST ONLY: this exercises EYE's backup, verification and restore code "
    "on invented data in a disposable directory. It does not prove an off-site Google Drive "
    "backup, a private installation or any recovery objective."
)
SHA256 = frozenset("0123456789abcdef")


class BackupError(RuntimeError):
    """A backup generation cannot be written or read."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(doc) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()


def _write_once(path: Path, data: bytes) -> None:
    """Write a content-addressed file atomically; an existing copy must match."""
    if path.exists():
        if _sha(path.read_bytes()) != path.stem:
            raise BackupError(f"{path.parent.name}/{path.name}: existing copy is corrupt")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _blob(directory: Path, kind: str, sha: str) -> Path:
    if len(sha) != 64 or not set(sha) <= SHA256:
        raise BackupError(f"invalid checksum {sha!r}")
    return directory / kind / f"{sha}.json"


def named_metrics(conn: Connection) -> dict[str, list]:
    """The metrics a restore must reproduce, by stable name."""
    metrics: dict[str, list] = {}
    for line_id, version, start, end, state, inbound, outbound, total in conn.run(
        "SELECT line_id, line_version, eye.iso_utc(interval_start), eye.iso_utc(interval_end), "
        "state::text, inbound, outbound, total FROM eye.transit_count"
    ):
        metrics[f"transit/{line_id}/v{version}/{start}/{end}"] = [state, inbound, outbound, total]
    for source, layer, day, derivation, scope, key, metric, state, value in conn.run(
        "SELECT source_id, layer, day::text, derivation, scope, row_key, metric, state::text, "
        "value FROM eye.current_rollup"
    ):
        name = f"rollup/{source}/{layer}/{day}/{derivation}/{scope}/{key}"
        metrics[name] = [metric, state, rollups.num(value)]
    return dict(sorted(metrics.items()))


def _current_manifests(conn: Connection) -> list[dict]:
    return [
        {
            "manifest_id": mid,
            "source_id": source,
            "layer": layer,
            "day": day,
            "derivation": derivation,
            "derivation_version": version,
            "scope": scope,
            "input_sha256": isha,
            "output_sha256": osha,
            "output_row_count": n,
        }
        for mid, source, layer, day, derivation, version, scope, isha, osha, n in conn.run(
            "SELECT manifest_id::text, source_id, layer, day::text, derivation, "
            "derivation_version, scope, input_sha256, output_sha256, output_row_count "
            "FROM eye.current_manifest ORDER BY source_id, layer, day, derivation, scope"
        )
    ]


def export(conn: Connection, directory: Path, lines_dir: Path | None) -> str:
    """Write one generation of every settled batch's evidence. Returns its id."""
    rows = conn.run(
        "SELECT b.batch_id::text, b.source_id, b.layer, b.status::text, e.sha256, e.byte_size, "
        "e.content FROM eye.capture_batch b JOIN eye.raw_evidence e USING (batch_id) "
        "WHERE b.status <> 'pending' ORDER BY b.batch_id"
    )
    batches = []
    for batch_id, source, layer, status, sha, size, content in rows:
        if not source.startswith("synthetic-") or source not in capture.APPROVED_SOURCES:
            raise BackupError(f"batch {batch_id}: only synthetic sources are backed up here")
        path = _blob(directory, "evidence", sha)
        if content is None:  # pruned: its bytes must already be in this store
            if not path.exists() or _sha(path.read_bytes()) != sha:
                raise BackupError(f"batch {batch_id}: pruned evidence is not in this store")
        else:
            data = bytes(content)
            if _sha(data) != sha:
                raise BackupError(f"batch {batch_id}: stored bytes do not match their checksum")
            _write_once(path, data)
        batches.append(
            {
                "batch_id": batch_id,
                "source_id": source,
                "layer": layer,
                "status": status,
                "sha256": sha,
                "byte_size": size,
            }
        )
    line_files = []
    for path in sorted(lines_dir.glob("*.json")) if lines_dir else []:
        data = path.read_bytes()
        line = transits.load_line(path)
        _write_once(_blob(directory, "lines", _sha(data)), data)
        line_files.append({"line_id": line.line_id, "version": line.version, "sha256": _sha(data)})
    intervals = {}
    for entry in line_files:
        found = conn.run(
            "SELECT count_intervals FROM eye.derivation_run WHERE line_id = :l "
            "AND line_version = :v AND algorithm_version = :a "
            "ORDER BY derived_at DESC, run_id DESC LIMIT 1",
            l=entry["line_id"],
            v=entry["version"],
            a=transits.ALGORITHM_VERSION,
        )
        if found:
            value = found[0][0]
            intervals[f"line:{entry['line_id']}/v{entry['version']}"] = (
                json.loads(value) if isinstance(value, str) else value
            )
    body = _canonical(
        {
            "format": FORMAT,
            "synthetic": True,
            "label": LABEL,
            "target_class": TARGET_CLASS,
            "batches": batches,
            "lines": line_files,
            "manifests": _current_manifests(conn),
            "transit_intervals": intervals,
            "metrics": named_metrics(conn),
        }
    )
    generation_id = _sha(body)
    _write_once(_blob(directory, "generations", generation_id), body)
    return generation_id


def read_generation(directory: Path, generation_id: str) -> dict:
    path = _blob(directory, "generations", generation_id)
    data = path.read_bytes()  # OSError when missing or unreadable
    if _sha(data) != generation_id:
        raise BackupError("generation file does not match its id (damaged)")
    doc = json.loads(data)
    if doc.get("format") != FORMAT or doc.get("synthetic") is not True:
        raise BackupError("not a synthetic EYE backup generation")
    return doc


def _check_blob(directory: Path, sha: str, size: int | None = None) -> str | None:
    path = _blob(directory, "evidence", sha)
    if not path.is_file():
        return f"evidence {sha[:12]}: missing from the backup"
    data = path.read_bytes()
    if _sha(data) != sha or (size is not None and len(data) != size):
        return f"evidence {sha[:12]}: backup copy is corrupt"
    return None


@dataclass(frozen=True)
class Proof:
    proof_id: str
    state: str
    reason: str | None
    generation_id: str


def verify(conn: Connection, directory: Path, generation_id: str) -> Proof:
    """Read the generation back from disk and record a proof (verified or failed).

    Independent of ``export``: it trusts nothing the writer computed in
    memory, re-hashes every file and compares each checksum with the
    database's own record of that batch's evidence.
    """
    problems: list[str] = []
    doc: dict = {}
    generation_sha = None
    try:
        doc = read_generation(directory, generation_id)
        generation_sha = generation_id
    except (OSError, ValueError, BackupError) as exc:
        problems.append(
            f"generation unreadable: {exc}"
            if not isinstance(exc, OSError)
            else "generation file is missing or unreadable"
        )
    batches = doc.get("batches", [])
    stored = (
        {
            bid: sha
            for bid, sha in conn.run(
                "SELECT batch_id::text, sha256 FROM eye.raw_evidence WHERE batch_id = "
                "ANY(CAST(:ids AS uuid[]))",
                ids=[b["batch_id"] for b in batches],
            )
        }
        if batches
        else {}
    )
    for b in batches:
        if stored.get(b["batch_id"]) != b["sha256"]:
            problems.append(f"batch {b['batch_id']}: not in the database with this checksum")
            continue
        try:
            problem = _check_blob(directory, b["sha256"], b["byte_size"])
        except OSError:
            problem = f"evidence {b['sha256'][:12]}: unreadable"
        if problem:
            problems.append(problem)
    for entry in doc.get("lines", []):
        path = _blob(directory, "lines", entry["sha256"])
        if not path.is_file() or _sha(path.read_bytes()) != entry["sha256"]:
            problems.append(f"line {entry['line_id']}: backup copy missing or corrupt")
    manifests = doc.get("manifests", [])
    known = (
        {
            mid: osha
            for mid, osha in conn.run(
                "SELECT manifest_id::text, output_sha256 FROM eye.derivation_manifest WHERE "
                "manifest_id = ANY(CAST(:ids AS uuid[]))",
                ids=[m["manifest_id"] for m in manifests],
            )
        }
        if manifests
        else {}
    )
    for m in manifests:
        if known.get(m["manifest_id"]) != m["output_sha256"]:
            problems.append(f"manifest {m['manifest_id']}: not recorded with these outputs")
    state = "failed" if problems else "verified"
    reason = "; ".join(problems)[:500] if problems else None
    with transaction(conn):
        proof_id = conn.run(
            "INSERT INTO eye.backup_proof (target_class, synthetic, generation_id, "
            "generation_sha256, batch_ids, evidence_sha256s, manifest_ids, state, reason, "
            "verifier_version) VALUES (:t, true, :g, :gs, CAST(:b AS uuid[]), CAST(:e AS text[]), "
            "CAST(:m AS uuid[]), :st, :r, :v) RETURNING proof_id::text",
            t=TARGET_CLASS,
            g=generation_id,
            gs=generation_sha,
            b=[x["batch_id"] for x in batches],
            e=[x["sha256"] for x in batches],
            m=[x["manifest_id"] for x in manifests],
            st=state,
            r=reason,
            v=VERIFIER_VERSION,
        )[0][0]
    return Proof(proof_id, state, reason, generation_id)


def recheck(directory: Path, generation_id: str, evidence: list[tuple[str, str]]) -> list[str]:
    """Re-read named batches' copies just before pruning. Raises OSError if unreachable."""
    if not directory.is_dir():
        raise OSError("backup directory is not reachable")
    doc = read_generation(directory, generation_id)
    listed = {b["batch_id"]: b for b in doc["batches"]}
    problems = []
    for batch_id, sha in evidence:
        entry = listed.get(batch_id)
        if entry is None or entry["sha256"] != sha:
            problems.append(f"batch {batch_id}: not in backup generation {generation_id[:12]}")
            continue
        problem = _check_blob(directory, sha, entry["byte_size"])
        if problem:
            problems.append(f"batch {batch_id}: {problem}")
    return problems


@dataclass
class DrillReport:
    state: str  # verified | failed
    problems: list[str] = field(default_factory=list)
    batches_restored: int = 0
    metrics_compared: int = 0
    manifests_compared: int = 0
    label: str = LABEL

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "state": self.state,
            "problems": self.problems,
            "batches_restored": self.batches_restored,
            "metrics_compared": self.metrics_compared,
            "manifests_compared": self.manifests_compared,
        }


def restore_drill(
    directory: Path,
    generation_id: str,
    target: Connection,
    lateness_hours: int = rollups.DEFAULT_LATENESS_HOURS,
) -> DrillReport:
    """Restore one generation into an empty database and reproduce its named metrics.

    The target must hold no EYE schema. Nothing is read from the database the
    backup came from: evidence, lines and expectations all come from the
    backup store, and restored rows come from the ordinary archive and commit
    path. Any missing or damaged copy, refused batch, replay discrepancy or
    differing metric makes the report ``failed``; only a complete, exact
    reproduction is ``verified``.
    """
    from eye.ingest.capture import CaptureRejected, ingest, verify_replay
    from eye.storage.migrate import migrate

    report = DrillReport(state="failed")
    if target.run("SELECT to_regnamespace('eye') IS NOT NULL")[0][0]:
        report.problems.append("target database is not empty; a drill restores into a clean one")
        return report
    try:
        doc = read_generation(directory, generation_id)
    except OSError:
        report.problems.append("generation file is missing or unreadable (incomplete backup)")
        return report
    except (ValueError, BackupError) as exc:
        report.problems.append(f"generation unreadable: {exc}")
        return report
    migrate(target)
    for entry in doc["batches"]:
        path = _blob(directory, "evidence", entry["sha256"])
        if not path.is_file():
            report.problems.append(f"batch {entry['batch_id']}: evidence missing (incomplete)")
            continue
        data = path.read_bytes()
        if _sha(data) != entry["sha256"]:
            report.problems.append(f"batch {entry['batch_id']}: evidence damaged")
            continue
        try:
            result = ingest(target, data)
        except CaptureRejected as exc:
            report.problems.append(f"batch {entry['batch_id']}: refused on restore ({exc})")
            continue
        if result.batch_id != entry["batch_id"] or result.status != entry["status"]:
            report.problems.append(
                f"batch {entry['batch_id']}: restored as {result.batch_id} {result.status}"
            )
            continue
        report.batches_restored += 1
    if report.problems:
        return report
    lines = []
    with tempfile.TemporaryDirectory(prefix="eye-drill-lines-") as tmp:
        for entry in doc["lines"]:
            source = _blob(directory, "lines", entry["sha256"])
            if not source.is_file() or _sha(source.read_bytes()) != entry["sha256"]:
                report.problems.append(f"line {entry['line_id']}: missing or damaged")
                continue
            copy = Path(tmp) / f"{entry['sha256']}.json"
            copy.write_bytes(source.read_bytes())
            lines.append(transits.load_line(copy))
    if report.problems:
        return report
    for line in lines:
        wanted = doc["transit_intervals"].get(rollups.line_scope(line))
        if wanted is None:
            continue
        intervals = [(_parse(a), _parse(b)) for a, b in wanted]
        transits.store(target, transits.SOURCE_ID, line, intervals)
    by_partition: dict[rollups.Partition, list[tuple[str, str]]] = {}
    for m in doc["manifests"]:
        part = rollups.Partition(m["source_id"], m["layer"], _day(m["day"]))
        by_partition.setdefault(part, []).append((m["derivation"], m["scope"]))
    for part, only in sorted(by_partition.items()):
        for r in rollups.refresh(target, part, lines, lateness_hours, only=only):
            if r.error:
                report.problems.append(f"{r.derivation} {part.key}: {r.error}")
    report.problems += [f"replay: {p}" for p in verify_replay(target)[:20]]
    restored_ids = {m["manifest_id"] for m in _current_manifests(target)}
    for m in doc["manifests"]:
        report.manifests_compared += 1
        if m["manifest_id"] not in restored_ids:
            report.problems.append(f"manifest {m['manifest_id']}: not reproduced")
    restored = named_metrics(target)
    for name, value in doc["metrics"].items():
        report.metrics_compared += 1
        if restored.get(name) != value:
            report.problems.append(
                f"metric {name}: restored {restored.get(name)!r}, backed up {value!r}"
            )
    if not doc["metrics"]:
        report.problems.append("the generation names no metrics; nothing was reproduced")
    report.state = "failed" if report.problems else "verified"
    return report


def _parse(stamp: str):
    from datetime import datetime

    return datetime.fromisoformat(stamp)


def _day(text: str):
    from datetime import date

    return date.fromisoformat(text)
