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
