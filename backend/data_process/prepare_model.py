"""Warm the pinned CPU model before an offline processing container starts."""
import importlib.metadata
import json
import os
from pathlib import Path

from backend.data_process.sentiment import SentimentModel
from backend.data_process.eligibility import LanguageDetector


def main():
    model = SentimentModel(batch_size=8, truncation_side="right")
    results = model.classify_many(["The bus service is wonderful.", "Fuel prices are terrible."])
    if len(results) != 2:
        raise RuntimeError("Model warmup returned incomplete results")
    language, _ = LanguageDetector().classify("The public transport service connects our city.")
    if language != "en":
        raise RuntimeError("Language detector warmup failed")
    info = {"model_name": model.model_name, "model_revision": model.revision,
            "release_sha256": os.environ.get("RELEASE_SHA256"), "warmup_predictions": len(results),
            "versions": {name: importlib.metadata.version(name) for name in ("torch", "transformers", "langid")}}
    Path("/work/model-build-info.json").write_text(json.dumps(info, indent=2)+"\n")
    print(json.dumps({"model_prepared": info}), flush=True)


if __name__ == "__main__":
    main()
