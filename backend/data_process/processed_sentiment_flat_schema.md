# Processed sentiment schema v2

The authoritative mapping is database/mappings/social_posts_processed.json.
Each document represents one source post, upserted under a stable doc_id.

| Field | Meaning |
|---|---|
| local_sentiment_label | Class predicted from the target text alone |
| local_sentiment_score | Class probability/proportion, not signed direction |
| contextual_sentiment_label | Context/rule result; root uses local result |
| contextual_sentiment_score | Contextual class confidence/proportion |
| contextual_sentiment_polarity | P(positive)-P(negative) for RoBERTa; explicitly tagged VADER compound for the baseline |
| candidate_topics | Union of direct and inherited topic assignments |
| direct_candidate_topics / inherited_candidate_topics | Auditable distinction between text matching and thread context |
| topic_source | direct / inherited / unassigned |
| sentiment_method | Local/context classification or explicit agreement inheritance |
| context_missing_parent | Required ancestor was unavailable in bounded reconstruction |
| model_name / model_revision / pipeline_version / schema_version | Reproducible processing identity |
| created_at / fetched_at / processed_at | Event, ingestion and processing timestamps |
| dataset_kind / source_dataset | Separate live/imported/synthetic provenance |

API net sentiment is (positive-negative)/classified_count; it is computed from
labels and is not an average of confidence. Do not merge polarity values from
different scoring models without explicit comparison/calibration.

Disagreement never mechanically flips a parent's sentiment. Agreement inheritance
is a documented heuristic awaiting domain evaluation. Long context retains the
target reply at the end of a left-truncated transformer input.

