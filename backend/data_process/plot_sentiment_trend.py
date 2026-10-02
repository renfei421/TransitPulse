import argparse
import csv
import os
from datetime import datetime
from pathlib import Path


DEFAULT_OUTPUT_DIR = Path("backend/data_process/output")
os.environ.setdefault("MPLCONFIGDIR", str(DEFAULT_OUTPUT_DIR / ".matplotlib"))

import matplotlib.pyplot as plt


def read_rows(path):
    rows = []

    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    return rows


def ratios_from_rows(rows):
    dates = [datetime.fromisoformat(row["period"]) for row in rows]
    positive = [float(row["positive_ratio"]) for row in rows]
    neutral = [float(row["neutral_ratio"]) for row in rows]
    negative = [float(row["negative_ratio"]) for row in rows]
    return dates, positive, neutral, negative


def plot_one_axis(ax, rows, title):
    dates, positive, neutral, negative = ratios_from_rows(rows)
    ax.stackplot(
        dates,
        negative,
        neutral,
        positive,
        labels=["Negative", "Neutral", "Positive"],
        colors=["#d95f5f", "#b8c2cc", "#4c9f70"],
        alpha=0.9,
    )
    ax.set_title(title)
    ax.set_ylabel("Share of posts")
    ax.set_ylim(0, 1)
    ax.grid(axis="y", alpha=0.25)


def plot_trend(input_path, output_path, group_field=None):
    rows = read_rows(input_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if group_field:
        groups = {}
        for row in rows:
            groups.setdefault(row.get(group_field, "unknown"), []).append(row)

        fig, axes = plt.subplots(len(groups), 1, figsize=(14, 4 * len(groups)), sharex=True)
        if len(groups) == 1:
            axes = [axes]
        for ax, (group, group_rows) in zip(axes, sorted(groups.items())):
            plot_one_axis(ax, group_rows, f"Daily Contextual Sentiment Ratio - {group}")
        axes[0].legend(loc="upper left", ncols=3, frameon=False)
        axes[-1].set_xlabel("Date")
    else:
        fig, ax = plt.subplots(figsize=(14, 7))
        plot_one_axis(ax, rows, "Daily Contextual Sentiment Ratio")
        ax.set_xlabel("Date")
        ax.legend(loc="upper left", ncols=3, frameon=False)

    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(output_path, dpi=180)
    plt.close(fig)


def parse_args():
    parser = argparse.ArgumentParser(description="Plot daily sentiment ratios from CSV.")
    parser.add_argument(
        "--input",
        default="backend/data_process/output/sentiment_trend_daily_transformer.csv",
    )
    parser.add_argument(
        "--output",
        default="backend/data_process/output/sentiment_trend_daily_transformer.png",
    )
    parser.add_argument(
        "--group-field",
        help="Optional CSV field to split into separate panels, for example topic.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    plot_trend(input_path, output_path, args.group_field)
    print(f"Chart written to: {output_path}")


if __name__ == "__main__":
    main()
