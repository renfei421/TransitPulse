"""CPU batch-size comparison on explicit synthetic text; excludes model load time."""
import json
import os
from pathlib import Path
import time
import torch
from backend.data_process.sentiment import SentimentModel


def run():
    torch.set_num_threads(2)
    texts = [f"Example {i}: "+text for i in range(32) for text in (
        "Petrol prices are terrible and the bus service is unreliable.",
        "Melbourne trams are excellent and I love using public transport.",
        "Electric vehicles were discussed in today's transport meeting.",
    )]
    model = SentimentModel()
    model.classify("Model warmup.")
    timings = []
    for batch in (1, 8, 16):
        model.cache.clear()
        model.batch_size = batch
        start = time.perf_counter()
        output = model.classify_many(texts)
        elapsed = time.perf_counter()-start
        timings.append({"batch_size": batch, "seconds": elapsed, "texts_per_second": len(texts)/elapsed,
                        "predictions": len(output)})
    result = {"workload": "96 unique short synthetic English transport texts",
              "model": model.model_name, "revision": model.revision, "device": "CPU",
              "torch_threads": torch.get_num_threads(), "torch_version": torch.__version__,
              "cache": "cleared before each variant", "load_time_included": False, "results": timings,
              "limit": "One run per batch size; not a statistical performance claim or accuracy evaluation."}
    Path("artifacts").mkdir(exist_ok=True)
    Path("artifacts/model-benchmark.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    run()
