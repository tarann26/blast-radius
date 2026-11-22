"""Run chosen sqlglot tests against a patched overlay tree in forked children."""
import importlib
import json
import os
import select
import signal
import sys
import time
import unittest


_CUR = [None, 0]  # the running test's case filter, and how many pool tasks it has sent


def _install_filters():
    import concurrent.futures as cf
    import syncpool

    orig = syncpool.SyncPool.submit

    def submit(self, fn, /, *args, **kwargs):
        f = _CUR[0]
        if f is not None and f.get("q") is not None:
            k = _CUR[1]
            _CUR[1] += 1
            if k not in f["q"]:
                fut = cf.Future()
                fut.set_result(None)
                return fut
        return orig(self, fn, *args, **kwargs)

    syncpool.SyncPool.submit = submit


def _wrap_pairs(orig):
    def pairs(filename):
        f = _CUR[0]
        for idx, item in enumerate(orig(filename)):
            if f is None or f.get("p") is None:
                yield item
                continue
            meta, sql, expected = item
            if (filename, idx) in f["p"] or (tuple(sorted(meta.items())), sql, expected) in f["content"]:
                yield item
    return pairs


class _Timeout(BaseException):
    pass


def _alarm(signum, frame):
    raise _Timeout("test timed out")


class _Result(unittest.TestResult):
    """Collects failing test ids and reports each finished test at once."""

    def __init__(self, wfd, timeouts, filters):
        super().__init__()
        self.wfd = wfd
        self.timeouts = timeouts
        self.filters = filters
        self.bad = set()

    def _key(self, test):
        if isinstance(test, unittest.case._SubTest):
            test = test.test_case
        if isinstance(test, unittest.TestCase):
            return f"{type(test).__module__}.{type(test).__qualname__}.{test._testMethodName}"
        return None

    def startTest(self, test):
        super().startTest(test)
        import resource
        r = resource.getrusage(resource.RUSAGE_SELF)
        self.t0 = r.ru_utime + r.ru_stime
        k = self._key(test)
        _CUR[0] = self.filters.get(k)
        _CUR[1] = 0
        t = self.timeouts.get(k, 10.0)
        os.write(self.wfd, (json.dumps(["start", k]) + "\n").encode())
        signal.setitimer(signal.ITIMER_REAL, t)

    def stopTest(self, test):
        signal.setitimer(signal.ITIMER_REAL, 0)
        _CUR[0] = None
        super().stopTest(test)
        k = self._key(test)
        import resource
        r = resource.getrusage(resource.RUSAGE_SELF)
        dt = round(r.ru_utime + r.ru_stime - getattr(self, "t0", 0.0), 3)
        os.write(self.wfd, (json.dumps(["done", k, k in self.bad, dt]) + "\n").encode())

    def addError(self, test, err):
        k = self._key(test)
        if k is None:  # setUpClass / setUpModule failure: the holder names the class or module
            os.write(self.wfd, (json.dumps(["fixture", str(test)]) + "\n").encode())
        else:
            self.bad.add(k)

    def addFailure(self, test, err):
        self.bad.add(self._key(test))

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.bad.add(self._key(subtest))

    def addUnexpectedSuccess(self, test):
        self.bad.add(self._key(test))


def _child(root, plan, timeouts, wfd, filters, skip_tpc, import_check=()):
    """plan: list of (module, [(class, [methods])])."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.chdir(root)
    sys.path.insert(0, root)
    os.environ["PYTHONPATH"] = root
    os.environ["SKIP_INTEGRATION"] = "0"
    signal.signal(signal.SIGALRM, _alarm)
    import syncpool  # in-process stand-in for the tests' spawn pools: no extra interpreters

    syncpool.install()
    _install_filters()
    for modname in import_check:
        try:
            importlib.import_module(modname)
            os.write(wfd, (json.dumps(["impok", modname]) + "\n").encode())
        except BaseException:
            os.write(wfd, (json.dumps(["impfail", modname]) + "\n").encode())
    import resource
    def _cpu():
        r = resource.getrusage(resource.RUSAGE_SELF)
        return r.ru_utime + r.ru_stime
    for modname, classes in plan:
        c0 = _cpu()
        try:
            mod = importlib.import_module(modname)
        except BaseException:
            os.write(wfd, (json.dumps(["module", modname]) + "\n").encode())
            continue
        os.write(wfd, (json.dumps(["import", modname, round(_cpu() - c0, 3)]) + "\n").encode())
        if modname == "tests.test_optimizer" and hasattr(mod, "load_sql_fixture_pairs"):
            mod.load_sql_fixture_pairs = _wrap_pairs(mod.load_sql_fixture_pairs)
        if modname == "tests.test_executor" and skip_tpc:
            mod.TPCH_SCHEMA = {}
            mod.TPCDS_SCHEMA = {}
        for clsname, methods in classes:
            result = _Result(wfd, timeouts, filters)
            cases = []
            for m in methods:
                try:
                    cases.append(getattr(mod, clsname)(m))
                except BaseException:
                    os.write(wfd, (json.dumps(["done", f"{modname}.{clsname}.{m}", True]) + "\n").encode())
            try:
                unittest.TestSuite(cases).run(result)
            except BaseException:
                os.write(wfd, (json.dumps(["class", f"{modname}.{clsname}"]) + "\n").encode())
            signal.setitimer(signal.ITIMER_REAL, 0)
    os.write(wfd, b'["end"]\n')


class Job:
    def __init__(self, key, root, tests, timeouts, budget):
        self.key = key
        self.root = root
        self.tests = tests  # list of dotted ids module.Class.method
        self.timeouts = timeouts
        self.budget = budget
        self.pid = None
        self.rfd = None
        self.buf = b""
        self.done = {}
        self.ended = False
        self.deadline = None
        self.imports = []
        self.running = None
        self.times = {}
        self.import_check = []
        self.import_ok = set()
        self.impfail = set()
        self.cpu = None
        self.wall = None

    filters = {}
    skip_tpc = False
    stop_on = frozenset()
    stop_now = False

    def child_filters(self):
        out = {}
        for d, sel in self.filters.items():
            keys = sel["keys"]
            if any(k[0] == "q" for k in keys) or d.endswith((".test_execute_tpch", ".test_execute_tpcds")):
                out[d] = {"q": {k[1] for k in keys if k[0] == "q"}}
            else:
                out[d] = {"p": {(k[1], k[2]) for k in keys if k[0] == "p"}, "content": set(sel["content"])}
        return out

    def plan(self):
        """Keep the given test order, grouping consecutive tests of one class into one suite."""
        plan = []
        for t in self.tests:
            mod, cls, meth = t.rsplit(".", 2)
            if plan and plan[-1][0] == mod:
                classes = plan[-1][1]
                if classes and classes[-1][0] == cls:
                    classes[-1][1].append(meth)
                else:
                    classes.append((cls, [meth]))
            else:
                plan.append((mod, [(cls, [meth])]))
        return plan

    def start(self):
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                os.close(r)
                _child(self.root, self.plan(), self.timeouts, w, self.child_filters(), self.skip_tpc, self.import_check)
            finally:
                os._exit(0)
        os.close(w)
        self.pid, self.rfd = pid, r
        self.deadline = time.monotonic() + self.budget

    def feed(self, data):
        self.buf += data
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            msg = json.loads(line)
            kind = msg[0]
            if kind == "start":
                self.running = (msg[1], time.monotonic())
            elif kind == "done":
                self.running = None
                self.done[msg[1]] = msg[2]
                if len(msg) > 3:
                    self.times[msg[1]] = msg[3]
                if msg[2] and msg[1] in self.stop_on:
                    self.stop_now = True
            elif kind == "module":
                for t in self.tests:
                    if t.rsplit(".", 2)[0] == msg[1]:
                        self.done[t] = True
            elif kind == "class":
                for t in self.tests:
                    if t.rsplit(".", 1)[0] == msg[1] and t not in self.done:
                        self.done[t] = True
            elif kind == "fixture":
                # "setUpClass (tests.test_x.TestX)" or "setUpModule (tests.test_x)"
                name = msg[1]
                inner = name[name.find("(") + 1:name.rfind(")")]
                late = name.startswith("tearDown")
                for t in self.tests:
                    if t.startswith(inner + ".") and (late or t not in self.done):
                        self.done[t] = True
            elif kind == "impok":
                self.import_ok.add(msg[1])
            elif kind == "impfail":
                self.impfail.add(msg[1])
            elif kind == "import":
                self.imports.append((msg[1], msg[2]))
            elif kind == "end":
                self.ended = True

    def failing(self):
        """Tests that failed, plus every test that did not report (crash or budget)."""
        return {t for t in self.tests if self.done.get(t, True)}


def _stuck(j, now):
    """The running test has overstayed its own limit (a loop the alarm cannot interrupt)."""
    if j.running is None:
        return False
    name, since = j.running
    return now - since > j.timeouts.get(name, 10.0) + 1.5


def run_jobs(jobs, parallel=2, deadline=None):
    """Run jobs, at most `parallel` at once. Each job ends at its budget, when a test hangs,
    or at the global deadline; jobs not started by the deadline never start (their tests count as failing)."""
    pending = list(jobs)
    active = {}
    while pending or active:
        while pending and len(active) < parallel:
            if deadline is not None and time.monotonic() > deadline:
                pending = []
                break
            j = pending.pop(0)
            j.start()
            active[j.rfd] = j
        if not active:
            break
        now = time.monotonic()
        timeout = max(0.0, min(min(j.deadline for j in active.values()) - now, 0.5))
        ready, _, _ = select.select(list(active), [], [], timeout)
        for fd in ready:
            j = active[fd]
            data = os.read(fd, 65536)
            if data:
                j.feed(data)
            if j.stop_now and not j.ended:
                try:
                    os.kill(j.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                _finish(j)
                del active[fd]
            elif not data or j.ended:
                _finish(j)
                del active[fd]
        now = time.monotonic()
        for fd, j in list(active.items()):
            if now >= j.deadline or _stuck(j, now) or (deadline is not None and now > deadline):
                try:
                    os.kill(j.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                _finish(j)
                del active[fd]


def _finish(j):
    try:
        os.close(j.rfd)
    except OSError:
        pass
    try:
        _, _, ru = os.wait4(j.pid, 0)
        j.cpu = ru.ru_utime + ru.ru_stime
    except ChildProcessError:
        j.cpu = None
    j.wall = time.monotonic() - (j.deadline - j.budget)
