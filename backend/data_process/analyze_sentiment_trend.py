import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


LABELS = ("positive", "neutral", "negative")
DEFAULT_TOPICS = ("public_transport", "ev", "fuel_price")


def parse_datetime(value):
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def period_key(dt, interval):
    if interval == "day":
        return dt.date().isoformat()
    if interval == "week":
        year, week, _ = dt.isocalendar()
        return f"{year}-W{week:02d}"
    if interval == "month":
        return f"{dt.year:04d}-{dt.month:02d}"
    raise ValueError("interval must be day, week, or month")


def read_rows(path):
    with path.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid JSON on line {line_number}: {error}") from error


def normalize_topics(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value]
    return []


def aggregate(input_path, interval, sentiment_field, group_by, by_topic, topic_field, topics):
    buckets = defaultdict(lambda: {label: 0 for label in LABELS} | {"total": 0})
    skipped = 0
    topic_set = set(topics)

    for row in read_rows(input_path):
        created_at = parse_datetime(row.get("created_at"))
        label = row.get(sentiment_field)

        if created_at is None or label not in LABELS:
            skipped += 1
            continue

        row_topics = [None]
        if by_topic:
            row_topics = [
                topic for topic in normalize_topics(row.get(topic_field))
                if topic in topic_set
            ]
            if not row_topics:
                skipped += 1
                continue

        for topic in row_topics:
            key_parts = [period_key(created_at, interval)]
            if by_topic:
                key_parts.append(topic)
            for field in group_by:
                key_parts.append(str(row.get(field) or "unknown"))

            bucket = buckets[tuple(key_parts)]
            bucket[label] += 1
            bucket["total"] += 1

    return buckets, skipped


def write_csv(output_path, buckets, group_by, by_topic):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    headers = (
        ["period"]
        + (["topic"] if by_topic else [])
        + group_by
        + ["total"]
        + [f"{label}_count" for label in LABELS]
        + [f"{label}_ratio" for label in LABELS]
    )

    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()

        for key, counts in sorted(buckets.items()):
            total = counts["total"]
            row = {"period": key[0], "total": total}
            start_index = 1
            if by_topic:
                row["topic"] = key[1]
                start_index = 2
            for index, field in enumerate(group_by, start=start_index):
                row[field] = key[index]
            for label in LABELS:
                row[f"{label}_count"] = counts[label]
                row[f"{label}_ratio"] = counts[label] / total if total else 0.0
            writer.writerow(row)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Aggregate sentiment label counts and ratios over time."
    )
    parser.add_argument(
        "--input",
        default="backend/data_process/output/thread_sentiment_flat_transformer.jsonl",
    )
    parser.add_argument(
        "--output",
        default="backend/data_process/output/sentiment_trend_daily.csv",
    )
    parser.add_argument(
        "--interval",
        choices=("day", "week", "month"),
        default="day",
    )
    parser.add_argument(
        "--sentiment-field",
        choices=("contextual_sentiment_label", "local_sentiment_label"),
        default="contextual_sentiment_label",
    )
    parser.add_argument(
        "--group-by",
        nargs="*",
        default=[],
        help="Optional extra fields, for example candidate_topics or thread_seed_type.",
    )
    parser.add_argument(
        "--by-topic",
        action="store_true",
        help="Explode candidate topic lists and aggregate each selected topic separately.",
    )
    parser.add_argument(
        "--topic-field",
        default="candidate_topics",
    )
    parser.add_argument(
        "--topics",
        nargs="*",
        default=list(DEFAULT_TOPICS),
        help="Topic labels to include when --by-topic is set.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)

    buckets, skipped = aggregate(
        input_path=input_path,
        interval=args.interval,
        sentiment_field=args.sentiment_field,
        group_by=args.group_by,
        by_topic=args.by_topic,
        topic_field=args.topic_field,
        topics=args.topics,
    )
    write_csv(output_path, buckets, args.group_by, args.by_topic)

    total = sum(bucket["total"] for bucket in buckets.values())
    print(f"Rows included: {total}")
    print(f"Rows skipped: {skipped}")
    print(f"Periods: {len(buckets)}")
    print(f"Output written to: {output_path}")


if __name__ == "__main__":
    main()
