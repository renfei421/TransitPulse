"""Generate a clean-kernel API v1 analysis notebook and aggregate HTML export."""
import nbformat as nbf


def build():
    cells = []
    def md(value): cells.append(nbf.v4.new_markdown_cell(value))
    def code(value): cells.append(nbf.v4.new_code_cell(value))
    md("""# Transport discourse: evidence, coverage and exploratory association
This notebook reads API v1, never credentials or Elasticsearch directly.
Configure API_BASE, ANALYSIS_FROM, ANALYSIS_TO, DATASET_KIND and optionally SENTIMENT_MODEL.

The reproducible demo is **synthetic**. Select **observed** for real samples.
Topic groups overlap, platform coverage differs, and model labels do not measure population opinion.
""")
    code('''import os, json, html
from datetime import date, timedelta
from pathlib import Path
import requests
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from IPython.display import display

API_BASE = os.getenv("API_BASE", "http://127.0.0.1:9090/api/v1").rstrip("/")
FROM = os.getenv("ANALYSIS_FROM", (date.today()-timedelta(days=29)).isoformat())
TO = os.getenv("ANALYSIS_TO", date.today().isoformat())
KIND = os.getenv("DATASET_KIND", "observed")
MODEL = os.getenv("SENTIMENT_MODEL")
PARAMS = {"from": FROM, "to": TO, "dataset_kind": KIND}
if MODEL:
    PARAMS["model"] = MODEL
figures = []

def fetch(resource, params=None, optional=False):
    selected = dict(PARAMS if params is None else params)
    if resource in {"oil/prices", "news/volume"}:
        selected.pop("model", None)  # Source observations have no inference model.
    response = requests.get(f"{API_BASE}/{resource}", params=selected, timeout=30)
    if optional and response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()["data"]

def frame(resource, columns):
    return pd.DataFrame(fetch(resource), columns=columns)

print(json.dumps({"window": [FROM, TO], "dataset_kind": KIND, "model": MODEL or "all (inspect mixture below)"}, indent=2))
if KIND in {"synthetic", "all"}:
    print("DEMONSTRATION / MIXED DATA: do not present these plots as real research findings.")
''')
    md("""## Coverage before interpretation
Independent threads, processing coverage and model mixture determine what charts can support.
Distinct counts are approximate. A high processing ratio does not establish representative sampling or accuracy.
""")
    code('''quality = fetch("quality")
display(pd.DataFrame([quality]).drop(columns=["models", "interpretation"], errors="ignore").T.rename(columns={0: "value"}))
display(pd.DataFrame(quality.get("models", [])))
if len(quality.get("models", [])) > 1 and not MODEL:
    print("Multiple models found: set SENTIMENT_MODEL before comparing model polarity.")
print(quality["interpretation"])
''')
    md("""## S1 — Discussion volume, news attention and Brent prices
These are separate measurements. Oil observations remain missing on dates without published prices.
No artificial weekend interpolation is used.
""")
    code('''volume = frame("social/volume", ["date", "topic", "value"])
news_volume = frame("news/volume", ["date", "daily_volume", "avg_share_percent"])
oil = frame("oil/prices", ["date", "avg_price", "daily_return_pct"])
fig = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=["Discussion sample", "News attention", "Brent observations"])
if not volume.empty:
    for topic, rows in volume.groupby("topic"):
        fig.add_trace(go.Scatter(x=rows["date"], y=rows["value"], name=topic), row=1, col=1)
if not news_volume.empty:
    fig.add_trace(go.Bar(x=news_volume["date"], y=news_volume["daily_volume"], name="GDELT articles"), row=2, col=1)
if not oil.empty:
    fig.add_trace(go.Scatter(x=oil["date"], y=oil["avg_price"], name="USD/barrel", connectgaps=False), row=3, col=1)
fig.update_layout(height=820, title=f"S1 — {KIND} data", template="plotly_white")
fig.update_yaxes(title_text="posts", row=1, col=1)
fig.update_yaxes(title_text="articles", row=2, col=1)
fig.update_yaxes(title_text="USD/bbl", row=3, col=1)
figures.append(fig)
fig.show()
''')
    md("""## S2 — Signed sentiment and sample size
**Net sentiment = (positive − negative) / classified count**, in [−1, 1].
Confidence never substitutes for direction. Hover reveals the contributing sample size.
""")
    code('''sentiment = frame("social/sentiment", ["date", "topic", "positive", "neutral", "negative", "doc_count", "net_sentiment"])
news_sentiment = frame("news/sentiment", ["date", "positive", "neutral", "negative", "doc_count", "net_sentiment"])
if not sentiment.empty:
    fig = px.line(sentiment, x="date", y="net_sentiment", color="topic", hover_data=["doc_count", "positive", "neutral", "negative"],
                  title=f"S2 — Signed label balance ({KIND})", range_y=[-1, 1], template="plotly_white")
    figures.append(fig)
    fig.show()
else:
    print("No processed social records in this window.")
display(news_sentiment.head(10))
''')
    md("""## S3 — Platform comparison
Compare label balance alongside counts. Differences may reflect coverage, users, language, matching or model error.
They do not establish that a platform causes a preference.
""")
    code('''profile = fetch("platforms/profiles")
profiles = pd.DataFrame([{"platform": platform, "topic": topic, **values}
                        for platform, topics in profile.items() for topic, values in topics.items()])
if not profiles.empty:
    display(profiles[["platform", "topic", "doc_count", "net_sentiment", "unique_threads_approx"]])
    fig = px.bar(profiles, x="topic", y="net_sentiment", color="platform", barmode="group",
                 hover_data=["doc_count", "classified_count"], range_y=[-1, 1],
                 title=f"S3 — Platform profiles ({KIND})", template="plotly_white")
    figures.append(fig)
    fig.show()
''')
    md("""## S4 — Calendar-aligned exploratory association
Positive lag pairs oil return on day d with sentiment on day d + lag.
Missing dates are excluded, never compressed into adjacent records.
At least 14 observed pairs are required; constant series produce null coefficients.
Adjusted p-values address the lag search only. Serial dependence and confounding remain.
These charts establish neither causality nor forecasting ability.
""")
    code('''correlation = fetch("analyses/oil-sentiment", optional=True)
if correlation is None:
    print("No matching precomputed result. Run the correlation job for this exact window/cohort/model.")
else:
    display(pd.DataFrame([{k: correlation.get(k) for k in
        ["from_date", "to_date", "model_name", "dataset_kind", "n_days_used", "correlation_pearson", "best_lag_days", "minimum_pairs", "interpretation"]}]).T)
    lags = pd.DataFrame(correlation["ccf_window"])
    fig = px.bar(lags, x="lag", y="corr", hover_data=["n_pairs", "p_value", "p_value_bonferroni"],
                 title=f"S4 — Calendar-day lag search ({KIND})", range_y=[-1, 1], template="plotly_white")
    figures.append(fig)
    fig.show()
    pairs = pd.DataFrame(correlation.get("best_lag_scatter_data", []))
    if not pairs.empty:
        fig = px.scatter(pairs, x="brent_return_pct", y="oil_sentiment", hover_data=["price_date", "sentiment_date"],
                         title="Observed pairs at selected lag — exploratory", template="plotly_white")
        figures.append(fig)
        fig.show()
''')
    md("""## Inspect examples and export
Use examples to check topic inheritance and disagreement errors. Do not export personal text publicly.
The HTML export contains aggregate charts only.
""")
    code('''sample = fetch("social/posts", params={**PARAMS, "limit": 10})
display(pd.DataFrame(sample["items"]))
export_path = Path(os.getenv("ANALYSIS_HTML", "../artifacts/analysis.html"))
export_path.parent.mkdir(parents=True, exist_ok=True)
heading = f"Transport analytics · {KIND} · {FROM} to {TO}"
parts = ["<!doctype html><html><head><meta charset='utf-8'><title>Transport analytics</title></head><body>",
         f"<h1>{html.escape(heading)}</h1>",
         "<p>Observed sample analytics. Synthetic demo results are not research findings. No causal claim.</p>",
         "<pre>"+html.escape(json.dumps(quality, ensure_ascii=False, indent=2))+"</pre>"]
for i, fig in enumerate(figures):
    parts.append(fig.to_html(full_html=False, include_plotlyjs=True if i == 0 else False))
parts.append("</body></html>")
export_path.write_text("\\n".join(parts), encoding="utf-8")
print("Aggregate-only HTML export:", export_path.resolve())
''')
    notebook = nbf.v4.new_notebook(cells=cells, metadata={"kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"}})
    nbf.write(notebook, "frontend/hormuz_analysis.ipynb")


if __name__ == "__main__":
    build()
