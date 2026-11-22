"""Offline tracer: run every sqlglot test alone in a fresh forked child and record what it touched.

Per test: every (file, line) executed in the repo (sys.monitoring LINE, each location once),
files opened in the repo (audit hook), whether it spawned a process, pass/fail, CPU seconds.
Usage: python trace.py REPO TESTS_JSON OUTDIR [JOBS] [ONLY_PREFIX]
"""
import json, os, pickle, resource, sys, time

def _main():
    global REPO, TESTS, OUT, JOBS, ONLY, HEAVY, LIGHT, tests
    REPO, TESTS, OUT = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2]), os.path.abspath(sys.argv[3])
    JOBS = int(sys.argv[4]) if len(sys.argv) > 4 else 6
    ONLY = sys.argv[5] if len(sys.argv) > 5 else ""
    os.makedirs(OUT, exist_ok=True)

    import unittest, importlib, traceback, signal  # noqa: E401
    # No threaded libraries in the parent: a fork after threads start can deadlock the child.

    tests = json.load(open(TESTS))["tests"]
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "selector"))


    def child(t):
        os.chdir(REPO)
        sys.path.insert(0, REPO)
        os.environ["SKIP_INTEGRATION"] = "0"
        if not os.environ.get("TRACE_DEBUG"):
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
        lines = {}
        opens = set()
        spawn = [False]
        rp = REPO + os.sep

        def audit(event, args):
            if event == "open":
                p = args[0]
                if isinstance(p, bytes):
                    p = p.decode(errors="replace")
                if isinstance(p, str):
                    p = os.path.abspath(p)
                    if p.startswith(rp):
                        opens.add(p[len(rp):])
            elif event in ("subprocess.Popen", "os.posix_spawn", "os.exec", "os.system", "os.spawn", "os.fork"):
                spawn[0] = True

        mon = sys.monitoring
        tool = mon.COVERAGE_ID
        mon.use_tool_id(tool, "br")
        DISABLE = mon.DISABLE

        sink = [lines]   # where lines go now: the test's own lines, or one fixture case or query

        def on_line(code, line):
            f = code.co_filename
            if f.startswith(rp):
                key = (f, code.co_flags & 1)  # 1: function, lambda or generator code; 0: module or class body
                d = sink[0]
                s = d.get(key)
                if s is None:
                    s = d[key] = set()
                s.add(line)
            return DISABLE

        classes = set()
        seen_types = set()
        getframe = sys._getframe

        def on_start(code, offset):
            # Record the class of every object whose __init__ runs: a class-body change reaches only
            # tests that created an instance of that class or a subclass.
            if code.co_name != "__init__" or not code.co_argcount:
                return DISABLE
            try:
                obj = getframe(1).f_locals.get(code.co_varnames[0])
            except Exception:
                return None
            tp = type(obj)
            if tp in seen_types:
                return None
            seen_types.add(tp)
            for k in tp.__mro__:
                m = getattr(k, "__module__", "") or ""
                if m.startswith("sqlglot") or m.startswith("tests"):
                    classes.add(f"{m}:{k.__qualname__}")
            return None

        mon.register_callback(tool, mon.events.LINE, on_line)
        mon.register_callback(tool, mon.events.PY_START, on_start)
        sys.addaudithook(audit)
        mon.set_events(tool, mon.events.LINE | mon.events.PY_START)
        import syncpool
        syncpool.install()
        REVERSE = [None]  # number of TPC queries when they run in reverse order
        cases = {}       # case key -> lines dict
        last_case = [None]
        titles = {}
        meth_name = t["id"].split("::")[-1]
        restart = mon.restart_events

        def enter(key):
            prev = sink[0]
            sink[0] = cases.setdefault(key, {})
            restart()
            return prev

        def leave(prev):
            sink[0] = prev
            restart()

        orig_submit = syncpool.SyncPool.submit

        def submit(self, fn, /, *args, **kwargs):
            if meth_name in ("test_execute_tpch", "test_execute_tpcds"):
                k = submit.count
                submit.count += 1
                if REVERSE[0] is not None:
                    k = REVERSE[0] - 1 - k  # queries ran last-to-first: report the original index
                key = ("q", k)
            elif last_case[0] is not None:
                key = ("p",) + last_case[0]
            else:
                return orig_submit(self, fn, *args, **kwargs)
            prev = enter(key)
            try:
                return orig_submit(self, fn, *args, **kwargs)
            finally:
                leave(prev)
        submit.count = 0
        syncpool.SyncPool.submit = submit

        import contextlib
        orig_subtest = unittest.TestCase.subTest

        @contextlib.contextmanager
        def subtest(self, msg=unittest.case._subtest_msg_sentinel, **params):
            key = None
            if isinstance(msg, str):
                m = msg[len("(execute) "):] if msg.startswith("(execute) ") else msg
                if m in titles:
                    key = titles[m]
                elif m.startswith("tpc-h ") or m.startswith("tpc-ds "):
                    try:
                        key = ("q", int(m.split(" ")[1]) - 1)
                        if REVERSE[0] is not None:
                            key = ("q", REVERSE[0] - 1 - key[1])
                    except ValueError:
                        key = None
            with orig_subtest(self, msg, **params):
                if key is None:
                    yield
                else:
                    prev = enter(key)
                    try:
                        yield
                    finally:
                        leave(prev)
        unittest.TestCase.subTest = subtest

        def wrap_pairs(orig):
            def pairs(filename):
                items = list(enumerate(orig(filename)))
                if os.environ.get("TRACE_REVERSE"):
                    items.reverse()  # same cases, last first: cache effects show up in other cases
                for idx, item in items:
                    meta, sql, expected = item
                    last_case[0] = (filename, idx)
                    titles[meta.get("title") or f"{idx + 1}, {sql}"] = ("p", filename, idx)
                    yield item
                last_case[0] = None
            return pairs
        r0 = resource.getrusage(resource.RUSAGE_SELF)
        ok, detail = True, ""
        try:
            path, cls, meth = t["id"].split("::")
            modname = path[:-3].replace("/", ".")
            mod = importlib.import_module(modname)
            if modname == "tests.test_optimizer" and hasattr(mod, "load_sql_fixture_pairs"):
                mod.load_sql_fixture_pairs = wrap_pairs(mod.load_sql_fixture_pairs)
            if modname == "tests.test_executor" and os.environ.get("TRACE_REVERSE") and meth in ("test_execute_tpch", "test_execute_tpcds"):
                orig_lp = mod.load_sql_fixture_pairs
                want = "tpc-h" if meth == "test_execute_tpch" else "tpc-ds"

                def rev_pairs(filename, _orig=orig_lp, _want=want):
                    items = list(_orig(filename))
                    if _want in filename:
                        REVERSE[0] = len(items)
                        items.reverse()
                    return iter(items)
                mod.load_sql_fixture_pairs = rev_pairs
            case = getattr(mod, cls)(meth)
            result = unittest.TestResult()
            suite = unittest.TestSuite([case])
            suite.run(result)
            bad = result.errors + result.failures + [(c, "unexpected success") for c in result.unexpectedSuccesses]
            if bad:
                ok, detail = False, "\n".join(str(b[1])[-800:] for b in bad)
            if result.skipped:
                detail += "\nSKIPPED " + str(result.skipped[0][1])
        except BaseException:
            ok, detail = False, traceback.format_exc()[-1500:]
        mon.set_events(tool, 0)
        r1 = resource.getrusage(resource.RUSAGE_SELF)
        cpu = (r1.ru_utime - r0.ru_utime) + (r1.ru_stime - r0.ru_stime)
        out = {
            "lines": {(f[len(rp):], kind): sorted(s) for (f, kind), s in lines.items()},
            "opens": sorted(opens),
            "spawn": spawn[0],
            "classes": sorted(classes),
            "cases": {k: {(f[len(rp):], kind): sorted(s) for (f, kind), s in v.items()} for k, v in cases.items()},
            "ok": ok,
            "detail": detail,
            "cpu": cpu,
        }
        with open(os.path.join(OUT, f"{t['number']}.pkl.tmp"), "wb") as fh:
            pickle.dump(out, fh, protocol=5)
        os.rename(os.path.join(OUT, f"{t['number']}.pkl.tmp"), os.path.join(OUT, f"{t['number']}.pkl"))
        os._exit(0)


    HEAVY = float(os.environ.get("TRACE_HEAVY", "1e9"))  # only tests at most this many recorded seconds
    LIGHT = float(os.environ.get("TRACE_LIGHT", "-1"))  # only tests above this many recorded seconds
    todo = [t for t in tests if t["id"].startswith(ONLY) and LIGHT < t["seconds"] <= HEAVY
            and not os.path.exists(os.path.join(OUT, f"{t['number']}.pkl"))]
    running = {}
    start = time.time()
    done = 0
    while todo or running:
        while todo and len(running) < JOBS:
            t = todo.pop(0)
            pid = os.fork()
            if pid == 0:
                try:
                    child(t)
                except BaseException:
                    import traceback as _tb
                    with open(os.path.join(OUT, f"{t['number']}.err"), "w") as fh:
                        fh.write(_tb.format_exc())
                finally:
                    os._exit(1)
            running[pid] = (t, time.time())
            print(f"start {pid} {t['number']} {t['id']}", file=sys.stderr, flush=True)
        pid, status = os.waitpid(-1, os.WNOHANG)
        if pid == 0:
            now = time.time()
            for p, (tt, t0) in list(running.items()):
                if now - t0 > 400:
                    os.kill(p, signal.SIGKILL)
            time.sleep(0.05)
            continue
        t, t0 = running.pop(pid)
        done += 1
        if status != 0:
            print(f"child for {t['number']} {t['id']} exited {status}", file=sys.stderr)
        if done % 100 == 0:
            print(f"{done} done, {len(todo)} left, {time.time()-start:.0f}s", file=sys.stderr)
    print(f"all done in {time.time()-start:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    _main()
