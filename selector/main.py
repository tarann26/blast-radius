"""Blast radius selector: for each patch, pick the sqlglot tests it can break and split them."""
import os as _os
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    _os.environ.setdefault(_v, "1")  # forked children must not inherit a thread pool
import ast
import json
import os
import pickle
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import diffparse  # noqa: E402
import overlay  # noqa: E402

DATA = os.environ.get("BR_DATA", "/data")
REPO = os.environ.get("BR_REPO", "/app/repo")
MAP = os.environ.get("BR_MAP", os.path.join(HERE, "..", "map"))
TMP = os.environ.get("BR_TMP", "/tmp")
RUN_MAX = float(os.environ.get("BR_RUN_MAX", "1.0"))  # tests slower than this are never run, only included
VERIFY = os.environ.get("BR_VERIFY", "1") == "1"
T0 = time.monotonic()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def bits(x):
    out = []
    i = 0
    while x:
        if x & 1:
            out.append(i)
        x >>= 1
        i += 1
    return out


def bits_fast(x):
    out = []
    while x:
        low = x & -x
        out.append(low.bit_length() - 1)
        x ^= low
    return out


class Map:
    def __init__(self, path, ids):
        import zlib
        self.dir = path
        with open(os.path.join(path, "core.pkl.z"), "rb") as fh:
            core = pickle.loads(zlib.decompress(fh.read()))
        self.core = core
        self.m = core
        self._zlib = zlib
        self._rt = {}
        self._cases = {}
        self._rt_files = set(core["rt_files"])
        self._case_files = set(core["case_files"])
        if core["ids"] == ids:
            self.remap = None
        else:
            pos = {t: i for i, t in enumerate(ids)}
            self.remap = [pos.get(t) for t in core["ids"]]
        self.unknown = 0
        if self.remap is not None:
            seen = {p for p in self.remap if p is not None}
            for i in range(len(ids)):
                if i not in seen:
                    self.unknown |= 1 << i
        self.n = len(ids)
        self.all = (1 << self.n) - 1

    def _load(self, sub, path):
        with open(os.path.join(self.dir, sub, path.replace("/", "__") + ".z"), "rb") as fh:
            return pickle.loads(self._zlib.decompress(fh.read()))

    def conv(self, b):
        if self.remap is None:
            return b
        out = 0
        for i in bits_fast(b):
            p = self.remap[i]
            if p is not None:
                out |= 1 << p
        return out | self.unknown

    def conv_index(self, i):
        return i if self.remap is None else self.remap[i]

    def rt(self, path):
        if path not in self._rt:
            self._rt[path] = self._load("rt", path) if path in self._rt_files else {}
        return self._rt[path]

    def cases(self, path):
        if path not in self._cases:
            self._cases[path] = self._load("cases", path) if path in self._case_files else {}
        return self._cases[path]

    def importers(self, path):
        return self.conv(self.core["importers"].get(path, 0))

    def opens(self, path):
        return self.conv(self.core["opens"].get(path, 0))

    def spawn(self):
        return self.conv(self.core["spawn"])

    def lister(self):
        return self.conv(self.core.get("lister", 0))


def _parse(text):
    try:
        return ast.parse(text.encode("utf-8", "surrogateescape"))
    except (SyntaxError, ValueError):
        return None


def _scopes(tree):
    """Function-like scopes: (qualname, body_start, end, node). Lambdas are named by position."""
    out = []
    counters = {}

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = f"{prefix}{child.name}"
                start = child.body[0].lineno if child.body else child.lineno
                # docstring is not part of what runs
                out.append((q, start, child.end_lineno, child))
                visit(child, q + ".")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, ast.Lambda):
                k = counters.get(prefix, 0)
                counters[prefix] = k + 1
                q = f"{prefix}<lambda#{k}>"
                out.append((q, child.body.lineno, child.body.end_lineno, child))
                visit(child, q + ".")
            else:
                visit(child, prefix)

    visit(tree, "")
    return out


def _innermost(scopes, line):
    best = None
    for q, s, e, node in scopes:
        if s <= line <= e and (best is None or (e - s) < (best[2] - best[1])):
            best = (q, s, e, node)
    return best


def _func_sig(node):
    """What a function scope looks like from outside its body: the names it binds and its kind."""
    if isinstance(node, ast.Lambda):
        return ast.dump(node.args)
    return (ast.dump(node.args), [ast.dump(d) for d in node.decorator_list], ast.dump(node.returns) if node.returns else None,
            type(node).__name__)


def _structural(old_node, new_node):
    """True when a body change alters how the function is compiled as a whole (generator, locals, globals)."""
    def facts(node):
        names, flags = set(), set()
        for n in ast.walk(node):
            if isinstance(n, (ast.Yield, ast.YieldFrom)):
                flags.add("yield")
            elif isinstance(n, ast.Await):
                flags.add("await")
            elif isinstance(n, (ast.Global, ast.Nonlocal)):
                names.update(("g:" + x) for x in n.names)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                names.add(n.id)
        return names, flags
    return facts(old_node) != facts(new_node)


def _src(lines, node):
    return "\n".join(lines[node.lineno - 1:node.end_lineno])


def _same_list(a, b, ol, nl):
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if type(x) is not type(y):
            return False
        if _src(ol, x).strip() == _src(nl, y).strip() and not _src(ol, x).count("#"):
            continue  # same text and no comment that could hide a difference in line layout
        if isinstance(x, ast.ClassDef):
            head_x = (x.name, [ast.dump(d) for d in x.decorator_list], [ast.dump(d) for d in x.bases],
                      [ast.dump(k) for k in x.keywords], [ast.dump(tp) for tp in getattr(x, "type_params", [])])
            head_y = (y.name, [ast.dump(d) for d in y.decorator_list], [ast.dump(d) for d in y.bases],
                      [ast.dump(k) for k in y.keywords], [ast.dump(tp) for tp in getattr(y, "type_params", [])])
            if head_x != head_y or not _same_list(x.body, y.body, ol, nl):
                return False
            continue
        if ast.dump(x) != ast.dump(y):
            return False
    return True


def _same_code(old_tree, new_tree, ol, nl):
    """True when the two modules have the same syntax tree (comments, blank lines and layout aside)."""
    try:
        return _same_list(old_tree.body, new_tree.body, ol, nl)
    except Exception:
        return ast.dump(old_tree) == ast.dump(new_tree)


def _classes(tree):
    """(qualname, header_start, body_start, end) for every class."""
    out = []

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                q = f"{prefix}{child.name}"
                hs = min([d.lineno for d in child.decorator_list] + [child.lineno])
                bs = child.body[0].lineno if child.body else child.lineno
                out.append((q, hs, bs, child.end_lineno, child))
                visit(child, q + ".")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return out


def _class_at(classes, line):
    """The class a definition-time line belongs to, or None for module level."""
    best = None
    for q, hs, bs, e, node in classes:
        if hs <= line <= e and (best is None or (e - hs) < (best[3] - best[1])):
            best = (q, hs, bs, e, node)
    return best


def _func_header_at(scopes, line):
    """A function whose decorators or signature hold this line."""
    for q, s, e, node in scopes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            hs = min([d.lineno for d in node.decorator_list] + [node.lineno])
            if hs <= line < s:
                return q
    return None


def _stmt_at(tree, line):
    best = None
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt) and node.lineno <= line <= (node.end_lineno or node.lineno):
            if best is None or (node.end_lineno - node.lineno) < (best.end_lineno - best.lineno):
                best = node
    return best


def _bound_names(stmt):
    names = set()
    if stmt is None:
        return names
    targets = []
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets = [stmt.target]
    elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(stmt.name)
    for tgt in targets:
        for n in ast.walk(tgt):
            if isinstance(n, ast.Name):
                names.add(n.id)
    return names


def _rt_span(rt, s, e):
    acc = 0
    for ln in range(s, e + 1):
        b = rt.get(ln)
        if b:
            acc |= b
    return acc


def scope_impact(M, path, old_text, new_text, hunks):
    imp = M.importers(path)
    if old_text is None:
        return M.lister() | M.spawn(), 0, "new module"
    if new_text is None:
        return 0, imp | M.spawn() | M.lister(), "deleted module"
    old_tree = _parse(old_text)
    new_tree = _parse(new_text)
    if new_tree is None:
        return 0, imp | M.spawn(), "syntax error"
    if old_tree is None:
        return M.all, 0, "unparsable original"
    if _same_code(old_tree, new_tree, old_text.split("\n"), new_text.split("\n")):
        return 0, 0, "same code"
    mod = path[:-3].replace("/", ".")
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]
    old_scopes, new_scopes = _scopes(old_tree), _scopes(new_tree)
    old_by_q = {q: (s, e, n) for q, s, e, n in old_scopes}
    new_by_q = {q: (s, e, n) for q, s, e, n in new_scopes}
    old_classes, new_classes = _classes(old_tree), _classes(new_tree)
    removed, _inserted = diffparse.changed_old_lines(hunks)
    added = diffparse.changed_new_lines(hunks)
    touched = set()   # function scopes whose body changed (qualnames)
    deftime = []      # (side, line) definition-time changes
    for line in removed:
        sc = _innermost(old_scopes, line)
        if sc is None:
            deftime.append(("old", line))
        else:
            touched.add(sc[0])
    for line in added:
        sc = _innermost(new_scopes, line)
        if sc is None:
            deftime.append(("new", line))
        elif sc[0] not in old_by_q:
            deftime.append(("new", new_by_q[sc[0]][2].lineno))
        else:
            touched.add(sc[0])
    for q in list(touched):
        if q not in new_by_q:
            deftime.append(("old", old_by_q[q][2].lineno))
            continue
        on, nn = old_by_q[q][2], new_by_q[q][2]
        if _func_sig(on) != _func_sig(nn):
            deftime.append(("old", on.lineno))
    # scopes that vanished or appeared change the definitions around them
    for q in set(old_by_q) - set(new_by_q):
        deftime.append(("old", old_by_q[q][2].lineno))
    for q in set(new_by_q) - set(old_by_q):
        deftime.append(("new", new_by_q[q][2].lineno))
    rt = M.rt(path)
    raw = 0
    for q in touched:
        if q in old_by_q:
            s, e, _ = old_by_q[q]
            raw |= _rt_span(rt, s, e)
            SPANS.append((path, s, e))
    cand = M.conv(raw)
    notes = []
    classes_map = M.core.get("classes", {})
    refs = M.core.get("refs", {})
    importers_raw = M.core["importers"]
    seen = set()
    deft = 0  # tests reached through a definition-time change: every case of theirs may differ
    for side, line in deftime:
        tree, classes, scopes = (old_tree, old_classes, old_scopes) if side == "old" else (new_tree, new_classes, new_scopes)
        fq = _func_header_at(scopes, line)
        if fq and fq in old_by_q:
            s, e, _ = old_by_q[fq]
            cand |= M.conv(_rt_span(rt, s, e))
            SPANS.append((path, s, e))
        cls = _class_at(classes, line)
        stmt = _stmt_at(tree, line)
        names = _bound_names(stmt)
        if fq:
            names.add(fq.split(".")[-1])
        key = (cls[0] if cls else None, tuple(sorted(names)))
        if key in seen:
            continue
        seen.add(key)
        if cls is None:
            deft |= imp
            notes.append("module-level")
            continue
        users = classes_map.get(f"{mod}:{cls[0]}", 0)
        if not users:
            deft |= imp
            notes.append(f"class {cls[0]} (no instances)")
            continue
        acc = users
        simple = cls[0].split(".")[-1]
        for name in names:
            for ref in refs.get((simple, name), ()):
                if ref[0] == "class":
                    acc |= classes_map.get(ref[1], 0) or importers_raw.get(ref[1].split(":")[0].replace(".", "/") + ".py", 0)
                elif ref[0] == "func":
                    cand |= M.conv(_rt_span(M.rt(ref[1]), ref[2], ref[3]))
                    SPANS.append((ref[1], ref[2], ref[3]))
                elif ref[0] == "module":
                    acc |= importers_raw.get(ref[1], 0)
        deft |= M.conv(acc)
        notes.append(f"class {cls[0]}")
    if path in M.core.get("import_modules", ()):
        irt = M.core.get("import_rt", {}).get(path, ())
        if deftime or any(s <= ln <= e for ln in irt for (pp, s, e) in SPANS if pp == path):
            deft |= M.spawn()
    cand |= deft
    FULL[0] |= deft
    note = f"{len(touched)} functions" + (("; " + ", ".join(sorted(set(notes)))) if notes else "")
    return cand, 0, note


def pack(nums, secs):
    if not nums:
        return []
    items = sorted(((round(secs[n] * 1000), n) for n in nums), reverse=True)
    best = None
    for k in range(1, min(8, len(items)) + 1):
        loads = [0] * k
        groups = [[] for _ in range(k)]
        for ms, n in items:
            i = loads.index(min(loads))
            loads[i] += ms
            groups[i].append(n)
        cost = max(loads) + 2000 * k
        if best is None or cost < best[0]:
            best = (cost, groups)
    return best[1]


def main():
    with open(os.path.join(DATA, "tests.json")) as fh:
        tests = json.load(fh)["tests"]
    ids = [t["id"] for t in tests]
    secs = [t["seconds"] for t in tests]
    dotted = [_dotted(t["id"]) for t in tests]
    M = Map(MAP, ids)
    for mi, keys in M.core.get("case_tests", {}).items():
        n = M.conv_index(mi)
        if n is not None:
            CASE_KEYS[n] = keys
            MAP_INDEX[n] = mi
    INPROC.update(M.core.get("inproc", {}))
    CASE_CPU.update(M.core.get("case_cpu", {}))
    for n, keys in CASE_KEYS.items():
        CASE_POS[n] = {k: j for j, k in enumerate(keys)}
    EXEC["common"] = M.core.get("exec_common", {})
    EXEC["tests"] = M.conv(M.core.get("exec_tests", 0))
    cdir = os.path.join(DATA, "commits")
    commits = sorted(f[:-6] for f in os.listdir(cdir) if f.endswith(".patch"))
    plans = {}
    for c in commits:
        try:
            plans[c] = analyze(M, os.path.join(cdir, c + ".patch"), secs)
        except Exception as e:  # anything unexpected: run nothing risky, include everything
            log(f"{c}: analysis failed ({type(e).__name__}: {e}); including every test")
            plans[c] = {"static": M.all, "verify": 0, "changes": None, "note": "fallback"}
    results = verify(plans, dotted, secs, ids)
    for c in commits:
        p = plans[c]
        out = p["static"] | results.get(c, 0)
        nums = bits_fast(out)
        print(json.dumps({"commit": c, "workers": pack(nums, secs)}), flush=True)
    log(f"done in {time.monotonic()-T0:.1f}s wall, cpu {time.process_time():.1f}s (main only)")


def _dotted(tid):
    path, cls, meth = tid.split("::")
    return path[:-3].replace("/", ".") + "." + cls + "." + meth


SPANS = []
FULL = [0]


def _filter_comments(s):
    return "\n".join([line for line in s.splitlines() if line and not line.startswith("--")])


def fixture_pairs(text):
    """The same cases tests/helpers.py load_sql_fixture_pairs yields, as hashable tuples."""
    statements = _filter_comments(text).split(";")
    out = []
    for i in range(0, len(statements), 2):
        if i + 1 < len(statements):
            sql = statements[i].strip()
            lines = sql.split("\n")
            j = 0
            meta = {}
            ok = True
            while j < len(lines) and lines[j].startswith("#"):
                if ":" not in lines[j]:
                    ok = False
                    break
                key, val = lines[j].split(":", maxsplit=1)
                meta[key.lstrip("#").strip()] = val.strip()
                j += 1
            if not ok:
                return None
            out.append((tuple(sorted(meta.items())), "\n".join(lines[j:]), statements[i + 1].strip()))
    return out


def analyze(M, patch_path, secs):
    del SPANS[:]
    FULL[0] = 0
    with open(patch_path, "rb") as fh:
        text = fh.read().decode("utf-8", "surrogateescape")
    fps = diffparse.parse(text)
    changes = {}
    cand = 0
    static = 0
    notes = []
    content = {}  # test number -> set of fixture case tuples that changed
    for fp in fps:
        old_path, new_path = fp.old, fp.new
        paths = {p for p in (old_path, new_path) if p}
        if fp.binary:
            for p in paths:
                static |= M.opens(p)
            notes.append(f"binary {paths}")
            continue
        if fp.mode_only:
            for p in paths:
                cand |= M.opens(p)
                FULL[0] |= M.opens(p)
            continue
        old_text = None
        if old_path:
            with open(os.path.join(REPO, old_path), "rb") as fh:
                old_text = fh.read().decode("utf-8", "surrogateescape")
        if fp.deleted:
            new_text = None
        else:
            new_text = diffparse.apply(old_text if old_text is not None else "", fp.hunks)
        if old_path and new_path and old_path != new_path:
            changes[old_path] = None
            changes[new_path] = new_text
        else:
            changes[new_path or old_path] = new_text
        for p in paths:
            text_old = old_text if p == old_path else None
            text_new = new_text if p == new_path else None
            if p.endswith(".py") and (p.startswith("sqlglot/") or p.startswith("tests/")):
                c, s, note = scope_impact(M, p, text_old, text_new, fp.hunks if old_path == new_path else [])
                cand |= c
                static |= s
                notes.append(f"{p}: {note}")
                continue
            readers = M.opens(p)
            c = readers
            if text_old is None or text_new is None:
                c |= M.lister()
                d = os.path.dirname(p)
                for f, b in M.core["opens"].items():
                    if os.path.dirname(f) == d:
                        c |= M.conv(b)
            if p.endswith(".py"):
                c |= M.spawn()
            cand |= c
            full = c
            if p.startswith("tests/fixtures/") and text_old is not None and text_new is not None:
                old_cases, new_cases = fixture_pairs(text_old), fixture_pairs(text_new)
                rel = p[len("tests/fixtures/"):]
                if old_cases is not None and new_cases is not None:
                    changed = set(new_cases) - set(old_cases)
                    for n in bits_fast(readers):
                        keys = CASE_KEYS.get(n)
                        if keys and any(k[0] == "p" and k[1] == rel for k in keys):
                            content.setdefault(n, set()).update(changed)
                            full &= ~(1 << n)
            FULL[0] |= full
            notes.append(f"{p}: file")
    cand &= ~static
    # which cases each case-mode candidate must run
    select = {}
    for n in bits_fast(cand):
        keys = CASE_KEYS.get(n)
        if not keys or FULL[0] >> n & 1:
            continue
        mi = MAP_INDEX[n]
        mask = 0
        for path, s, e in SPANS:
            tm = M.cases(path).get(mi)
            if tm:
                for ln in range(s, e + 1):
                    v = tm.get(ln)
                    if v:
                        mask |= v
        select[n] = {"keys": [keys[i] for i in bits_fast(mask)], "content": content.get(n, set())}
    fixture_only = all(not (p.endswith(".py")) for p in changes) and bool(changes)
    return {"fixture_only": fixture_only, "static": static, "verify": cand, "changes": changes, "note": "; ".join(notes), "select": select,
            "spans": list(SPANS), "full": FULL[0]}


CASE_KEYS = {}
MAP_INDEX = {}
EXEC = {"common": {}, "tests": 0}


ALPHA = float(os.environ.get("BR_ALPHA", "2.0"))   # scorer CPU seconds per second measured here
OVH = float(os.environ.get("BR_OVH", "0.3"))       # scorer CPU of one verification child, imports included
Q_CODE = float(os.environ.get("BR_Q_CODE", "0.35"))  # chance a code change breaks nothing it reaches
Q_FIXTURE = float(os.environ.get("BR_Q_FIXTURE", "0.05"))
INPROC = {}
CASE_CPU = {}
CASE_POS = {}


def pack_cost(nums, secs):
    if not nums:
        return 0.0
    items = sorted((secs[n] for n in nums), reverse=True)
    best = None
    for k in range(1, min(8, len(items)) + 1):
        loads = [0.0] * k
        for s in items:
            i = loads.index(min(loads))
            loads[i] += s
        c = max(loads) + 2 * k
        best = c if best is None or c < best else best
    return best


def verify(plans, dotted, secs, ids):
    import runner
    results = {}
    jobs = []
    for c, p in plans.items():
        v = p["verify"]
        if not v:
            continue
        nums = bits_fast(v)
        est, filters = {}, {}
        for n in nums:
            base = INPROC.get(ids[n])
            cost = base * ALPHA if base is not None else secs[n]
            sel = p["select"].get(n)
            cc = CASE_CPU.get(ids[n])
            if cc is not None:
                cost = cc["total"] * ALPHA
            if sel is not None:
                if cc is not None:
                    kpos = CASE_POS[n]
                    picked = sum(cc["cases"][kpos[k]] for k in sel["keys"] if k in kpos)
                    picked += len(sel["content"]) * (sum(cc["cases"]) / max(1, len(cc["cases"])))
                    cost = (cc["base"] + picked) * ALPHA
                else:
                    total = max(1, len(CASE_KEYS[n]))
                    k = len(sel["keys"]) + len(sel["content"])
                    cost = cost * max(k, 0.5) / total
                filters[dotted[n]] = sel
            est[n] = cost
        q = Q_FIXTURE if p.get("fixture_only") else Q_CODE
        # tests far cheaper to run than to include: always run them
        forced = [n for n in nums if est[n] < 0.6 * secs[n] - 0.05 and est[n] < 8]
        optional = [n for n in nums if n not in forced]
        # likely failures first: tests reached through a hit fixture case or named after the changed module
        stems = {os.path.splitext(os.path.basename(f))[0] for f in (p["changes"] or {})}
        def order(n):
            sel = p["select"].get(n)
            direct = sel is not None and (sel["keys"] or sel["content"])
            home = any(stem and stem in ids[n] for stem in stems)
            return (0 if direct else 1 if home else 2, est[n])
        optional.sort(key=order)
        ovh = 0.0 if forced else OVH
        verify_opt = False
        if optional and p["changes"] is not None and VERIFY:
            s_opt = pack_cost(optional, secs)
            c1 = est[optional[0]]
            lhs = sum(est[n] for n in optional) + ovh + ((1 - q) / max(q, 0.01)) * (c1 + ovh)
            verify_opt = lhs < s_opt
        run = sorted(forced, key=lambda n: est[n]) + (optional if verify_opt else [])
        if not verify_opt:
            p["static"] |= sum(1 << n for n in optional)
        if not run:
            continue
        if not VERIFY or p["changes"] is None:
            p["static"] |= sum(1 << n for n in run)
            continue
        root = os.path.join(TMP, "ov", c)
        try:
            overlay.build(REPO, root, p["changes"])
        except Exception as e:
            log(f"{c}: overlay failed ({e}); including candidates")
            p["static"] |= sum(1 << n for n in run)
            continue
        timeouts = {dotted[n]: max(3.0, 5 * secs[n] + 2) for n in run}
        budget = 10 + sum(timeouts.values())
        j = runner.Job(c, root, [dotted[n] for n in run], timeouts, min(budget, 90))
        j.filters = {d: f for d, f in filters.items() if d in set(j.tests)}
        j.stop_on = frozenset(dotted[n] for n in optional) if verify_opt else frozenset()
        tpc = {"tests.test_executor.TestExecutor." + m for m in ("test_optimized_tpch", "test_execute_tpch", "test_execute_tpcds")}
        if not any(dotted[n] in tpc for n in run):
            common = EXEC["common"]
            hit = any(ln in common.get(path, ()) for path, s, e in p["spans"] for ln in range(s, e + 1))
            j.skip_tpc = not hit and not (p["full"] & EXEC["tests"])
        jobs.append(j)
    heavy = sum(1 for j in jobs if any(t.startswith(("tests.test_optimizer.", "tests.test_executor.")) for t in j.tests))
    if heavy >= 2:
        for name in ("numpy", "pandas", "duckdb"):
            try:
                __import__(name)
            except Exception:
                pass
    if jobs:
        runner.run_jobs(jobs, parallel=2)
    back = {d: i for i, d in enumerate(dotted)}
    for j in jobs:
        bad = j.failing()
        results[j.key] = sum(1 << back[d] for d in bad if d in back)
        overlay.remove(j.root)
        imp = sum(c for _, c in j.imports)
        nf = sum(1 for d in j.tests if d in j.filters)
        done = sum(1 for d in j.tests if d in j.done)
        if os.environ.get("BR_DEBUG"):
            top = sorted(j.times.items(), key=lambda kv: -kv[1])[:5]
            log(f"   slowest: {[(k.rsplit('.', 1)[-1], v) for k, v in top]}")
        log(f"{j.key}: ran {done}/{len(j.tests)} ({nf} filtered{', stopped' if j.stop_now else ''}), failing {len(bad)}, cpu {j.cpu:.2f}s (imports {imp:.2f}s)")
    return results


if __name__ == "__main__":
    main()
