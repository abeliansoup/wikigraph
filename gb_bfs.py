"""
GraphBLAS BFS on Wikipedia graph built by your existing pipeline.
Reuses: cache/.../title_index.sqlite and edges.bin. Caches A/AT for fast reloads.

Usage:
  # One-time: build GraphBLAS cache (A, AT) from edges.bin
  python gb_wiki_bfs.py build-grb --cache cache/enwiki-latest

  # Query shortest path using GraphBLAS BFS
  python gb_wiki_bfs.py query --cache cache/enwiki-latest --src "Bandsaw" --dst "Taylor_Swift"
"""
import os
import sys
import json
import struct
import sqlite3
import argparse
import logging
import numpy as np
import graphblas as gb
from pathlib import Path
gb.init("suitesparse", blocking=True)
from graphblas import Matrix, Vector, monoid, binary, semiring, dtypes, agg

CACHE_A = "A.ssgb.npz"       # keep the same names you already use
CACHE_AT = "AT.ssgb.npz"
CACHE_A_FALLBACK = "A.coo.npz"
CACHE_AT_FALLBACK = "AT.coo.npz"

MAIN_NS = 0

# Global logging configuration
ENABLE_LOGGING = False
LOG_LEVEL = logging.INFO

# Configure logging
def setup_logging():
    if ENABLE_LOGGING:
        logging.basicConfig(
            level=LOG_LEVEL,
            format='%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%H:%M:%S'
        )
    else:
        logging.disable(logging.CRITICAL)

logger = logging.getLogger(__name__)
setup_logging()


# ---- SuiteSparse: configure before heavy work (threads, format, logs) ----
# nthreads: set to your machine (24); 'burble'=True prints per-op timings if you want to diagnose
gb.ss.config["nthreads"] = os.cpu_count() or 8
gb.ss.config["format"] = "by_row"      # good default for push (frontier @ A)
# gb.ss.config["burble"] = True        # uncomment to see SuiteSparse diagnostics

def _safe_path(p: str | Path) -> str:
    return Path(p).resolve().as_posix()

from pathlib import Path
import sqlite3
import graphblas as gb

def _ensure_cache_graphblas(cache_dir: Path, *, npz_name: str = "A.coo.npz", sqlite_name: str | None = None):
    """
    Load directed adjacency A from a SciPy .npz (COO/CSR/etc) into GraphBLAS,
    recompute AT = A.T to avoid staleness, and open the titles DB.

    Returns
    -------
    (A, AT, conn)
      A  : gb.Matrix[BOOL], square; A[u, v] == True means u -> v
      AT : gb.Matrix[BOOL], recomputed transpose of A
      conn : sqlite3.Connection (first *.sqlite in cache or `sqlite_name`)
    """
    cache_dir = Path(cache_dir)

    # --- load A from SciPy .npz then convert to GraphBLAS
    npz_path = cache_dir / npz_name
    if not npz_path.exists():
        raise FileNotFoundError(f"Adjacency not found: {npz_path}")

    A = load_matrix_npz(npz_path)
    if A.dtype != gb.dtypes.BOOL: 
        A = A.dup(dtype=gb.dtypes.BOOL)

    if A.nrows != A.ncols:
        raise ValueError(f"Adjacency must be square; got {A.nrows}x{A.ncols}")

    AT = A.T
    assert A.isequal(AT.T), "A != (A.T).T; adjacency mutated during load?"

    # --- open SQLite DB for title<->nid lookups
    if sqlite_name:
        db_path = cache_dir / sqlite_name
        if not db_path.exists():
            raise FileNotFoundError(f"SQLite DB not found: {db_path}")
    else:
        dbs = sorted(cache_dir.glob("*.sqlite"))
        if not dbs:
            raise FileNotFoundError(f"No *.sqlite DB found in {cache_dir}")
        db_path = dbs[0]

    conn = sqlite3.connect(str(db_path))
    return A, AT, conn


# ----------------------------- SQLite helpers -----------------------------
def open_title_db_readonly(db_path: str):
    return sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)

def resolve_title_to_nid(cur, ns: int, title: str):
    row = cur.execute("SELECT nid, page_id FROM title WHERE ns=? AND title=? LIMIT 1", (ns, title)).fetchone()
    if row:
        nid, pid = row[0], row[1]
        rd = cur.execute("SELECT tgt_page_id FROM redirect WHERE src_page_id=?", (pid,)).fetchone()
        if rd:
            nid2 = cur.execute("SELECT nid FROM title WHERE page_id=? LIMIT 1", (rd[0],)).fetchone()
            if nid2:
                return nid2[0]
        return nid
    return None

def nid_to_title(cur, nid: int) -> str:
    r = cur.execute("SELECT title FROM title WHERE nid=? LIMIT 1", (nid,)).fetchone()
    return r[0] if r else f"nid:{nid}"

def titles_to_nids(conn, titles: list[str]) -> list[int]:
    """
    Resolve page titles to node IDs using your SQLite DB.
    Raises ValueError if any title is missing.
    """
    cur = conn.cursor()
    nids = []
    for t in titles:
        cur.execute("SELECT nid FROM title WHERE title = ?", (t,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"unknown title: {t}")
        nids.append(int(row[0]))
    return nids

# ------------------------- Build/load GraphBLAS A, AT ----------------------
def _node_count(db_path: str) -> int:
    with open_title_db_readonly(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM title").fetchone()[0])

def _stream_edges_bin(bin_path: str, block_bytes: int = 256 << 20):
    """Yield (rows, cols) np.uint64 arrays from <u32,u32> pairs in edges.bin"""
    pack_sz = 8
    with open(bin_path, "rb") as f:
        while True:
            buf = f.read(block_bytes)
            if not buf:
                break
            usable = (len(buf) // pack_sz) * pack_sz
            if usable == 0:
                continue
            arr = np.frombuffer(memoryview(buf)[:usable], dtype="<u4")
            arr = arr.astype(np.uint64, copy=False)
            rows = arr[0::2]
            cols = arr[1::2]
            yield rows, cols

def _reconstruct_path_from_parent(parent: Vector, src: int, dst: int, *, max_steps=None):
    # Follow parent pointers until we reach src (or fail)
    path = [dst]
    v = dst
    steps = 0
    while v != src:
        pv = parent[v].value
        if pv is None:
            logger.warning(f"[recon] missing parent for {v}; abort")
            return []
        path.append(pv)
        v = pv
        steps += 1
        if max_steps is not None and steps > max_steps + 5:  # sanity guard
            logger.warning(f"[recon] too many steps ({steps}); abort")
            return []
    path.reverse()
    ok = (path[0] == src and path[-1] == dst)
    logger.debug(f"[recon] len={len(path)} ok={ok}")
    return path if ok else []

def _validate_path(A: gb.Matrix, path: list[int]) -> bool:
    """Quick structural check that every consecutive (u,v) is an edge in A."""
    for u, v in zip(path, path[1:]):
        s = A[u, v]
        if not (s is not None and getattr(s, "value", False)):
            return False
    return True

def verify_path_by_nids_gb(A: gb.Matrix, path_nids: list[int]) -> dict:
    if not path_nids or len(path_nids) < 2:
        return {"ok": False, "msg": "path must contain at least two nodes", "fail_index": 0}
    n = A.nrows
    for i in range(len(path_nids) - 1):
        u, v = path_nids[i], path_nids[i + 1]
        if u < 0 or v < 0 or u >= n or v >= n:
            return {"ok": False, "msg": f"invalid node id at step {i}: {u}->{v}", "fail_index": i}
        if not bool(A.get(u, v, default=False)):
            return {"ok": False, "msg": f"missing directed edge at step {i}: {u}->{v}", "fail_index": i}
    return {"ok": True, "msg": f"valid path of length {len(path_nids) - 1}", "fail_index": None}

def verify_path_by_titles_gb(conn, A: gb.Matrix, titles: list[str]) -> dict:
    try:
        nids = titles_to_nids(conn, titles)
    except ValueError as e:
        return {"ok": False, "msg": str(e), "fail_index": 0}
    return verify_path_by_nids_gb(A, nids)

def build_grb_from_edges(edges_bin: str, n: int, *, tile_pairs: int = 25_000_000) -> Matrix:
    """
    Load <u32,u32> pairs from edges.bin into a BOOL adjacency A (n x n),
    using memory-mapped, pair-aligned tiles. Ensures no pair straddling loss.
    Verifies parity against file size and logs basic stats.
    """
    # 1) File sanity + memmap as uint32; every two uint32 = one edge
    fsz_bytes = os.path.getsize(edges_bin)
    assert fsz_bytes % 8 == 0, f"edges.bin not pair-aligned: size={fsz_bytes}"
    total_pairs = fsz_bytes // 8
    mm = np.memmap(edges_bin, mode="r", dtype="<u4", shape=(total_pairs * 2,))  # little-endian u32
    pairs = mm.reshape(-1, 2)  # shape = (total_pairs, 2)

    # 2) Build A by tiles to keep memory bounded; use int64 indices (boring, safe)
    A = Matrix(dtypes.BOOL, nrows=n, ncols=n, name="A")
    start = 0
    while start < total_pairs:
        stop = min(start + tile_pairs, total_pairs)
        chunk = pairs[start:stop]  # (m, 2), zero-copy view
        rows = chunk[:, 0].astype(np.int64, copy=False)
        cols = chunk[:, 1].astype(np.int64, copy=False)

        # Optional: quick bounds guard (cheap and catches mapping issues fast)
        if rows.max(initial=0) >= n or cols.max(initial=0) >= n:
            bad = int(max(rows.max(initial=0), cols.max(initial=0)))
            raise ValueError(f"Edge index {bad} >= n={n}")

        # Collapse dups inside the tile, then OR-accumulate into A
        vals = np.ones(rows.shape[0], dtype=np.bool_)
        B = Matrix.from_coo(rows, cols, vals, nrows=n, ncols=n, dup_op=binary.lor)
        A(binary.lor) << B

        start = stop

    # 3) Parity log: A.nvals is unique directed edges; file pairs include duplicates.
    logger.info(f"[GRB] built A: n={n:,}  A.nvals={A.nvals:,}  file_pairs={total_pairs:,}")
    return A

def save_matrix_npz(A: Matrix, path: str | Path):
    rows, cols, vals = A.to_coo(rows=True, columns=True, values=True, sort=False)
    np.savez_compressed(
        Path(path),
        rows=rows.astype(np.int64, copy=False),
        cols=cols.astype(np.int64, copy=False),
        vals=vals.astype(np.bool_, copy=False),
        nrows=np.array([A.nrows], dtype=np.int64),
        ncols=np.array([A.ncols], dtype=np.int64),
    )

def load_matrix_npz(path: str | Path) -> Matrix:
    z = np.load(Path(path), allow_pickle=False)
    A = Matrix(
        dtypes.BOOL,
        nrows=int(z["nrows"][0]),
        ncols=int(z["ncols"][0]),
        name="A",
    )
    A.build(z["rows"], z["cols"], z["vals"], dup_op=gb.binary.lor)
    return A

def load_or_create_A_AT(cache_dir: str):
    cache_dir = Path(cache_dir)
    nkbg    = cache_dir / "graph.nkbg"
    titledb = cache_dir / "title_index.sqlite"
    edges   = cache_dir / "edges.bin"

    # Preferred (fast) SuiteSparse paths, sanitized
    a_ss   = _safe_path(cache_dir / CACHE_A)
    at_ss  = _safe_path(cache_dir / CACHE_AT)
    # Fallback NPZ (COO) paths
    a_npz  = cache_dir / CACHE_A_FALLBACK
    at_npz = cache_dir / CACHE_AT_FALLBACK

    # --- Load or build A ---
    A = None

    # Try SuiteSparse import
    try:
        if os.path.exists(a_ss):
            A = Matrix.ss.import_any(a_ss)
    except Exception:
        A = None
    # Try NPZ fallback
    if A is None and a_npz.exists():
        A = load_matrix_npz(a_npz)
    # Build from edges if still missing
    if A is None:
        n = _node_count(str(titledb))
        A = build_grb_from_edges(str(edges), n)
        # Try to save via SuiteSparse first
        try:
            Matrix.ss.export(A, a_ss) if hasattr(Matrix, "ss") else A.ss.export(a_ss)  # either style
        except Exception:
            save_matrix_npz(A, a_npz)  # safe fallback

    # --- Load or build AT ---
    AT = A.T.new()

    return A, AT
# ----------------------------- BFS (GraphBLAS) -----------------------------
def bfs_shortest_path(A, AT, src: int, dst: int, max_depth: int | None = None):
    """
    Direction-optimizing BFS with early exit when `dst` is reached.
    Records parent pointers using index-propagating semirings to avoid implicit-zero pitfalls.
    Returns nid path [src..dst] or [] if unreachable.
    """
    n = A.nrows
    if src == dst:
        return [src]

    # --- State
    frontier = Vector(dtypes.BOOL, size=n)
    visited  = Vector(dtypes.BOOL, size=n)
    parent   = Vector(dtypes.INT64, size=n)      # store one parent id per discovered node
    dist     = Vector(dtypes.UINT32, size=n)     # keep for debugging only

    frontier[src] = True
    visited[src]  = True
    parent[src]   = src
    dist[src]     = 0

    # Heuristic bookkeeping
    outdeg = agg.count(A, rowwise=True).new()
    rem_edges = A.nvals

    logger.info(f"SRC: {src} DST: {dst}")
    logger.debug(f"[init] n={n} edges={A.nvals} src={src} dst={dst} max_depth={max_depth}")
    logger.debug(f"[init] outdeg(src)={A[src, :].new().nvals}")

    # quick parity / sanity probe
    probe = Vector(dtypes.BOOL, size=n); probe[src] = True
    push_1 = semiring.lor_pair(probe @ A).new()
    pull_1 = semiring.lor_pair(AT @ probe).new() if AT is not None else push_1.dup()
    logger.debug(f"[init] push_1={push_1.nvals} pull_1={pull_1.nvals} sets_equal={push_1.isequal(pull_1)}")

    k = 0
    while frontier.nvals:
        # Early exit if dst is already in current frontier
        if frontier[dst].value:
            logger.info(f"[end] found dst in current frontier at k={k}")
            return _reconstruct_path_from_parent(parent, src, dst, max_steps=k)

        # stamp distance for current frontier (level k)
        dist(mask=frontier.S) << k

        # Mark frontier as visited
        visited << (visited | frontier).new()

        # Heuristic: estimate work = sum(outdeg[u] for u in frontier)
        f_int = frontier.dup(dtype=outdeg.dtype)
        work  = outdeg.ewise_mult(f_int, binary.times).reduce(binary.plus).new().value
        use_pull = (AT is not None) and (work > rem_edges // 14)

        # Raw expansions just to log parity
        raw_push = semiring.lor_pair(frontier @ A).new()
        raw_pull = semiring.lor_pair(AT @ frontier).new() if AT is not None else raw_push
        logger.debug(f"[k={k}] frontier={frontier.nvals} visited={visited.nvals} work={work} rem={rem_edges} use_pull={use_pull}")
        logger.debug(f"       raw parity: push={raw_push.nvals} pull={raw_pull.nvals} equal={raw_push.isequal(raw_pull)}")

        # Expand one level with complement mask applied to the multiply
        frontier_next = Vector(dtypes.BOOL, size=n)
        if use_pull:
            # New neighbors and parents via incoming edges
            frontier_next(mask=~visited.S, replace=True) << semiring.lor_pair(AT @ frontier)
            # Capture one parent id per discovered node: ANY(SECONDI(A^T, frontier)) -> parent=k (frontier index)
            par_candidates = semiring.any_secondi(AT @ frontier).new()
        else:
            # Push mode
            frontier_next(mask=~visited.S, replace=True) << semiring.lor_pair(frontier @ A)
            # Capture parent ids when pushing: ANY(FIRSTJ(frontier, A)) -> parent=j (frontier index)
            par_candidates = semiring.ss.any_firstj(frontier @ A).new()

        # If nothing new, stop expanding
        mask_kill = (raw_push.nvals if not use_pull else raw_pull.nvals) - frontier_next.nvals
        logger.debug(f"       mask-kill: next={frontier_next.nvals} (~visited≈{n - visited.nvals})")
        if frontier_next.nvals == 0:
            break

        # Assign parents for *newly discovered* nodes only (don't overwrite existing)
        # Note: we mask by frontier_next.S to ensure we only set parents for the fresh layer.
        parent(mask=frontier_next.S, replace=False) << par_candidates

        # Assign distance for newly discovered nodes (level k+1)
        k += 1
        dist(mask=frontier_next.S) << k
        logger.debug(f"[advance] to k={k} | frontier_next={frontier_next.nvals} visited={visited.nvals}")

        # Early exit as soon as dst is discovered at the new level
        if frontier_next[dst].value:
            logger.info(f"[end] found dst in next frontier at k={k}")
            return _reconstruct_path_from_parent(parent, src, dst, max_steps=k)

        if max_depth is not None and k >= max_depth:
            logger.info(f"[end] reached max_depth={max_depth}")
            break

        frontier = frontier_next
        rem_edges = max(0, rem_edges - work)

    if not visited[dst].value:
        logger.info(f"[end] no path; levels discovered={k}; visited={visited.nvals}")
        print("[!] No path found.")
        return []

    # Fallback: we discovered dst earlier but didn’t early-exit
    return _reconstruct_path_from_parent(parent, src, dst, max_steps=k)

def exists_bidirectional(
    A: gb.Matrix, AT: gb.Matrix, src: int, dst: int, *, debug: bool = True, use_symmetrized: bool = False
) -> bool:
    if src == dst:
        return True
    if use_symmetrized:
        A = (A | A.T).new()
    AT = A.T

    ANY_PAIR = gb.semiring.lor_pair

    f_fwd = gb.Vector(dtypes.BOOL, size=A.nrows); f_fwd[src] = True
    f_bwd = gb.Vector(dtypes.BOOL, size=A.nrows); f_bwd[dst] = True
    v_fwd = f_fwd.dup()
    v_bwd = f_bwd.dup()
    k = 0
    while f_fwd.nvals and f_bwd.nvals:
        # Meet?
        if (f_fwd & v_bwd).nvals or (f_bwd & v_fwd).nvals:
            logger.debug(f"[bi] meet at k={k} (|f_fwd|={f_fwd.nvals}, |f_bwd|={f_bwd.nvals})")
            return True
        # Expand the smaller side
        expand_fwd = f_fwd.nvals <= f_bwd.nvals
        if expand_fwd:
            nxt = gb.Vector(dtypes.BOOL, size=A.nrows)
            nxt(mask=~v_fwd.V, replace=True) << ANY_PAIR(AT @ f_fwd)  # successors
            if nxt.nvals == 0:
                return False
            v_fwd |= nxt; f_fwd = nxt
        else:
            nxt = gb.Vector(dtypes.BOOL, size=A.nrows)
            nxt(mask=~v_bwd.V, replace=True) << ANY_PAIR(A @ f_bwd)   # predecessors (i.e., successors in reverse graph)
            if nxt.nvals == 0:
                return False
            v_bwd |= nxt; f_bwd = nxt
        k += 1
        if debug and (k % 2 == 0):
            logger.debug(f"[bi] k={k} | |f_fwd|={f_fwd.nvals} |f_bwd|={f_bwd.nvals}")
    return False

def verify_path_by_nids_gb(A: gb.Matrix, path_nids: list[int]) -> dict:
    """
    Validate a path [n0, n1, ..., nk] against *directed* graph A (BOOL).
    Returns dict like your NetworKit version: ok, msg, fail_index.
    """
    if not path_nids or len(path_nids) < 2:
        return {"ok": False, "msg": "path must contain at least two nodes", "fail_index": 0}

    n = A.nrows
    for i in range(len(path_nids) - 1):
        u, v = path_nids[i], path_nids[i + 1]
        # bounds (GraphBLAS doesn't have hasNode; sparsity is allowed)
        if u < 0 or v < 0 or u >= n or v >= n:
            return {"ok": False, "msg": f"invalid node id at step {i}: {u}->{v}", "fail_index": i}

        # single-element lookup; returns Python scalar (or default if absent)
        exists = bool(A.get(u, v, default=False))
        # (Equivalently: s = A[u, v]; exists = s is not None and bool(getattr(s, "value", False)))
        # Doc: element access / .get(row, col, default=...) returns a Python scalar. :contentReference[oaicite:0]{index=0}
        if not exists:
            return {"ok": False, "msg": f"missing directed edge at step {i}: {u}->{v}", "fail_index": i}

    return {"ok": True, "msg": f"valid path of length {len(path_nids) - 1}", "fail_index": None}

def verify_path_by_nids_gb_batch(A: gb.Matrix, path_nids: list[int]) -> dict:
    """
    Batch-verify all consecutive edges of path_nids on directed A (BOOL).
    Creates a sparse mask M with ones at each (u_i, v_i) and tests A ∧ M.
    """
    if not path_nids or len(path_nids) < 2:
        return {"ok": False, "msg": "path must contain at least two nodes", "fail_index": 0}

    n = A.nrows
    rows = path_nids[:-1]
    cols = path_nids[1:]

    # bounds check first
    for i, (u, v) in enumerate(zip(rows, cols)):
        if u < 0 or v < 0 or u >= n or v >= n:
            return {"ok": False, "msg": f"invalid node id at step {i}: {u}->{v}", "fail_index": i}

    m = len(rows)
    # Build a sparse mask with just the path’s edges (no large memory: only m entries)
    # Matrix/Vector have .from_coo() for constructing from coordinate lists. :contentReference[oaicite:1]{index=1}
    M = gb.Matrix.from_coo(rows, cols, [True] * m, nrows=n, ncols=A.ncols, dtype=dtypes.BOOL)

    # Intersection: C has entries only where both A and M have structure.
    C = (A & M).new()  # eWiseMult on BOOL acts as structural AND (see ops docs). :contentReference[oaicite:2]{index=2}
    if C.nvals == m:
        return {"ok": True, "msg": f"valid path of length {m}", "fail_index": None}

    # Identify first missing edge: symmetric difference M ^ C == (edges requested) minus (edges found)
    missing = (M ^ C).new()
    # Get one coordinate back to an index in the path
    miss_r, miss_c, _ = missing.to_coo()  # to_coo for coordinates/values. :contentReference[oaicite:3]{index=3}
    # Map (u,v) -> i
    pos = { (u, v): i for i, (u, v) in enumerate(zip(rows, cols)) }
    i = pos.get((int(miss_r[0]), int(miss_c[0])), 0)
    return {"ok": False, "msg": f"missing directed edge at step {i}: {rows[i]}->{cols[i]}", "fail_index": i}


# ----------------------------- CLI -----------------------------------------
def cmd_build_grb(args):
    cache = args.cache
    A, AT = load_or_create_A_AT(cache)
    print(f"[✓] Built GraphBLAS cache: A={A.nvals:,} edges, AT ready.")

def cmd_query(args):
    cache = args.cache
    titledb = os.path.join(cache, "title_index.sqlite")
    A, AT = load_or_create_A_AT(cache)
    with open_title_db_readonly(titledb) as conn:
        cur = conn.cursor()
        src = resolve_title_to_nid(cur, MAIN_NS, args.src)
        dst = resolve_title_to_nid(cur, MAIN_NS, args.dst)
        logger.info(f"SRC: {src} {nid_to_title(cur, src)} DST: {dst} {nid_to_title(cur, dst)}")
        if src is None or dst is None:
            print("[!] Could not resolve one or both titles."); return
        path = bfs_shortest_path(A, AT, src, dst)
        if not path:
            print("[!] No path found."); return
        titles = [nid_to_title(cur, nid) for nid in path]
        print(f"[+] Path ({len(path)-1} hops): " + " -> ".join(titles))
        return titles

def cmd_verify(args):
    cache = Path(args.cache)

    # Uses your GB loader that reads A.coo.npz and opens the DB
    A, AT, conn = _ensure_cache_graphblas(cache)  # defined earlier
    if args.nids:
        path = [int(x) for x in args.nids.split(",") if x.strip()]
        res = verify_path_by_nids_gb(A, path)
    else:
        titles = [t.strip() for t in args.titles.split(",") if t.strip()]
        res = verify_path_by_titles_gb(conn, A, titles)

    print(json.dumps(res, ensure_ascii=False))

def main():
    ap = argparse.ArgumentParser(prog="gb_bfs.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ap_b = sub.add_parser("build-grb", help="Build/load GraphBLAS A/AT from cache")
    ap_b.add_argument("--cache", required=True)
    ap_b.set_defaults(func=cmd_build_grb)

    ap_q = sub.add_parser("query", help="Query shortest path with GraphBLAS BFS")
    ap_q.add_argument("--cache", required=True)
    ap_q.add_argument("--src", required=True)
    ap_q.add_argument("--dst", required=True)
    ap_q.set_defaults(func=cmd_query)

    ap_verify = sub.add_parser("verify", help="Verify that a given path is valid")
    ap_verify.add_argument("--cache", required=True)
    g = ap_verify.add_mutually_exclusive_group(required=True)
    g.add_argument("--titles", help="Comma-separated titles")
    g.add_argument("--nids", help="Comma-separated nids")
    ap_verify.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
