import argparse
import csv
import os
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path


DEFAULT_OUTPUT_DIR = Path("backend/data_process/output")
os.environ.setdefault("MPLCONFIGDIR", str(DEFAULT_OUTPUT_DIR / ".matplotlib"))

import matplotlib.pyplot as plt


LABELS = ("negative", "neutral", "positive")
TOPICS = ("public_transport", "ev", "fuel_price")

TOPIC_LABELS = {
    "public_transport": "Public Transport",
    "ev": "Electric Vehicles",
    "fuel_price": "Fuel Price",
}

TOPIC_COLORS = {
    "public_transport": "#2f6fbb",
    "ev": "#4c9f70",
    "fuel_price": "#d9822b",
}

SENTIMENT_COLORS = {
    "negative": "#d95f5f",
    "neutral": "#b8c2cc",
    "positive": "#4c9f70",
}


def read_topic_rows(path):
    rows_by_topic = defaultdict(list)
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            topic = row["topic"]
            rows_by_topic[topic].append({
                "date": datetime.fromisoformat(row["period"]),
                "total": int(row["total"]),
                "negative": int(row["negative_count"]),
                "neutral": int(row["neutral_count"]),
                "positive": int(row["positive_count"]),
            })

    for rows in rows_by_topic.values():
        rows.sort(key=lambda item: item["date"])

    return rows_by_topic


def rolling_rows(rows, window):
    queue = deque()
    sums = {label: 0 for label in LABELS}
    total_sum = 0
    output = []

    for row in rows:
        queue.append(row)
        total_sum += row["total"]
        for label in LABELS:
            sums[label] += row[label]

        if len(queue) > window:
            old = queue.popleft()
            total_sum -= old["total"]
            for label in LABELS:
                sums[label] -= old[label]

        days = len(queue)
        smooth = {
            "date": row["date"],
            "volume_mean": total_sum / days,
            "total": total_sum,
        }
        for label in LABELS:
            smooth[f"{label}_ratio"] = sums[label] / total_sum if total_sum else 0.0
        output.append(smooth)

    return output


def plot_smoothed_volume(rows_by_topic, output_path, window):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(14, 7))

    for topic in TOPICS:
        rows = rows_by_topic.get(topic, [])
        if not rows:
            continue
        smooth = rolling_rows(rows, window)
        ax.plot(
            [row["date"] for row in smooth],
            [row["volume_mean"] for row in smooth],
            label=TOPIC_LABELS.get(topic, topic),
            color=TOPIC_COLORS.get(topic),
            linewidth=2.5,
        )

    ax.set_title(f"Daily Discussion Volume Trend by Topic ({window}-day rolling mean)")
    ax.set_xlabel("Date")
    ax.set_ylabel("Post count")
    ax.grid(axis="y", alpha=0.25)
    ax.legend(loc="upper left", frameon=False)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(output_path, dpi=180)
    plt.close(fig)


def plot_smoothed_sentiment(rows_by_topic, output_path, window):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(len(TOPICS), 1, figsize=(14, 10), sharex=True)

    for ax, topic in zip(axes, TOPICS):
        rows = rows_by_topic.get(topic, [])
        if not rows:
            ax.set_title(TOPIC_LABELS.get(topic, topic))
            continue

        smooth = rolling_rows(rows, window)
        dates = [row["date"] for row in smooth]
        ax.stackplot(
            dates,
            [row["negative_ratio"] for row in smooth],
            [row["neutral_ratio"] for row in smooth],
            [row["positive_ratio"] for row in smooth],
            labels=["Negative", "Neutral", "Positive"],
            colors=[SENTIMENT_COLORS[label] for label in LABELS],
            alpha=0.9,
        )
        ax.set_title(f"{TOPIC_LABELS.get(topic, topic)} ({window}-day rolling ratio)")
        ax.set_ylabel("Share")
        ax.set_ylim(0, 1)
        ax.grid(axis="y", alpha=0.25)

    axes[0].legend(loc="upper left", ncols=3, frameon=False)
    axes[-1].set_xlabel("Date")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(output_path, dpi=180)
    plt.close(fig)


def parse_args():
    parser = argparse.ArgumentParser(description="Plot smoothed topic volume and sentiment trends.")
    parser.add_argument(
        "--input",
        default="backend/data_process/output/sentiment_trend_daily_by_topic_transformer.csv",
    )
    parser.add_argument("--window", type=int, default=7)
    parser.add_argument(
        "--volume-output",
        default="backend/data_process/output/topic_volume_daily_7d_trend.png",
    )
    parser.add_argument(
        "--sentiment-output",
        default="backend/data_process/output/sentiment_trend_daily_by_topic_7d_trend.png",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    rows_by_topic = read_topic_rows(Path(args.input))
    plot_smoothed_volume(rows_by_topic, Path(args.volume_output), args.window)
    plot_smoothed_sentiment(rows_by_topic, Path(args.sentiment_output), args.window)
    print(f"Volume trend written to: {args.volume_output}")
    print(f"Sentiment trend written to: {args.sentiment_output}")


if __name__ == "__main__":
    main()
