# Evaluation and model choice

RoBERTa is retained as a fixed English social-text classifier. Its checkpoint is
cardiffnlp/twitter-roberta-base-sentiment-latest at revision
3216a57f2a0d9c45a2e6c20157c20c49fb4bf9c7.
[Model card](https://huggingface.co/cardiffnlp/twitter-roberta-base-sentiment-latest)

The model card motivates a social-text baseline. It does not establish superiority
on Australian transport discussion, news documents or reply-context inputs.
VADER remains an explicit, cheap baseline. There is no silent fallback.

## Human evaluation protocol

1. Sample 300–500 real posts stratified by platform, topic, week and reply type.
   Keep all members of a thread together. Include irrelevant/ambiguous-topic examples.
2. Label sentiment separately from transport stance. “I disagree” does not imply
   positive sentiment, and a sarcastic “exactly” may defeat the inheritance rule.
3. Two annotators independently label a subset, discuss disagreements and freeze
   an adjudication guide. Save human-adjudicated labels only after review.
4. Use deterministic thread-grouped dev/test splits. Tune rules on dev, then freeze
   the code/model/checkpoint before reporting test results.
5. Report macro F1, per-class precision/recall/support, confusion matrix, root/reply
   slices and a thread-cluster bootstrap interval. Report missing context separately.

Required NDJSON columns: post_id, thread_root_id, text, label; optional parent_post_id,
candidate_topics. Real evaluation requires label_source=human_adjudicated.

~~~bash
uv run --extra model python -m backend.evaluation.evaluate data/labels/gold.ndjson --split dev --output artifacts/eval-dev.json
uv run --extra model python -m backend.evaluation.evaluate data/labels/gold.ndjson --split test --output artifacts/eval-test.json
~~~

The tool compares VADER local, RoBERTa local and contextual RoBERTa. It does not
train a model. The split seed, data hash, model revision and prediction IDs are
saved. Illustrative labels need an explicit --allow-illustrative flag and produce
a software-check-only status. They must never be reported as human accuracy.

A held-out gold set was not supplied or fabricated in this work. No new domain
accuracy, significance improvement or “RoBERTa beats VADER” claim is made.

## Fine-tuning decision

First measure failure slices and improve data quality. Fine-tuning becomes
worthwhile only if persistent domain/stance errors remain, labels are sufficient,
and gains survive an untouched thread/time holdout. A three-class sentiment
classifier is not automatically a stance classifier or an Agent. This project
already provides useful infrastructure without fine-tuning.

## Performance evidence

scripts/benchmark.py measures actual Elasticsearch ingestion/queries on synthetic
data. scripts/benchmark_model.py separately measures fixed-model CPU batch sizes.
See evidence/ for the measured JSON results and DELIVERY_REPORT.zh-CN.md for
interpretation. The model benchmark clears caches, excludes model load time and
uses 96 unique synthetic short texts. One run per batch size is a demonstration,
not a confidence interval on production performance.
