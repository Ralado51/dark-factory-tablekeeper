# Tablekeeper — Dark Factory Run 4

Tablekeeper is the result of a four-stage judged dark-factory run for the WeAreDevelopers × BAND challenge. The repository preserves the complete cumulative delivery from Stage 1 through Stage 4, together with the generic factory mandates and the full BAND room transcript.

## Final result

The final accepted Stage 4 revision is:

`a21507bae1e598c6c53dafd8b93063cd11c6dd9e`

Independent reviewer acceptance was recorded after the official isolated cumulative harness completed with zero unexpected failures or errors:

- Stage 1: 120/120
- Stage 2: 25/25
- Stage 3: 7/7
- Stage 4: 6/6
- Highest contiguous stage: 4
- Claimed stage: 4

The reviewer also independently exercised adversarial API, concurrency, persistence/import, upgrade, browser and product behavior beyond the public harness.

## Repository layout

- `mandates/` — generic role mandates for the coordinator, implementer and reviewer seats.
- `room.json` — full BAND room transcript for the judged Run 4.
- `stage-1/` — accepted standalone Stage 1 delivery.
- `stage-2/` — accepted cumulative Stage 2 delivery.
- `stage-3/` — accepted cumulative Stage 3 delivery.
- `stage-4/` — final accepted cumulative delivery.
- `FACTORY.md` — factory design, workflow, review strategy, failures and measured evidence.

Each stage is a standalone service delivery with its own Dockerfile and RUN.md. See the relevant stage directory for stage-specific execution instructions.

## Factory model

The judged run used three distinct BAND seats:

- **coordinator** — orchestration, self-contained delegation, revision/evidence tracking and stage boundaries.
- **implementer** — implementation, tests, commits and correction work.
- **reviewer** — independent verification of exact committed revisions, adversarial testing and explicit ACCEPT/REJECT decisions.

The reviewer did not infer acceptance from implementer results. Defects were returned through the coordinator and corrected in new additive commits before independent re-review.

## Verification highlights

Final Stage 4 review included an exhaustive optimizer oracle with 1,144 assertions over 24 varied fixtures, a cumulative independent API suite of 59/59, and browser/product verification across actual Stage 1/2/3 upgrade sources. The browser review recorded 41/41 assertions for each source (123 total), plus 24 independently authored pre/post-commit fault assertions.

Testing included isolated/no-outbound-network execution under 2 vCPU / 2 GiB limits, deterministic optimization, concurrency, idempotency, rollback, stale-plan handling, corruption rejection, recurring amendments, legacy export/import upgrades, responsive browser behavior and recovery from uncertain outcomes.

## History and evidence

The Git history intentionally preserves rejected revisions and corrective commits. This is part of the factory evidence rather than history to be hidden or rewritten. Earlier accepted stage directories and mandates were preserved while later cumulative stages were developed.

See `FACTORY.md` and `room.json` for the detailed autonomous workflow and review history.
