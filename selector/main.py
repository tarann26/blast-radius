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


_PARSE_CACHE = {}


def _parse(text):
    key = hash(text)
    hit = _PARSE_CACHE.get(key)
    if hit is not None and hit[0] == text:
        return hit[1]
    try:
        tree = ast.parse(text.encode("utf-8", "surrogateescape"))
    except (SyntaxError, ValueError):
        tree = None
    if len(text) > 20000:
        _PARSE_CACHE[key] = (text, tree)
    return tree


_SCAN_CACHE = {}


def _scan_cached(tree):
    hit = _SCAN_CACHE.get(id(tree))
    if hit is not None and hit[0] is tree:
        return hit[1]
    res = _scan(tree)
    _SCAN_CACHE[id(tree)] = (tree, res)
    return res


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


def _scan(tree):
    """One pass: function-like scopes (as _scopes) and classes (as _classes)."""
    scopes, classes = [], []
    counters = {}
    FD, AFD, CD, LAM = ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda
    iter_children = ast.iter_child_nodes

    def visit(node, sprefix, cprefix):
        for child in iter_children(node):
            tp = type(child)
            if tp is FD or tp is AFD:
                q = f"{sprefix}{child.name}"
                start = child.body[0].lineno if child.body else child.lineno
                scopes.append((q, start, child.end_lineno, child))
                visit(child, q + ".", f"{cprefix}{child.name}.")
            elif tp is CD:
                cq = f"{cprefix}{child.name}"
                hs = min([d.lineno for d in child.decorator_list] + [child.lineno])
                bs = child.body[0].lineno if child.body else child.lineno
                classes.append((cq, hs, bs, child.end_lineno, child))
                visit(child, f"{sprefix}{child.name}.", cq + ".")
            elif tp is LAM:
                k = counters.get(sprefix, 0)
                counters[sprefix] = k + 1
                q = f"{sprefix}<lambda#{k}>"
                scopes.append((q, child.body.lineno, child.body.end_lineno, child))
                visit(child, q + ".", cprefix)
            elif isinstance(child, (ast.expr_context, ast.operator, ast.cmpop, ast.unaryop, ast.boolop)):
                continue
            else:
                visit(child, sprefix, cprefix)

    visit(tree, "", "")
    return scopes, classes


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


def _doc_lines(tree):
    lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            b = node.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) and isinstance(b[0].value.value, str):
                lines.update(range(b[0].lineno, b[0].end_lineno + 1))
    return lines


def _same_code(old_tree, new_tree, ol, nl, hunks=None):
    """True when the two modules have the same syntax tree (comments, blank lines and layout aside)."""
    try:
        if _same_list(old_tree.body, new_tree.body, ol, nl):
            return True
    except Exception:
        if ast.dump(old_tree) == ast.dump(new_tree):
            return True
    # docstrings: no test reads __doc__ and the doctest file collects nothing under pytest
    if hunks is not None:
        removed, _ = diffparse.changed_old_lines(hunks)
        added = diffparse.changed_new_lines(hunks)
        od, nd = _doc_lines(old_tree), _doc_lines(new_tree)
        if not (removed <= od and added <= nd):
            return False
    return _strip_docs(old_tree) == _strip_docs(new_tree)


def _strip_docs(tree):
    import copy
    t2 = copy.deepcopy(tree)
    for node in ast.walk(t2):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            b = node.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) and isinstance(b[0].value.value, str):
                b[0] = ast.Expr(value=ast.Constant(value=""))
    return ast.dump(t2)


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
    """Innermost statement whose span holds the line (descends only into nodes that contain it)."""
    best = None
    stack = [tree]
    while stack:
        node = stack.pop()
        for child in ast.iter_child_nodes(node):
            ln = getattr(child, "lineno", None)
            if ln is None:
                if isinstance(child, (ast.arguments, ast.comprehension, ast.match_case, ast.withitem)) or not isinstance(child, ast.AST):
                    stack.append(child)
                continue
            if ln <= line <= (getattr(child, "end_lineno", None) or ln):
                if isinstance(child, ast.stmt):
                    if best is None or (child.end_lineno - child.lineno) <= (best.end_lineno - best.lineno):
                        best = child
                stack.append(child)
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


_REGISTRY_MODULES = {"sqlglot/dialects/__init__.py"}  # lazy dialect registry: order pollution seen


def _module_reach(M, path, stmt, old_tree, new_tree):
    """Tests that can notice a module-level definition change, or None when that cannot be bounded."""
    if path in _REGISTRY_MODULES or stmt is None:
        return None
    if not isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.Import, ast.ImportFrom)):
        return None
    names = _bound_names(stmt)
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        names = {(a.asname or a.name).split(".")[0] for a in stmt.names}
    if not names or any(n.startswith("__") for n in names):
        return None
    reads = M.core.get("modreads", {})
    new_names = set()
    for st in new_tree.body:
        new_names |= _bound_names(st)
        if isinstance(st, (ast.Import, ast.ImportFrom)):
            new_names |= {(a.asname or a.name).split(".")[0] for a in st.names}
    acc, todo, seen = 0, list(names), set()
    while todo:
        n = todo.pop()
        if n in seen:
            continue
        seen.add(n)
        e = reads.get((path, n))
        if e is None:
            continue  # nobody reads it
        if e["cross"]:
            return None  # copied into another module at import time
        acc |= e["bits"]
        todo.extend(e["local"])
        if n not in new_names:
            # the name is gone: modules that import it by name fail to import
            for imp_path in e["importers"]:
                mod = imp_path[:-3].replace("/", ".")
                if mod.endswith(".__init__"):
                    mod = mod[: -len(".__init__")]
                IMPORT_CHECK.append((mod, imp_path))
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return acc  # the body's own executors are added from its span
    return acc


def _is_enum(cls_node):
    for b in cls_node.bases:
        name = b.attr if isinstance(b, ast.Attribute) else b.id if isinstance(b, ast.Name) else ""
        if name in ("Enum", "IntEnum", "StrEnum", "Flag", "IntFlag", "AutoName"):
            return True
    return False


_PLAIN_BASES_SKIP = {"NamedTuple", "TypedDict", "Enum", "IntEnum", "StrEnum", "Flag", "IntFlag", "Protocol"}


def _narrowable(stmt, cls_node, names, defreads):
    """A data setting in a plain class whose value only matters where code reads it by name."""
    if not names or not isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    if cls_node.decorator_list or any(k.arg != "metaclass" for k in cls_node.keywords):
        return False  # a metaclass is fine: its named and generic attribute reads are in the read maps
    for b in cls_node.bases:
        bname = b.attr if isinstance(b, ast.Attribute) else b.id if isinstance(b, ast.Name) else None
        if bname in _PLAIN_BASES_SKIP:
            return False
    if stmt not in cls_node.body:
        return False
    return not any(n in defreads for n in names) and not any(n.startswith("__") for n in names)


def _line_map_new_to_old(hunks):
    """Map unchanged new-file lines to old-file lines."""
    pairs = []  # (new_start, old_start, length) runs of unchanged lines inside and between hunks
    mapping = {}
    for os_, ol, ns, nl, body in hunks:
        o = os_ if ol > 0 else os_ + 1
        n = ns if nl > 0 else ns + 1
        for tag, _, _ in body:
            if tag == " ":
                mapping[n] = o
                o += 1
                n += 1
            elif tag == "-":
                o += 1
            else:
                n += 1
    starts = sorted((ns if nl > 0 else ns + 1, os_ if ol > 0 else os_ + 1, ol, nl) for os_, ol, ns, nl, _ in hunks)

    def to_old(n):
        if n in mapping:
            return mapping[n]
        # outside hunks: shift by the net size change of every hunk that ends before n
        shift = 0
        for nstart, ostart, ol, nl in starts:
            if nstart + nl <= n:
                shift = (ostart + ol) - (nstart + nl)
            else:
                break
        return n + shift
    return to_old


def _blocks(tree):
    """Every statement list with its owner node: (owner, list)."""
    out = []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody", "handlers"):
            seq = getattr(node, field, None)
            if isinstance(seq, list) and seq and isinstance(seq[0], (ast.stmt, ast.excepthandler)):
                out.append((node, seq))
        if isinstance(node, ast.Match):
            for case in node.cases:
                out.append((case, case.body))
    return out


def _precise_lines(old_tree, new_tree, hunks, removed, added):
    """Old-file lines whose executors include every test a change can reach (statement level)."""
    try:
        lines = set()
        # removed or modified lines: the old statement that holds them
        for L in removed:
            st = _stmt_at(old_tree, L)
            if st is None:
                continue
            body0 = None
            for field in ("body", "orelse", "finalbody", "handlers"):
                seq = getattr(st, field, None)
                if isinstance(seq, list) and seq:
                    body0 = seq[0].lineno if body0 is None else min(body0, seq[0].lineno)
            if body0 is None:  # simple statement: all its lines
                lines.update(range(st.lineno, (st.end_lineno or st.lineno) + 1))
            else:  # compound: its header (reached whenever the statement runs)
                lines.update(range(st.lineno, body0))
                lines.add(st.lineno)
        # added lines: tests that reach the insertion point = those that ran the statement before it in
        # the same block, or the block's owner when it is the first statement
        to_old = _line_map_new_to_old(hunks)
        added_set = set(added)
        blocks = _blocks(new_tree)
        for L in added:
            st = _stmt_at(new_tree, L)
            if st is None:
                continue
            owner, seq, idx = None, None, None
            for ow, sq in blocks:
                for i, s in enumerate(sq):
                    if s is st:
                        owner, seq, idx = ow, sq, i
                        break
                if seq is not None:
                    break
            if seq is None:
                return None
            anchor = None
            j = idx - 1
            while j >= 0:
                prev = seq[j]
                if not any(ln in added_set for ln in range(prev.lineno, (prev.end_lineno or prev.lineno) + 1)):
                    anchor = prev.lineno
                    break
                j -= 1
            if anchor is None:
                if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.Module, ast.ClassDef)):
                    return None  # first statement of a function or module: fall back to the whole scope
                anchor = owner.lineno
            lines.add(to_old(anchor))
            # a compound statement added around existing code also reaches what it wraps
            lines.update(to_old(x) for x in range(st.lineno, (st.end_lineno or st.lineno) + 1) if x not in added_set)
        return lines
    except Exception:
        return None


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
        return 0, imp | M.spawn() | M.opens(path), "syntax error"
    if old_tree is None:
        return M.all, 0, "unparsable original"
    if _same_code(old_tree, new_tree, old_text.split("\n"), new_text.split("\n"), hunks):
        return 0, 0, "same code"
    mod = path[:-3].replace("/", ".")
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]
    readers = M.opens(path)  # tests that read this source as text
    FULL[0] |= readers
    (old_scopes, old_classes), (new_scopes, new_classes) = _scan_cached(old_tree), _scan(new_tree)
    old_by_q = {q: (s, e, n) for q, s, e, n in old_scopes}
    new_by_q = {q: (s, e, n) for q, s, e, n in new_scopes}
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
            pq = None
            parts = q.split(".")
            for i in range(len(parts) - 1, 0, -1):
                cand_q = ".".join(parts[:i])
                if cand_q in old_by_q:
                    pq = cand_q
                    break
            if pq is not None:
                if pq in new_by_q:
                    touched.add(pq)
                continue  # the enclosing function changed or vanished: handled with it
            deftime.append(("old", old_by_q[q][2].lineno))
            continue
        on, nn = old_by_q[q][2], new_by_q[q][2]
        if _func_sig(on) != _func_sig(nn):
            deftime.append(("old", on.lineno))
    # scopes that vanished or appeared change the definitions around them; a scope nested in a function
    # only changes that function's code
    def parent_func(q, by_q):
        parts = q.split(".")
        for i in range(len(parts) - 1, 0, -1):
            pq = ".".join(parts[:i])
            if pq in by_q:
                return pq
        return None
    for q in set(old_by_q) - set(new_by_q):
        pq = parent_func(q, old_by_q)
        if pq is not None:
            if pq in new_by_q:
                touched.add(pq)
            continue
        deftime.append(("old", old_by_q[q][2].lineno))
    for q in set(new_by_q) - set(old_by_q):
        pq = parent_func(q, new_by_q)
        if pq is not None:
            if pq in old_by_q:
                touched.add(pq)
            continue
        deftime.append(("new", new_by_q[q][2].lineno))
    rt = M.rt(path)
    raw = 0
    line_level = os.environ.get("BR_LINE_LEVEL", "1") == "1"
    precise = _precise_lines(old_tree, new_tree, hunks, removed, added) if line_level else None
    for q in touched:
        if q in old_by_q:
            s, e, onode = old_by_q[q]
            nn = new_by_q.get(q, (None, None, None))[2]
            if precise is not None and nn is not None and not _structural(onode, nn):
                # statement level: only tests that ran a changed statement or reached an insertion point
                lines = sorted(ln for ln in precise if s <= ln <= e)
                if not lines:
                    lines = list(range(s, e + 1))
            else:
                lines = list(range(s, e + 1))
            for ln in lines:
                b = rt.get(ln)
                if b:
                    raw |= b
                SPANS.append((path, ln, ln))
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
        if isinstance(stmt, ast.ClassDef) and line >= (stmt.body[0].lineno if stmt.body else stmt.lineno) and fq is None:
            continue  # a blank or comment line between statements of a class body: no effect by itself
        if stmt is None and fq is None:
            continue  # a blank or comment line at module level
        names = _bound_names(stmt)
        if fq:
            names.add(fq.split(".")[-1])
        key = (cls[0] if cls else None, tuple(sorted(names)))
        if key in seen:
            continue
        seen.add(key)
        if os.environ.get("BR_DEBUG_DEFT"):
            log(f"   deftime {side}:{line} class={cls[0] if cls else None} stmt={type(stmt).__name__} names={sorted(names)} fq={fq}")
        if cls is None:
            reach = _module_reach(M, path, stmt, old_tree, new_tree)
            if reach is not None:
                deft |= M.conv(reach)
                notes.append(f"module names {','.join(sorted(_bound_names(stmt)))[:60]}")
                continue
        if cls is None and bin(imp).count("1") * 2 > M.n:
            BROAD[0] = True  # a module-level change most tests import: isolated passes are not trusted
        if cls is None:
            if (isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name in old_by_q
                    and stmt.name in new_by_q):
                # a module-level function that still exists changed its signature or decorators:
                # only code that runs it can notice (its body span is already in)
                s, e, _ = old_by_q[stmt.name]
                cand |= M.conv(_rt_span(rt, s, e))
                SPANS.append((path, s, e))
                notes.append(f"function {stmt.name} header")
                continue
            deft |= imp
            notes.append("module-level")
            continue
        users = classes_map.get(f"{mod}:{cls[0]}", 0)
        if not users and _is_enum(cls[4]) and isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt in cls[4].body \
                and names and "." not in cls[0]:
            # an enum member: code reads it by name (Enum.MEMBER), or reads the enum class itself
            # (iteration, lookup by value, membership); both are in the read maps
            e = M.core.get("modreads", {}).get((path, cls[0]))
            if e is not None and not e["cross"]:
                reach = e["bits"] | M.core.get("generic_bits", 0)
                for name in names:
                    reach |= M.core.get("attr_bits", {}).get(name, 0)
                deft |= M.conv(reach)
                notes.append(f"enum {cls[0]} member {','.join(sorted(names))}")
                continue
        if not users:
            deft |= imp
            if bin(imp).count("1") * 2 > M.n:
                BROAD[0] = True
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
        if _narrowable(stmt, cls[4], names, M.core.get("defreads", ())):
            reach = M.core.get("generic_bits", 0)
            for name in names:
                reach |= M.core.get("attr_bits", {}).get(name, 0)
                if name.endswith("_sql"):
                    reach |= M.core.get("generic_sql", 0)  # generator dispatch finds *_sql methods by name
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = f"{cls[0]}.{stmt.name}"
                if q in old_by_q:  # tests that ran the method as it was
                    s, e, _ = old_by_q[q]
                    reach |= _rt_span(rt, s, e)
            acc &= reach
            notes.append(f"class {cls[0]} setting {','.join(sorted(names))}")
        else:
            notes.append(f"class {cls[0]}")
        deft |= M.conv(acc)
    if path in M.core.get("import_modules", ()):
        irt = M.core.get("import_rt", {}).get(path, ())
        if deftime or any(s <= ln <= e for ln in irt for (pp, s, e) in SPANS if pp == path):
            deft |= M.spawn()
    cand |= deft
    FULL[0] |= deft
    if deftime:
        IMPORT_CHECK.append((mod, path))
    cand |= readers
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
    MAPREF[0] = M
    IDS[:] = ids
    for mi, keys in M.core.get("case_tests", {}).items():
        n = M.conv_index(mi)
        if n is not None:
            CASE_KEYS[n] = keys
            MAP_INDEX[n] = mi
    INPROC.update(M.core.get("inproc", {}))
    tid = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
    if tid in ids:
        TPCDS[0] = ids.index(tid)
        mi = M.core["ids"].index(tid) if tid in M.core["ids"] else None
        MAP_INDEX_ANY[TPCDS[0]] = mi
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
    try:
        results = verify(plans, dotted, secs, ids)
    except BaseException as e:  # never end without answers: include every candidate
        log(f"verification failed ({type(e).__name__}: {e}); including candidates")
        results = {c: p.get("verify", 0) for c, p in plans.items()}
    for c in commits:
        try:
            p = plans[c]
            out = p["static"] | results.get(c, 0)
            nums = bits_fast(out)
            workers = pack(nums, secs)
        except BaseException:
            workers = pack(list(range(len(secs))), secs)
        print(json.dumps({"commit": c, "workers": workers}), flush=True)
    log(f"done in {time.monotonic()-T0:.1f}s wall, cpu {time.process_time():.1f}s (main only)")


def _dotted(tid):
    path, cls, meth = tid.split("::")
    return path[:-3].replace("/", ".") + "." + cls + "." + meth


def _tests_under(d):
    acc = 0
    for i, tid in enumerate(IDS):
        if d == "" or tid.startswith(d.rstrip("/") + "/"):
            acc |= 1 << i
    return acc


def _is_pytest_config(p):
    return p in ("pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini", "conftest.py", "tests/conftest.py")


def _pytest_config_changed(p, old, new):
    """True when the change can alter how pytest runs every test (broken file or pytest settings)."""
    if old is None or new is None:
        return True
    try:
        if p == "pyproject.toml":
            import tomllib
            o, n = tomllib.loads(old), tomllib.loads(new)
            return o.get("tool", {}).get("pytest") != n.get("tool", {}).get("pytest")
        if p in ("setup.cfg", "tox.ini", "pytest.ini"):
            import configparser
            co, cn = configparser.ConfigParser(), configparser.ConfigParser()
            co.read_string(old)
            cn.read_string(new)
            secs = ("pytest", "tool:pytest")
            return any((co.has_section(s) and dict(co[s])) != (cn.has_section(s) and dict(cn[s])) for s in secs)
    except Exception:
        return True
    return True


SPANS = []
FULL = [0]
IMPORT_CHECK = []  # (dotted module, path) whose definitions changed: import them before any test
BROAD = [False]
QSEL = {}  # test number -> TPC query indices whose fixture text changed


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
    del IMPORT_CHECK[:]
    BROAD[0] = False
    QSEL.clear()
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
                if p.endswith(".py"):
                    static |= M.importers(p) | M.spawn() | M.lister()
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
                if p.startswith("tests/"):
                    FULL[0] |= c  # test code can change how cases are loaded or checked: run every case
                    if os.path.basename(p) == "conftest.py" or (text_old is None and p.endswith("__init__.py")):
                        static |= _tests_under(os.path.dirname(p))
                notes.append(f"{p}: {note}")
                continue
            if _is_pytest_config(p):
                broken_or_relevant = _pytest_config_changed(p, text_old, text_new)
                if broken_or_relevant:
                    static |= M.all
                    notes.append(f"{p}: pytest configuration")
                    continue
            readers = M.opens(p)
            c = readers
            if not (p.startswith("sqlglot/") or p.startswith("tests/")):
                static |= readers  # such tests may resolve the real path and never see the overlay
                notes.append(f"{p}: outside file")
                continue
            if text_new is None:
                # a deleted file: tests that list or read its folder may notice
                c |= M.lister()
                d = os.path.dirname(p)
                for f, b in M.core["opens"].items():
                    if os.path.dirname(f) == d:
                        c |= M.conv(b)
            elif text_old is None:
                c |= M.lister()
            if p.endswith(".py"):
                c |= M.spawn()
            cand |= c
            full = c
            if p.startswith("tests/fixtures/") and text_old is not None and text_new is not None:
                old_cases, new_cases = fixture_pairs(text_old), fixture_pairs(text_new)
                rel = p[len("tests/fixtures/"):]
                tpc = {"optimizer/tpc-h/tpc-h.sql": "test_execute_tpch", "optimizer/tpc-ds/tpc-ds.sql": "test_execute_tpcds"}
                if rel in tpc and old_cases is not None and new_cases is not None:
                    for n in bits_fast(readers):
                        tid = IDS[n]
                        if not tid.startswith("tests/test_executor.py::"):
                            continue
                        if tid.endswith("::" + tpc[rel]) and len(old_cases) == len(new_cases):
                            # the executor test reads only each query's SQL and metadata, never the expected text
                            QSEL[n] = {i for i in range(len(new_cases))
                                       if old_cases[i][:2] != new_cases[i][:2]}
                            full &= ~(1 << n)
                            if not QSEL[n]:
                                cand &= ~(1 << n)
                        elif tid.endswith("::test_optimized_tpch") and rel == "optimizer/tpc-h/tpc-h.sql":
                            pass  # compares against the expected text: stays a full candidate
                        else:
                            # loads the file in class setup only (which parsed fine), or reads the other TPC file
                            full &= ~(1 << n)
                            cand &= ~(1 << n)
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
        lazy_hit = False
        for path, s, e in SPANS:
            cf = M.cases(path)
            tm = cf.get(mi)
            lz = cf.get("__lazy__", {}).get(mi, ())
            for ln in range(s, e + 1):
                if ln in lz:
                    lazy_hit = True  # a cache-dependent line: later cases may reuse what it computed
                if tm:
                    v = tm.get(ln)
                    if v:
                        mask |= v
        if lazy_hit:
            continue  # run every case
        picked = [keys[i] for i in bits_fast(mask)]
        if n in QSEL:
            picked += [("q", i) for i in sorted(QSEL[n]) if ("q", i) not in picked]
        select[n] = {"keys": picked, "content": content.get(n, set())}
    fixture_only = all(not (p.endswith(".py")) for p in changes) and bool(changes)
    return {"fixture_only": fixture_only, "static": static, "verify": cand, "changes": changes, "note": "; ".join(notes), "select": select,
            "spans": list(SPANS), "full": FULL[0], "import_check": list(IMPORT_CHECK), "broad": BROAD[0]}


CASE_KEYS = {}
MAP_INDEX = {}
MAP_INDEX_ANY = {}
EXEC = {"common": {}, "tests": 0}
IDS = []
MAPREF = [None]


ALPHA = float(os.environ.get("BR_ALPHA", "1.0"))   # scorer CPU seconds per second measured here
OVH = float(os.environ.get("BR_OVH", "0.3"))       # scorer CPU of one verification child, imports included
Q_CODE = float(os.environ.get("BR_Q_CODE", "0.35"))  # chance a code change breaks nothing it reaches
Q_FIXTURE = float(os.environ.get("BR_Q_FIXTURE", "0.05"))
INPROC = {}
TPCDS = [None]


def _tpcds_evidence(p, others):
    """How many other reached tests run the changed lines that TPC-DS runs (definition changes: all others)."""
    if not p["spans"]:
        return len(others)
    M = MAPREF[0]
    tp_map = MAP_INDEX_ANY.get(TPCDS[0])
    if tp_map is None:
        return 0
    acc = 0
    for path, s, e in p["spans"]:
        rt = M.rt(path)
        for ln in range(s, e + 1):
            b = rt.get(ln, 0)
            if b >> tp_map & 1:
                acc |= b & ~(1 << tp_map)
    acc = M.conv(acc)
    return sum(1 for n in others if acc >> n & 1)
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
        if not v and not p.get("import_check"):
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
        if p.get("broad"):
            p["static"] |= v
            log(f"{c}: broad module-level change, including every candidate")
            continue
        q = Q_FIXTURE if p.get("fixture_only") else Q_CODE
        # tests far cheaper to run than to include: always run them
        forced = [n for n in nums if est[n] < 0.6 * secs[n] - 0.05 and est[n] < 8]
        optional = [n for n in nums if n not in forced]
        # TPC-DS through the executor (21 s) is left out when every other reached test runs and passes and
        # enough of them cover the same lines; measured on 40 mutants of lines it shares: it never failed alone.
        cond = None
        if TPCDS[0] is not None and TPCDS[0] in optional and os.environ.get("BR_TPCDS_RULE", "1") == "1":
            others = [n for n in nums if n != TPCDS[0]]
            if len(others) >= 3 and _tpcds_evidence(p, others) >= 3 and sum(est[n] for n in others) < 30:
                cond = TPCDS[0]
                optional.remove(cond)
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
        if cond is not None and p["changes"] is not None and VERIFY:
            verify_opt = bool(optional) or bool(forced)  # running the others may save the 21 s test
        elif optional and p["changes"] is not None and VERIFY:
            s_opt = pack_cost(optional, secs)
            c1 = est[optional[0]]
            lhs = sum(est[n] for n in optional) + ovh + ((1 - q) / max(q, 0.01)) * (c1 + ovh)
            verify_opt = lhs < s_opt
        if os.environ.get("BR_EXACT") == "1":  # experiments: run every candidate, never stop early
            forced, optional, verify_opt = forced + optional, [], False
        run = sorted(forced, key=lambda n: est[n]) + (optional if verify_opt else [])
        if not verify_opt:
            p["static"] |= sum(1 << n for n in optional)
            if cond is not None:
                p["static"] |= 1 << cond
                cond = None
        p["cond"] = cond
        if not run and not p.get("import_check"):
            if p.get("cond") is not None:
                p["static"] |= 1 << p["cond"]
            continue
        if not VERIFY or p["changes"] is None:
            p["static"] |= sum(1 << n for n in run)
            continue
        root = os.path.join(TMP, "ov", c)
        timeouts = {dotted[n]: max(6.0, 8 * secs[n] + 4) for n in run}
        budget = 15 + sum(timeouts.values())
        cap = 600 if os.environ.get("BR_EXACT") == "1" else 120
        j = runner.Job(c, root, [dotted[n] for n in run], timeouts, min(budget, cap))
        j.prepare = (lambda root=root, changes=p["changes"]: overlay.build(REPO, root, changes))
        j.cleanup = (lambda root=root: overlay.remove(root))
        j.import_check = [m for m, _ in p.get("import_check", [])]
        j.import_paths = {m: path for m, path in p.get("import_check", [])}
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
        # stay well inside the batch limit (3 minutes; 4.5 in the final with ~1.5x commits)
        limit = float(os.environ.get("BR_WALL", "150"))
        runner.run_jobs(jobs, parallel=2, deadline=T0 + limit)
    back = {d: i for i, d in enumerate(dotted)}
    for j in jobs:
        bad = j.failing()
        results[j.key] = sum(1 << back[d] for d in bad if d in back)
        cond = plans[j.key].get("cond")
        if cond is not None:
            clean = j.ended and not j.stop_now and not bad and not j.impfail and len(j.done) == len(j.tests)
            if not clean:
                results[j.key] |= 1 << cond
            log(f"{j.key}: TPC-DS {'left out (every other reached test passed)' if clean else 'included'}")
        if j.impfail or (j.import_check and not j.import_ok and not j.ended):
            # a changed module did not import (or the check never finished): every importer fails
            extra = 0
            for m in j.import_check:
                if m in j.impfail or m not in j.import_ok:
                    extra |= MAPREF[0].importers(j.import_paths[m]) | MAPREF[0].spawn()
            results[j.key] |= extra
            log(f"{j.key}: import check failed for {sorted(j.impfail) or 'unfinished'}; including importers")
        overlay.remove(j.root)
        imp = sum(c for _, c in j.imports)
        nf = sum(1 for d in j.tests if d in j.filters)
        done = sum(1 for d in j.tests if d in j.done)
        if os.environ.get("BR_DEBUG_ORDER"):
            log(f"   order {j.key}: " + " | ".join(f"{d.rsplit('.', 1)[-1]}={'F' if j.done.get(d) else 'p' if d in j.done else '-'}" for d in j.tests))
        if os.environ.get("BR_DEBUG"):
            top = sorted(j.times.items(), key=lambda kv: -kv[1])[:5]
            log(f"   slowest: {[(k.rsplit('.', 1)[-1], v) for k, v in top]}")
        if j.cpu is None:
            j.cpu = 0.0
        log(f"{j.key}: ran {done}/{len(j.tests)} ({nf} filtered{', stopped' if j.stop_now else ''}), failing {len(bad)}, cpu {j.cpu:.2f}s (imports {imp:.2f}s)")
    return results


if __name__ == "__main__":
    main()
