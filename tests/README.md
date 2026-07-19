# tests

Per-unit tests. Each BUILD_PLAN unit ships with the test that proves its "done when".

- test_blackboard.py   (U1)  schema initializes; tables exist; WAL enabled
- test_indexer.py      (U2)  parse_file yields expected parcels + spans + hashes
- test_graph.py        (U3)  blast radius + frozen-contract extraction + co_schedulable
- test_index_api.py    (U4)  POST /index populates parcels + contracts
- test_leases.py       (U5)  concurrent write-lease -> exactly one granted, one denied
- test_events.py       (U6)  emit/tail ordering; pheromone decay
- test_server.py       (U7)  endpoints via FastAPI TestClient
- test_git_ops.py      (U8)  worktree add/remove/commit/merge on a temp repo
- test_agent.py        (U9)  agent leases, edits in worktree, commits, releases
- test_integrator.py   (U10) two disjoint branches merge clean; test-breaker rejected
- test_reaper.py       (U11) stale lease reaped after TTL; parcel reacquirable
- test_broker.py       (U12) co-schedulable tasks dispatched concurrently
- test_sample_repo.py  (U13) sample_repo pytest suite is green
- test_demo.py         (U14/U15) demo test case assertions all PASS
