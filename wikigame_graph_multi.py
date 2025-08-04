"""
Wikipedia link-graph builder & query tool (cached).

Features
- Build once from dumps -> graph.nkbg + title_index.sqlite
- On subsequent runs, skip dumps and load cached artifacts
- Exact shortest paths via Bidirectional BFS (NetworKit, native-speed)
- Optional C++ import path via EdgeListReader for faster final assembly
- Utility to convert legacy graph.bin -> graph.nkbg (+ mappings.pkl)

Install:
  pip install mwsql networkit tqdm psutil

Examples:
  # Build once (Windows default: low-mem; Linux/macOS default: fast POSIX):
  python wikigame_graph_eve.py build --wiki enwiki --date latest --cache cache/enwiki-latest --drop-disambig

  # Faster final assembly (both paths) using C++ EdgeListReader:
  python wikigame_graph_eve.py build --wiki enwiki --date latest --cache cache/enwiki-latest --use-edgelist-import

  # Query from cache (no dumps):
  python wikigame_graph_eve.py query --cache cache/enwiki-latest --src "Bandsaw" --dst "Taylor_Swift"

  # Sample 5 reachable pairs:
  python wikigame_graph_eve.py sample --cache cache/enwiki-latest --count 5

  # Convert old pickle -> nkbg:
  python wikigame_graph_eve.py convert --in graph.bin --out cache/enwiki-latest/graph.nkbg --map-out cache/enwiki-latest/mappings.pkl
"""

import os
import math
import json
import pickle
import struct
import psutil
import random
import sqlite3
import platform
import argparse
from tqdm import tqdm
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
import networkit as nk
from functools import lru_cache
from mwsql import Dump as MwDump
from mwsql import load as mw_load
from multiprocessing import get_context

MAIN_NS = 0

from rindex import SCCReachabilityIndex
from gb_bfs import cmd_query
from c_helpers import (
    bin_to_edgelist,
    shards_to_edgelist,
    build_graph_via_edgelist
)




# ----------------------------- Low-memory SQLite builder -----------------------
def _sqlite_exec_many(cur, sql, rows, batch=100000):
    buf = []
    for r in rows:
        buf.append(r)
        if len(buf) >= batch:
            cur.executemany(sql, buf); buf.clear()
    if buf:
        cur.executemany(sql, buf)

def build_title_db_lowmem(page_sql: Path, page_props_sql: Optional[Path],
                          drop_disambig: bool, db_path: Path):
    """
    Build a disk index of main-namespace pages with contiguous nids, optionally
    dropping disambiguation pages. No large in-memory dicts.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.executescript("""
      PRAGMA journal_mode=OFF;
      PRAGMA synchronous=OFF;
      DROP TABLE IF EXISTS title_tmp;
      CREATE TABLE title_tmp(
        page_id INTEGER PRIMARY KEY,
        ns INT NOT NULL,
        title TEXT NOT NULL,
        is_redirect INT NOT NULL,
        keep INT NOT NULL DEFAULT 1
      );
      CREATE INDEX IF NOT EXISTS idx_title_tmp_ns_title ON title_tmp(ns, title);
    """)
    # Stream 'page'
    d = MwDump.from_file(str(page_sql))
    cols = d.col_names
    i_ns, i_title, i_id, i_redir = (cols.index("page_namespace"),
                                    cols.index("page_title"),
                                    cols.index("page_id"),
                                    cols.index("page_is_redirect"))
    batch = []
    for row in d.rows(convert_dtypes=True):
        if row[i_ns] == MAIN_NS:
            batch.append((row[i_id], row[i_ns], row[i_title], row[i_redir], 1))
            if len(batch) >= 100_000:
                cur.executemany(
                    "INSERT OR REPLACE INTO title_tmp(page_id, ns, title, is_redirect, keep) VALUES (?,?,?,?,?)", batch)
                batch.clear()
    if batch:
        cur.executemany(
            "INSERT OR REPLACE INTO title_tmp(page_id, ns, title, is_redirect, keep) VALUES (?,?,?,?,?)", batch)
    conn.commit()

    # Drop disambiguations, streaming page_props to avoid RAM spikes.
    if drop_disambig and page_props_sql:
        d2 = MwDump.from_file(str(page_props_sql))
        d2.encoding = "utf-8"
        try:
            rows_iter = d2.rows(convert_dtypes=True)
            cols2 = d2.col_names
            i_page = cols2.index("pp_page"); i_prop = cols2.index("pp_propname")
            batch = []
            for r in rows_iter:
                if r[i_prop] == "disambiguation":
                    batch.append((r[i_page],))
                    if len(batch) >= 200_000:
                        cur.executemany("UPDATE title_tmp SET keep=0 WHERE page_id=?", batch); batch.clear()
            if batch:
                cur.executemany("UPDATE title_tmp SET keep=0 WHERE page_id=?", batch)
        except UnicodeDecodeError:
            d2.encoding = "latin-1"
            # re-run quickly; content of pp_value not needed
            rows_iter = d2.rows(convert_dtypes=True)
            cols2 = d2.col_names
            i_page = cols2.index("pp_page"); i_prop = cols2.index("pp_propname")
            for r in rows_iter:
                if r[i_prop] == "disambiguation":
                    cur.execute("UPDATE title_tmp SET keep=0 WHERE page_id=?", (r[i_page],))
        conn.commit()

    # Materialize contiguous nids
    cur.executescript("""
      DROP TABLE IF EXISTS title;
      CREATE TABLE title(
        page_id INTEGER PRIMARY KEY,
        ns INT NOT NULL,
        title TEXT NOT NULL,
        nid INT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_title_ns_title ON title(ns, title);
      CREATE INDEX IF NOT EXISTS idx_title_nid ON title(nid);
    """)
    cur.execute("DROP TABLE IF EXISTS title_kept;")
    cur.executescript("""
      CREATE TABLE title_kept(
        nid INTEGER PRIMARY KEY AUTOINCREMENT,
        page_id INT UNIQUE,
        ns INT,
        title TEXT
      );
    """)
    cur.execute("INSERT INTO title_kept(page_id, ns, title) "
                "SELECT page_id, ns, title FROM title_tmp WHERE keep=1 ORDER BY page_id;")
    cur.execute("INSERT INTO title(page_id, ns, title, nid) "
                "SELECT page_id, ns, title, nid FROM title_kept;")
    cur.execute("DROP TABLE title_tmp;")
    conn.commit()
    conn.close()

def load_redirect_into_db(redirect_sql: Path, db_path: Path):
    """
    Load redirects into SQLite (src_page_id -> tgt_page_id via ns/title lookups).
    """
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.executescript("""
      CREATE TABLE IF NOT EXISTS redirect(
        src_page_id INTEGER PRIMARY KEY,
        tgt_page_id INTEGER
      );
      CREATE INDEX IF NOT EXISTS idx_redirect_src ON redirect(src_page_id);
    """)
    d = MwDump.from_file(str(redirect_sql))
    cols = d.col_names
    i_from, i_ns, i_title = cols.index("rd_from"), cols.index("rd_namespace"), cols.index("rd_title")
    batch = []
    for row in d.rows(convert_dtypes=True):
        tgt_ns, tgt_title = row[i_ns], row[i_title]
        # map (ns,title) -> page_id if it exists in title
        pid_row = cur.execute("SELECT page_id FROM title WHERE ns=? AND title=? LIMIT 1",
                              (tgt_ns, tgt_title)).fetchone()
        if pid_row:
            batch.append((row[i_from], pid_row[0]))
            if len(batch) >= 100_000:
                cur.executemany("INSERT OR REPLACE INTO redirect(src_page_id, tgt_page_id) VALUES (?,?)", batch); batch.clear()
    if batch:
        cur.executemany("INSERT OR REPLACE INTO redirect(src_page_id, tgt_page_id) VALUES (?,?)", batch)
    conn.commit(); conn.close()

def build_linktarget2pid_table(linktarget_sql: Optional[Path], db_path: Path):
    """
    If modern schema is present, precompute lt_id -> pid in SQLite so workers
    don't need a large Python dict.
    """
    if not linktarget_sql:
        return
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.executescript("""
      CREATE TABLE IF NOT EXISTS linktarget2pid(
        lt_id INTEGER PRIMARY KEY,
        pid INTEGER
      );
    """)
    d = MwDump.from_file(str(linktarget_sql))
    cols = d.col_names
    i_id, i_ns, i_title = cols.index("lt_id"), cols.index("lt_namespace"), cols.index("lt_title")
    batch = []
    for row in d.rows(convert_dtypes=True):
        if row[i_ns] != MAIN_NS:
            continue
        r = cur.execute("SELECT page_id FROM title WHERE ns=? AND title=? LIMIT 1", (row[i_ns], row[i_title])).fetchone()
        if r:
            batch.append((row[i_id], r[0]))
            if len(batch) >= 100_000:
                cur.executemany("INSERT OR REPLACE INTO linktarget2pid(lt_id, pid) VALUES (?,?)", batch); batch.clear()
    if batch:
        cur.executemany("INSERT OR REPLACE INTO linktarget2pid(lt_id, pid) VALUES (?,?)", batch)
    conn.commit(); conn.close()

def extract_edges_to_bin_lowmem(pagelinks_sql: Path,
                                db_path: Path,
                                out_bin: Path,
                                *,
                                also_build: bool = False,
                                edgelist_path: Optional[Path] = None,
                                nkbg_out: Optional[Path] = None,
                                directed: bool = True,
                                block_bytes: int = 64 << 20
                                ) -> Tuple[Path, Optional[Path], Optional[nk.Graph]]:
    """
    Low-RAM extractor:
      1) Streams pagelinks, resolves (src_pid, tgt) via SQLite (read-only, immutable),
         follows redirects, maps to contiguous nids, writes <u32,u32> pairs to edges.bin.
      2) (Optional) Converts edges.bin -> text edgelist and builds the graph in C++ via
         EdgeListReader, padding nodes to include isolates, and writes .nkbg.

    Returns: (edges_bin_path, edges_txt_path_or_None, Graph_or_None)
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    cur = conn.cursor()

    @lru_cache(maxsize=1_000_000)
    def pid_to_nid(pid: int) -> Optional[int]:
        r = cur.execute("SELECT nid FROM title WHERE page_id=? LIMIT 1", (pid,)).fetchone()
        return r[0] if r else None

    @lru_cache(maxsize=1_000_000)
    def resolve_redirect(pid: int) -> int:
        seen = set()
        cur_pid = pid
        while True:
            if cur_pid in seen:
                return cur_pid
            seen.add(cur_pid)
            r = cur.execute("SELECT tgt_page_id FROM redirect WHERE src_page_id=? LIMIT 1", (cur_pid,)).fetchone()
            if not r or r[0] is None:
                return cur_pid
            cur_pid = int(r[0])

    # Detect schema (legacy vs pl_target_id)
    d = MwDump.from_file(str(pagelinks_sql))
    cols = d.col_names
    has_target_id = "pl_target_id" in cols
    i_from = cols.index("pl_from")
    i_tgt_id = cols.index("pl_target_id") if has_target_id else -1
    i_tgt_ns = cols.index("pl_namespace") if not has_target_id else -1
    i_tgt_title = cols.index("pl_title") if not has_target_id else -1

    out_bin.parent.mkdir(parents=True, exist_ok=True)
    pack = struct.Struct("<II").pack
    written_edges = 0

    with tqdm(total=None, smoothing=0.05, mininterval=0.5, desc="Pagelinks → edges.bin") as pbar, \
         open(out_bin, "wb", buffering=1024*1024) as fout:

        for row in d.rows(convert_dtypes=True):
            src_pid = row[i_from]
            u = pid_to_nid(resolve_redirect(src_pid))
            if u is None:
                continue

            if has_target_id:
                r = cur.execute("SELECT pid FROM linktarget2pid WHERE lt_id=? LIMIT 1", (row[i_tgt_id],)).fetchone()
                if not r:
                    continue
                tgt_pid = r[0]
            else:
                tgt_ns, tgt_title = row[i_tgt_ns], row[i_tgt_title]
                r = cur.execute("SELECT page_id FROM title WHERE ns=? AND title=? LIMIT 1",
                                (tgt_ns, tgt_title)).fetchone()
                if not r:
                    continue
                tgt_pid = r[0]

            v = pid_to_nid(resolve_redirect(tgt_pid))
            if v is None or u == v:
                continue

            fout.write(pack(u, v))
            written_edges += 1
            if written_edges % 200_000 == 0:
                pbar.update(200_000)

        # flush progress for any remainder
        if written_edges % 200_000 != 0:
            pbar.update(written_edges % 200_000)

    # Optional: finish the build via C++ importer
    edges_txt = None
    built_graph = None
    if also_build:
        # Determine expected node count so isolates exist after import
        r = cur.execute("SELECT COUNT(*) FROM title").fetchone()
        expected_nodes = int(r[0]) if r else None

        if edgelist_path is None:
            edgelist_path = out_bin.with_suffix(".txt")

        edges_txt = bin_to_edgelist(out_bin, edgelist_path, block_bytes=block_bytes)
        built_graph = build_graph_via_edgelist(edges_txt, nkbg_out,
                                               directed=directed,
                                               expected_nodes=expected_nodes)
    conn.close()
    return out_bin, edges_txt, built_graph

# ----------------------------- Graph assembly ----------------------------------
def assemble_shards_to_graph(G, shard_paths, block_bytes=64 << 20):
    """Stream shards (<u32,u32>) into G without ever holding a full shard in RAM."""
    total_bytes = sum(os.path.getsize(p) for p in shard_paths if os.path.exists(p))
    with tqdm(total=total_bytes, unit="B", unit_scale=True, desc="Assembling shards") as pbar:
        for p in shard_paths:
            if not os.path.exists(p):
                continue
            rem = b""
            with open(p, "rb") as f:
                while True:
                    buf = rem + f.read(block_bytes)
                    if not buf:
                        break
                    # consume full 8-byte edge pairs; carry remainder
                    usable = (len(buf) // 8) * 8
                    for u, v in struct.iter_unpack("<II", memoryview(buf)[:usable]):
                        G.addEdge(u, v)
                    rem = buf[usable:]
                    pbar.update(usable)

# ----------------------------- Dump helpers -----------------------------------
def download_table(wiki: str, table: str, date: str, outdir: Path) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    if date == "latest":
        fpath = mw_load(wiki, table)
    else:
        fpath = mw_load(wiki, table, date)
    fpath = Path(fpath)
    dest = outdir / fpath.name
    if not dest.exists():
        dest.write_bytes(fpath.read_bytes())
    return dest

def load_pages(page_sql: Path) -> Tuple[Dict[int, Tuple[int, str, int]], Dict[Tuple[int, str], int]]:
    d = MwDump.from_file(str(page_sql))
    cols = d.col_names
    i_ns = cols.index("page_namespace")
    i_title = cols.index("page_title")
    i_id = cols.index("page_id")
    i_redir = cols.index("page_is_redirect")

    pid_to_tuple: Dict[int, Tuple[int, str, int]] = {}
    key_to_pid: Dict[Tuple[int, str], int] = {}
    for row in d.rows(convert_dtypes=True):
        ns = row[i_ns]
        if ns != MAIN_NS:
            continue
        pid = row[i_id]
        title = row[i_title]
        is_redir = row[i_redir]
        pid_to_tuple[pid] = (ns, title, is_redir)
        key_to_pid[(ns, title)] = pid
    return pid_to_tuple, key_to_pid

def load_disambigs(page_props_sql: Path) -> set[int]:
    d = MwDump.from_file(str(page_props_sql))
    d.encoding = "utf-8"  # default; may fail on some rows in page_props
    cols = d.col_names
    i_page = cols.index("pp_page")
    i_prop = cols.index("pp_propname")

    def gather() -> set[int]:
        out = set()
        for row in d.rows(convert_dtypes=True):
            if row[i_prop] == "disambiguation":
                out.add(row[i_page])
        return out

    try:
        return gather()
    except UnicodeDecodeError:
        # Some dumps contain bytes not valid in UTF-8 (pp_value is a BLOB).
        # mwsql recommends switching encoding when this happens.
        d.encoding = "latin-1"
        return gather()

def load_redirects(redirect_sql: Path, key_to_pid: Dict[Tuple[int, str], int]) -> Dict[int, int]:
    d = MwDump.from_file(str(redirect_sql))
    cols = d.col_names
    i_from = cols.index("rd_from")
    i_ns = cols.index("rd_namespace")
    i_title = cols.index("rd_title")

    rd = {}
    for row in d.rows(convert_dtypes=True):
        src = row[i_from]
        tgt_ns = row[i_ns]
        tgt_title = row[i_title]
        pid = key_to_pid.get((tgt_ns, tgt_title))
        if pid is not None:
            rd[src] = pid
    return rd

def compress_redirects(pid_to_tuple: Dict[int, Tuple[int, str, int]],
                       redirect_map: Dict[int, int]) -> Dict[int, int]:
    final = {}
    def find(pid: int) -> int:
        seen = []
        cur = pid
        while cur in redirect_map:
            seen.append(cur)
            nxt = redirect_map[cur]
            if nxt == cur or nxt in seen:
                break
            cur = nxt
        for s in seen:
            final[s] = cur
        return cur
    for pid, (_ns, _title, is_redir) in pid_to_tuple.items():
        if is_redir:
            find(pid)
    return final

def load_linktarget(linktarget_sql: Optional[Path]) -> Optional[Dict[int, Tuple[int, str]]]:
    if not linktarget_sql:
        return None
    d = MwDump.from_file(str(linktarget_sql))
    cols = d.col_names
    i_id = cols.index("lt_id")
    i_ns = cols.index("lt_namespace")
    i_title = cols.index("lt_title")
    m = {}
    for row in d.rows(convert_dtypes=True):
        m[row[i_id]] = (row[i_ns], row[i_title])
    return m

def build_edges_pagelinks(pagelinks_sql: Path,
                          pid_to_tuple: Dict[int, Tuple[int, str, int]],
                          key_to_pid: Dict[Tuple[int, str], int],
                          redirect_final: Dict[int, int],
                          linktarget_map: Optional[Dict[int, Tuple[int, str]]] = None
                          ) -> Iterable[Tuple[int, int]]:
    d = MwDump.from_file(str(pagelinks_sql))
    cols = d.col_names
    has_target_id = "pl_target_id" in cols
    i_from = cols.index("pl_from")
    if has_target_id:
        i_tgt_id = cols.index("pl_target_id")
    else:
        i_tgt_ns = cols.index("pl_namespace")
        i_tgt_title = cols.index("pl_title")

    for row in d.rows(convert_dtypes=True):
        src_pid = row[i_from]
        if src_pid not in pid_to_tuple:
            continue
        if has_target_id:
            lt_id = row[i_tgt_id]
            if linktarget_map is None or lt_id not in linktarget_map:
                continue
            tgt_ns, tgt_title = linktarget_map[lt_id]
        else:
            tgt_ns, tgt_title = row[i_tgt_ns], row[i_tgt_title]
        if tgt_ns != MAIN_NS:
            continue
        tgt_pid = key_to_pid.get((tgt_ns, tgt_title))
        if tgt_pid is None:
            continue  # redlink
        tgt_pid = redirect_final.get(tgt_pid, tgt_pid)
        src_eff = redirect_final.get(src_pid, src_pid)
        if src_eff != tgt_pid:
            yield (src_eff, tgt_pid)

# ----------------------------- Cache (nkbg + sqlite) ---------------------------
def write_title_db(db_path: Path,
                   title_rows: Iterable[Tuple[int, int, str, int]],
                   redirects: Iterable[Tuple[int, int]]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f"file:{db_path}?mode=rwc", uri=True)
    cur = conn.cursor()
    cur.executescript("""
      PRAGMA journal_mode=WAL;
      PRAGMA synchronous=NORMAL;
      CREATE TABLE IF NOT EXISTS title(
        page_id INTEGER PRIMARY KEY,
        ns INT NOT NULL,
        title TEXT NOT NULL,
        nid INT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_title_ns_title ON title(ns, title);
      CREATE TABLE IF NOT EXISTS redirect(
        src_page_id INTEGER PRIMARY KEY,
        tgt_page_id INTEGER NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_redirect_tgt ON redirect(tgt_page_id);
    """)
    cur.executemany("INSERT OR REPLACE INTO title(page_id, ns, title, nid) VALUES (?,?,?,?)", title_rows)
    cur.executemany("INSERT OR REPLACE INTO redirect(src_page_id, tgt_page_id) VALUES (?,?)", redirects)
    conn.commit()
    conn.close()

def open_title_db_readonly(db_path: Path):
    # immutable=1 skips locking/change detection; mmap_size speeds reads (if supported).
    conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    conn.execute("PRAGMA mmap_size=268435456")  # 256MB
    return conn

def write_nkbg(G: nk.Graph, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    nk.graphio.writeGraph(G, str(path), nk.Format.NetworkitBinary)

def read_nkbg(path: Path) -> nk.Graph:
    return nk.graphio.readGraph(str(path), nk.Format.NetworkitBinary)

# ----------------------------- Graph assembly helpers --------------------------
def build_networkit_graph(edges: Iterable[Tuple[int, int]],
                          vertices: Set[int]) -> Tuple[nk.Graph, Dict[int, int], List[int]]:
    pid2nid: Dict[int, int] = {}
    nid2pid: List[int] = []
    for pid in sorted(vertices):
        pid2nid[pid] = len(nid2pid)
        nid2pid.append(pid)

    G = nk.Graph(len(nid2pid), weighted=False, directed=True)
    for u, v in edges:
        if u in pid2nid and v in pid2nid and u != v:
            G.addEdge(pid2nid[u], pid2nid[v])
    return G, pid2nid, nid2pid

# ----------------------------- Query helpers -----------------------------------
def _edge_exists(G: nk.Graph, u: int, v: int) -> bool:
    """Constant-time adjacency check when available; otherwise fall back to iterating neighbors."""
    # NetworKit exposes Graph.hasEdge(u, v) and neighbor iterators; both work on directed graphs. 
    try:
        return G.hasEdge(u, v)
    except Exception:
        return any(w == v for w in G.iterNeighbors(u))

def verify_path_by_nids(G: nk.Graph, path_nids: list[int]) -> dict:
    """
    Validate a path [n0, n1, ..., nk] against the directed graph.
    Returns dict with: ok(bool), msg(str), fail_index(int|None) where fail_index is
    the index i where edge (path[i] -> path[i+1]) failed (or None on success).
    """
    if not path_nids or len(path_nids) < 2:
        return {"ok": False, "msg": "path must contain at least two nodes", "fail_index": 0}

    n = G.numberOfNodes()
    for i in range(len(path_nids) - 1):
        u, v = path_nids[i], path_nids[i + 1]
        # Be defensive: NetworKit warns that many methods assume valid node ids; check first.
        if u < 0 or v < 0 or u >= n or v >= n or (not G.hasNode(u)) or (not G.hasNode(v)):
            return {"ok": False, "msg": f"invalid node id at step {i}: {u}->{v}", "fail_index": i}
        if not _edge_exists(G, u, v):
            return {"ok": False, "msg": f"missing directed edge at step {i}: {u}->{v}", "fail_index": i}

    return {"ok": True, "msg": f"valid path of length {len(path_nids) - 1}", "fail_index": None}

def verify_path_by_titles(conn, G: nk.Graph, titles: list[str]) -> dict:
    """
    Validate a title path. Titles should match DB format (underscores). Redirects are resolved.
    Uses your existing resolve_title_to_nid().
    """
    titles = [t.strip().replace(" ", "_") for t in titles if t.strip()]
    if len(titles) < 2:
        return {"ok": False, "msg": "need at least two titles", "fail_index": 0}

    nids = []
    for i, t in enumerate(titles):
        nid = resolve_title_to_nid(conn.cursor(), 0, t)
        if nid is None:
            return {"ok": False, "msg": f'unknown title at index {i}: "{t}"', "fail_index": i}
        nids.append(nid)
    return verify_path_by_nids(G, nids)

def exists_path_bidir(G: nk.Graph, src_nid: int, dst_nid: int) -> tuple[bool, int | None]:
    """
    Existence check via Bidirectional BFS. Returns (exists, distance or None).
    """
    algo = nk.distance.BidirectionalBFS(G, src_nid, dst_nid)  # STSP subclass
    algo.run()
    dist = algo.getDistance()  # STSP.getDistance()
    if dist == float("inf") or math.isinf(dist):
        return (False, None)
    return (True, int(dist))

def resolve_title_to_nid(cur, ns: int, title: str) -> Optional[int]:
    # Try exact article first
    row = cur.execute("SELECT nid, page_id FROM title WHERE ns=? AND title=? LIMIT 1", (ns, title)).fetchone()
    if row:
        nid, pid = row[0], row[1]
        # If the page is a redirect source, follow it
        r = cur.execute("SELECT tgt_page_id FROM redirect WHERE src_page_id=?", (pid,)).fetchone()
        if r:
            nid2 = cur.execute("SELECT nid FROM title WHERE page_id=? LIMIT 1", (r[0],)).fetchone()
            if nid2:
                return nid2[0]
        return nid
    # Not found
    return None

def bidir_shortest_path(G: nk.Graph, src_nid: int, dst_nid: int) -> List[int]:
    algo = nk.distance.BidirectionalBFS(G, src_nid, dst_nid, True)
    algo.run()
    if algo.getDistance() == float("inf"):
        return []
    mid = list(algo.getPath())  # NetworKit excludes endpoints
    return [src_nid] + mid + [dst_nid]

def k_random_neighbors(G: nk.Graph, nid: int, k: int = 5) -> List[int]:
    nbs = list(G.iterNeighbors(nid))
    random.shuffle(nbs)
    return nbs[:k]

# ----------------------------- CLI commands ------------------------------------
# ---- worker globals (set by _init_worker) ----
_WORK_pid2nid = None
_WORK_key_to_pid = None
_WORK_redirect_final = None
_WORK_lt_map = None
_WORK_has_target_id = None
_WORK_i_from = None
_WORK_i_tgt_id = None
_WORK_i_tgt_ns = None
_WORK_i_tgt_title = None

def _init_worker(shared_state):
    """Initializer run once per process to install large read-only structures."""
    global _WORK_pid2nid, _WORK_key_to_pid, _WORK_redirect_final, _WORK_lt_map
    global _WORK_has_target_id, _WORK_i_from, _WORK_i_tgt_id, _WORK_i_tgt_ns, _WORK_i_tgt_title
    (_WORK_pid2nid, _WORK_key_to_pid, _WORK_redirect_final, _WORK_lt_map,
     _WORK_has_target_id, _WORK_i_from, _WORK_i_tgt_id, _WORK_i_tgt_ns, _WORK_i_tgt_title) = shared_state

def _process_chunk(rows, shard_path):
    """Append packed <u32,u32> edges to shard_path from minimal pagelinks rows."""
    import os, struct
    from pathlib import Path
    pack = struct.Struct("<II").pack
    Path(shard_path).parent.mkdir(parents=True, exist_ok=True)
    with open(shard_path, "ab", buffering=1024*1024) as f:
        for r in rows:
            src_pid = r[0]
            if _WORK_has_target_id:
                lt_id = r[1]
                tgt = _WORK_lt_map.get(lt_id) if _WORK_lt_map is not None else None
                if not tgt:
                    continue
                tgt_ns, tgt_title = tgt
            else:
                tgt_ns, tgt_title = r[1], r[2]

            if tgt_ns != MAIN_NS:
                continue
            if src_pid not in _WORK_pid2nid:
                continue

            tgt_pid = _WORK_key_to_pid.get((tgt_ns, tgt_title))
            if tgt_pid is None:
                continue

            u = _WORK_pid2nid.get(_WORK_redirect_final.get(src_pid, src_pid))
            v = _WORK_pid2nid.get(_WORK_redirect_final.get(tgt_pid, tgt_pid))
            if u is not None and v is not None and u != v:
                f.write(pack(u, v))
    return os.path.basename(shard_path)

def cmd_build(args):
    """
    Build cache from dumps.

    Auto-detects platform:
      - Windows (default) -> low-memory SQLite builder
      - POSIX (default)   -> parallel sharded builder (fork)

    Flags:
      --lowmem            force low-memory builder
      --fast              force parallel builder
      --workers N         processes for parallel builder (default: cpu_count()-1)
      --chunk-rows K      pagelinks rows per task (default: 200_000)
      --mem-threshold P   producer backpressure threshold (% RAM, default: 85)
      --block-bytes B     block size for constant-memory assembly (default: 64 MiB)
      --use-edgelist-import  use NetworKit C++ importer for final graph build
    """

    cache = Path(args.cache)
    dumps = cache / "dumps"
    nkbg = cache / "graph.nkbg"
    titledb = cache / "title_index.sqlite"
    meta = cache / "meta.json"
    edges_bin = cache / "edges.bin"
    edges_txt = cache / "edges.txt"

    # Tuning
    workers = max(1, int(getattr(args, "workers", (os.cpu_count() or 2) - 1)))
    chunk_rows = int(getattr(args, "chunk_rows", 200_000))
    mem_threshold = float(getattr(args, "mem_threshold", 85.0))
    block_bytes = int(getattr(args, "block_bytes", 64 << 20))  # 64 MiB

    force_lowmem = bool(getattr(args, "lowmem", False))
    force_fast = bool(getattr(args, "fast", False))
    use_edgelist_import = bool(getattr(args, "use_edgelist_import", False))
    is_windows = (platform.system().lower() == "windows")
    use_lowmem = force_lowmem or (is_windows and not force_fast)

    print(f"[+] Downloading dumps ({args.wiki}/{args.date}) ...")
    # Download-if-missing logic (keep intent: do not re-download when cached)
    expected_page        = dumps / f"{args.wiki}-{args.date}-page.sql.gz"
    expected_pagelinks   = dumps / f"{args.wiki}-{args.date}-pagelinks.sql.gz"
    expected_redirect    = dumps / f"{args.wiki}-{args.date}-redirect.sql.gz"
    expected_page_props  = dumps / f"{args.wiki}-{args.date}-page_props.sql.gz"
    expected_linktarget  = dumps / f"{args.wiki}-{args.date}-linktarget.sql.gz"

    page_sql       = expected_page       if expected_page.exists()       else download_table(args.wiki, "page",       args.date, dumps)
    pagelinks_sql  = expected_pagelinks  if expected_pagelinks.exists()  else download_table(args.wiki, "pagelinks",  args.date, dumps)
    redirect_sql   = expected_redirect   if expected_redirect.exists()   else download_table(args.wiki, "redirect",   args.date, dumps)
    page_props_sql = expected_page_props if expected_page_props.exists() else download_table(args.wiki, "page_props", args.date, dumps)
    try:
        linktarget_sql = expected_linktarget if expected_linktarget.exists() else download_table(args.wiki, "linktarget", args.date, dumps)
    except Exception:
        linktarget_sql = None

    if use_lowmem:
        print("[+] Low-memory build path enabled (Windows-safe).")
        # 1) Title index with contiguous nids (optional disambig drop)
        build_title_db_lowmem(page_sql, page_props_sql, getattr(args, "drop_disambig", False), titledb)
        # 2) Redirects
        print("[+] Loading redirects into SQLite ...")
        load_redirect_into_db(redirect_sql, titledb)
        # 3) linktarget→pid (modern schema)
        if linktarget_sql:
            print("[+] Precomputing linktarget2pid table ...")
            build_linktarget2pid_table(linktarget_sql, titledb)

        if use_edgelist_import:
            # Produce edges.bin then do C++ import directly to nkbg
            print("[+] Extracting edges and building via EdgeListReader ...")
            _, _edges_txt, G = extract_edges_to_bin_lowmem(
                pagelinks_sql, titledb, edges_bin,
                also_build=True,
                edgelist_path=edges_txt,
                nkbg_out=nkbg,
                directed=True,
                block_bytes=block_bytes
            )
            assert G is not None
            print(f"    |V|={G.numberOfNodes():,} |E|={G.numberOfEdges():,}")
            meta.write_text(json.dumps({
                "wiki": args.wiki, "date": args.date,
                "nodes": G.numberOfNodes(), "edges": G.numberOfEdges(),
                "builder": "lowmem-sqlite+edgelist", "block_bytes": block_bytes
            }))
            print(f"[✓] Build complete. Cache at: {cache}")
            return

        # Allocate then assemble from edges.bin in constant memory
        conn = sqlite3.connect(f"file:{titledb}?mode=ro&immutable=1", uri=True)
        n_nodes = conn.execute("SELECT COUNT(*) FROM title").fetchone()[0]
        conn.close()
        print(f"[+] Allocating graph with {n_nodes:,} nodes ...")
        G = nk.Graph(int(n_nodes), weighted=False, directed=True)

        print("[+] Post-processing graph (dedup, sort, compact) ...")
        G.removeSelfLoops()
        G.removeMultiEdges()
        G.sortEdges()
        G.compactEdges()
        print(f"    |V|={G.numberOfNodes():,} |E|={G.numberOfEdges():,}")

        print("[+] Extracting edges to edges.bin ...")
        extract_edges_to_bin_lowmem(pagelinks_sql, titledb, edges_bin, also_build=False)

        print("[+] Assembling edges.bin into NetworKit graph ...")
        assemble_shards_to_graph(G, [str(edges_bin)], block_bytes=block_bytes)
        print(f"    |V|={G.numberOfNodes():,} |E|={G.numberOfEdges():,}")
        print("[+] Writing graph.nkbg ...")
        write_nkbg(G, nkbg)
        meta.write_text(json.dumps({
            "wiki": args.wiki, "date": args.date,
            "nodes": G.numberOfNodes(), "edges": G.numberOfEdges(),
            "builder": "lowmem-sqlite", "block_bytes": block_bytes
        }))
        print(f"[✓] Build complete. Cache at: {cache}")
        return

    # ===================== FAST POSIX PARALLEL BUILDER =====================
    print("[+] Fast path enabled (POSIX). Building in parallel with fork and shards.")

    # --- Load minimal in-RAM dicts (shared by fork) ---
    print("[+] Loading pages ...")
    pid_to_tuple, key_to_pid = load_pages(page_sql)
    if getattr(args, "drop_disambig", False):
        print("[+] Loading disambiguation flags ...")
        dis = load_disambigs(page_props_sql)
        for dpid in dis:
            pid_to_tuple.pop(dpid, None)

    print("[+] Resolving redirects ...")
    rd_first = load_redirects(redirect_sql, key_to_pid)
    rd_final = compress_redirects(pid_to_tuple, rd_first)

    print("[+] Preparing linktarget map (if present) ...")
    lt_map = load_linktarget(linktarget_sql) if linktarget_sql else None

    # Build empty graph with contiguous ids; add edges after sharding
    vertices = set(pid_to_tuple.keys())
    # We need pid2nid/nid2pid for redirect resolution/shard mapping and for title_index.sqlite later.
    G_dummy, pid2nid, nid2pid = build_networkit_graph((), vertices)  # G_dummy may be replaced later

    # --- Parallel row -> edge shards with progress + memory guard ---
    def build_edges_parallel(pagelinks_sql_path, workers, chunk_rows, mem_threshold):
        d = MwDump.from_file(str(pagelinks_sql_path))
        cols = d.col_names
        has_target_id = "pl_target_id" in cols
        i_from = cols.index("pl_from")
        i_tgt_id = cols.index("pl_target_id") if has_target_id else -1
        i_tgt_ns = cols.index("pl_namespace")  if not has_target_id else -1
        i_tgt_title = cols.index("pl_title")   if not has_target_id else -1

        shard_dir = (cache / "edge_shards"); shard_dir.mkdir(parents=True, exist_ok=True)
        shard_paths = [str(shard_dir / f"edges_{i}.bin") for i in range(workers)]

        # Explicitly request 'fork' (POSIX); falls back to default if not available
        try:
            ctx = get_context("fork")
        except ValueError:
            ctx = get_context()

        shared_state = (pid2nid, key_to_pid, rd_final, lt_map,
                        has_target_id, i_from, i_tgt_id, i_tgt_ns, i_tgt_title)
        pool = ctx.Pool(processes=workers, initializer=_init_worker, initargs=(shared_state,))

        rows_bar = tqdm(total=None, desc="Pagelinks rows → shards", smoothing=0.05, mininterval=0.5)
        futures, chunks = [], [[] for _ in range(workers)]
        assign = 0
        completed = 0
        max_queue = max(2 * workers, 8)

        for row in d.rows(convert_dtypes=True):
            if has_target_id:
                chunks[assign].append((row[i_from], row[i_tgt_id]))
            else:
                chunks[assign].append((row[i_from], row[i_tgt_ns], row[i_tgt_title]))

            if len(chunks[assign]) >= chunk_rows:
                batch = chunks[assign]; shardslot = shard_paths[assign]
                futures.append(pool.apply_async(_process_chunk, (batch, shardslot)))
                rows_bar.update(len(batch))
                chunks[assign] = []
                assign = (assign + 1) % workers

                # Backpressure on queued tasks
                while len(futures) - completed >= max_queue:
                    futures[completed].get(); completed += 1
                # Memory guard
                if psutil.virtual_memory().percent >= mem_threshold:
                    if completed < len(futures):
                        futures[completed].get(); completed += 1

        # Flush tail
        for i in range(workers):
            if chunks[i]:
                futures.append(pool.apply_async(_process_chunk, (chunks[i], shard_paths[i])))
                rows_bar.update(len(chunks[i]))
                chunks[i] = []

        for k in range(completed, len(futures)):
            futures[k].get()
        pool.close(); pool.join()
        rows_bar.close()
        return shard_paths

    print(f"[+] Streaming edges from pagelinks in parallel (workers={workers}, chunk_rows={chunk_rows}) ...")
    shard_paths = build_edges_parallel(pagelinks_sql, workers, chunk_rows, mem_threshold)

    if use_edgelist_import:
        print("[+] Converting shards to text and importing via EdgeListReader ...")
        shards_to_edgelist(shard_paths, edges_txt, block_bytes=block_bytes)
        G = build_graph_via_edgelist(edges_txt, nkbg, directed=True, expected_nodes=len(pid2nid))
        print(f"    |V|={G.numberOfNodes():,} |E|={G.numberOfEdges():,}")
    else:
        print("[+] Assembling shards into NetworKit graph ...")
        # Reuse dummy graph but add edges in constant memory
        G = nk.Graph(len(pid2nid), weighted=False, directed=True)
        assemble_shards_to_graph(G, shard_paths, block_bytes=block_bytes)
        print(f"    |V|={G.numberOfNodes():,} |E|={G.numberOfEdges():,}")
        print("[+] Writing graph.nkbg ...")
        write_nkbg(G, nkbg)

    print("[+] Writing title_index.sqlite ...")
    title_rows = ((pid, MAIN_NS, pid_to_tuple[pid][1], pid2nid[pid]) for pid in pid2nid)
    redirect_rows = ((src, tgt) for src, tgt in rd_final.items())
    write_title_db(titledb, title_rows, redirect_rows)

    meta.write_text(json.dumps({
        "wiki": args.wiki, "date": args.date,
        "nodes": G.numberOfNodes(), "edges": G.numberOfEdges(),
        "builder": "posix-parallel" + ("+edgelist" if use_edgelist_import else ""),
        "workers": workers, "chunk_rows": chunk_rows,
        "mem_threshold": mem_threshold, "block_bytes": block_bytes
    }))
    print(f"[✓] Build complete. Cache at: {cache}")

def _ensure_cache(cache: Path) -> Tuple[nk.Graph, sqlite3.Connection]:
    nkbg = cache / "graph.nkbg"
    titledb = cache / "title_index.sqlite"
    if not nkbg.exists() or not titledb.exists():
        raise FileNotFoundError("Cache not found. Run `build` first.")
    G = read_nkbg(nkbg)
    conn = open_title_db_readonly(titledb)
    return G, conn

def cmd_nk_query(args):
    cache = Path(args.cache)
    G, conn = _ensure_cache(cache)
    cur = conn.cursor()
    src = resolve_title_to_nid(cur, MAIN_NS, args.src)
    dst = resolve_title_to_nid(cur, MAIN_NS, args.dst)
    if src is None or dst is None:
        print("[!] Could not resolve one or both titles.")
        return
    path = bidir_shortest_path(G, src, dst)
    if not path:
        print("[!] No path found.")
        return
    # Map nid->title
    titles = []
    for nid in path:
        row = cur.execute("SELECT title FROM title WHERE nid=? LIMIT 1", (nid,)).fetchone()
        titles.append(row[0] if row else f"nid:{nid}")
    print(f"[+] Path ({len(path)-1} hops): " + " -> ".join(titles))

def cmd_sample(args):
    cache = Path(args.cache)
    G, conn = _ensure_cache(cache)
    cur = conn.cursor()
    count = args.count
    n = G.numberOfNodes()
    printed = 0
    tries = 0
    while printed < count and tries < count * 20:
        tries += 1
        s = random.randrange(n)
        # random reachable target via a one-shot BFS frontier sample
        bfs = nk.distance.BFS(G, s, storePaths=False)
        bfs.run()
        reach = [v for v in range(n) if v != s and bfs.distance(v) < float("inf")]
        if not reach:
            continue
        t = random.choice(reach)
        path = bidir_shortest_path(G, s, t)
        if not path:
            continue
        st = cur.execute("SELECT title FROM title WHERE nid=? LIMIT 1", (s,)).fetchone()
        tt = cur.execute("SELECT title FROM title WHERE nid=? LIMIT 1", (t,)).fetchone()
        print(f"{st[0] if st else s} -> {tt[0] if tt else t} : {len(path)-1} hops")
        printed += 1

def cmd_convert(args):
    """Convert legacy graph.bin (pickle with {'graph','pid2nid','nid2pid'}) to graph.nkbg + mappings.pkl"""
    inpath = Path(args.infile)
    out_nkbg = Path(args.out)
    out_map = Path(args.map_out) if args.map_out else out_nkbg.with_suffix(".mappings.pkl")

    with open(inpath, "rb") as f:
        obj = pickle.load(f)
    G = obj.get("graph")
    pid2nid = obj.get("pid2nid")
    nid2pid = obj.get("nid2pid")

    if G is None:
        raise ValueError("graph.bin did not contain 'graph' object")
    print("[+] Writing graph.nkbg ...")
    write_nkbg(G, out_nkbg)

    print("[+] Writing mappings.pkl (pid2nid, nid2pid) ...")
    with open(out_map, "wb") as f:
        pickle.dump({"pid2nid": pid2nid, "nid2pid": nid2pid}, f, protocol=pickle.HIGHEST_PROTOCOL)

    print(f"[✓] Converted to {out_nkbg} and {out_map}")

def cmd_verify(args):
    cache = Path(args.cache)
    G, conn = _ensure_cache(cache)
    if args.nids:
        path = [int(x) for x in args.nids.split(",") if x.strip()]
        res = verify_path_by_nids(G, path)
    else:
        titles = [t.strip() for t in args.titles.split(",")]
        res = verify_path_by_titles(conn, G, titles)
    print(json.dumps(res, ensure_ascii=False))

def cmd_exists(args):
    cache = Path(args.cache)
    G, conn = _ensure_cache(cache)
    if args.src_nid is not None and args.dst_nid is not None:
        s, t = int(args.src_nid), int(args.dst_nid)
    else:
        s = resolve_title_to_nid(conn.cursor(), 0, args.src.replace(" ", "_"))
        t = resolve_title_to_nid(conn.cursor(), 0, args.dst.replace(" ", "_"))
        if s is None or t is None:
            print(json.dumps({"ok": False, "msg": "could not resolve src/dst"})); return

    if args.use_scc_index:
        idx = SCCReachabilityIndex(G)
        ok = idx.reachable(s, t)
        out = {"ok": ok, "method": "scc-dag"}
        if ok and args.want_distance:
            # fall back to bidir for the exact distance
            ok, dist = exists_path_bidir(G, s, t)
            out["distance"] = dist if ok else None
    else:
        ok, dist = exists_path_bidir(G, s, t)
        out = {"ok": ok}
        if args.want_distance:
            out["distance"] = dist
    print(json.dumps(out, ensure_ascii=False))
# ----------------------------- Main --------------------------------------------
def main():
    ap = argparse.ArgumentParser(prog="wikigame_graph_eve.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ap_build = sub.add_parser("build", help="Build cache from dumps")
    ap_build.add_argument("--wiki", default="enwiki")
    ap_build.add_argument("--date", default="latest", help="'latest' or YYYYMMDD")
    ap_build.add_argument("--cache", default="cache/enwiki-latest")
    ap_build.add_argument("--drop-disambig", action="store_true")
    # Builder selection + tuning
    ap_build.add_argument("--lowmem", action="store_true", help="Force low-memory builder (default on Windows)")
    ap_build.add_argument("--fast", action="store_true", help="Force POSIX parallel builder (default on Linux/macOS)")
    ap_build.add_argument("--workers", type=int, help="Parallel workers (POSIX fast path)")
    ap_build.add_argument("--chunk-rows", type=int, help="Rows per worker task (POSIX)")
    ap_build.add_argument("--mem-threshold", type=float, help="Backpressure threshold, percent RAM (POSIX)")
    ap_build.add_argument("--block-bytes", type=int, help="Block size for streaming assembly/conversion, bytes")
    ap_build.add_argument("--use-edgelist-import", action="store_true",
                          help="Use C++ EdgeListReader for final graph import (faster assembly)")
    ap_build.set_defaults(func=cmd_build)

    ap_query = sub.add_parser("query", help="Query shortest path from cache")
    ap_query.add_argument("--cache", required=True)
    ap_query.add_argument("--src", required=True, help="Title (use underscores as in DB)")
    ap_query.add_argument("--dst", required=True)
    ap_query.set_defaults(func=cmd_query)

    ap_verify = sub.add_parser("verify", help="Verify that a given path is valid")
    ap_verify.add_argument("--cache", required=True)
    g = ap_verify.add_mutually_exclusive_group(required=True)
    g.add_argument("--titles", help="Comma-separated titles")
    g.add_argument("--nids", help="Comma-separated nids")
    ap_verify.set_defaults(func=cmd_verify)

    ap_exists = sub.add_parser("exists", help="Check if any path exists between two nodes")
    ap_exists.add_argument("--cache", required=True)
    g2 = ap_exists.add_mutually_exclusive_group(required=True)
    g2.add_argument("--src", help="Source title"); g2.add_argument("--src-nid", type=int)
    g3 = ap_exists.add_mutually_exclusive_group(required=True)
    g3.add_argument("--dst", help="Target title"); g3.add_argument("--dst-nid", type=int)
    ap_exists.add_argument("--use-scc-index", action="store_true",
                        help="Build SCC condensation DAG and check reachability on it (faster for many queries)")
    ap_exists.add_argument("--want-distance", action="store_true",
                        help="If reachable, also return the shortest-path distance (runs bidirectional BFS once)")
    ap_exists.set_defaults(func=cmd_exists)

    ap_sample = sub.add_parser("sample", help="Sample reachable pairs")
    ap_sample.add_argument("--cache", required=True)
    ap_sample.add_argument("--count", type=int, default=5)
    ap_sample.set_defaults(func=cmd_sample)

    ap_conv = sub.add_parser("convert", help="Convert graph.bin -> graph.nkbg")
    ap_conv.add_argument("--in", dest="infile", required=True, help="graph.bin path")
    ap_conv.add_argument("--out", required=True, help="graph.nkbg output path")
    ap_conv.add_argument("--map-out", help="Optional mappings pickle output path")
    ap_conv.set_defaults(func=cmd_convert)

    args = ap.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
