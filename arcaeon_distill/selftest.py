"""Self-test: golden distill-digest vectors + the planted-inconsistency fixture.

    python -m arcaeon_distill.selftest

Ships in the package rather than living only in CI, so a stranger runs it on
THEIR machine and trusts their own output, not ours:

1. Golden vectors. Digests are only a reliable "prove what got cut" signal if
   every environment computes the same one from the same content, at the same
   package version, under the same budget. These constants were frozen at the
   v1 schema freeze (0.1.0, 2026-08-14). If your Python, your platform, or a
   future release computes anything else here, this command fails loudly —
   do not trust receipts it produces, and do not silently widen the vectors.

2. Determinism. The whole engineering point of this package (see the module
   docstring) is that the same input at the same budget produces the same
   output every time — because a distiller that drifts call to call
   invalidates the provider's prompt cache, per arXiv 2607.12161. Run the
   same distill() several times here and require byte-identical results.

3. The planted inconsistency. The honesty hook's whole claim is "you can
   prove what got cut." So this plants exactly the failure verify_receipt
   exists to catch: a receipt whose `truncated` flag disagrees with whether
   it actually recorded any drops. It must fail. An honest receipt over the
   same content must pass.

Exit code 0 = every check passed.
"""
from __future__ import annotations

import json
import sys

from . import DropReceipt, SCHEMA, distill, verify_receipt

# Frozen fixture. Covers all three value-drop kinds in one shot: a long
# string (string_truncated) and a long list (list_truncated).
FIXTURE = {
    "title": "incident report",
    "body": "x" * 600,
    "tags": [f"tag{i}" for i in range(30)],
}
FIXTURE_BUDGET = 100

# Frozen at schema freeze (arcaeon-distill:receipt:v1, 0.1.0, 2026-08-14).
# These never change. A v2 schema gets NEW vectors alongside these.
GOLDEN_FULL_DIGEST = ("sha256:json-c14n:v1:"
    "d565a246f12b3c947b244a539b7f2e17878ffd05f6d0371ee939c225f56fc321")
GOLDEN_DISTILLED_DIGEST = ("sha256:json-c14n:v1:"
    "89a6b3cf5ff08e158220e87d5e34406be43650be7812878c5ce365f124c7ca3c")
GOLDEN_BODY_DROP_DIGEST = ("sha256:raw-bytes:v1:"
    "72883c91aace3aac769a5ba3713fbcb81bfda764b47f2e0e9ce672c9fcec603b")
GOLDEN_TAGS_DROP_DIGEST = ("sha256:json-c14n:v1:"
    "b454c7ae4a5a6fa0cb3d19fe1ad66a44a8386e2c9320600a4f93736b7b19dc0b")


def run() -> int:
    failures = 0

    print("== golden vectors (schema + digest enforcement) ==")
    result = distill(FIXTURE, budget=FIXTURE_BUDGET)
    # distill() is called here with its default receipt=True (never passed
    # False below), so .receipt is guaranteed non-None — narrow it once,
    # here, rather than asserting/casting at every access site below.
    assert result.receipt is not None, "distill(receipt=True) must return a receipt"
    receipt = result.receipt
    checks = [
        ("full.digest", receipt.full["digest"], GOLDEN_FULL_DIGEST),
        ("distilled.digest", receipt.distilled["digest"], GOLDEN_DISTILLED_DIGEST),
        ("drops[body].digest", receipt.drops[0]["digest"], GOLDEN_BODY_DROP_DIGEST),
        ("drops[tags].digest", receipt.drops[1]["digest"], GOLDEN_TAGS_DROP_DIGEST),
    ]
    for name, got, want in checks:
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            print(f"        want {want}\n        got  {got}")

    print("== determinism (5 repeated calls, same input+budget) ==")
    outs = {json.dumps(distill(FIXTURE, budget=FIXTURE_BUDGET).content, sort_keys=True)
            for _ in range(5)}
    ok = len(outs) == 1
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  5 runs collapse to one distilled output")

    print("== planted inconsistency (truncated flag disagrees with drops) ==")
    honest = receipt
    v_honest = verify_receipt(honest)
    ok = v_honest["ok"]
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  honest receipt verifies clean")

    lied = DropReceipt(**{**honest.to_dict(), "drops": []})  # claims nothing dropped
    v_lied = verify_receipt(lied)
    ok = not v_lied["ok"]
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  planted lie (drops erased, truncated=True kept) is caught")

    print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
