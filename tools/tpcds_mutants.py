"""Mutants on lines that test_execute_tpcds shares with other tests (for the TPC-DS policy experiment)."""
import ast, difflib, json, os, pickle, random, re, sys, zlib
REPO = "/Users/taran/Desktop/track4/sqlglot"; MAP = "/Users/taran/Desktop/track4/br/map"; OUT = sys.argv[1]; N = int(sys.argv[2])
core = pickle.loads(zlib.decompress(open(f"{MAP}/core.pkl.z", "rb").read()))
ids = core["ids"]; tp = ids.index("tests/test_executor.py::TestExecutor::test_execute_tpcds")
rng = random.Random(7)
OPS = [(r" == ", " != "), (r" != ", " == "), (r" is not ", " is "), (r" is ", " is not "), (r" and ", " or "), (r" or ", " and "),
       (r" < ", " <= "), (r" <= ", " < "), (r" > ", " >= "), (r" >= ", " > "), (r" \+ ", " - "), (r" - ", " + "),
       (r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r"\bnot ", ""), (r"\[0\]", "[1]"), (r"\[1\]", "[0]"), (r"\[-1\]", "[0]")]
cands = []
for f in core["rt_files"]:
    if not f.startswith("sqlglot/"):
        continue
    m = pickle.loads(zlib.decompress(open(f"{MAP}/rt/{f.replace('/', '__')}.z", "rb").read()))
    for ln, b in m.items():
        if b >> tp & 1 and (b & ~(1 << tp)):
            cands.append((f, ln))
rng.shuffle(cands)
made = 0
os.makedirs(f"{OUT}/commits", exist_ok=True)
for f, ln in cands:
    if made >= N:
        break
    src = open(f"{REPO}/{f}").read(); lines = src.split("\n"); line = lines[ln - 1]
    if line.strip().startswith("#"):
        continue
    ops = [(a, b) for a, b in OPS if re.search(a, line)]
    if not ops:
        continue
    a, b = rng.choice(ops)
    new_line = re.sub(a, b, line, count=1)
    new = lines[:ln - 1] + [new_line] + lines[ln:]
    try:
        if ast.dump(ast.parse("\n".join(new))) == ast.dump(ast.parse(src)):
            continue
    except SyntaxError:
        continue
    diff = "".join(difflib.unified_diff([l + "\n" for l in lines], [l + "\n" for l in new], f"a/{f}", f"b/{f}", n=3))
    cid = f"t{made:03d}"
    open(f"{OUT}/commits/{cid}.patch", "w").write(f"diff --git a/{f} b/{f}\n" + diff)
    made += 1
print("made", made, "from", len(cands), "shared lines")
