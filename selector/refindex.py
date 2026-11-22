"""Where class settings are read by name: `Name.ATTR` in a class body or a function body.

A class-body change to ATTR in class `Name` also reaches classes that copy `Name.ATTR` at definition
time without inheriting from it, and functions that read `Name.ATTR` directly.
"""
import ast
import os


def module_of(path):
    mod = path[:-3].replace("/", ".")
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]
    return mod


def build(repo):
    refs = {}  # (base simple name, attr) -> list of ("class", "module:qualname") or ("func", path, start, end)
    for top in ("sqlglot", "tests"):
        for dirpath, dirnames, filenames in os.walk(os.path.join(repo, top)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, repo)
                try:
                    tree = ast.parse(open(full, "rb").read())
                except SyntaxError:
                    continue
                _walk(tree, rel, module_of(rel), "", ("module", rel), refs)
    return refs


def _base_name(node):
    v = node.value
    if isinstance(v, ast.Name):
        return v.id
    if isinstance(v, ast.Attribute):
        return v.attr
    return None


def _walk(node, rel, mod, prefix, owner, refs):
    """owner: ("class", key) inside a class body, ("func", rel, start, end) inside a function."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            _walk(child, rel, mod, f"{prefix}{child.name}.", ("class", f"{mod}:{prefix}{child.name}"), refs)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = child.body[0].lineno if child.body else child.lineno
            _walk(child, rel, mod, f"{prefix}{child.name}.", ("func", rel, start, child.end_lineno), refs)
        elif isinstance(child, ast.Lambda):
            _walk(child, rel, mod, prefix, ("func", rel, child.body.lineno, child.body.end_lineno), refs)
        else:
            if isinstance(child, ast.Attribute) and owner is not None:
                base = _base_name(child)
                if base and base not in ("self", "cls"):
                    lst = refs.setdefault((base, child.attr), [])
                    if owner not in lst:
                        lst.append(owner)
            _walk(child, rel, mod, prefix, owner, refs)


def attr_reads(repo):
    """For class-body data names: where function code reads them, where code reads attributes generically,
    and which names are read at definition time (copied into other definitions)."""
    trees = {}
    for dirpath, dirnames, filenames in os.walk(os.path.join(repo, "sqlglot")):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if fn.endswith(".py"):
                full = os.path.join(dirpath, fn)
                try:
                    trees[os.path.relpath(full, repo)] = ast.parse(open(full, "rb").read())
                except SyntaxError:
                    pass
    assigned = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for st in node.body:
                    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and not st.name.startswith("__"):
                        assigned.add(st.name)
                    tgts = st.targets if isinstance(st, ast.Assign) else [st.target] if isinstance(st, (ast.AnnAssign, ast.AugAssign)) else []
                    for tg in tgts:
                        for n in ast.walk(tg):
                            if isinstance(n, ast.Name):
                                assigned.add(n.id)
    reads, generic, defreads = {}, [], set()

    def classify(call_or_attr, func_node, parent):
        """None (not a generic read of class settings), "sql" (method dispatch by *_sql name) or "all"."""
        n = call_or_attr
        if isinstance(n, ast.Attribute) and n.attr == "__dict__":
            # klass.__dict__.get("NAME") / "NAME" in klass.__dict__ / klass.__dict__["NAME"]: a named read
            if isinstance(parent, ast.Attribute) and parent.attr == "get":
                return None
            if isinstance(parent, ast.Subscript) and isinstance(parent.slice, ast.Constant):
                return None
            if isinstance(parent, ast.Compare):
                return None
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("getattr", "hasattr") and n.args:
            first = n.args[0]
            if isinstance(first, ast.Name) and first.id in ("module", "mod"):
                return None
            if isinstance(first, ast.Call):
                return None  # getattr(importlib.import_module(...), name): a module attribute
        src = ast.unparse(func_node) if func_node is not None else ""
        if "_sql" in src and ("_sql\"" in src or "'_sql'" in src or '"_sql' in src or "_sql}" in src or "[:-4]" in src):
            return "sql"
        if "getmembers" in src and "module" in src:
            return None
        return "all"

    def visit(node, rel, in_func, in_class, func_node=None):
        for child in ast.iter_child_nodes(node):
            is_func = isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            f = in_func or is_func
            fn_node = child if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func_node
            c = (in_class or isinstance(child, ast.ClassDef)) and not f
            if isinstance(child, ast.Attribute) and child.attr in assigned and isinstance(child.ctx, ast.Load):
                if in_func:
                    reads.setdefault(child.attr, []).append((rel, child.lineno))
                else:
                    defreads.add(child.attr)
            elif isinstance(child, ast.Constant) and isinstance(child.value, str) and child.value in assigned:
                if in_func:
                    reads.setdefault(child.value, []).append((rel, child.lineno))
                else:
                    defreads.add(child.value)
            elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load) and child.id in assigned and in_class and not in_func:
                defreads.add(child.id)
            if in_func:
                kind = None
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                    fn = child.func.id
                    if fn in ("getattr", "hasattr") and len(child.args) >= 2 and not isinstance(child.args[1], ast.Constant):
                        kind = classify(child, func_node, node)
                    elif fn in ("vars", "dir"):
                        kind = classify(child, func_node, node)
                elif isinstance(child, ast.Attribute) and child.attr in ("__dict__", "getmembers"):
                    kind = classify(child, func_node, node)
                if kind:
                    generic.append((rel, child.lineno, kind))
            visit(child, rel, f, c, fn_node)

    for rel, tree in trees.items():
        visit(tree, rel, False, False)
    _forward(trees, assigned, reads)
    # Reviewed by hand at the pinned commit: these read instance, module or expression attributes,
    # or dispatch *_sql methods, never a class data setting.
    ignore = {
        ("sqlglot/helper.py", "**{k: v.copy() if hasattr(v"),
        ("sqlglot/dialects/dialect.py", "for attr_name in dir(module)"),
        ("sqlglot/expressions/core.py", "this=to_identifier(k), expression=convert(getattr(value, k)"),
        ("sqlglot/expressions/core.py", "for k, v in value.__dict__.items()"),
        ("sqlglot/executor/env.py", '"EXTRACT": null_if_any(lambda this, e: getattr(e, this))'),
        ("sqlglot/generators/duckdb.py", "result_type = getattr(annotated, return_value_attr)"),
    }
    sql = {("sqlglot/generator.py", 'ops.add(getattr(self, f"{node.key}_sql")')}
    out = []
    for rel, ln, kind in generic:
        text = open(os.path.join(repo, rel)).read().split("\n")[ln - 1].strip()
        if any(rel == f and text.startswith(pfx) for f, pfx in ignore):
            continue
        if any(rel == f and text.startswith(pfx) for f, pfx in sql):
            kind = "sql"
        out.append((rel, ln, kind))
    return reads, out, defreads


def module_reads(repo):
    """Readers of module-level names.

    Returns {(module path, name): {"fn": [(file, line)], "local": [names in the same module whose
    module-level definitions read it], "cross": bool (read at definition time in another module),
    "importers": [paths of modules that `from <module> import name`]}}.
    """
    trees = {}
    for top in ("sqlglot", "tests"):  # test modules import sqlglot names too; their import failing fails their tests
        for dirpath, dirnames, filenames in os.walk(os.path.join(repo, top)):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "fixtures")]
            for fn in filenames:
                if fn.endswith(".py"):
                    full = os.path.join(dirpath, fn)
                    try:
                        trees[os.path.relpath(full, repo)] = ast.parse(open(full, "rb").read())
                    except SyntaxError:
                        pass
    path_of = {module_of(p): p for p in trees}
    out = {}

    def entry(path, name):
        return out.setdefault((path, name), {"fn": [], "local": [], "cross": False, "importers": []})

    def module_names(tree):
        names = set()
        for st in tree.body:
            for tg in (st.targets if isinstance(st, ast.Assign) else [st.target] if isinstance(st, (ast.AnnAssign, ast.AugAssign)) else []):
                for n in ast.walk(tg):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(st.name)
            if isinstance(st, (ast.Import, ast.ImportFrom)):
                for a in st.names:
                    names.add((a.asname or a.name).split(".")[0])
        return names

    defined = {p: module_names(t) for p, t in trees.items()}
    # star imports: module path -> source module paths, in order
    stars = {}
    explicit = {}  # module path -> {local name: (source path, source name)} for module-level from-imports
    for p, tree in trees.items():
        for st in _top_level_imports(tree):
            if isinstance(st, ast.ImportFrom) and st.module and st.level == 0:
                src = path_of.get(st.module)
                if src is None:
                    continue
                for a in st.names:
                    if a.name == "*":
                        stars.setdefault(p, []).append(src)
                    elif not path_of.get(f"{st.module}.{a.name}"):
                        explicit.setdefault(p, {})[a.asname or a.name] = (src, a.name)

    def defines_locally(p, name):
        return name in defined.get(p, ()) and name not in explicit.get(p, {})

    def resolve(p, name, depth=0):
        """The module that really defines `name` as seen from module p (follows re-exports)."""
        if depth > 6:
            return (p, name)
        if name in explicit.get(p, {}):
            sp, sn = explicit[p][name]
            return resolve(sp, sn, depth + 1)
        if defines_locally(p, name):
            return (p, name)
        for sp in stars.get(p, []):
            if name in defined.get(sp, ()) or any(name in defined.get(x, ()) for x in stars.get(sp, [])):
                return resolve(sp, name, depth + 1)
        return None

    for path, tree in trees.items():
        mod = module_of(path)
        # names this module takes from other modules: local alias -> (source path, source name)
        imported = {}
        aliases = {}  # local module alias -> source path
        # imports inside functions run when the function runs: a removed name fails there
        for fnode in ast.walk(tree):
            if isinstance(fnode, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for st in ast.walk(fnode):
                    if isinstance(st, ast.ImportFrom) and st.module and st.level == 0:
                        src = path_of.get(st.module)
                        for a in st.names:
                            if src and not path_of.get(f"{st.module}.{a.name}"):
                                entry(src, a.name)["fn"].append((path, st.lineno))
        for st in tree.body if False else _top_level_imports(tree):
            if isinstance(st, ast.ImportFrom) and st.module and st.level == 0:
                src = path_of.get(st.module)
                for a in st.names:
                    sub = path_of.get(f"{st.module}.{a.name}")
                    if sub:
                        aliases[a.asname or a.name] = sub
                    elif src:
                        imported[a.asname or a.name] = (src, a.name)
                        entry(src, a.name)["importers"].append(path)
            elif isinstance(st, ast.Import):
                for a in st.names:
                    src = path_of.get(a.name)
                    if src:
                        aliases[a.asname or a.name.split(".")[0]] = src

        def visit(node, in_func, top_stmt):
            for child in ast.iter_child_nodes(node):
                f = in_func or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                ts = top_stmt if top_stmt is not None else (child if node is tree else None)
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                    src_key = resolve(path, child.id)
                    if src_key is not None:
                        e = entry(*src_key)
                        if in_func:
                            e["fn"].append((path, child.lineno))
                        elif src_key[0] == path:
                            if ts is not None:
                                for nm in _stmt_names(ts):
                                    if nm != child.id:
                                        e["local"].append(nm)
                        else:
                            e["cross"] = True
                elif isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load) and isinstance(child.value, ast.Name) \
                        and child.value.id in aliases:
                    key = resolve(aliases[child.value.id], child.attr) or (aliases[child.value.id], child.attr)
                    e = entry(*key)
                    if in_func:
                        e["fn"].append((path, child.lineno))
                    else:
                        e["cross"] = True
                visit(child, f, ts)

        visit(tree, False, None)
    return out


def _top_level_imports(tree):
    """Import statements outside any function (module level, class bodies, if/try blocks)."""
    out = []

    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                out.append(child)
            visit(child)
    visit(tree)
    return out


def _stmt_names(st):
    names = set()
    for tg in (st.targets if isinstance(st, ast.Assign) else [st.target] if isinstance(st, (ast.AnnAssign, ast.AugAssign)) else []):
        for n in ast.walk(tg):
            if isinstance(n, ast.Name):
                names.add(n.id)
    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(st.name)
    return names


def _forward(trees, assigned, reads):
    """Follow a setting passed unchanged as a keyword argument and stored unchanged on the callee
    (`Core(x=self.SETTING)` then `self.x = x`): the setting is consumed where `.x` is read, not at
    the call. Any other use of the parameter stays a read of the setting at that line."""
    parents = {}
    funcs_by_param = {}
    attr_loads = {}
    for rel, tree in trees.items():
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for a in node.args.args + node.args.kwonlyargs:
                    funcs_by_param.setdefault(a.arg, []).append((rel, node))
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                attr_loads.setdefault(node.attr, []).append((rel, node.lineno))
    for rel, tree in trees.items():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute) and node.attr in assigned and isinstance(node.ctx, ast.Load)):
                continue
            kw = parents.get(id(node))
            if not (isinstance(kw, ast.keyword) and kw.arg and kw.value is node):
                continue
            callees = funcs_by_param.get(kw.arg, [])
            if not callees or len(callees) > 3:
                continue
            consumed = []
            ok = True
            for frel, fn in callees:
                for sub in ast.walk(fn):
                    if isinstance(sub, ast.Name) and sub.id == kw.arg and isinstance(sub.ctx, ast.Load):
                        par = parents.get(id(sub))
                        if (isinstance(par, ast.Assign) and par.value is sub and len(par.targets) == 1
                                and isinstance(par.targets[0], ast.Attribute)
                                and isinstance(par.targets[0].value, ast.Name) and par.targets[0].value.id == "self"):
                            stored = par.targets[0].attr
                            consumed.extend(attr_loads.get(stored, []))
                        else:
                            consumed.append((frel, sub.lineno))
            if not ok:
                continue
            lst = reads.get(node.attr, [])
            site = (rel, node.lineno)
            if site in lst:
                lst = [s for s in lst if s != site]  # the call only passes the value on
            reads[node.attr] = lst + consumed

