# Blast Radius selector

For each patch to sqlglot (pinned at `eb11aec`), picks the tests it can break and splits them over workers.

**Map (built once from the pinned commit, in `map/`).** Every test was run alone in a fresh process with
`sys.monitoring`, recording the lines it executed (function code separately from module and class bodies),
the files it read, the classes it instantiated, and, for the optimizer fixture tests and the TPC query
tests, the lines each fixture case or query executed. `tools/trace.py` and `tools/buildmap.py` rebuild it.

**Per patch (`selector/main.py`).**
- Python changes are mapped to the functions whose bodies changed; tests that ran those lines are candidates.
  Comment-only or layout-only changes (same syntax tree) select nothing.
- Changes to class settings or methods select tests that both created an instance of the class and ran a line
  that reads that name. Other definition-time changes select the module's importers, and a changed module is
  imported first so an import failure includes every importer.
- Fixture changes select only the fixture cases or TPC queries whose text changed.
- Cheap candidates are run on a patched copy (a symlink overlay in `/tmp`) in a forked child, with the
  optimizer and executor process pools replaced by an in-process pool. Likely failures run first and the run
  stops at the first failure; slow candidates are included without running, by measured cost.
- The selection is packed onto 1-8 workers with longest-first placement.
