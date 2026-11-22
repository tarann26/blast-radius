"""Lines executed by a bare `import sqlglot` (what tests that start `python -c "import sqlglot..."` run)."""
import json, os, sys
REPO = os.path.abspath(sys.argv[1]); OUT = sys.argv[2]
sys.path.insert(0, REPO); os.chdir(REPO)
mon = sys.monitoring; tool = mon.COVERAGE_ID; mon.use_tool_id(tool, "imp")
lines = {}
rp = REPO + os.sep
def on_line(code, line):
    f = code.co_filename
    if f.startswith(rp):
        lines.setdefault((f[len(rp):], code.co_flags & 1), set()).add(line)
    return mon.DISABLE
mon.register_callback(tool, mon.events.LINE, on_line); mon.set_events(tool, mon.events.LINE)
import sqlglot  # noqa
mon.set_events(tool, 0)
mods = sorted(m.__file__[len(rp):] for m in list(sys.modules.values()) if getattr(m, "__file__", None) and m.__file__.startswith(rp))
json.dump({"modules": mods, "rt": {f: sorted(s) for (f, k), s in lines.items() if k}}, open(OUT, "w"))
print(len(mods), "modules")
