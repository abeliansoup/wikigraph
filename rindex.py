from collections import deque
import networkit as nk

class SCCReachabilityIndex:
    """
    Build once; answer reachability queries on the SCC condensation DAG.
    If comp[u] == comp[v] => reachable; else BFS on compact DAG.
    """
    def __init__(self, G: nk.Graph):
        scc = nk.components.StronglyConnectedComponents(G)
        scc.run()
        # Map each node to a component id (ComponentDecomposition API).
        # Docs show both getPartition() and componentOfNode(); either is fine.
        # https://networkit.github.io/dev-docs/python_api/components.html
        self.comp_of = [scc.componentOfNode(u) for u in G.iterNodes()]
        self.num_comp = scc.numberOfComponents()

        # Build DAG adjacency (deduplicate multi-edges between components)
        self.adj = [set() for _ in range(self.num_comp)]
        for (u, v) in G.iterEdges():  # in directed graphs yields out-edges (u, v)
            cu, cv = self.comp_of[u], self.comp_of[v]
            if cu != cv:
                self.adj[cu].add(cv)

    def reachable(self, u: int, v: int) -> bool:
        cu, cv = self.comp_of[u], self.comp_of[v]
        if cu == cv:
            return True
        # BFS on DAG (usually tiny).
        q, seen = deque([cu]), {cu}
        while q:
            x = q.popleft()
            for y in self.adj[x]:
                if y == cv:
                    return True
                if y not in seen:
                    seen.add(y); q.append(y)
        return False