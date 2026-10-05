# Factory Report — Tablekeeper Judged Run 4

## 1. Factory objective

This factory was designed to deliver the Tablekeeper challenge cumulatively while making implementation and acceptance separate responsibilities. The goal was not simply to make the public harness pass: every exact committed candidate also had to survive independent requirement-derived review before a stage could be accepted.

The judged Run 4 used only three configured agent seats and human input was limited to the separate stage task dispatches. During each active stage, the factory was instructed to proceed autonomously until independent acceptance or a definitive blocker, then stop and wait for a new human dispatch.

## 2. Seats and responsibilities

### Coordinator

The coordinator orchestrated work but did not implement application code. Its responsibilities were to inspect the authoritative contract, send self-contained handoffs, enforce stage boundaries, preserve accepted history, assign exact committed revisions for review, route concrete defects back to the implementer, and record final evidence and accepted SHAs.

### Implementer

The implementer owned application changes. It inspected the cumulative specification, implemented the stage as a standalone delivery, derived additional checks, ran verification, committed changes and reported exact revisions and evidence. Valid reviewer defects were corrected in new commits; accepted history was not amended, squashed, rebased or rewritten.

### Reviewer

The reviewer was deliberately independent from implementation. It inspected the exact committed revision, reran required verification and derived adversarial tests from the normative requirements instead of accepting implementer claims. Its mandate explicitly covered boundaries, malformed inputs, failure/recovery, concurrency, atomicity, rollback, retries, replay, persistence, authorization, temporal behavior and interactions between otherwise-correct behaviors.

The reviewer could ACCEPT only when no known specification violation remained. A defect required an explicit REJECT with requirement, reproduction, expected/observed behavior, evidence and exact SHA.

## 3. Workflow

Each stage followed the same controlled loop:

1. Human dispatch authorized exactly one stage.
2. Coordinator produced self-contained implementation and review assignments.
3. Implementer delivered a new committed revision and evidence.
4. Reviewer independently inspected and tested that exact commit.
5. A valid defect caused REJECT and a new additive correction commit.
6. Reviewer independently re-reviewed the new commit.
7. Coordinator recorded the accepted full SHA and evidence.
8. Factory stopped until the next human stage dispatch.

Cumulative stages copied accepted behavior forward while preserving earlier accepted directories and mandates. The Git history intentionally retained rejected and intermediate revisions as forensic evidence.

## 4. Why this design

The main design choice was separation of creation from acceptance. Passing supplied tests can miss state-machine, retry, corruption, concurrency and browser-recovery defects. Giving the reviewer an independent mandate and authority to reject exact commits created a second control layer that repeatedly found defects after other verification had passed.

Self-contained handoffs were used so agents did not depend on implicit conversational state. Exact repository paths, revisions, verification commands, constraints and relevant requirements travelled with assignments. This also made the room transcript useful as auditable factory evidence.

Stage boundaries were strict. Automatic next-stage probes were treated only as verification; they did not authorize implementation of the next stage. This prevented the factory from silently overshooting the human-authorized scope.

## 5. Failure handling and review-driven corrections

The run demonstrates that review materially changed the delivered product.

### Stage 1

Stage 1 was ultimately accepted at:

`a6effdbb961698e2dda2f2eac685082dd75f5db0`

Independent final verification passed 120/120 official Stage 1 checks. The accepted revision followed a rejected parent retained in history. Reviewer verification also included independently authored HTTP checks in an isolated container.

### Stage 2

The reviewer rejected:

`cfd4704a70b8ee065fbbdee87a2de29dae89e487`

The defect was browser retry identity: reselecting the same seat after an uncertain outcome discarded the pending idempotency identity, causing a new key instead of retrying the original request. The reviewer reproduced the problem for both single and paired seating.

The implementer corrected it in a new commit. Independent re-review accepted:

`9a90005e3dd29907fcbed58e1e7e9c77523533d3`

Official cumulative verification then passed Stage 1 120/120 and Stage 2 25/25. Independent verification included 16 authored API assertions, four browser fault-injection cases, a cumulative API suite of 24/24 and browser suite of 35/35.

### Stage 3

The reviewer rejected:

`a83673f0a87a60f9f5f8d4064fac17f114b7d8d5`

The defect allowed an imported corrupted creation receipt with an invalid revision to be accepted rather than rejected atomically. The correction expanded validation beyond the single reproduction to analogous receipt integrity behavior.

Independent re-review accepted:

`41a090d871b39645b865bda4794016f8df444223`

Official cumulative verification passed Stage 1 120/120, Stage 2 25/25 and Stage 3 7/7. Independent verification recorded 57 authored API assertions, cumulative API 39/39, corruption/rollback checks, concurrency contenders and browser upgrade/fault testing.

### Stage 4

The first Stage 4 candidate passed the public cumulative harness but was still rejected by independent review:

`1506e32e1b8264a882584cb3e953c1abf5229aec`

The reviewer found inconsistent imported restaurant revision counters could revive a plan that should remain stale. This was a hidden-like persisted-state invariant that passing public tests had not established.

The correction process was additive. Intermediate corrective revisions were retained, including:

- `0d232ece1c9c290131b485b4d960760cf93d5a91`
- `94e5530cb0bc2658bb7ca1ee3506b36df1997734`

After three additive correction commits, independent re-review accepted the final revision:

`a21507bae1e598c6c53dafd8b93063cd11c6dd9e`

The original counter/stale-plan reproduction then rejected corrupt import with 422, rolled back completely and preserved stale-plan behavior.

## 6. Final measured verification

The final accepted revision completed the official isolated cumulative harness with exit 0 and zero unexpected failures/errors:

- Stage 1: 120/120
- Stage 2: 25/25
- Stage 3: 7/7
- Stage 4: 6/6
- Highest contiguous stage: 4
- Claimed stage: 4

Independent Stage 4 review went substantially beyond those public checks:

- exhaustive optimizer oracle: 1,144 assertions across 24 varied fixtures;
- cumulative independent API suite: 59/59;
- repaired-series interaction and 50 distinct-key expected-revision contender checks;
- browser/product verification: 41/41 assertions for each actual Stage 1, Stage 2 and Stage 3 source, 123 total;
- 24 additional independently authored single/pair pre/post-commit fault assertions;
- fresh desktop and 375px browser checks;
- actual accepted Stage 1/2/3 export/import upgrade verification;
- corruption, rollback, idempotency, concurrency, revision, closure, plan, receipt, history and recurring-series interaction checks.

The independent API verification was run without outbound network access under 2 vCPU / 2 GiB limits. Browser review checked readable product behavior, seating labels, distinct feedback, visible labels/focus/navigation, absence of horizontal scrolling and uncaught JavaScript errors, and correct UI updates after seating repairs.

## 7. Upgrade and compatibility verification

The reviewer exercised actual accepted earlier-stage exports rather than synthetic approximations. Stage 1 and Stage 2 exports retained login/tokens/references and exact legacy receipts through import and subsequent operations. Stage 3 state containing moved-date exceptions, cancelled occurrences and collective changes preserved histories and relevant receipts while supporting eligible recurring amendment.

Browser upgrade checks preserved signed-in pending form/body/key state without reload and verified recovery paths after uncertain outcomes.

## 8. Repository integrity

At final acceptance the reviewer verified the exact HEAD and a clean working tree before and after review. Accepted `stage-1/`, `stage-2/`, `stage-3/` and `mandates/` remained byte-for-byte unchanged during the final Stage 4 correction/review cycle. Accepted and rejected Git history was preserved. No prohibited symlinks or submodules were reported.

Post-run submission artifacts (`room.json`, this file and `README.md`) were intentionally created only after the factory had stopped.

## 9. Known limitations

No known cumulative contract violation or blocker remained at final acceptance. Verification is necessarily finite. The final reviewer report records restart-ephemeral state and `planning_limit` above the explicitly supported sizes as permitted characteristics rather than known contract violations.

## 10. Result

The important factory outcome is not only that all four stages passed. Independent review repeatedly discovered valid defects after implementation-level and/or official verification had succeeded, and the autonomous correction loop converted those findings into additive, reviewable fixes without rewriting evidence.

Final accepted Stage 4 SHA:

`a21507bae1e598c6c53dafd8b93063cd11c6dd9e`
