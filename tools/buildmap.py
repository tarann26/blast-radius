"""Turn per-test traces into the selector's map (directory of small zlib pickles).

Usage: buildmap.py TESTS_JSON TRACE_DIR OUT_DIR
  core.pkl.z   ids, importers, opens, spawn, lister, classes, refs, case tests, executor-common lines
  rt/<file>.z  {line: bitset of tests} for function code in that file (cases included)
  cases/<file>.z {test index: {line: bitmask over that test's cases}}
"""
import glob, json, os, pickle, sys, zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "selector"))
import refindex  # noqa: E402

tests = json.load(open(sys.argv[1]))["tests"]
ids = [t["id"] for t in tests]
TR, OUT = sys.argv[2], sys.argv[3]
REPO = os.environ.get("BR_REPO", "/Users/taran/Desktop/track4/sqlglot")


def safe(path):
    return path.replace("/", "__")


rt, dt, opens, classes = {}, {}, {}, {}
cases_by_file = {}   # file -> test -> line -> mask
case_tests = {}      # test index -> list of case keys
spawn = lister = 0
cpu = [None] * len(ids)
for i, t in enumerate(tests):
    d = pickle.load(open(os.path.join(TR, f"{t['number']}.pkl"), "rb"))
    b = 1 << i
    cpu[i] = d["cpu"]
    assert d["ok"], t["id"]
    def add_lines(linesd):
        for (f, kind), lines in linesd.items():
            m = (rt if kind else dt).setdefault(f, {})
            for ln in lines:
                m[ln] = m.get(ln, 0) | b
    add_lines(d["lines"])
    cs = d.get("cases") or {}
    if cs:
        keys = sorted(cs, key=lambda k: (k[0], str(k[1:])))
        case_tests[i] = keys
        for ci, k in enumerate(keys):
            add_lines(cs[k])
            for (f, kind), lines in cs[k].items():
                if not kind:
                    continue
                tm = cases_by_file.setdefault(f, {}).setdefault(i, {})
                for ln in lines:
                    tm[ln] = tm.get(ln, 0) | (1 << ci)
    test_src = open(os.path.join(REPO, t["file"])).read()
    reads_files = any(s in test_src for s in ("read_text", "open(", "runpy", "getsource", "linecache"))
    for f in d["opens"]:
        if f.endswith(".pyc") or "__pycache__" in f:
            continue
        if f.endswith(".py") and not reads_files:
            continue  # an import recompiling a stale cache, not a test reading source text
        opens[f] = opens.get(f, 0) | b
    if d["spawn"]:
        spawn |= b
    for k in d.get("classes", ()):
        classes[k] = classes.get(k, 0) | b

# tests whose code lists package directories (a new or deleted module can change what they see)
import re as _re
for i, t in enumerate(tests):
    src = open(os.path.join(REPO, t["file"])).read()
    if _re.search(r"pkgutil|os\.listdir|scandir|\.iterdir\(|glob\(|walk_packages|iter_modules", src):
        lister |= 1 << i

importers = {}
for src in (dt, rt):
    for f, m in src.items():
        acc = importers.get(f, 0)
        for v in m.values():
            acc |= v
        importers[f] = acc

exec_tests = 0
for i, t in enumerate(tests):
    if t["file"] == "tests/test_executor.py":
        exec_tests |= 1 << i
exec_common = {}
for f, m in rt.items():
    ls = [ln for ln, v in m.items() if v & exec_tests == exec_tests]
    if ls:
        exec_common[f] = set(ls)

refs = refindex.build(REPO)
os.makedirs(os.path.join(OUT, "rt"), exist_ok=True)
os.makedirs(os.path.join(OUT, "cases"), exist_ok=True)
for f, m in rt.items():
    open(os.path.join(OUT, "rt", safe(f) + ".z"), "wb").write(zlib.compress(pickle.dumps(m, protocol=5), 6))
for f, m in cases_by_file.items():
    open(os.path.join(OUT, "cases", safe(f) + ".z"), "wb").write(zlib.compress(pickle.dumps(m, protocol=5), 6))
inproc = {}
ip = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "build", "inproc_cpu.json")
if os.path.exists(ip):
    inproc = json.load(open(ip))
imptrace = {"modules": [], "rt": {}}
it = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "build", "import_trace.json")
if os.path.exists(it):
    imptrace = json.load(open(it))
import ast as _ast
case_cpu = {}
cp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "build", "case_cpu.json")
if os.path.exists(cp):
    raw = json.load(open(cp))
    pos = {t: i for i, t in enumerate(ids)}
    for tid, d in raw.items():
        i = pos[tid]
        keys = case_tests.get(i, [])
        kpos = {k: j for j, k in enumerate(keys)}
        per = [0.0] * len(keys)
        for ks, v in d["cases"].items():
            k = _ast.literal_eval(ks)
            if k in kpos:
                per[kpos[k]] += v
        case_cpu[tid] = {"base": d["base"], "total": d["total"], "cases": per}
reads, generic, defreads = refindex.attr_reads(REPO)
def bits_of(locs):
    acc = 0
    for f, ln in locs:
        acc |= rt.get(f, {}).get(ln, 0)
    return acc
generic_all = bits_of([(f, ln) for f, ln, k in generic if k == "all"])
generic_sql = bits_of([(f, ln) for f, ln, k in generic if k == "sql"])
attr_bits = {name: bits_of(locs) | (generic_sql if name.endswith("_sql") else 0) for name, locs in reads.items()}
generic_bits = generic_all
modreads = {}
for key, e in refindex.module_reads(REPO).items():
    modreads[key] = {"bits": bits_of(e["fn"]), "local": sorted(set(e["local"])), "cross": e["cross"],
                     "importers": sorted(set(e["importers"]))}
core = {"modreads": modreads, "attr_bits": attr_bits, "generic_bits": generic_bits, "defreads": defreads, "case_cpu": case_cpu, "import_modules": set(imptrace["modules"]), "import_rt": {f: set(v) for f, v in imptrace["rt"].items()},
        "inproc": inproc, "ids": ids, "importers": importers, "opens": opens, "spawn": spawn, "lister": lister,
        "classes": classes, "refs": refs, "case_tests": case_tests, "exec_common": exec_common,
        "exec_tests": exec_tests, "rt_files": sorted(rt), "case_files": sorted(cases_by_file)}
open(os.path.join(OUT, "core.pkl.z"), "wb").write(zlib.compress(pickle.dumps(core, protocol=5), 6))
tot = sum(os.path.getsize(p) for p in glob.glob(os.path.join(OUT, "**", "*.z"), recursive=True))
print("attr names", len(attr_bits), "generic tests", bin(generic_bits).count("1"), "defreads", len(defreads))
print("rt files", len(rt), "case tests", len(case_tests), "cases", sum(len(v) for v in case_tests.values()),
      "case files", len(cases_by_file), "total bytes", tot, "core bytes", os.path.getsize(os.path.join(OUT, "core.pkl.z")))
