# WikiGame Graph Builder

**Build a navigable Wikipedia article link graph (main namespace) as a single NetworKit binary** that reloads near‑instantly for path‑finding benchmarks such as the Wiki Game.

---

## Features

* **Two execution modes**

  * **Low‑memory (Windows default)** - single‑process, SQLite‑indexed pipeline.  Streams `pagelinks` once, keeps peak RSS ≈ graph size.
  * **Fast POSIX (Linux/macOS default)** - parallel fork workers write binary shards; copy‑on‑write keeps RAM low and build time fast.
* **Optional EdgeListReader import** (`--use-edgelist-import`) - converts the packed binary edge file to a text edgelist and lets NetworKit’s C++ importer build the graph, cutting Python→C++ overhead.
* **Automatic dump caching** - each `*.sql.gz` is downloaded only if absent.
* **Exact shortest paths** via NetworKit’s bidirectional BFS.
* **Cache artefacts**

  * `graph.nkbg` - NetworKitBinary format (fastest reload)
  * `title_index.sqlite` - tiny read‑only DB: article → nid, redirects, `linktarget→pid` (if modern schema)
  * `edges.bin` (+ optional `edges.txt`) - intermediate packed edges / text edgelist.
  * `A.coo.npz` - GraphBLAS sparse matrices (coordinate format) for fast BFS queries


---

## Install

```bash
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install mwsql networkit tqdm psutil python-graphblas graphblas-algorithms suitesparse-graphblas 
```

> NetworKit 10+ wheels exist for modern CPython on Win/Linux/macOS.

---

## One‑time build

### Windows (low‑mem default)

```bash
python wikigame_graph_multi.py build \
       --wiki enwiki --date latest \
       --cache cache/enwiki-latest \
       --drop-disambig \
       --use-edgelist-import   # optional speedup
```

### Linux/macOS (fast default)

```bash
python wikigame_graph_multi.py build \
       --wiki enwiki --date latest \
       --cache cache/enwiki-latest \
       --drop-disambig \
       --workers 8 --chunk-rows 200000 \
       --use-edgelist-import   # optional - replaces Python edge assembly
```

| Flags                   | Meaning                                                    |
| ----------------------- | ---------------------------------------------------------- |
| `--lowmem`              | Force low‑mem builder on any OS                            |
| `--fast`                | Force parallel builder even on Windows (not recommended)   |
| `--workers`             | Processes in POSIX path (default = cores − 1)              |
| `--chunk-rows`          | Pagelinks rows per task shard                              |
| `--mem-threshold`       | %RAM at which producer pauses (POSIX)                      |
| `--block-bytes`         | Block size for constant‑memory reads/writes                |
| `--use-edgelist-import` | Convert `edges.bin → edges.txt` and use C++ EdgeListReader |

---

## Query examples

```bash
# Find the shortest path between src/dst inputs
python wikigame_graph_multi.py query --cache cache/enwiki-latest \
       --src "Bandsaw" --dst "Taylor_Swift"

# Verify a candidate path
python wikigame_graph_multi.py verify --cache cache/enwiki-latest --titles Bandsaw,Tape_measure,Distance,Kevin_Bacon

# Sample 20 optimal paths from randomly selects src/dst pairs
python wikigame_graph_multi.py sample-set --cache cache/enwiki-latest --count 20 --output samples.txt

# Sample 5 optimal paths from randomly selected srcs to Kevin Bacon
python wikigame_graph_multi.py sample-set --cache cache/enwiki-latest --count 5 --dst "Kevin_Bacon" --output samples.txt

# Sample 5 optimal paths from Paul_Erdős to randomly selected dsts
python wikigame_graph_multi.py sample-set --cache cache/enwiki-latest --count 5 --src "Paul_Erdős" --output samples.txt
```

---

## Design notes

* **Why EdgeListReader?** Importing 100 M edges with Python `addEdge` costs minutes; C++ parser is often ×5-×10 faster, then you still save to `.nkbg` for future runs.
* **Why SQLite?** Windows uses `spawn`; big dicts would be copied to every worker. A read‑only `immutable=1` SQLite DB is page‑cached and shared.
* **Schema quirks** - modern dumps use `pl_target_id`→`linktarget`; legacy dumps store `(namespace,title)` directly. Script auto‑detects.
* **Disambiguations** - removed via `page_props.pp_propname='disambiguation'` when `--drop-disambig` is set.
* **RAM bound** - final graph dominates memory: ≈ `(|V|+|E|)·(8-12 bytes)`.

---

## Troubleshooting

* **OOM on Windows** → stay with default low‑mem path; avoid `--fast`.
* **Slow assembly (POSIX)** → add `--use-edgelist-import`.
* **UnicodeDecodeError in page\_props** → script retries with `latin‑1` automatically.
* **Dump lag** - ensure you use the same `date` for all tables.
