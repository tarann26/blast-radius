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
