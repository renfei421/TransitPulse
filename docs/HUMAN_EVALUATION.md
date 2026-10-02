# Human evaluation handoff (not yet labelled)

`scripts.replay_analysis` prepares private tasks in
`artifacts/offline-analysis/human-review-tasks.private.json` and stores predictions
separately in `human-review-predictions.private.json`. All label fields start null.
AI review is not substituted for human annotation.

Sampling: at most five deterministic hash-ranked post/target tasks per platform ×
event phase × target × model-acceptance stratum. Sparse strata contribute fewer.
A post may occur for multiple targets. This is a diagnostic enriched sample,
not an unbiased estimate of population accuracy. The exact count is in
`docs/evidence/offline-analysis-verification.json`.

Reviewers receive only the task file. For each named target, assign one state:
`expressed`, `factual_only`, `quoted_only`, `not_mentioned`, `mixed`, or `unclear`.
Only `expressed` receives a sentiment label: `negative`, `neutral`, or `positive`.
Use neutral for an expressed evaluative attitude without clear polarity, not for
every factual sentence. Do not infer combustion-engine preference from a generic
mention of cars. Do not infer a contemporaneous price reaction from a general
preference for cheap fuel. Missing context should remain unclear.

Have a second person independently label at least an overlapping subset, ideally
the whole set, and adjudicate disagreements without using the model as the judge.
Retain original labels and notes. Never overwrite the frozen experimental outputs.

Once real labels exist, report state confusion, accepted-target precision,
missed expressed attitudes, abstention/coverage, and sentiment metrics on the
appropriate expressed subset. Break down by platform and target, provide sample
sizes and uncertainty, and report enrichment/selection. Treat small strata as
diagnostic. A threshold selected on these tasks needs a separate held-out set.

No human-labelled accuracy, precision, recall or F1 has been measured yet.
