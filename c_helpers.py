import os
import struct
from tqdm import tqdm
import networkit as nk
from pathlib import Path
from typing import List, Optional

# ----------------------------- Helpers for C++ edgelist import -----------------
def bin_to_edgelist(bin_path: Path, txt_path: Path, block_bytes: int = 64 << 20) -> Path:
    """
    Stream-convert packed <u32,u32> edges.bin -> text edgelist "u v\n" (constant memory).
    """
    txt_path.parent.mkdir(parents=True, exist_ok=True)
    total_bytes = os.path.getsize(bin_path)
    with open(bin_path, "rb") as fin, open(txt_path, "w", buffering=1024*1024) as fout, \
         tqdm(total=total_bytes, unit="B", unit_scale=True, desc="bin→edgelist") as pbar:
        while True:
            buf = fin.read(block_bytes)
            if not buf:
                break
            usable = (len(buf) // 8) * 8
            for u, v in struct.iter_unpack("<II", memoryview(buf)[:usable]):
                fout.write(f"{u} {v}\n")
            pbar.update(usable)
    return txt_path

def shards_to_edgelist(shard_paths: List[str], txt_path: Path, block_bytes: int = 64 << 20) -> Path:
    """
    Convert multiple shard binaries (<u32,u32>) into a single text edgelist.
    """
    txt_path.parent.mkdir(parents=True, exist_ok=True)
    total_bytes = sum(os.path.getsize(p) for p in shard_paths if os.path.exists(p))
    with open(txt_path, "w", buffering=1024*1024) as fout, \
         tqdm(total=total_bytes, unit="B", unit_scale=True, desc="shards→edgelist") as pbar:
        for p in shard_paths:
            if not os.path.exists(p):
                continue
            with open(p, "rb") as fin:
                while True:
                    buf = fin.read(block_bytes)
                    if not buf:
                        break
                    usable = (len(buf) // 8) * 8
                    for u, v in struct.iter_unpack("<II", memoryview(buf)[:usable]):
                        fout.write(f"{u} {v}\n")
                    pbar.update(usable)
    return txt_path

def build_graph_via_edgelist(edges_txt: Path, nkbg_out: Optional[Path],
                             directed: bool = True,
                             expected_nodes: Optional[int] = None) -> nk.Graph:
    """
    Import a text edgelist via NetworKit's C++ EdgeListReader (faster than Python addEdge),
    then (optionally) pad to expected_nodes and write .nkbg.
    """
    reader = nk.graphio.EdgeListReader(' ', 0, '#', True, directed)  # firstNode=0, continuous=True
    G = reader.read(str(edges_txt))
    if expected_nodes is not None and G.numberOfNodes() < expected_nodes:
        G.addNodes(int(expected_nodes) - G.numberOfNodes())
    if nkbg_out is not None:
        nk.graphio.writeGraph(G, str(nkbg_out), nk.Format.NetworkitBinary)
    return G
