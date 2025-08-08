#!/usr/bin/env python3
"""
Parser for samples.txt to generate mermaid diagrams with PROPER randomization
Each connection between nodes randomly chooses which of 3 arrows to use
"""

import re
import random
from typing import List, Tuple, Optional

def parse_sample_line(line: str) -> Optional[Tuple[int, List[str]]]:
    """Parse a line from samples.txt"""
    line = line.strip()
    
    if '[+]' not in line:
        return None
    
    pattern = r'.*\[\+\] Path \((\d+) hops\): (.+)'
    match = re.match(pattern, line)
    
    if not match:
        return None
    
    hops = int(match.group(1))
    path_str = match.group(2)
    nodes = [node.strip() for node in path_str.split(' → ')]
    
    return hops, nodes

def create_mermaid_diagram(hops: int, nodes: List[str]) -> str:
    """
    Create a mermaid diagram with TRUE randomization per connection
    """
    
    config = """%%{init: {
  "theme": "dark",                             
  "flowchart": {
    "useMaxWidth": true,
    "defaultRenderer": "elk",
    "nodeSpacing": 30, "rankSpacing": 40
  },
  "themeVariables": { "fontFamily": "ui-monospace" }
}}%%"""

    mermaid_content = ["```mermaid", config, "graph TD"]
    dummy_nodes = []
    
    # Add all node definitions first
    for i, node in enumerate(nodes):
        node_id = f"N{i}"
        clean_name = node.replace("_", " ")
        if len(clean_name) > 100:
            clean_name = clean_name[:97] + "..."
        mermaid_content.append(f'    {node_id}["{clean_name}"]')
    
    # Add random connections
    for i in range(len(nodes) - 1):
        current = f"N{i}"
        next_node = f"N{i + 1}"
        chosen_position = random.randint(1, 3)
        
        print(f"Connection {current} -> {next_node}: using position {chosen_position}")
        
        for pos in range(1, 4):
            if pos == chosen_position:
                # Real connection
                mermaid_content.append(f"    {current} --> {next_node}")
            else:
                # Dummies
                dummy_id = f"DUMMY_{i}_{pos}"
                dummy_nodes.append(dummy_id)
                mermaid_content.append(f"    {current} -.-> {dummy_id}")
    
    # Add dummy node definitions
    mermaid_content.append("")
    for dummy in dummy_nodes:
        mermaid_content.append(f'    {dummy}[ ]')
    
    # Add styling
    mermaid_content.append("")
    mermaid_content.append("    %% Make dummy nodes transparent")
    for dummy in dummy_nodes:
        mermaid_content.append(f"    style {dummy} fill:transparent,stroke:transparent")
    
    mermaid_content.append("```")
    return "\n".join(mermaid_content)

def main():
    """Main function"""
    
    # Use no seed for true randomness each run
    random.seed()  
    
    # Read samples.txt
    try:
        with open('samples.txt', 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except FileNotFoundError:
        print("Error: samples.txt not found")
        return
    
    # Parse all paths
    paths = []
    for line_num, line in enumerate(lines, 1):
        result = parse_sample_line(line)
        if result:
            hops, nodes = result
            paths.append((hops, nodes))
            print(f"Parsed line {line_num}: {hops} hops, {len(nodes)} nodes")
    
    print(f"\nTotal paths found: {len(paths)}")
    
    # Generate README with individual diagrams
    readme_content = ["# WikiGraph Path Visualizations"]
    readme_content.append("Each connection between nodes uses a RANDOMLY chosen arrow position (1, 2, or 3).")
    readme_content.append("Every single connection is independently randomized!")
    readme_content.append("")
    
    # Generate one diagram per path with true randomization
    for path_idx, (hops, nodes) in enumerate(paths, 1):
        print(f"\nGenerating diagram for Path {path_idx}:")
        
        readme_content.append(f"## Path {path_idx}")
        readme_content.append("")
        readme_content.append(f"**{hops} hops**: {' → '.join(nodes)}")
        readme_content.append("")
        
        # Create diagram with independent randomization
        mermaid_diagram = create_mermaid_diagram(hops, nodes)
        readme_content.append(mermaid_diagram)
        readme_content.append("")
    
    # Write to file
    with open('mermaid_samples.md', 'w', encoding='utf-8') as f:
        f.write("\n".join(readme_content))
    
    print(f"\nGenerated truly_random_samples.md with {len(paths)} randomized diagrams")

if __name__ == "__main__":
    main()