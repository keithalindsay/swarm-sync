"""swarm-sync — stigmergic lease fabric for AI coding swarms (Pheromesh architecture).

See DESIGN.md for the full spec. Package layout:
  classifier/  §3  repo -> parcel map, blast radius, frozen contracts
  blackboard/  §4  SQLite (WAL) shared memory: schema + models
  server/      §4.2 FastAPI endpoints + lease manager + event log
  coordinator/ §5.4/§6 broker, reaper, serial test-gated integrator
  agent/       §4.3 thin blackboard client + worktree runner + scripted mutators
  worktree/    §5.1 git worktree isolation primitives
"""

__version__ = "0.1.0"
