"""Tests for core_db.ThreadLocalDB — the per-thread connection wrapper
   app.state.db carries in production.

   Motivation (observed in container testing): the two top-nav HTMX polls
   (/fleet-status, /fleet-sparkline) fire phase-locked every 10s, their
   sync require_session dependencies land on two threadpool threads, and
   both run the identical session re-validation query. On a single shared
   sqlite3 connection the two threads collide on the cached prepared
   statement — sqlite3.InterfaceError SQLITE_MISUSE on one request and a
   silently-wrong row (spurious logout) on the other. ThreadLocalDB gives
   each thread its own connection so there is no shared object to race on.
"""
import threading

from core import core_db


def _tldb(tmp_path):
    """A migrated database file plus a ThreadLocalDB over it — the
       production shape: connect() migrates once at startup, the wrapper
       serves the routes."""
    path = str(tmp_path / "hone.db")
    core_db.connect(path).close()
    return core_db.ThreadLocalDB(path)


def test_same_thread_reuses_one_connection(tmp_path):
    db = _tldb(tmp_path)
    assert db._conn() is db._conn()


def test_each_thread_gets_its_own_connection(tmp_path):
    db = _tldb(tmp_path)
    mine = db._conn()
    theirs = []
    t = threading.Thread(target=lambda: theirs.append(db._conn()))
    t.start()
    t.join()
    assert theirs[0] is not mine


def test_identical_queries_from_many_threads_do_not_race(tmp_path):
    """The regression test for the fleet-status/fleet-sparkline collision:
       many threads running the SAME SQL string at the same instant. On a
       shared connection this intermittently raises SQLITE_MISUSE from the
       per-connection statement cache; per-thread connections must take it
       without error."""
    db = _tldb(tmp_path)
    uid = core_db.create_user(db, "alice@x", "alice", "local")
    n_threads, n_iters = 8, 100
    barrier = threading.Barrier(n_threads)
    errors = []

    def hammer():
        try:
            barrier.wait()
            for _ in range(n_iters):
                row = core_db.get_user_by_id(db, uid)
                assert row["email"] == "alice@x"
        except Exception as e:                      # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=hammer) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_concurrent_writers_serialize_instead_of_erroring(tmp_path):
    """WAL serializes writers across connections; busy_timeout makes a
       lock collision wait rather than raise 'database is locked'. Every
       thread's inserts must land."""
    db = _tldb(tmp_path)
    n_threads, n_users = 6, 10
    barrier = threading.Barrier(n_threads)
    errors = []

    def write(tno):
        try:
            barrier.wait()
            for i in range(n_users):
                core_db.create_user(db, f"u{tno}-{i}@x", "u", "local")
        except Exception as e:                      # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=write, args=(tno,))
               for tno in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    n = db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    assert n == n_threads * n_users


def test_close_closes_every_threads_connection(tmp_path):
    db = _tldb(tmp_path)
    db.execute("SELECT 1")                          # main thread's conn
    t = threading.Thread(target=lambda: db._conn().execute("SELECT 1"))
    t.start()
    t.join()
    conns = list(db._all)
    assert len(conns) == 2
    db.close()
    for c in conns:
        try:
            c.execute("SELECT 1")
            raise AssertionError("connection still usable after close()")
        except Exception:
            pass
