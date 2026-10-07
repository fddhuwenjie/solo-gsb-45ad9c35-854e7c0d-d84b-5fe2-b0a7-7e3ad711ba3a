#!/usr/bin/env bash
# End-to-end demo of the blind classification review workflow.
# Requires: curl and python3. Usage: ./scripts/demo.sh [base_url]
set -euo pipefail

BASE="${1:-http://localhost:8000}"

json_get() { python3 -c "import sys, json; d = json.load(sys.stdin); print($1)"; }

echo "== 1. create batch (credentials appear only in this response) =="
CREATE=$(curl -sS -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "allowed_categories": ["cat", "dog", "bird"],
  "samples": [
    {"code": "S01", "true_label": "cat"},
    {"code": "S02", "true_label": "dog"},
    {"code": "S03", "true_label": "bird"}
  ]
}')
echo "$CREATE" | python3 -m json.tool
BID=$(json_get "d['batch_id']" <<<"$CREATE")
R1=$(json_get "d['reviewer1_token']" <<<"$CREATE")
R2=$(json_get "d['reviewer2_token']" <<<"$CREATE")
ARB=$(json_get "d['arbiter_token']" <<<"$CREATE")

echo "== 2. public view: only codes and phase =="
curl -sS "$BASE/batches/$BID" | python3 -m json.tool

echo "== 3. reviewer 1 submits =="
curl -sS -X POST "$BASE/batches/$BID/submissions" \
  -H "X-Reviewer-Token: $R1" -H 'Content-Type: application/json' \
  -d '{"labels": {"S01": "cat", "S02": "dog", "S03": "bird"}}' | python3 -m json.tool

echo "== 4. reviewer 2 submits (disagrees on S03) -> adjudicating =="
curl -sS -X POST "$BASE/batches/$BID/submissions" \
  -H "X-Reviewer-Token: $R2" -H 'Content-Type: application/json' \
  -d '{"labels": {"S01": "cat", "S02": "dog", "S03": "cat"}}' | python3 -m json.tool

echo "== 5. arbiter lists disagreements =="
curl -sS "$BASE/batches/$BID/disagreements" -H "X-Arbiter-Token: $ARB" | python3 -m json.tool

echo "== 6. arbiter adjudicates -> sealed =="
curl -sS -X POST "$BASE/batches/$BID/adjudications" \
  -H "X-Arbiter-Token: $ARB" -H 'Content-Type: application/json' \
  -d '{"decisions": {"S03": "bird"}}' | python3 -m json.tool

echo "== 7. unblinded results =="
curl -sS "$BASE/batches/$BID/results" | python3 -m json.tool

echo "== 8. errata: S02 truth corrected dog -> cat (expected_revision 0) =="
curl -sS -X POST "$BASE/batches/$BID/errata" \
  -H "X-Arbiter-Token: $ARB" -H 'Content-Type: application/json' \
  -d '{"expected_revision": 0, "corrections": [{"code": "S02", "true_label": "cat"}]}' | python3 -m json.tool

echo "== 9. stale expected_revision is rejected with the current revision =="
curl -sS -X POST "$BASE/batches/$BID/errata" \
  -H "X-Arbiter-Token: $ARB" -H 'Content-Type: application/json' \
  -d '{"expected_revision": 0, "corrections": [{"code": "S03", "true_label": "dog"}]}' | python3 -m json.tool

echo "== 10. version chain =="
curl -sS "$BASE/batches/$BID/errata" | python3 -m json.tool

echo "== 11. versioned view: revision 1 recomputes correctness against the new truth =="
curl -sS "$BASE/batches/$BID/errata/1" | python3 -m json.tool

echo "== 12. plain results still return the sealed original (revision 0) =="
curl -sS "$BASE/batches/$BID/results" | python3 -m json.tool

echo "== 13. arbiter opens an independent re-review at truth revision 1 =="
CREATE2=$(curl -sS -X POST "$BASE/batches/$BID/re-reviews" \
  -H "X-Arbiter-Token: $ARB" -H 'Content-Type: application/json' \
  -d '{"truth_revision": 1}')
echo "$CREATE2" | python3 -m json.tool
RR=$(json_get "d['batch_id']" <<<"$CREATE2")
RR1=$(json_get "d['reviewer1_token']" <<<"$CREATE2")
RR2=$(json_get "d['reviewer2_token']" <<<"$CREATE2")
RRARB=$(json_get "d['arbiter_token']" <<<"$CREATE2")

echo "== 14. the re-review is blind: only codes and phase, no source or truth =="
curl -sS "$BASE/batches/$RR" | python3 -m json.tool
# comparison is closed until the re-review is itself sealed
curl -sS -o /dev/null -w "comparison while collecting -> %{http_code}\n" "$BASE/batches/$RR/comparison"

echo "== 15. both new reviewers submit (they agree -> seals immediately) =="
# reviewer labels use the corrected truth: S02 is cat; S03 stays bird
curl -sS -X POST "$BASE/batches/$RR/submissions" \
  -H "X-Reviewer-Token: $RR1" -H 'Content-Type: application/json' \
  -d '{"labels": {"S01": "cat", "S02": "cat", "S03": "bird"}}' | python3 -m json.tool
curl -sS -X POST "$BASE/batches/$RR/submissions" \
  -H "X-Reviewer-Token: $RR2" -H 'Content-Type: application/json' \
  -d '{"labels": {"S01": "cat", "S02": "cat", "S03": "bird"}}' | python3 -m json.tool

echo "== 16. cross-round comparison (both rounds judged against revision 1 truth) =="
curl -sS "$BASE/batches/$RR/comparison" | python3 -m json.tool

echo "== 17. a new source erratum cannot change the re-review snapshot =="
curl -sS -X POST "$BASE/batches/$BID/errata" \
  -H "X-Arbiter-Token: $ARB" -H 'Content-Type: application/json' \
  -d '{"expected_revision": 1, "corrections": [{"code": "S01", "true_label": "dog"}]}' \
  | python3 -m json.tool
curl -sS "$BASE/batches/$RR/comparison" | python3 -m json.tool

echo "== 18. guards: unsealed source / missing revision / wrong credential =="
curl -sS -o /dev/null -w "re-review at missing revision 9 -> %{http_code}\n" \
  -X POST "$BASE/batches/$BID/re-reviews" \
  -H "X-Arbiter-Token: $ARB" -H 'Content-Type: application/json' \
  -d '{"truth_revision": 9}'
curl -sS -o /dev/null -w "re-review with a reviewer token -> %{http_code}\n" \
  -X POST "$BASE/batches/$BID/re-reviews" \
  -H "X-Reviewer-Token: $R1" -H 'Content-Type: application/json' \
  -d '{"truth_revision": 0}'
