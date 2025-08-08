#!/usr/bin/env python3
"""
Betweenness Centrality Calculator for Wikipedia Graph

This script calculates betweenness centrality for all nodes in the Wikipedia graph
and outputs a ranked CSV file with node IDs, titles, and centrality scores.

Usage:
    python betweenness_centrality.py --cache cache/enwiki-latest --output betweenness_rankings.csv

Requirements:
    - NetworkKit graph file (graph.nkbg) in cache directory
    - SQLite title index (title_index.sqlite) in cache directory
"""

import os
import csv
import sys
import sqlite3
import argparse
from pathlib import Path
from typing import List, Tuple
import networkit as nk


def load_graph_and_db(cache_path: Path) -> Tuple[nk.Graph, sqlite3.Connection]:
    """
    Load the NetworkKit graph and SQLite title database.
    
    Args:
        cache_path: Path to the cache directory
        
    Returns:
        Tuple of (NetworkKit Graph, SQLite Connection)
    """
    nkbg_path = cache_path / "graph.nkbg"
    db_path = cache_path / "title_index.sqlite"
    
    if not nkbg_path.exists():
        raise FileNotFoundError(f"Graph file not found: {nkbg_path}")
    if not db_path.exists():
        raise FileNotFoundError(f"Database file not found: {db_path}")
    
    print(f"[+] Loading graph from {nkbg_path}")
    graph = nk.graphio.readGraph(str(nkbg_path), nk.Format.NetworkitBinary)
    
    print(f"[+] Opening database from {db_path}")
    # Open as read-only for safety
    conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    
    print(f"[+] Graph loaded: {graph.numberOfNodes():,} nodes, {graph.numberOfEdges():,} edges")
    
    return graph, conn


def get_node_title(cursor, nid: int) -> str:
    """
    Get the Wikipedia title for a given node ID.
    
    Args:
        cursor: SQLite cursor
        nid: Node ID
        
    Returns:
        Wikipedia article title or fallback string
    """
    result = cursor.execute("SELECT title FROM title WHERE nid=? LIMIT 1", (nid,)).fetchone()
    return result[0] if result else f"nid:{nid}"


def calculate_betweenness_centrality(graph: nk.Graph) -> List[float]:
    """
    Calculate betweenness centrality for all nodes in the graph.
    
    Args:
        graph: NetworkKit graph
        
    Returns:
        List of betweenness centrality scores indexed by node ID
    """
    print("[+] Calculating betweenness centrality...")
    print("    This may take a while for large graphs...")
    
    # Use NetworkKit's betweenness centrality algorithm
    # For very large graphs, we might want to use approximation, but let's start with exact
    bc = nk.centrality.Betweenness(graph, normalized=True)
    bc.run()
    
    centrality_scores = bc.scores()
    print(f"[+] Betweenness centrality calculation complete")
    
    return centrality_scores


def create_ranked_csv(graph: nk.Graph, 
                     centrality_scores: List[float], 
                     conn: sqlite3.Connection, 
                     output_path: Path) -> None:
    """
    Create a ranked CSV file with node IDs, titles, and centrality scores.
    
    Args:
        graph: NetworkKit graph
        centrality_scores: List of centrality scores
        conn: SQLite database connection
        output_path: Path for the output CSV file
    """
    print("[+] Creating ranked CSV output...")
    
    cursor = conn.cursor()
    
    # Create list of (nid, title, centrality_score) tuples
    rankings = []
    
    for nid in range(graph.numberOfNodes()):
        if graph.hasNode(nid):  # Check if node exists (graphs can have gaps)
            title = get_node_title(cursor, nid)
            score = centrality_scores[nid]
            rankings.append((nid, title, score))
    
    # Sort by centrality score (highest first)
    rankings.sort(key=lambda x: x[2], reverse=True)
    
    # Write to CSV
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.writer(csvfile)
        
        # Write header
        writer.writerow(['rank', 'node_id', 'title', 'betweenness_centrality'])
        
        # Write data
        for rank, (nid, title, score) in enumerate(rankings, 1):
            writer.writerow([rank, nid, title, score])
    
    print(f"[+] Rankings written to {output_path}")
    print(f"[+] Total nodes ranked: {len(rankings):,}")
    
    # Show top 10 for preview
    print("\n[+] Top 10 nodes by betweenness centrality:")
    print("Rank | Node ID | Title | Centrality Score")
    print("-" * 60)
    for i, (nid, title, score) in enumerate(rankings[:10], 1):
        # Truncate long titles for display
        display_title = title[:40] + "..." if len(title) > 40 else title
        print(f"{i:4d} | {nid:7d} | {display_title:43s} | {score:.6f}")


def main():
    """Main function to orchestrate the betweenness centrality calculation."""
    parser = argparse.ArgumentParser(
        description="Calculate betweenness centrality for Wikipedia graph nodes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python betweenness_centrality.py --cache cache/enwiki-latest --output rankings.csv
  python betweenness_centrality.py --cache cache/enwiki-latest --output results/centrality.csv
        """
    )
    
    parser.add_argument(
        "--cache", 
        required=True, 
        type=Path,
        help="Path to cache directory containing graph.nkbg and title_index.sqlite"
    )
    
    parser.add_argument(
        "--output", 
        required=True, 
        type=Path,
        help="Output CSV file path for rankings"
    )
    
    parser.add_argument(
        "--approximate",
        action="store_true",
        help="Use approximation algorithm for faster computation on very large graphs"
    )
    
    args = parser.parse_args()
    
    try:
        # Load graph and database
        graph, conn = load_graph_and_db(args.cache)
        
        # Calculate betweenness centrality
        if args.approximate:
            print("[+] Using approximation algorithm...")
            # For very large graphs, we could use EstimateBetweenness
            bc = nk.centrality.EstimateBetweenness(graph, nSamples=500, normalized=True)
            bc.run()
            centrality_scores = bc.scores()
        else:
            centrality_scores = calculate_betweenness_centrality(graph)
        
        # Create ranked CSV output
        create_ranked_csv(graph, centrality_scores, conn, args.output)
        
        print(f"\n[✓] Betweenness centrality analysis complete!")
        print(f"    Results saved to: {args.output}")
        
    except Exception as e:
        print(f"[!] Error: {e}", file=sys.stderr)
        sys.exit(1)
    
    finally:
        # Clean up
        if 'conn' in locals():
            conn.close()


if __name__ == "__main__":
    main()
