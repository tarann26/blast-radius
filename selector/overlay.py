"""A patched view of the pristine tree: symlinks everywhere except the changed paths."""
import os
import shutil


def build(pristine, root, changes):
    """changes: {relpath: new_text or None (deleted)}. Creates `root` and returns it."""
    os.makedirs(root)
    real_dirs = {""}
    for rel in changes:
        parts = rel.split("/")
        for i in range(1, len(parts)):
            real_dirs.add("/".join(parts[:i]))
    for d in sorted(real_dirs, key=lambda x: x.count("/")):
        src = os.path.join(pristine, d) if d else pristine
        dst = os.path.join(root, d) if d else root
        if d:
            os.makedirs(dst, exist_ok=True)
        try:
            names = os.listdir(src)
        except FileNotFoundError:
            names = []
        for name in names:
            rel = f"{d}/{name}" if d else name
            if rel in real_dirs or rel in changes:
                continue
            os.symlink(os.path.join(src, name), os.path.join(dst, name))
    for rel, text in changes.items():
        if text is None:
            continue
        dst = os.path.join(root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    return root


def remove(root):
    shutil.rmtree(root, ignore_errors=True)
