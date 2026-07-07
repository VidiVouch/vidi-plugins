---
name: vidi-review-attestation
description: Design or implement Vidi review attestation workflows that connect deep-review or other tool reviews to Vidi ToolReviewV2 records. Use when working on review-attestation, vidi attest --tool, deep-review mint gates, diff-to-unit mapping, tool-lane signing, or the review/authorship matrix.
---

# Vidi Review Attestation

Use this skill when turning tool review results into Vidi review-attestation records or when reviewing designs that touch the deep-review to Vidi seam.

Before implementing or judging the seam, read `references/REVIEW-ATTESTATION-PROPOSAL.html`. Treat it as the current design proposal, not a ratified final spec.

## Boundaries

- Emit review attestations through a dedicated machine entrypoint such as `vidi attest --tool`, not through human verbs like `vidi vouch`, `vidi sign`, or an overloaded interactive `vidi review`.
- Reuse Vidi's existing `ToolReviewV2` predicate shape where possible. Do not invent a new predicate without first proving the existing one cannot represent the claim.
- Do not let the tool wrapper compute or pass unit content digests. Vidi must resolve the unit address and compute the canonical content hash through its own normalizer.
- Label A0/self-asserted records honestly as transparency-by-plain-file, not cryptographic integrity. Treat signed tool lanes as later work unless `Lane::Tool` or equivalent authenticated tool identity exists.

## Mint Gates

Never mint a clean tool-review record unless all of these are true:

- Every reviewer whose result contributes to the attestation completed without degradation. A crashed or degraded reviewer is not a clean result.
- The observed review scope covered the full unit body being attested. A hunk-only review must not attest the whole unit hash.
- The workflow has checked for an identical already-approved line and skips duplicate minting where possible.

## Mapping

Map review results to Vidi units by intersecting frozen-worktree diff hunks with post-image unit `line_span` data from Vidi scanning. For renamed units, fail closed or surface an explicit reconcile path; do not claim review freshness silently survives a rename.

## Matrix Composition

Keep review records and authorship separate. Compose "who wrote this unit" and "who reviewed it clean" at read time by joining the current Vidi unit span with current git-ai authorship data. Do not store authorship references inside review predicates.

## Open Prerequisites

- Non-interactive Vidi CLI contract for tool attestation, including tool, model, rigor, verdict, criteria, and machine-readable output. No digest flag.
- Deep-review glue for diff-to-unit mapping and mint gating.
- Optional later `Lane::Tool` or equivalent authenticated tool identity for signed tool review lanes.
