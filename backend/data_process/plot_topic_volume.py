import argparse
import csv
import os
from datetime import datetime
from pathlib import Path


DEFAULT_OUTPUT_DIR = Path("backend/data_process/output")
os.environ.setdefault("MPLCONFIGDIR", str(DEFAULT_OUTPUT_DIR / ".matplotlib"))

import matplotlib.pyplot as plt


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


def read_topic_volume(path):
    data = {}
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            topic = row["topic"]
            data.setdefault(topic, []).append(
                (datetime.fromisoformat(row["period"]), int(row["total"]))
            )

    for rows in data.values():
        rows.sort(key=lambda item: item[0])

    return data


def plot_volume(input_path, output_path):
    data = read_topic_volume(input_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(14, 7))

    for topic in ("public_transport", "ev", "fuel_price"):
        rows = data.get(topic, [])
        if not rows:
            continue
        dates = [item[0] for item in rows]
        totals = [item[1] for item in rows]
        ax.plot(
            dates,
            totals,
            label=TOPIC_LABELS.get(topic, topic),
            color=TOPIC_COLORS.get(topic),
            linewidth=2,
        )

    ax.set_title("Daily Discussion Volume by Topic")
    ax.set_xlabel("Date")
    ax.set_ylabel("Post count")
    ax.grid(axis="y", alpha=0.25)
    ax.legend(loc="upper left", frameon=False)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(output_path, dpi=180)
    plt.close(fig)


def parse_args():
    parser = argparse.ArgumentParser(description="Plot daily discussion volume by topic.")
    parser.add_argument(
        "--input",
        default="backend/data_process/output/sentiment_trend_daily_by_topic_transformer.csv",
    )
    parser.add_argument(
        "--output",
        default="backend/data_process/output/topic_volume_daily.png",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    plot_volume(Path(args.input), Path(args.output))
    print(f"Chart written to: {args.output}")


if __name__ == "__main__":
    main()
