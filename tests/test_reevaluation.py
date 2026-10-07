import threading

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import (
    CorrectionItem,
    ErrataRequest,
    ReevaluationRequest,
    create_app,
    logic_create_reevaluation,
    logic_submit_errata,
)

SAMPLES = [
    {"code": "S01", "true_label": "cat"},
    {"code": "S02", "true_label": "dog"},
    {"code": "S03", "true_label": "bird"},
]
CATEGORIES = ["cat", "dog", "bird"]

# Original round: reviewer1 gets everything right; reviewer2 flips S03 and
# the arbiter upholds reviewer1, so the sealed originals are
# S01=cat, S02=dog, S03=bird.
R1_LABELS = {"S01": "cat", "S02": "dog", "S03": "bird"}
R2_LABELS = {"S01": "cat", "S02": "dog", "S03": "cat"}


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_batch(client):
    r = client.post(
        "/batches", json={"allowed_categories": CATEGORIES, "samples": SAMPLES}
    )
    assert r.status_code == 201, r.text
    return r.json()


def submit(client, batch_id, token, labels):
    return client.post(
        f"/batches/{batch_id}/submissions",
        json={"labels": labels},
        headers={"X-Reviewer-Token": token},
    )


def seal_source(client, batch=None):
    """Create + seal the source batch with one adjudicated disagreement."""
    batch = batch or make_batch(client)
    bid = batch["batch_id"]
    submit(client, bid, batch["reviewer1_token"], R1_LABELS)
    submit(client, bid, batch["reviewer2_token"], R2_LABELS)
    r = client.post(
        f"/batches/{bid}/adjudications",
        json={"decisions": {"S03": "bird"}},
        headers={"X-Arbiter-Token": batch["arbiter_token"]},
    )
    assert r.status_code == 201 and r.json()["phase"] == "sealed"
    return batch


def add_erratum(client, bid, arb, expected, corrections):
    return client.post(
        f"/batches/{bid}/errata",
        json={"expected_revision": expected, "corrections": corrections},
        headers={"X-Arbiter-Token": arb},
    )


def create_reevaluation(client, source_id, token, revision):
    headers = {"X-Arbiter-Token": token} if token is not None else {}
    return client.post(
        f"/batches/{source_id}/reevaluations",
        json={"truth_revision": revision},
        headers=headers,
    )


def run_reevaluation_round(client, reeval, r1_labels, r2_labels, decisions=None):
    """Drive the new round through submit/adjudicate/seal; returns final phase."""
    nid = reeval["batch_id"]
    r = submit(client, nid, reeval["reviewer1_token"], r1_labels)
    assert r.status_code == 201, r.text
    r = submit(client, nid, reeval["reviewer2_token"], r2_labels)
    assert r.status_code == 201, r.text
    phase = r.json()["phase"]
    if phase == "adjudicating":
        r = client.post(
            f"/batches/{nid}/adjudications",
            json={"decisions": decisions},
            headers={"X-Arbiter-Token": reeval["arbiter_token"]},
        )
        assert r.status_code == 201, r.text
        phase = r.json()["phase"]
    assert phase == "sealed"
    return nid


# --------------------------------------------------------------------------
# Creation + snapshot
# --------------------------------------------------------------------------

def test_create_reevaluation_snapshots_named_revision(client):
    source = seal_source(client)
    sid, arb = source["batch_id"], source["arbiter_token"]
    # revision 1 truth: S02 corrected dog -> cat
    r = add_erratum(client, sid, arb, 0, [{"code": "S02", "true_label": "cat"}])
    assert r.status_code == 201

    r = create_reevaluation(client, sid, arb, 1)
    assert r.status_code == 201, r.text
    reeval = r.json()
    assert reeval["phase"] == "collecting"
    assert reeval["sample_count"] == 3
    assert reeval["source_batch_id"] == sid
    assert reeval["truth_revision"] == 1
    # brand new batch with three brand new credentials
    assert reeval["batch_id"] != sid
    for key in ("reviewer1_token", "reviewer2_token", "arbiter_token"):
        assert reeval[key]
    assert len({
        reeval["reviewer1_token"], reeval["reviewer2_token"], reeval["arbiter_token"]
    }) == 3
    assert reeval["arbiter_token"] != arb
    assert reeval["reviewer1_token"] not in (source["reviewer1_token"], source["reviewer2_token"])

    # blind view of the new round: codes only, in sample order
    blind = client.get(f"/batches/{reeval['batch_id']}").json()
    assert blind == {
        "batch_id": reeval["batch_id"],
        "phase": "collecting",
        "samples": ["S01", "S02", "S03"],
    }


def test_snapshot_truth_is_the_chosen_revision_not_the_original(client):
    source = seal_source(client)
    sid, arb = source["batch_id"], source["arbiter_token"]
    add_erratum(client, sid, arb, 0, [{"code": "S02", "true_label": "cat"}])
    reeval = create_reevaluation(client, sid, arb, 1).json()

    # both new reviewers label S02 as cat (matching the corrected truth); no
    # disagreement -> seals immediately
    nid = run_reevaluation_round(
        client, reeval,
        {"S01": "cat", "S02": "cat", "S03": "bird"},
        {"S01": "cat", "S02": "cat", "S03": "bird"},
    )
    body = client.get(f"/batches/{nid}/results").json()
    truth = {row["code"]: row["true_label"] for row in body["results"]}
    assert truth == {"S01": "cat", "S02": "cat", "S03": "bird"}
    assert body["summary"]["reviewer1_correct"] == 3


def test_reevaluation_requires_sealed_source(client):
    batch = make_batch(client)
    sid, arb = batch["batch_id"], batch["arbiter_token"]

    # collecting
    assert create_reevaluation(client, sid, arb, 0).status_code == 409
    submit(client, sid, batch["reviewer1_token"], R1_LABELS)
    submit(client, sid, batch["reviewer2_token"], R2_LABELS)
    # adjudicating
    assert client.get(f"/batches/{sid}").json()["phase"] == "adjudicating"
    assert create_reevaluation(client, sid, arb, 0).status_code == 409
    # nothing was created
    assert client.get(f"/batches/{sid}").json()["phase"] == "adjudicating"


def test_reevaluation_rejects_unknown_revision(client):
    source = seal_source(client)
    sid, arb = source["batch_id"], source["arbiter_token"]

    assert create_reevaluation(client, sid, arb, 99).status_code == 404
    assert create_reevaluation(client, sid, arb, -1).status_code == 404
    # revision 0 (sealed original) always exists
    assert create_reevaluation(client, sid, arb, 0).status_code == 201


def test_reevaluation_on_missing_batch(client):
    r = create_reevaluation(client, "does-not-exist", "nope", 0)
    assert r.status_code == 404


def test_reevaluation_requires_arbiter_of_source(client):
    source = seal_source(client)
    sid = source["batch_id"]
    corrections_headers = [
        {},
        {"X-Arbiter-Token": "nope"},
        {"X-Arbiter-Token": source["reviewer1_token"]},
        {"X-Arbiter-Token": source["reviewer2_token"]},
    ]
    for headers in corrections_headers:
        r = client.post(
            f"/batches/{sid}/reevaluations",
            json={"truth_revision": 0},
            headers=headers,
        )
        assert r.status_code == 401


def test_credentials_are_bound_to_their_own_round(client):
    source = seal_source(client)
    sid = source["batch_id"]
    reeval = create_reevaluation(client, sid, source["arbiter_token"], 0).json()
    nid = reeval["batch_id"]

    # source credentials do not work on the re-evaluation
    r = client.get(
        f"/batches/{nid}/disagreements",
        headers={"X-Arbiter-Token": source["arbiter_token"]},
    )
    assert r.status_code == 401
    r = submit(client, nid, source["reviewer1_token"], R1_LABELS)
    assert r.status_code == 401
    # new credentials do not work on the source
    r = add_erratum(client, sid, reeval["arbiter_token"], 0,
                    [{"code": "S02", "true_label": "cat"}])
    assert r.status_code == 401


# --------------------------------------------------------------------------
# Blind isolation
# --------------------------------------------------------------------------

def test_blind_isolation_until_reevaluation_sealed(client):
    source = seal_source(client)
    sid, arb = source["batch_id"], source["arbiter_token"]
    add_erratum(client, sid, arb, 0, [{"code": "S02", "true_label": "cat"}])
    reeval = create_reevaluation(client, sid, arb, 1).json()
    nid = reeval["batch_id"]

    # plain query: codes + phase only -- no source, no truth, no old labels
    r = client.get(f"/batches/{nid}")
    public = r.json()
    assert set(public) == {"batch_id", "phase", "samples"}
    assert "S02" in r.text and "cat" not in r.text  # codes only, no labels
    assert sid not in r.text

    # reviewer query: own status only
    r = client.get(
        f"/batches/{nid}/submission",
        headers={"X-Reviewer-Token": reeval["reviewer1_token"]},
    )
    assert r.status_code == 200 and r.json()["submitted"] is False
    assert sid not in r.text and "true_label" not in r.text

    # comparison refuses early and reveals nothing while blind
    r = client.get(f"/batches/{nid}/comparison")
    assert r.status_code == 409
    assert sid not in r.text
    assert "S01" not in r.text

    # results still closed
    assert client.get(f"/batches/{nid}/results").status_code == 409

    # even after one submission, nothing leaks
    submit(client, nid, reeval["reviewer1_token"],
           {"S01": "cat", "S02": "cat", "S03": "cat"})
    r = client.get(f"/batches/{nid}/comparison")
    assert r.status_code == 409 and sid not in r.text
    r = client.get(f"/batches/{nid}")
    assert set(r.json()) == {"batch_id", "phase", "samples"}


def test_comparison_endpoint_only_on_reevaluation_batches(client):
    source = seal_source(client)
    # an ordinary sealed batch has no comparison
    assert client.get(f"/batches/{source['batch_id']}/comparison").status_code == 404


# --------------------------------------------------------------------------
# Cross-round comparison
# --------------------------------------------------------------------------

def test_comparison_after_sealing_against_chosen_revision(client):
    source = seal_source(client)
    sid, arb = source["batch_id"], source["arbiter_token"]
    # revision 1: S02 truth dog -> cat; chosen truth is cat/cat/bird
    add_erratum(client, sid, arb, 0, [{"code": "S02", "true_label": "cat"}])
    reeval = create_reevaluation(client, sid, arb, 1).json()

    # new round: reviewers disagree on S03; arbiter picks cat (wrong vs truth)
    nid = run_reevaluation_round(
        client, reeval,
        {"S01": "cat", "S02": "dog", "S03": "cat"},
        {"S01": "cat", "S02": "dog", "S03": "bird"},
        decisions={"S03": "cat"},
    )

    r = client.get(f"/batches/{nid}/comparison")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["batch_id"] == nid
    assert body["source_batch_id"] == sid
    assert body["truth_revision"] == 1
    assert body["phase"] == "sealed"

    assert [row["code"] for row in body["results"]] == ["S01", "S02", "S03"]
    assert body["results"] == [
        {
            "code": "S01",
            "source_final_label": "cat",
            "reevaluation_final_label": "cat",
            "changed": False,
            "true_label": "cat",
            "source_correct": True,
            "reevaluation_correct": True,
        },
        {
            "code": "S02",
            # source classification taken from the sealed original (dog),
            # even though revision 1 corrected the truth to cat
            "source_final_label": "dog",
            "reevaluation_final_label": "dog",
            "changed": False,
            "true_label": "cat",
            "source_correct": False,
            "reevaluation_correct": False,
        },
        {
            "code": "S03",
            "source_final_label": "bird",
            "reevaluation_final_label": "cat",
            "changed": True,
            "true_label": "bird",
            "source_correct": True,
            "reevaluation_correct": False,
        },
    ]
    assert body["summary"] == {
        "sample_count": 3,
        "changed_count": 1,
        "source_correct": 2,
        "reevaluation_correct": 1,
    }


def test_comparison_revision_zero_uses_sealed_original_truth(client):
    source = seal_source(client)
    sid = source["batch_id"]
    reeval = create_reevaluation(client, sid, source["arbiter_token"], 0).json()

    # both new reviewers agree on cat/cat/cat -> immediate seal
    nid = run_reevaluation_round(
        client, reeval,
        {"S01": "cat", "S02": "cat", "S03": "cat"},
        {"S01": "cat", "S02": "cat", "S03": "cat"},
    )
    body = client.get(f"/batches/{nid}/comparison").json()
    assert body["truth_revision"] == 0
    by_code = {row["code"]: row for row in body["results"]}
    assert by_code["S01"] == {
        "code": "S01",
        "source_final_label": "cat",
        "reevaluation_final_label": "cat",
        "changed": False,
        "true_label": "cat",
        "source_correct": True,
        "reevaluation_correct": True,
    }
    assert by_code["S02"]["changed"] is True
    assert by_code["S02"]["true_label"] == "dog"
    assert by_code["S02"]["source_correct"] is True
    assert by_code["S02"]["reevaluation_correct"] is False
    assert by_code["S03"]["changed"] is True
    assert body["summary"] == {
        "sample_count": 3,
        "changed_count": 2,
        "source_correct": 3,
        "reevaluation_correct": 1,
    }


# --------------------------------------------------------------------------
# Snapshot immutability
# --------------------------------------------------------------------------

def test_later_source_errata_do_not_change_snapshot_or_comparison(client):
    source = seal_source(client)
    sid, arb = source["batch_id"], source["arbiter_token"]
    add_erratum(client, sid, arb, 0, [{"code": "S02", "true_label": "cat"}])
    reeval = create_reevaluation(client, sid, arb, 1).json()
    nid = run_reevaluation_round(
        client, reeval,
        {"S01": "cat", "S02": "dog", "S03": "cat"},
        {"S01": "cat", "S02": "dog", "S03": "bird"},
        decisions={"S03": "cat"},
    )
    comparison_before = client.get(f"/batches/{nid}/comparison").json()
    results_before = client.get(f"/batches/{nid}/results").json()

    # source keeps being corrected after the re-evaluation was snapshotted
    r = add_erratum(client, sid, arb, 1, [{"code": "S01", "true_label": "dog"}])
    assert r.status_code == 201 and r.json()["revision"] == 2
    add_erratum(client, sid, arb, 2, [{"code": "S03", "true_label": "cat"}])

    # snapshot, own results and comparison are frozen on revision 1
    assert client.get(f"/batches/{nid}/comparison").json() == comparison_before
    assert client.get(f"/batches/{nid}/results").json() == results_before
    assert client.get(f"/batches/{nid}").json() == results_before
    # and the source keeps its original behaviour/results
    assert client.get(f"/batches/{sid}/errata/0").json()["revision"] == 0
    source_original = client.get(f"/batches/{sid}/results").json()
    assert {row["code"]: row["true_label"] for row in source_original["results"]} == {
        "S01": "cat", "S02": "dog", "S03": "bird",
    }


def test_reevaluation_own_errata_keep_comparison_on_chosen_truth(client):
    source = seal_source(client)
    sid = source["batch_id"]
    reeval = create_reevaluation(client, sid, source["arbiter_token"], 0).json()
    nid = run_reevaluation_round(
        client, reeval,
        {"S01": "cat", "S02": "cat", "S03": "cat"},
        {"S01": "cat", "S02": "cat", "S03": "cat"},
    )
    before = client.get(f"/batches/{nid}/comparison").json()

    # an erratum filed on the re-evaluation itself never moves the comparison:
    # correctness stays evaluated against the chosen (revision 0) truth
    r = client.post(
        f"/batches/{nid}/errata",
        json={"expected_revision": 0,
              "corrections": [{"code": "S01", "true_label": "dog"}]},
        headers={"X-Arbiter-Token": reeval["arbiter_token"]},
    )
    assert r.status_code == 201
    assert client.get(f"/batches/{nid}/comparison").json() == before


def test_source_behaviour_unchanged_after_reevaluation(client):
    source = seal_source(client)
    sid, arb = source["batch_id"], source["arbiter_token"]
    add_erratum(client, sid, arb, 0, [{"code": "S02", "true_label": "cat"}])
    reeval = create_reevaluation(client, sid, arb, 1).json()

    # source errata chain and sealed originals are untouched
    listing = client.get(f"/batches/{sid}/errata").json()
    assert [v["revision"] for v in listing["versions"]] == [0, 1]
    original = client.get(f"/batches/{sid}/results").json()
    assert {row["code"]: row["final_label"] for row in original["results"]} == {
        "S01": "cat", "S02": "dog", "S03": "bird",
    }
    # submissions/adjudications on the source stay closed
    assert submit(client, sid, source["reviewer1_token"], R1_LABELS).status_code == 409
    r = client.post(
        f"/batches/{sid}/adjudications",
        json={"decisions": {"S03": "cat"}},
        headers={"X-Arbiter-Token": arb},
    )
    assert r.status_code == 409
    # the new round is an independent batch with no submissions yet
    own = client.get(
        f"/batches/{reeval['batch_id']}/submission",
        headers={"X-Reviewer-Token": reeval["reviewer1_token"]},
    ).json()
    assert own["submitted"] is False


# --------------------------------------------------------------------------
# Interleaving / atomicity / persistence
# --------------------------------------------------------------------------

def test_failed_creation_leaves_no_partial_batch(tmp_path):
    app = create_app(str(tmp_path / "atomic.db"))
    with TestClient(app) as client:
        source = seal_source(client)
        sid, arb = source["batch_id"], source["arbiter_token"]
        store = app.state.store

        assert create_reevaluation(client, sid, arb, 5).status_code == 404
        with store.read() as conn:
            assert conn.execute("SELECT COUNT(*) AS c FROM batches").fetchone()["c"] == 1
            assert conn.execute("SELECT COUNT(*) AS c FROM samples").fetchone()["c"] == 3
            assert conn.execute("SELECT COUNT(*) AS c FROM reevaluations").fetchone()["c"] == 0


def test_interleaved_creation_and_errata_see_one_consistent_revision(tmp_path):
    """Re-evaluation creation racing an erratum.

    One erratum moves latest 0 -> 1. Eight creations ask for revision 0: they
    must all succeed whether they are serialized before or after the erratum
    commits, and every one of them must snapshot the *complete revision-0*
    state (S02 stays ``dog`` even after the erratum landed). Eight more ask
    for revision 1: those serialized before the commit get 404 (revision does
    not exist yet), those after it succeed and snapshot complete revision-1
    state (S02 = ``cat``). Failures must leave no rows behind.
    """
    app = create_app(str(tmp_path / "race.db"))
    store = app.state.store
    with TestClient(app) as client:
        source = seal_source(client)
        sid, arb = source["batch_id"], source["arbiter_token"]
        erratum_payload = ErrataRequest(
            expected_revision=0,
            corrections=[CorrectionItem(code="S02", true_label="cat")],
        )

        created0, created1, rejected1 = [], [], []
        errata_ok, errata_fail = [], []

        def create_worker(revision, ok_bucket):
            try:
                out = logic_create_reevaluation(
                    store, sid, arb, ReevaluationRequest(truth_revision=revision)
                )
                ok_bucket.append(out["batch_id"])
            except HTTPException as exc:
                if revision == 1:
                    rejected1.append(exc.status_code)
                else:
                    raise

        def errata_worker():
            try:
                logic_submit_errata(store, sid, arb, erratum_payload)
                errata_ok.append(1)
            except HTTPException as exc:
                errata_fail.append(exc.status_code)

        threads = (
            [threading.Thread(target=create_worker, args=(0, created0)) for _ in range(8)]
            + [threading.Thread(target=create_worker, args=(1, created1)) for _ in range(8)]
            + [threading.Thread(target=errata_worker) for _ in range(8)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # exactly one erratum wins; revision-0 creations never fail
        assert len(errata_ok) == 1
        assert errata_fail == [409] * 7
        assert len(created0) == 8
        assert set(rejected1) <= {404}
        assert len(created1) + len(rejected1) == 8

        with store.read() as conn:
            n_batches = conn.execute("SELECT COUNT(*) AS c FROM batches").fetchone()["c"]
            n_links = conn.execute(
                "SELECT COUNT(*) AS c FROM reevaluations"
            ).fetchone()["c"]
            assert n_batches == 1 + len(created0) + len(created1)
            assert n_links == len(created0) + len(created1)
            rows = conn.execute(
                """
                SELECT b.id AS bid, r.truth_revision AS rev, s.code AS code,
                       s.true_label AS truth
                FROM batches b
                JOIN reevaluations r ON r.batch_id = b.id
                JOIN samples s ON s.batch_id = b.id
                WHERE b.id != ?
                ORDER BY b.id, s.position
                """,
                (sid,),
            ).fetchall()
            assert len(rows) == 3 * (len(created0) + len(created1))
            # every successful snapshot is wholly at ONE revision, never a mix
            by_batch = {}
            for row in rows:
                by_batch.setdefault(row["bid"], (row["rev"], {}))[1][row["code"]] = row["truth"]
            assert set(by_batch) == set(created0) | set(created1)
            for bid, (rev, truth) in by_batch.items():
                if bid in set(created0):
                    assert rev == 0
                    assert truth == {"S01": "cat", "S02": "dog", "S03": "bird"}
                else:
                    assert rev == 1
                    assert truth == {"S01": "cat", "S02": "cat", "S03": "bird"}


def test_reevaluation_persistence_across_restart(tmp_path):
    db = str(tmp_path / "persist.db")

    app1 = create_app(db)
    with TestClient(app1) as client1:
        source = seal_source(client1)
        sid, arb = source["batch_id"], source["arbiter_token"]
        add_erratum(client1, sid, arb, 0, [{"code": "S02", "true_label": "cat"}])
        reeval = create_reevaluation(client1, sid, arb, 1).json()
        nid = run_reevaluation_round(
            client1, reeval,
            {"S01": "cat", "S02": "dog", "S03": "cat"},
            {"S01": "cat", "S02": "dog", "S03": "bird"},
            decisions={"S03": "cat"},
        )
        comparison_before = client1.get(f"/batches/{nid}/comparison").json()
        results_before = client1.get(f"/batches/{nid}/results").json()
        blind_source = sid

    # "restart": brand-new app instance on the same database file
    app2 = create_app(db)
    with TestClient(app2) as client2:
        assert client2.get(f"/batches/{nid}/comparison").json() == comparison_before
        assert client2.get(f"/batches/{nid}/results").json() == results_before
        # the source round and its errata chain survive as well
        assert client2.get(f"/batches/{blind_source}/errata/1").status_code == 200
