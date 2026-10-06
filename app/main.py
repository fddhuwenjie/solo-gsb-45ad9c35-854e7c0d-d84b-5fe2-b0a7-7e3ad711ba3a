"""Blind sample classification review API.

Workflow
--------
1. ``POST /batches`` creates a batch from the allowed categories and the
   samples (code + true label). The response is the *only* place where the
   two reviewer credentials and the arbiter credential are shown.
2. Each reviewer submits a full classification with their own credential
   (``POST /batches/{id}/submissions``). Submissions are final.
3. While the batch is not sealed, plain queries only expose sample codes and
   the phase; a reviewer can only query their own submission status.
4. Once both submissions are in, samples where the reviewers agree take the
   common label. If there are disagreements the batch moves to
   ``adjudicating``: the arbiter lists them (``GET .../disagreements``) and
   picks one of the two labels per sample (``POST .../adjudications``).
   With no disagreements the batch seals immediately on the second
   submission.
5. Sealing unblinds the batch: results expose per-sample final label, true
   label and each reviewer's correct count. Final rows are written once at
   seal time and never change afterwards.
6. If a sample's true label turns out to be misrecorded after sealing, the
   arbiter files an erratum (``POST .../errata``) with the expected revision
   number and the corrected labels. Revision 0 is the sealed original; each
   accepted erratum creates the next revision, replacing only the listed
   samples' truth. ``GET .../errata/{revision}`` keeps the original final
   and reviewer labels but recomputes per-sample correctness and all three
   correct counts against that revision's truth. The plain batch/results
   queries always keep returning the sealed original.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from .db import Store

DEFAULT_DB_PATH = "./data/blindreview.db"


# --------------------------------------------------------------------------
# Payload models
# --------------------------------------------------------------------------

class SampleSpec(BaseModel):
    code: str
    true_label: str


class CreateBatchRequest(BaseModel):
    allowed_categories: list[str]
    samples: list[SampleSpec]


class SubmissionRequest(BaseModel):
    # maps sample code -> assigned label; must cover every sample exactly once
    labels: dict[str, str]


class AdjudicationRequest(BaseModel):
    # maps disagreement sample code -> chosen label (one of the two reviewers')
    decisions: dict[str, str]


class CorrectionItem(BaseModel):
    code: str
    true_label: str


class ErrataRequest(BaseModel):
    # optimistic concurrency: must equal the batch's current revision
    expected_revision: int
    # list (not dict) so duplicate codes in one request can be rejected
    corrections: list[CorrectionItem]


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _get_batch(conn, batch_id: str):
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="batch not found")
    return row


def _reviewer_for_token(batch, token: str):
    digest = _token_hash(token)
    if hmac.compare_digest(digest, batch["reviewer1_token_hash"]):
        return 1
    if hmac.compare_digest(digest, batch["reviewer2_token_hash"]):
        return 2
    return None


def _require_reviewer(batch, token: str | None) -> int:
    if not token:
        raise HTTPException(status_code=401, detail="missing reviewer token")
    reviewer = _reviewer_for_token(batch, token)
    if reviewer is None:
        raise HTTPException(status_code=401, detail="invalid reviewer token")
    return reviewer


def _require_arbiter(batch, token: str | None) -> None:
    if not token:
        raise HTTPException(status_code=401, detail="missing arbiter token")
    if not hmac.compare_digest(_token_hash(token), batch["arbiter_token_hash"]):
        raise HTTPException(status_code=401, detail="invalid arbiter token")


def _disagreement_rows(conn, batch_id: str):
    return conn.execute(
        """
        SELECT s.code AS code, i1.label AS reviewer1_label, i2.label AS reviewer2_label
        FROM samples s
        JOIN submission_items i1
          ON i1.batch_id = s.batch_id AND i1.code = s.code AND i1.reviewer = 1
        JOIN submission_items i2
          ON i2.batch_id = s.batch_id AND i2.code = s.code AND i2.reviewer = 2
        WHERE s.batch_id = ? AND i1.label != i2.label
        ORDER BY s.position
        """,
        (batch_id,),
    ).fetchall()


def _seal(conn, batch_id: str) -> None:
    """Write the immutable final rows and mark the batch sealed.

    Caller must guarantee both submissions exist and every disagreement has
    an adjudication. Runs inside the caller's transaction.
    """
    rows = conn.execute(
        """
        SELECT s.position AS position, s.code AS code, s.true_label AS true_label,
               i1.label AS reviewer1_label, i2.label AS reviewer2_label
        FROM samples s
        JOIN submission_items i1
          ON i1.batch_id = s.batch_id AND i1.code = s.code AND i1.reviewer = 1
        JOIN submission_items i2
          ON i2.batch_id = s.batch_id AND i2.code = s.code AND i2.reviewer = 2
        WHERE s.batch_id = ?
        ORDER BY s.position
        """,
        (batch_id,),
    ).fetchall()
    adjudications = {
        r["code"]: r["final_label"]
        for r in conn.execute(
            "SELECT code, final_label FROM adjudications WHERE batch_id = ?", (batch_id,)
        ).fetchall()
    }
    for r in rows:
        if r["reviewer1_label"] == r["reviewer2_label"]:
            final_label = r["reviewer1_label"]
        else:
            final_label = adjudications[r["code"]]  # invariant: must exist
        conn.execute(
            "INSERT INTO finals (batch_id, position, code, final_label, true_label,"
            " reviewer1_label, reviewer2_label) VALUES (?,?,?,?,?,?,?)",
            (
                batch_id,
                r["position"],
                r["code"],
                final_label,
                r["true_label"],
                r["reviewer1_label"],
                r["reviewer2_label"],
            ),
        )
    conn.execute(
        "UPDATE batches SET phase = 'sealed', sealed_at = ? WHERE id = ?",
        (_now(), batch_id),
    )


def _results_payload(conn, batch) -> dict:
    rows = conn.execute(
        "SELECT code, final_label, true_label, reviewer1_label, reviewer2_label"
        " FROM finals WHERE batch_id = ? ORDER BY position",
        (batch["id"],),
    ).fetchall()
    results = []
    r1_correct = r2_correct = final_correct = 0
    for r in rows:
        ok1 = r["reviewer1_label"] == r["true_label"]
        ok2 = r["reviewer2_label"] == r["true_label"]
        okf = r["final_label"] == r["true_label"]
        r1_correct += ok1
        r2_correct += ok2
        final_correct += okf
        results.append(
            {
                "code": r["code"],
                "final_label": r["final_label"],
                "true_label": r["true_label"],
                "reviewer1_label": r["reviewer1_label"],
                "reviewer2_label": r["reviewer2_label"],
                "agreed": r["reviewer1_label"] == r["reviewer2_label"],
                "reviewer1_correct": ok1,
                "reviewer2_correct": ok2,
            }
        )
    return {
        "batch_id": batch["id"],
        "phase": "sealed",
        "sealed_at": batch["sealed_at"],
        "allowed_categories": json.loads(batch["allowed_categories"]),
        "results": results,
        "summary": {
            "sample_count": len(results),
            "disagreement_count": sum(1 for r in results if not r["agreed"]),
            "reviewer1_correct": r1_correct,
            "reviewer2_correct": r2_correct,
            "final_correct": final_correct,
        },
    }


def _current_revision(conn, batch_id: str) -> int:
    """Latest errata revision of the batch; 0 means the sealed original."""
    row = conn.execute(
        "SELECT MAX(revision) AS r FROM errata WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    return row["r"] if row["r"] is not None else 0


def _truth_at_revision(conn, batch_id: str, revision: int) -> dict[str, str]:
    """True labels as of the given revision (0 = sealed original)."""
    truth = {
        r["code"]: r["true_label"]
        for r in conn.execute(
            "SELECT code, true_label FROM samples WHERE batch_id = ?", (batch_id,)
        ).fetchall()
    }
    for r in conn.execute(
        "SELECT code, new_true_label FROM errata_items"
        " WHERE batch_id = ? AND revision <= ? ORDER BY revision",
        (batch_id, revision),
    ).fetchall():
        truth[r["code"]] = r["new_true_label"]
    return truth


def _errata_items(conn, batch_id: str, revision: int) -> list[dict]:
    """Samples corrected by exactly this revision, in sample order."""
    rows = conn.execute(
        "SELECT e.code AS code, e.old_true_label AS old, e.new_true_label AS new"
        " FROM errata_items e"
        " JOIN samples s ON s.batch_id = e.batch_id AND s.code = e.code"
        " WHERE e.batch_id = ? AND e.revision = ? ORDER BY s.position",
        (batch_id, revision),
    ).fetchall()
    return [
        {"code": r["code"], "previous_true_label": r["old"], "true_label": r["new"]}
        for r in rows
    ]


def _errata_version_payload(conn, batch, revision: int) -> dict:
    """Unblinded view of one errata revision.

    Final and reviewer labels always come from the immutable ``finals``
    table; only the truth (and everything derived from it) reflects the
    requested revision.
    """
    truth = _truth_at_revision(conn, batch["id"], revision)
    rows = conn.execute(
        "SELECT code, final_label, reviewer1_label, reviewer2_label"
        " FROM finals WHERE batch_id = ? ORDER BY position",
        (batch["id"],),
    ).fetchall()
    results = []
    r1_correct = r2_correct = final_correct = 0
    for r in rows:
        true_label = truth[r["code"]]
        ok1 = r["reviewer1_label"] == true_label
        ok2 = r["reviewer2_label"] == true_label
        okf = r["final_label"] == true_label
        r1_correct += ok1
        r2_correct += ok2
        final_correct += okf
        results.append(
            {
                "code": r["code"],
                "final_label": r["final_label"],
                "true_label": true_label,
                "reviewer1_label": r["reviewer1_label"],
                "reviewer2_label": r["reviewer2_label"],
                "agreed": r["reviewer1_label"] == r["reviewer2_label"],
                "reviewer1_correct": ok1,
                "reviewer2_correct": ok2,
                "final_correct": okf,
            }
        )
    if revision == 0:
        created_at = batch["sealed_at"]
        corrected = []
    else:
        row = conn.execute(
            "SELECT created_at FROM errata WHERE batch_id = ? AND revision = ?",
            (batch["id"], revision),
        ).fetchone()
        created_at = row["created_at"]
        corrected = _errata_items(conn, batch["id"], revision)
    return {
        "batch_id": batch["id"],
        "phase": "sealed",
        "sealed_at": batch["sealed_at"],
        "revision": revision,
        "latest_revision": _current_revision(conn, batch["id"]),
        "created_at": created_at,
        "allowed_categories": json.loads(batch["allowed_categories"]),
        "corrected": corrected,
        "results": results,
        "summary": {
            "sample_count": len(results),
            "disagreement_count": sum(1 for r in results if not r["agreed"]),
            "reviewer1_correct": r1_correct,
            "reviewer2_correct": r2_correct,
            "final_correct": final_correct,
        },
    }


# --------------------------------------------------------------------------
# Business logic (kept route-independent so it can be exercised directly,
# e.g. from concurrency tests)
# --------------------------------------------------------------------------

def logic_create_batch(store: Store, payload: CreateBatchRequest) -> dict:
    categories = [c.strip() for c in payload.allowed_categories]
    if not categories or any(not c for c in categories):
        raise HTTPException(422, "allowed_categories must be a non-empty list of non-empty strings")
    if len(set(categories)) != len(categories):
        raise HTTPException(422, "allowed_categories must not contain duplicates")
    if not payload.samples:
        raise HTTPException(422, "samples must not be empty")
    codes = [s.code for s in payload.samples]
    if any(not c.strip() for c in codes):
        raise HTTPException(422, "sample codes must be non-empty")
    if len(set(codes)) != len(codes):
        raise HTTPException(422, "sample codes must be unique")
    for s in payload.samples:
        if s.true_label not in categories:
            raise HTTPException(
                422, f"true_label {s.true_label!r} of sample {s.code!r} is not in allowed_categories"
            )

    batch_id = uuid.uuid4().hex
    reviewer1_token = secrets.token_urlsafe(32)
    reviewer2_token = secrets.token_urlsafe(32)
    arbiter_token = secrets.token_urlsafe(32)
    with store.write() as conn:
        conn.execute(
            "INSERT INTO batches (id, allowed_categories, phase, reviewer1_token_hash,"
            " reviewer2_token_hash, arbiter_token_hash, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                batch_id,
                json.dumps(categories),
                "collecting",
                _token_hash(reviewer1_token),
                _token_hash(reviewer2_token),
                _token_hash(arbiter_token),
                _now(),
            ),
        )
        conn.executemany(
            "INSERT INTO samples (batch_id, position, code, true_label) VALUES (?,?,?,?)",
            [(batch_id, i, s.code, s.true_label) for i, s in enumerate(payload.samples)],
        )
    return {
        "batch_id": batch_id,
        "phase": "collecting",
        "sample_count": len(payload.samples),
        "reviewer1_token": reviewer1_token,
        "reviewer2_token": reviewer2_token,
        "arbiter_token": arbiter_token,
        "notice": "These credentials are shown only in this response and cannot be recovered.",
    }


def logic_get_batch(store: Store, batch_id: str) -> dict:
    with store.read() as conn:
        batch = _get_batch(conn, batch_id)
        if batch["phase"] == "sealed":
            return _results_payload(conn, batch)
        codes = [
            r["code"]
            for r in conn.execute(
                "SELECT code FROM samples WHERE batch_id = ? ORDER BY position", (batch_id,)
            ).fetchall()
        ]
        # Blind phase: expose nothing but the codes and the phase.
        return {"batch_id": batch_id, "phase": batch["phase"], "samples": codes}


def logic_own_submission(store: Store, batch_id: str, token: str | None) -> dict:
    with store.read() as conn:
        batch = _get_batch(conn, batch_id)
        reviewer = _require_reviewer(batch, token)
        sub = conn.execute(
            "SELECT submitted_at FROM submissions WHERE batch_id = ? AND reviewer = ?",
            (batch_id, reviewer),
        ).fetchone()
        if sub is None:
            return {"batch_id": batch_id, "reviewer": reviewer, "submitted": False}
        items = conn.execute(
            "SELECT i.code, i.label FROM submission_items i"
            " JOIN samples s ON s.batch_id = i.batch_id AND s.code = i.code"
            " WHERE i.batch_id = ? AND i.reviewer = ? ORDER BY s.position",
            (batch_id, reviewer),
        ).fetchall()
        # A reviewer only ever sees their own submission, never the truth or
        # the other reviewer's labels.
        return {
            "batch_id": batch_id,
            "reviewer": reviewer,
            "submitted": True,
            "submitted_at": sub["submitted_at"],
            "labels": {i["code"]: i["label"] for i in items},
        }


def logic_submit(store: Store, batch_id: str, token: str | None, labels: dict[str, str]) -> dict:
    with store.write() as conn:
        batch = _get_batch(conn, batch_id)
        reviewer = _require_reviewer(batch, token)
        if batch["phase"] != "collecting":
            raise HTTPException(409, f"batch is {batch['phase']}; submissions are closed")
        existing = conn.execute(
            "SELECT 1 FROM submissions WHERE batch_id = ? AND reviewer = ?",
            (batch_id, reviewer),
        ).fetchone()
        if existing:
            # A submitted review is final: duplicates never overwrite it.
            raise HTTPException(409, "submission already recorded and cannot be modified")

        categories = set(json.loads(batch["allowed_categories"]))
        expected = {
            r["code"]
            for r in conn.execute("SELECT code FROM samples WHERE batch_id = ?", (batch_id,))
        }
        provided = set(labels)
        missing = sorted(expected - provided)
        unknown = sorted(provided - expected)
        if missing or unknown:
            raise HTTPException(
                422,
                {
                    "detail": "labels must cover every sample exactly once",
                    "missing": missing,
                    "unknown": unknown,
                },
            )
        invalid = {c: l for c, l in labels.items() if l not in categories}
        if invalid:
            raise HTTPException(
                422, {"detail": "every label must be one of allowed_categories", "invalid": invalid}
            )

        conn.execute(
            "INSERT INTO submissions (batch_id, reviewer, submitted_at) VALUES (?,?,?)",
            (batch_id, reviewer, _now()),
        )
        conn.executemany(
            "INSERT INTO submission_items (batch_id, reviewer, code, label) VALUES (?,?,?,?)",
            [(batch_id, reviewer, code, label) for code, label in labels.items()],
        )

        submitted = conn.execute(
            "SELECT COUNT(*) AS c FROM submissions WHERE batch_id = ?", (batch_id,)
        ).fetchone()["c"]
        phase = "collecting"
        if submitted == 2:
            if _disagreement_rows(conn, batch_id):
                conn.execute("UPDATE batches SET phase = 'adjudicating' WHERE id = ?", (batch_id,))
                phase = "adjudicating"
            else:
                # No disagreement: the second submission seals the batch at once.
                _seal(conn, batch_id)
                phase = "sealed"
        return {"batch_id": batch_id, "reviewer": reviewer, "status": "recorded", "phase": phase}


def logic_disagreements(store: Store, batch_id: str, token: str | None) -> dict:
    with store.read() as conn:
        batch = _get_batch(conn, batch_id)
        _require_arbiter(batch, token)
        if batch["phase"] == "collecting":
            raise HTTPException(409, "both reviewer submissions are required before adjudication")
        if batch["phase"] == "sealed":
            raise HTTPException(409, "batch is already sealed")
        rows = _disagreement_rows(conn, batch_id)
        return {
            "batch_id": batch_id,
            "phase": "adjudicating",
            "disagreements": [
                {
                    "code": r["code"],
                    "reviewer1_label": r["reviewer1_label"],
                    "reviewer2_label": r["reviewer2_label"],
                }
                for r in rows
            ],
        }


def logic_adjudicate(store: Store, batch_id: str, token: str | None, decisions: dict[str, str]) -> dict:
    with store.write() as conn:
        batch = _get_batch(conn, batch_id)
        _require_arbiter(batch, token)
        if batch["phase"] == "sealed":
            # Adjudications are final: a sealed batch can never be re-decided.
            raise HTTPException(409, "batch is already sealed; adjudications cannot be modified")
        if batch["phase"] == "collecting":
            raise HTTPException(409, "both reviewer submissions are required before adjudication")

        rows = _disagreement_rows(conn, batch_id)
        options = {r["code"]: (r["reviewer1_label"], r["reviewer2_label"]) for r in rows}
        provided = set(decisions)
        missing = sorted(set(options) - provided)
        unknown = sorted(provided - set(options))
        if missing or unknown:
            raise HTTPException(
                422,
                {
                    "detail": "decisions must cover every disagreement exactly once",
                    "missing": missing,
                    "unknown": unknown,
                },
            )
        for code, choice in decisions.items():
            if choice not in options[code]:
                raise HTTPException(
                    422,
                    f"decision for {code!r} must be one of the two reviewers' labels:"
                    f" {sorted(options[code])}",
                )

        conn.executemany(
            "INSERT INTO adjudications (batch_id, code, final_label) VALUES (?,?,?)",
            [(batch_id, code, label) for code, label in decisions.items()],
        )
        _seal(conn, batch_id)
        return {"batch_id": batch_id, "status": "sealed", "phase": "sealed"}


def logic_results(store: Store, batch_id: str) -> dict:
    with store.read() as conn:
        batch = _get_batch(conn, batch_id)
        if batch["phase"] != "sealed":
            raise HTTPException(
                409, f"batch is {batch['phase']}; results are available only after sealing"
            )
        return _results_payload(conn, batch)


def logic_submit_errata(
    store: Store, batch_id: str, token: str | None, payload: ErrataRequest
) -> dict:
    with store.write() as conn:
        batch = _get_batch(conn, batch_id)
        _require_arbiter(batch, token)  # reviewer credentials cannot file errata
        if batch["phase"] != "sealed":
            raise HTTPException(
                409, f"batch is {batch['phase']}; errata are accepted only after sealing"
            )
        current = _current_revision(conn, batch_id)
        if payload.expected_revision != current:
            # Stale base revision: reject and tell the caller where we are.
            raise HTTPException(
                409,
                {
                    "detail": "expected_revision is stale",
                    "current_revision": current,
                },
            )
        if not payload.corrections:
            raise HTTPException(422, "corrections must not be empty")
        codes = [c.code for c in payload.corrections]
        if len(set(codes)) != len(codes):
            raise HTTPException(422, "corrections must not contain duplicate sample codes")
        samples = {
            r["code"]
            for r in conn.execute(
                "SELECT code FROM samples WHERE batch_id = ?", (batch_id,)
            ).fetchall()
        }
        unknown = sorted(set(codes) - samples)
        if unknown:
            raise HTTPException(
                422, {"detail": "corrections must reference samples of this batch",
                      "unknown": unknown}
            )
        categories = set(json.loads(batch["allowed_categories"]))
        invalid = {c.code: c.true_label for c in payload.corrections
                   if c.true_label not in categories}
        if invalid:
            raise HTTPException(
                422, {"detail": "every corrected true_label must be one of allowed_categories",
                      "invalid": invalid}
            )
        truth_now = _truth_at_revision(conn, batch_id, current)
        unchanged = sorted(c.code for c in payload.corrections
                           if c.true_label == truth_now[c.code])
        if unchanged:
            raise HTTPException(
                422, {"detail": "every correction must actually change the current true label",
                      "unchanged": unchanged}
            )

        revision = current + 1
        now = _now()
        conn.execute(
            "INSERT INTO errata (batch_id, revision, created_at) VALUES (?,?,?)",
            (batch_id, revision, now),
        )
        conn.executemany(
            "INSERT INTO errata_items (batch_id, revision, code, old_true_label, new_true_label)"
            " VALUES (?,?,?,?,?)",
            [
                (batch_id, revision, c.code, truth_now[c.code], c.true_label)
                for c in payload.corrections
            ],
        )
        return {
            "batch_id": batch_id,
            "revision": revision,
            "created_at": now,
            "corrected": [
                {
                    "code": c.code,
                    "previous_true_label": truth_now[c.code],
                    "true_label": c.true_label,
                }
                for c in payload.corrections
            ],
        }


def logic_list_errata(store: Store, batch_id: str) -> dict:
    with store.read() as conn:
        batch = _get_batch(conn, batch_id)
        if batch["phase"] != "sealed":
            raise HTTPException(
                409, f"batch is {batch['phase']}; errata are available only after sealing"
            )
        versions = [
            {"revision": 0, "created_at": batch["sealed_at"], "corrected": []}
        ]
        for r in conn.execute(
            "SELECT revision, created_at FROM errata WHERE batch_id = ? ORDER BY revision",
            (batch_id,),
        ).fetchall():
            versions.append(
                {
                    "revision": r["revision"],
                    "created_at": r["created_at"],
                    "corrected": _errata_items(conn, batch_id, r["revision"]),
                }
            )
        return {
            "batch_id": batch_id,
            "latest_revision": len(versions) - 1,
            "versions": versions,
        }


def logic_get_errata(store: Store, batch_id: str, revision: int) -> dict:
    with store.read() as conn:
        batch = _get_batch(conn, batch_id)
        if batch["phase"] != "sealed":
            raise HTTPException(
                409, f"batch is {batch['phase']}; errata are available only after sealing"
            )
        current = _current_revision(conn, batch_id)
        if revision < 0 or revision > current:
            raise HTTPException(
                404, f"revision {revision} not found; latest revision is {current}"
            )
        return _errata_version_payload(conn, batch, revision)


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or os.environ.get("DB_PATH", DEFAULT_DB_PATH)
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    store = Store(path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        store.close()

    app = FastAPI(title="Blind Classification Review API", version="1.0.0", lifespan=lifespan)
    app.state.store = store

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.post("/batches", status_code=201)
    def create_batch(payload: CreateBatchRequest):
        return logic_create_batch(store, payload)

    @app.get("/batches/{batch_id}")
    def get_batch(batch_id: str):
        return logic_get_batch(store, batch_id)

    @app.get("/batches/{batch_id}/submission")
    def get_own_submission(batch_id: str, x_reviewer_token: str | None = Header(default=None)):
        return logic_own_submission(store, batch_id, x_reviewer_token)

    @app.post("/batches/{batch_id}/submissions", status_code=201)
    def submit(
        batch_id: str,
        payload: SubmissionRequest,
        x_reviewer_token: str | None = Header(default=None),
    ):
        return logic_submit(store, batch_id, x_reviewer_token, payload.labels)

    @app.get("/batches/{batch_id}/disagreements")
    def get_disagreements(batch_id: str, x_arbiter_token: str | None = Header(default=None)):
        return logic_disagreements(store, batch_id, x_arbiter_token)

    @app.post("/batches/{batch_id}/adjudications", status_code=201)
    def adjudicate(
        batch_id: str,
        payload: AdjudicationRequest,
        x_arbiter_token: str | None = Header(default=None),
    ):
        return logic_adjudicate(store, batch_id, x_arbiter_token, payload.decisions)

    @app.get("/batches/{batch_id}/results")
    def get_results(batch_id: str):
        return logic_results(store, batch_id)

    @app.post("/batches/{batch_id}/errata", status_code=201)
    def submit_errata(
        batch_id: str,
        payload: ErrataRequest,
        x_arbiter_token: str | None = Header(default=None),
    ):
        return logic_submit_errata(store, batch_id, x_arbiter_token, payload)

    @app.get("/batches/{batch_id}/errata")
    def list_errata(batch_id: str):
        return logic_list_errata(store, batch_id)

    @app.get("/batches/{batch_id}/errata/{revision}")
    def get_errata(batch_id: str, revision: int):
        return logic_get_errata(store, batch_id, revision)

    return app


app = create_app()
