"""Read a git unified diff and apply it to the pristine tree, in memory."""
import re

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class FilePatch:
    __slots__ = ("old", "new", "hunks", "binary", "new_file", "deleted", "mode_only")

    def __init__(self):
        self.old = None  # path before (None when added)
        self.new = None  # path after (None when deleted)
        self.hunks = []  # (old_start, old_len, new_start, new_len, lines)
        self.binary = False
        self.new_file = False
        self.deleted = False
        self.mode_only = False


def _path(token):
    token = token.strip()
    if token.startswith('"') and token.endswith('"'):
        token = bytes(token[1:-1], "utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8")
    if token == "/dev/null":
        return None
    if token[:2] in ("a/", "b/"):
        return token[2:]
    return token


def parse(text):
    """Return a list of FilePatch. Raises ValueError on anything it cannot read."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    patches = []
    cur = None
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            cur = FilePatch()
            patches.append(cur)
            m = re.match(r"^diff --git (\"?a/.*?\"?) (\"?b/.*\"?)$", line)
            if m:
                cur.old, cur.new = _path(m.group(1)), _path(m.group(2))
            i += 1
            continue
        if cur is None:
            if line.startswith("--- "):
                cur = FilePatch()
                patches.append(cur)
            else:
                i += 1
                continue
        if line.startswith("new file mode"):
            cur.new_file = True
            cur.old = None
        elif line.startswith("deleted file mode"):
            cur.deleted = True
            cur.new = None
        elif line.startswith("rename from "):
            cur.old = line[len("rename from "):]
        elif line.startswith("rename to "):
            cur.new = line[len("rename to "):]
        elif line.startswith("copy from "):
            cur.old = line[len("copy from "):]
        elif line.startswith("copy to "):
            cur.new = line[len("copy to "):]
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            cur.binary = True
        elif line.startswith("--- "):
            cur.old = _path(line[4:].split("\t")[0])
            if cur.old is None:
                cur.new_file = True
        elif line.startswith("+++ "):
            cur.new = _path(line[4:].split("\t")[0])
            if cur.new is None:
                cur.deleted = True
        elif line.startswith("@@"):
            m = HUNK.match(line)
            if not m:
                raise ValueError("bad hunk header")
            os_, ol, ns, nl = m.groups()
            os_, ns = int(os_), int(ns)
            ol = 1 if ol is None else int(ol)
            nl = 1 if nl is None else int(nl)
            body = []
            i += 1
            o_seen = n_seen = 0
            while i < n and (o_seen < ol or n_seen < nl):
                b = lines[i]
                if b.startswith("\\"):
                    if body:
                        body[-1] = (body[-1][0], body[-1][1], True)
                    i += 1
                    continue
                tag = b[:1]
                if tag == " " or b == "":
                    body.append((" ", b[1:], False))
                    o_seen += 1
                    n_seen += 1
                elif tag == "-":
                    body.append(("-", b[1:], False))
                    o_seen += 1
                elif tag == "+":
                    body.append(("+", b[1:], False))
                    n_seen += 1
                else:
                    raise ValueError("bad hunk line")
                i += 1
            while i < n and lines[i].startswith("\\"):
                if body:
                    body[-1] = (body[-1][0], body[-1][1], True)
                i += 1
            cur.hunks.append((os_, ol, ns, nl, body))
            continue
        i += 1
    for p in patches:
        if not p.hunks and not p.binary and not p.new_file and not p.deleted and p.old == p.new:
            p.mode_only = True
    return patches


def apply(original, hunks):
    """Apply hunks to the original text. Returns the new text. Raises ValueError on mismatch."""
    src = original.split("\n")
    # A trailing newline gives a last empty piece: keep the line list without it.
    trailing = original.endswith("\n")
    if trailing:
        src.pop()
    out = []
    pos = 0  # index into src
    new_trailing = trailing
    offset = 0
    for os_, ol, ns, nl, body in hunks:
        old_lines = [t for tag, t, _ in body if tag != "+"]
        start = os_ - 1 if ol > 0 else os_
        start += offset
        if src[start:start + len(old_lines)] != old_lines:
            found = None
            for delta in range(1, 200):
                for s in (start - delta, start + delta):
                    if s >= pos and src[s:s + len(old_lines)] == old_lines:
                        found = s
                        break
                if found is not None:
                    break
            if found is None:
                raise ValueError("hunk does not apply")
            offset += found - start
            start = found
        if start < pos:
            raise ValueError("overlapping hunks")
        out.extend(src[pos:start])
        for tag, t, nonl in body:
            if tag != "-":
                out.append(t)
        pos = start + len(old_lines)
        # "\ No newline at end of file" handling on the last line of the hunk
        last_new = [x for x in body if x[0] != "-"]
        last_old = [x for x in body if x[0] != "+"]
        if pos >= len(src):
            if last_new and last_new[-1][2]:
                new_trailing = False
            elif last_new:
                new_trailing = True if not (last_old and last_old[-1][2] and last_new[-1][0] == " ") else False
    out.extend(src[pos:])
    text = "\n".join(out)
    if new_trailing and out:
        text += "\n"
    return text


def changed_old_lines(hunks):
    """Old-file line numbers removed, and old-file positions where lines were inserted.

    An insertion before old line k is reported as k (insert lands between k-1 and k).
    """
    removed, inserted_at = set(), set()
    for os_, ol, ns, nl, body in hunks:
        o = os_ if ol > 0 else os_ + 1
        for tag, t, _ in body:
            if tag == " ":
                o += 1
            elif tag == "-":
                removed.add(o)
                o += 1
            else:
                inserted_at.add(o)
    return removed, inserted_at


def changed_new_lines(hunks):
    added = set()
    for os_, ol, ns, nl, body in hunks:
        nn = ns if nl > 0 else ns + 1
        for tag, t, _ in body:
            if tag == " ":
                nn += 1
            elif tag == "+":
                added.add(nn)
                nn += 1
    return added
