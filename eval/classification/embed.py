"""Compute local sentence embeddings for items.csv (run in a THROWAWAY container, never the app image).

  python embed.py <fastembed-model-name> <out.npy>

Prints load time, per-item latency, peak RSS and model cache size so the resource cost can be judged.
"""
import csv
import os
import resource
import sys
import time

import numpy as np

model_name, out = sys.argv[1], sys.argv[2]
here = os.path.dirname(os.path.abspath(__file__))
items = list(csv.DictReader(open(os.path.join(here, "items.csv"), encoding="utf-8")))
texts = [it["title"] + ". " + it["summary"] for it in items]

t0 = time.time()
from fastembed import TextEmbedding  # noqa: E402

model = TextEmbedding(model_name=model_name, cache_dir="/tmp/fe_cache")
t_load = time.time() - t0

t1 = time.time()
E = np.array(list(model.embed(texts, batch_size=32)), dtype=np.float32)
t_all = time.time() - t1

t2 = time.time()
for t in texts[:20]:
    list(model.embed([t]))
t_single = (time.time() - t2) / 20

np.save(os.path.join(here, out), E)
cache_mb = sum(
    os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk("/tmp/fe_cache") for f in fs
) / 1e6
print(f"model={model_name} dim={E.shape[1]} items={E.shape[0]}")
print(f"load_s={t_load:.1f} batch_total_s={t_all:.1f} per_item_batch_ms={1000*t_all/len(texts):.1f} single_item_ms={1000*t_single:.1f}")
print(f"peak_rss_mb={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.0f} model_cache_mb={cache_mb:.0f}")
