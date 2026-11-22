"""Measure native CPU per fixture case / TPC query for the case-mode tests (in-process pool)."""
import contextlib, importlib, json, os, pickle, resource, sys, unittest, zlib
REPO = os.path.abspath(sys.argv[1]); MAPDIR = os.path.abspath(sys.argv[2]); OUT = os.path.abspath(sys.argv[3])
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "selector"))
os.chdir(REPO); sys.path.insert(0, REPO); os.environ["SKIP_INTEGRATION"] = "0"
import syncpool; syncpool.install()
core = pickle.loads(zlib.decompress(open(os.path.join(MAPDIR, "core.pkl.z"), "rb").read()))
def cpu():
    r = resource.getrusage(resource.RUSAGE_SELF); return r.ru_utime + r.ru_stime
acc = {}; cur = [None]; last = [None]; titles = {}; meth = [None]; count = [0]
orig_submit = syncpool.SyncPool.submit
def submit(self, fn, /, *a, **k):
    if meth[0] in ("test_execute_tpch", "test_execute_tpcds"):
        key = ("q", count[0]); count[0] += 1
    elif last[0] is not None:
        key = ("p",) + last[0]
    else:
        return orig_submit(self, fn, *a, **k)
    t = cpu()
    try:
        return orig_submit(self, fn, *a, **k)
    finally:
        acc[key] = acc.get(key, 0.0) + cpu() - t
syncpool.SyncPool.submit = submit
orig_sub = unittest.TestCase.subTest
@contextlib.contextmanager
def subtest(self, msg=unittest.case._subtest_msg_sentinel, **params):
    key = None
    if isinstance(msg, str):
        m = msg[len("(execute) "):] if msg.startswith("(execute) ") else msg
        key = titles.get(m)
        if key is None and (m.startswith("tpc-h ") or m.startswith("tpc-ds ")):
            key = ("q", int(m.split(" ")[1]) - 1)
    t = cpu()
    with orig_sub(self, msg, **params):
        yield
    if key is not None:
        acc[key] = acc.get(key, 0.0) + cpu() - t
unittest.TestCase.subTest = subtest
def wrap(orig):
    def pairs(filename):
        for idx, item in enumerate(orig(filename)):
            meta, sql, expected = item
            last[0] = (filename, idx); titles[meta.get("title") or f"{idx + 1}, {sql}"] = ("p", filename, idx)
            yield item
        last[0] = None
    return pairs
out = {}
mods = {}
for mi, keys in core["case_tests"].items():
    tid = core["ids"][mi]; path, cls, m = tid.split("::")
    modname = path[:-3].replace("/", ".")
    if modname not in mods:
        mods[modname] = importlib.import_module(modname)
        if modname == "tests.test_optimizer":
            mods[modname].load_sql_fixture_pairs = wrap(mods[modname].load_sql_fixture_pairs)
    acc.clear(); count[0] = 0; meth[0] = m
    t = cpu(); r = unittest.TestResult(); unittest.TestSuite([getattr(mods[modname], cls)(m)]).run(r); total = cpu() - t
    case_sum = sum(acc.values())
    out[tid] = {"total": round(total, 4), "base": round(max(0.0, total - case_sum), 4),
                "cases": {repr(k): round(v, 5) for k, v in acc.items()}}
    print(f"{total:7.3f} total {case_sum:7.3f} in cases  {len(acc)} cases  {tid}", flush=True)
json.dump(out, open(OUT, "w"))
