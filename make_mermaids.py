#!/usr/bin/env python3
"""
Parser for samples.txt to generate mermaid diagrams
Each path flows diagonally from top-left to bottom-right
Each node has 3 arrows: center arrow to next node, two side arrows to transparent endpoints
"""

import re
from typing import List, Tuple, Optional

def parse_sample_line(line: str) -> Optional[Tuple[int, List[str]]]:
    """
    Parse a line from samples.txt
    Returns: (hops, path_nodes) or None if not a valid path
    """
    line = line.strip()
    
    # Skip lines that don't have successful paths
    if '[+]' not in line:
        return None
    
    # Extract path information using regex, accounting for line numbers at start
    pattern = r'.*\[\+\] Path \((\d+) hops\): (.+)'
    match = re.match(pattern, line)
    
    if not match:
        return None
    
    hops = int(match.group(1))
    path_str = match.group(2)
    
    # Split path by arrow and clean up node names
    nodes = [node.strip() for node in path_str.split(' → ')]
    
    return hops, nodes

def create_mermaid_diagram(hops: int, nodes: List[str]) -> str:
    """
    Create a mermaid diagram for a single path flowing diagonally
    Each node has 3 arrows: center to next node, two sides to transparent endpoints
    """
    
    mermaid_content = ["```mermaid", "graph TD"]
    
    # Define transparent/dummy nodes for side arrows
    dummy_nodes = set()
    
    # Add nodes for this path
    for node_idx, node in enumerate(nodes):
        node_id = f"N{node_idx}"
        clean_node_name = node.replace("_", " ").replace("(", "").replace(")", "").replace(",", "")
        
        # Truncate very long node names
        if len(clean_node_name) > 100:
            clean_node_name = clean_node_name[:97] + "..."
        
        mermaid_content.append(f'    {node_id}["{clean_node_name}"]')
    
    # Add connections for this path
    for node_idx in range(len(nodes) - 1):
        current_node = f"N{node_idx}"
        next_node = f"N{node_idx + 1}"
        
        # Center arrow to next node in path
        mermaid_content.append(f"    {current_node} --> {next_node}")
        
        # Two side arrows to dummy nodes (transparent endpoints)
        left_dummy = f"D{node_idx}L"
        right_dummy = f"D{node_idx}R"
        
        dummy_nodes.add(left_dummy)
        dummy_nodes.add(right_dummy)
        
        mermaid_content.append(f"    {current_node} -.-> {left_dummy}")
        mermaid_content.append(f"    {current_node} -.-> {right_dummy}")
    
    # Add dummy nodes with transparent styling
    for dummy in sorted(dummy_nodes):
        mermaid_content.append(f'    {dummy}[ ]')
    
    # Add styling to make dummy nodes transparent/invisible
    mermaid_content.append("")
    mermaid_content.append("    %% Style dummy nodes as transparent")
    for dummy in sorted(dummy_nodes):
        mermaid_content.append(f"    style {dummy} fill:transparent,stroke:transparent")
    
    mermaid_content.append("```")
    
    return "\n".join(mermaid_content)

def main():
    """Main function to parse samples.txt and generate .md"""
    
    # Read samples.txt
    try:
        with open('samples.txt', 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except FileNotFoundError:
        print("Error: samples.txt not found")
        return
    
    # Parse all lines
    paths = []
    for line_num, line in enumerate(lines, 1):
        result = parse_sample_line(line)
        if result:
            hops, nodes = result
            paths.append((hops, nodes))
            print(f"Parsed line {line_num}: {hops} hops, {len(nodes)} nodes")
    
    print(f"\nTotal paths found: {len(paths)}")
    
    # Generate individual mermaid diagrams for each path
    readme_content = ["# WikiGraph Path Visualizations", ""]
    readme_content.append("This file contains individual mermaid diagrams for each Wikipedia path from samples.txt.")
    readme_content.append("Each path flows diagonally from top-left to bottom-right.")
    readme_content.append("Each node has three arrows: one central arrow leading to the next node, and two side arrows leading to transparent endpoints.")
    readme_content.append("")
    
    # Create individual diagram for each path
    for path_idx, (hops, nodes) in enumerate(paths, 1):
        readme_content.append(f"## Path {path_idx}")
        readme_content.append("")
        readme_content.append(f"**{hops} hops**: {' → '.join(nodes)}")
        readme_content.append("")
        
        # Add individual mermaid diagram
        mermaid_diagram = create_mermaid_diagram(hops, nodes)
        readme_content.append(mermaid_diagram)
        readme_content.append("")
    
    # Write README.md
    readme_text = "\n".join(readme_content)
    
    with open('mermaid_samples.md', 'w', encoding='utf-8') as f:
        f.write(readme_text)
    
    print(f"Generated mermaid_samples.md with {len(paths)} individual path diagrams")

if __name__ == "__main__":
    main()