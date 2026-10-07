import threading

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import (
    CorrectionItem,
    ErrataRequest,
    ReReviewRequest,
    create_app,
    logic_create_re_review,
    logic_submit_errata,
)

SAMPLES = [
    {"code": "S01", "true_label": "cat"},
    {"code": "S02", "true_label": "dog"},
    {"code": "S03", "true_label": "bird"},
]
CATEGORIES = ["cat", "dog", "bird"]

R1_LABELS = {"S01": "cat", "S02": "dog", "S03": "bird"}
R2_LABELS = {"S01": "cat", "S02": "dog", "S03": "cat"}


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_batch(client, samples=SAMPLES, categories=CATEGORIES):
    r = client.post(
        "/batches", json={"allowed_categories": categories, "samples": samples}
    )
    assert r.status_code == 201, r.text
    return r.json()


def submit(client, batch_id, token, labels):
    return client.post(
        f"/batches/{batch_id}/submissions",
        json={"labels": labels},
        headers={"X-Reviewer-Token": token},
    )


def adjudicate(client, batch_id, token, decisions):
    return client.post(
        f"/batches/{batch_id}/adjudications",
        json={"decisions": decisions},
        headers={"X-Arbiter-Token": token},
    )


def errata(client, batch_id, token, expected_revision, corrections):
    return client.post(
        f"/batches/{batch_id}/errata",
        json={"expected_revision": expected_revision, "corrections": corrections},
        headers={"X-Arbiter-Token": token},
    )


def seal_batch(client):
    batch = make_batch(client)
    bid = batch["batch_id"]
    submit(client, bid, batch["reviewer1_token"], R1_LABELS)
    submit(client, bid, batch["reviewer2_token"], R2_LABELS)
    r = adjudicate(client, bid, batch["arbiter_token"], {"S03": "bird"})
    assert r.status_code == 201 and r.json()["phase"] == "sealed"
    return batch


def create_re_review(client, source_id, token, revision):
    return client.post(
        f"/batches/{source_id}/re-reviews",
        json={"truth_revision": revision},
        headers={"X-Arbiter-Token": token},
    )


def drive_round(client, batch, r1_labels, r2_labels, decisions=None):
    bid = batch["batch_id"]
    r = submit(client, bid, batch["reviewer1_token"], r1_labels)
    assert r.status_code == 201, r.text
    r = submit(client, bid, batch["reviewer2_token"], r2_labels)
    assert r.status_code == 201, r.text
    if r.json()["phase"] == "adjudicating":
        r = adjudicate(client, bid, batch["arbiter_token"], decisions or {})
        assert r.status_code == 201, r.text
    assert client.get(f"/batches/{bid}").json()["phase"] == "sealed"


# --------------------------------------------------------------------------
# Creation, credentials and validation
# --------------------------------------------------------------------------

def test_create_re_review_snapshots_revision(client):
    batch = seal_batch(client)
    bid, arb = batch["batch_id"], batch["arbiter_token"]
    assert errata(client, bid, arb, 0, [{"code": "S02", "true_label": "cat"}]).status_code == 201

    # revision 0: sealed original truth
    r = create_re_review(client, bid, arb, 0)
    assert r.status_code == 201, r.text
    rr0 = r.json()
    assert rr0["phase"] == "collecting"
    assert rr0["sample_count"] == 3
    assert rr0["source_batch_id"] == bid
    assert rr0["source_truth_revision"] == 0
    for key in ("reviewer1_token", "reviewer2_token", "arbiter_token"):
        assert rr0[key] and isinstance(rr0[key], str)
    fresh = {rr0["reviewer1_token"], rr0["reviewer2_token"], rr0["arbiter_token"]}
    old = {batch["reviewer1_token"], batch["reviewer2_token"], batch["arbiter_token"]}
    assert len(fresh) == 3 and not (fresh & old)

    # revision 1: corrected truth S02=cat
    r = create_re_review(client, bid, arb, 1)
    assert r.status_code == 201
    rr1 = r.json()
    assert rr1["source_truth_revision"] == 1
    assert rr1["batch_id"] != rr0["batch_id"]

    # snapshots can be verified after each is sealed
    drive_round(client, rr0, R1_LABELS, R2_LABELS, {"S03": "bird"})
    body0 = client.get(f"/batches/{rr0['batch_id']}/results").json()
    assert {x["code"]: x["true_label"] for x in body0["results"]} == {
        "S01": "cat", "S02": "dog", "S03": "bird",
    }

    drive_round(client, rr1, R1_LABELS, R2_LABELS, {"S03": "bird"})
    body1 = client.get(f"/batches/{rr1['batch_id']}/results").json()
    assert {x["code"]: x["true_label"] for x in body1["results"]} == {
        "S01": "cat", "S02": "cat", "S03": "bird",
    }


def test_create_re_review_validation(client):
    batch = make_batch(client)
    bid, arb = batch["batch_id"], batch["arbiter_token"]
    corrections = [{"code": "S02", "true_label": "cat"}]

    # source not sealed: collecting
    assert create_re_review(client, bid, arb, 0).status_code == 409
    submit(client, bid, batch["reviewer1_token"], R1_LABELS)
    submit(client, bid, batch["reviewer2_token"], R2_LABELS)
    # adjudicating
    assert create_re_review(client, bid, arb, 0).status_code == 409
    adjudicate(client, bid, arb, {"S03": "bird"})

    # now sealed: revisions 1/2/-1 do not exist yet
    assert create_re_review(client, bid, arb, 1).status_code == 404
    assert create_re_review(client, bid, arb, 2).status_code == 404
    assert create_re_review(client, bid, arb, -1).status_code == 404
    assert errata(client, bid, arb, 0, corrections).status_code == 201
    # 2 is beyond the latest (1)
    assert create_re_review(client, bid, arb, 2).status_code == 404

    # unknown source batch
    assert create_re_review(client, "does-not-exist", arb, 0).status_code == 404

    # auth: missing / bogus / reviewer tokens rejected
    r = client.post(f"/batches/{bid}/re-reviews", json={"truth_revision": 0})
    assert r.status_code == 401
    assert create_re_review(client, bid, "nope", 0).status_code == 401
    assert create_re_review(client, bid, batch["reviewer1_token"], 0).status_code == 401
    assert create_re_review(client, bid, batch["reviewer2_token"], 0).status_code == 401

    # failed attempts left the source errata chain untouched
    assert client.get(f"/batches/{bid}/errata").json()["latest_revision"] == 1


def test_new_credentials_do_not_work_on_source(client):
    batch = seal_batch(client)
    bid = batch["batch_id"]
    rr = create_re_review(client, bid, batch["arbiter_token"], 0).json()

    # new reviewer tokens cannot submit against the source (already sealed,
    # but auth is checked first -> 401, never 409)
    r = submit(client, bid, rr["reviewer1_token"], R1_LABELS)
    assert r.status_code == 401
    r = submit(client, bid, rr["reviewer2_token"], R1_LABELS)
    assert r.status_code == 401
    # new arbiter token cannot drive or errata the source
    r = client.get(
        f"/batches/{bid}/disagreements",
        headers={"X-Arbiter-Token": rr["arbiter_token"]},
    )
    assert r.status_code == 401
    r = errata(client, bid, rr["arbiter_token"], 0, [{"code": "S01", "true_label": "dog"}])
    assert r.status_code == 401
    # old tokens do not work on the re-review either
    r = submit(client, rr["batch_id"], batch["reviewer1_token"], R1_LABELS)
    assert r.status_code == 401
    r = client.get(
        f"/batches/{rr['batch_id']}/disagreements",
        headers={"X-Arbiter-Token": batch["arbiter_token"]},
    )
    assert r.status_code == 401


# --------------------------------------------------------------------------
# Blind isolation before the re-review is sealed
# --------------------------------------------------------------------------

def test_blind_phase_does_not_expose_source(client):
    batch = seal_batch(client)
    bid = batch["batch_id"]
    assert errata(client, bid, batch["arbiter_token"], 0,
                  [{"code": "S02", "true_label": "cat"}]).status_code == 201
    rr = create_re_review(client, bid, batch["arbiter_token"], 1).json()
    rid = rr["batch_id"]

    # plain query: codes + phase only, in the snapshot's sample order
    r = client.get(f"/batches/{rid}")
    assert r.status_code == 200
    assert r.json() == {
        "batch_id": rid, "phase": "collecting", "samples": ["S01", "S02", "S03"],
    }
    # no source id, no revision, no truth, no old-round classifications
    for secret in (bid, "source", "revision", "true_label", "final_label"):
        assert secret not in r.text

    # results and comparison are closed
    assert client.get(f"/batches/{rid}/results").status_code == 409
    assert client.get(f"/batches/{rid}/comparison").status_code == 409

    # one reviewer submits; reviewer queries still expose only own labels
    submit(client, rid, rr["reviewer1_token"], R1_LABELS)
    r = client.get(
        f"/batches/{rid}/submission",
        headers={"X-Reviewer-Token": rr["reviewer1_token"]},
    )
    assert r.status_code == 200 and r.json()["labels"] == R1_LABELS
    assert "true_label" not in r.text and bid not in r.text
    r = client.get(
        f"/batches/{rid}/submission",
        headers={"X-Reviewer-Token": rr["reviewer2_token"]},
    )
    assert r.json()["submitted"] is False

    # the arbiter cannot unblind early
    r = client.get(
        f"/batches/{rid}/disagreements",
        headers={"X-Arbiter-Token": rr["arbiter_token"]},
    )
    assert r.status_code == 409

    # adjudicating phase stays blind as well
    submit(client, rid, rr["reviewer2_token"], R2_LABELS)
    r = client.get(f"/batches/{rid}")
    assert r.json()["phase"] == "adjudicating"
    assert "source" not in r.text and "true_label" not in r.text
    r = client.get(
        f"/batches/{rid}/disagreements",
        headers={"X-Arbiter-Token": rr["arbiter_token"]},
    )
    assert r.status_code == 200
    assert "true_label" not in r.text and bid not in r.text
    assert client.get(f"/batches/{rid}/comparison").status_code == 409


# --------------------------------------------------------------------------
# The re-review reuses the ordinary flow
# --------------------------------------------------------------------------

def test_rereview_full_flow_with_adjudication_and_agreement_seal(client):
    batch = seal_batch(client)
    bid = batch["batch_id"]

    # disagreement path in the re-review
    rr = create_re_review(client, bid, batch["arbiter_token"], 0).json()
    rid = rr["batch_id"]
    r1 = {"S01": "cat", "S02": "dog", "S03": "bird"}
    r2 = {"S01": "dog", "S02": "dog", "S03": "cat"}
    submit(client, rid, rr["reviewer1_token"], r1)
    r = submit(client, rid, rr["reviewer2_token"], r2)
    assert r.json()["phase"] == "adjudicating"
    disagreements = client.get(
        f"/batches/{rid}/disagreements",
        headers={"X-Arbiter-Token": rr["arbiter_token"]},
    ).json()["disagreements"]
    assert {d["code"] for d in disagreements} == {"S01", "S03"}
    # bad adjudication attempts do not seal
    r = adjudicate(client, rid, rr["arbiter_token"], {"S01": "dog"})
    assert r.status_code == 422
    r = adjudicate(client, rid, rr["arbiter_token"],
                   {"S01": "cat", "S03": "dog"})  # dog is neither reviewer's S03 label
    assert r.status_code == 422
    r = adjudicate(client, rid, rr["arbiter_token"],
                   {"S01": "cat", "S03": "cat"})
    assert r.status_code == 201 and r.json()["phase"] == "sealed"
    # adjudications are final
    assert adjudicate(client, rid, rr["arbiter_token"],
                      {"S01": "dog", "S03": "cat"}).status_code == 409
    # duplicate submissions stay rejected
    assert submit(client, rid, rr["reviewer1_token"], r1).status_code == 409

    body = client.get(f"/batches/{rid}/results").json()
    assert body["source_batch_id"] == bid
    assert body["source_truth_revision"] == 0
    finals = {x["code"]: x["final_label"] for x in body["results"]}
    assert finals == {"S01": "cat", "S02": "dog", "S03": "cat"}
    assert client.get(f"/batches/{rid}").json() == body

    # agreement-only re-review seals on the second submission
    rr2 = create_re_review(client, bid, batch["arbiter_token"], 0).json()
    submit(client, rr2["batch_id"], rr2["reviewer1_token"], R1_LABELS)
    r = submit(client, rr2["batch_id"], rr2["reviewer2_token"], dict(R1_LABELS))
    assert r.status_code == 201 and r.json()["phase"] == "sealed"


def test_rereview_accepts_errata_without_changing_snapshot_semantics(client):
    batch = seal_batch(client)
    bid = batch["batch_id"]
    arb = batch["arbiter_token"]
    rr = create_re_review(client, bid, arb, 0).json()
    rid = rr["batch_id"]
    drive_round(client, rr, R1_LABELS, R2_LABELS, {"S03": "bird"})

    # errata on the re-review itself follow the ordinary behavior
    r = errata(client, rid, rr["arbiter_token"], 0, [{"code": "S01", "true_label": "dog"}])
    assert r.status_code == 201 and r.json()["revision"] == 1
    # the source's arbiter cannot errata the re-review
    assert errata(client, rid, arb, 1, [{"code": "S02", "true_label": "cat"}]).status_code == 401
    v1 = client.get(f"/batches/{rid}/errata/1").json()
    assert v1["results"][0]["true_label"] == "dog"
    # plain results stay the snapshot original
    assert client.get(f"/batches/{rid}/results").json()["results"][0]["true_label"] == "cat"
    # the comparison keeps judging against the revision chosen at creation (0),
    # unaffected by the re-review's own erratum
    comp = client.get(f"/batches/{rid}/comparison").json()
    assert comp["source_revision"] == 0
    assert {x["code"]: x["true_label"] for x in comp["results"]}["S01"] == "cat"


# --------------------------------------------------------------------------
# Cross-round comparison
# --------------------------------------------------------------------------

def test_comparison_contents_and_order(client):
    batch = seal_batch(client)
    bid, arb = batch["batch_id"], batch["arbiter_token"]
    # source revision 1: S02 truth dog -> cat
    assert errata(client, bid, arb, 0, [{"code": "S02", "true_label": "cat"}]).status_code == 201

    rr = create_re_review(client, bid, arb, 1).json()
    rid = rr["batch_id"]
    # re-review final labels: S01 cat, S02 cat (now matches corrected truth),
    # S03 cat (still wrong; truth bird)
    new_r1 = {"S01": "cat", "S02": "cat", "S03": "bird"}
    new_r2 = {"S01": "cat", "S02": "cat", "S03": "cat"}
    drive_round(client, rr, new_r1, new_r2, {"S03": "cat"})

    r = client.get(f"/batches/{rid}/comparison")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["batch_id"] == rid
    assert body["source_batch_id"] == bid
    assert body["source_revision"] == 1
    assert [x["code"] for x in body["results"]] == ["S01", "S02", "S03"]

    by = {x["code"]: x for x in body["results"]}
    # source finals come from the sealed original: S02 dog, S03 bird
    assert by["S01"] == {
        "code": "S01", "true_label": "cat",
        "source_final_label": "cat", "rereview_final_label": "cat",
        "changed": False, "source_correct": True, "rereview_correct": True,
    }
    assert by["S02"]["source_final_label"] == "dog"
    assert by["S02"]["rereview_final_label"] == "cat"
    assert by["S02"]["true_label"] == "cat"          # selected revision truth
    assert by["S02"]["changed"] is True
    assert by["S02"]["source_correct"] is False
    assert by["S02"]["rereview_correct"] is True
    assert by["S03"]["source_final_label"] == "bird"  # sealed original, not cat
    assert by["S03"]["rereview_final_label"] == "cat"
    assert by["S03"]["changed"] is True
    assert by["S03"]["source_correct"] is True
    assert by["S03"]["rereview_correct"] is False

    assert body["summary"] == {
        "sample_count": 3,
        "changed_count": 2,
        "source_correct": 2,   # S01, S03 (S02 wrong vs corrected truth cat)
        "rereview_correct": 2,  # S01, S02
    }
    assert body["source_sealed_at"] and body["rereview_sealed_at"]


def test_comparison_requires_sealed_re_review(client):
    batch = seal_batch(client)
    bid, arb = batch["batch_id"], batch["arbiter_token"]
    rr = create_re_review(client, bid, arb, 0).json()
    rid = rr["batch_id"]

    # ordinary source batch has no comparison
    assert client.get(f"/batches/{bid}/comparison").status_code == 404
    # unknown batch
    assert client.get("/batches/does-not-exist/comparison").status_code == 404
    # unsealed re-review
    assert client.get(f"/batches/{rid}/comparison").status_code == 409
    submit(client, rid, rr["reviewer1_token"], R1_LABELS)
    submit(client, rid, rr["reviewer2_token"], R2_LABELS)
    assert client.get(f"/batches/{rid}/comparison").status_code == 409
    adjudicate(client, rid, rr["arbiter_token"], {"S03": "bird"})
    assert client.get(f"/batches/{rid}/comparison").status_code == 200


def test_later_source_errata_do_not_change_snapshot_or_comparison(client):
    batch = seal_batch(client)
    bid, arb = batch["batch_id"], batch["arbiter_token"]
    assert errata(client, bid, arb, 0, [{"code": "S02", "true_label": "cat"}]).status_code == 201

    rr = create_re_review(client, bid, arb, 1).json()
    rid = rr["batch_id"]
    new_r1 = {"S01": "cat", "S02": "cat", "S03": "bird"}
    drive_round(client, rr, new_r1, dict(new_r1))  # agreement -> instant seal

    before = client.get(f"/batches/{rid}/comparison").json()
    results_before = client.get(f"/batches/{rid}/results").json()

    # source keeps evolving after the re-review exists ...
    assert errata(client, bid, arb, 1, [{"code": "S01", "true_label": "dog"}]).status_code == 201
    assert errata(client, bid, arb, 2, [{"code": "S03", "true_label": "cat"}]).status_code == 201

    # ... but the re-review snapshot, results and comparison are frozen
    assert client.get(f"/batches/{rid}/comparison").json() == before
    assert client.get(f"/batches/{rid}/results").json() == results_before
    assert client.get(f"/batches/{rid}/errata/0").json()["results"][0]["true_label"] == "cat"

    # and a new re-review at revision 3 sees the newest truth
    rr3 = create_re_review(client, bid, arb, 3).json()
    drive_round(client, rr3, new_r1, dict(new_r1))
    truth3 = {x["code"]: x["true_label"]
              for x in client.get(f"/batches/{rr3['batch_id']}/results").json()["results"]}
    assert truth3 == {"S01": "dog", "S02": "cat", "S03": "cat"}


def test_source_keeps_original_behavior(client):
    batch = seal_batch(client)
    bid, arb = batch["batch_id"], batch["arbiter_token"]
    original = client.get(f"/batches/{bid}/results").json()

    rr = create_re_review(client, bid, arb, 0).json()
    rid = rr["batch_id"]
    drive_round(client, rr, R1_LABELS, R2_LABELS, {"S03": "bird"})

    # creating and sealing a re-review changes nothing on the source side
    assert client.get(f"/batches/{bid}").json() == original
    assert client.get(f"/batches/{bid}/results").json() == original
    assert client.get(f"/batches/{bid}/errata/0").json()["revision"] == 0
    listing = client.get(f"/batches/{bid}/errata").json()
    assert listing["latest_revision"] == 0
    assert submit(client, bid, batch["reviewer1_token"], R1_LABELS).status_code == 409
    assert adjudicate(client, bid, arb, {"S03": "cat"}).status_code == 409
    # errata still work and start at revision 1
    assert errata(client, bid, arb, 0, [{"code": "S02", "true_label": "cat"}]).status_code == 201


# --------------------------------------------------------------------------
# Atomicity: interleaving with errata, failure rollback and persistence
# --------------------------------------------------------------------------

def test_rereview_atomic_against_concurrent_errata(tmp_path):
    """Every created re-review copies one single revision state entirely."""
    app = create_app(str(tmp_path / "race.db"))
    store = app.state.store
    with TestClient(app) as client:
        batch = seal_batch(client)
        bid, arb = batch["batch_id"], batch["arbiter_token"]

        outcomes = {"review": [], "erratum": []}
        barrier = threading.Barrier(6)

        def make_review():
            barrier.wait()
            try:
                out = logic_create_re_review(
                    store, bid, arb, ReReviewRequest(truth_revision=0)
                )
                outcomes["review"].append(("ok", out["batch_id"]))
            except HTTPException as exc:
                outcomes["review"].append((exc.status_code, None))

        def make_erratum():
            barrier.wait()
            payload = ErrataRequest(
                expected_revision=0,
                corrections=[CorrectionItem(code="S02", true_label="cat")],
            )
            try:
                logic_submit_errata(store, bid, arb, payload)
                outcomes["erratum"].append("ok")
            except HTTPException as exc:
                outcomes["erratum"].append(exc.status_code)

        threads = [threading.Thread(target=make_review) for _ in range(5)]
        threads.append(threading.Thread(target=make_erratum))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # the single erratum either applied or lost the race
        assert outcomes["erratum"] == ["ok"] or outcomes["erratum"] == [409]
        created = [bid_ for status, bid_ in outcomes["review"] if status == "ok"]
        assert all(status in ("ok", 404, 409) for status, _ in outcomes["review"])

        with store.read() as conn:
            for rid in created:
                # every snapshot is internally complete and keeps revision-0
                # truth (S02=dog): a half-copied batch would miss rows or show
                # a mixed truth state
                n = conn.execute(
                    "SELECT COUNT(*) AS c FROM samples WHERE batch_id = ?", (rid,)
                ).fetchone()["c"]
                assert n == 3
                s02 = conn.execute(
                    "SELECT true_label FROM samples WHERE batch_id = ? AND code = 'S02'",
                    (rid,),
                ).fetchone()["true_label"]
                assert s02 == "dog"
                meta = conn.execute(
                    "SELECT source_batch_id, source_revision FROM batches WHERE id = ?",
                    (rid,),
                ).fetchone()
                assert meta["source_batch_id"] == bid
                assert meta["source_revision"] == 0


def test_failed_creation_leaves_no_partial_batch(client):
    batch = seal_batch(client)
    bid, arb = batch["batch_id"], batch["arbiter_token"]
    store = client.app.state.store

    # all three rejections run while other seals/errata exist; none may leave
    # rows in samples or batches
    for bad_revision in (1, 2, -1):
        with pytest.raises(HTTPException):
            logic_create_re_review(
                store, bid, arb, ReReviewRequest(truth_revision=bad_revision)
            )
    with store.read() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM batches WHERE source_batch_id = ?", (bid,)
        ).fetchone()["c"]
        orphan_samples = conn.execute(
            "SELECT COUNT(*) AS c FROM samples s"
            " LEFT JOIN batches b ON b.id = s.batch_id WHERE b.id IS NULL"
        ).fetchone()["c"]
    assert rows == 0
    assert orphan_samples == 0


def test_rereview_persistence_across_restart(tmp_path):
    db = str(tmp_path / "persist.db")

    app1 = create_app(db)
    with TestClient(app1) as client1:
        batch = seal_batch(client1)
        bid, arb = batch["batch_id"], batch["arbiter_token"]
        errata(client1, bid, arb, 0, [{"code": "S02", "true_label": "cat"}])
        rr = create_re_review(client1, bid, arb, 1).json()
        rid = rr["batch_id"]
        new_r1 = {"S01": "cat", "S02": "cat", "S03": "bird"}
        new_r2 = {"S01": "cat", "S02": "cat", "S03": "cat"}
        drive_round(client1, rr, new_r1, new_r2, {"S03": "cat"})
        before = {
            "batch": client1.get(f"/batches/{rid}").json(),
            "results": client1.get(f"/batches/{rid}/results").json(),
            "comparison": client1.get(f"/batches/{rid}/comparison").json(),
            "source_results": client1.get(f"/batches/{bid}/results").json(),
        }

    app2 = create_app(db)
    with TestClient(app2) as client2:
        assert client2.get(f"/batches/{rid}").json() == before["batch"]
        assert client2.get(f"/batches/{rid}/results").json() == before["results"]
        assert client2.get(f"/batches/{rid}/comparison").json() == before["comparison"]
        assert client2.get(f"/batches/{bid}/results").json() == before["source_results"]
        # the snapshot truth is still intact
        truth = {x["code"]: x["true_label"]
                 for x in client2.get(f"/batches/{rid}/results").json()["results"]}
        assert truth == {"S01": "cat", "S02": "cat", "S03": "bird"}
        # and a further source erratum after restart still cannot touch it
        errata(client2, bid, arb, 1, [{"code": "S01", "true_label": "dog"}])
        assert client2.get(f"/batches/{rid}/comparison").json() == before["comparison"]
