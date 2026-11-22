import json, sys
D = sys.argv[1]; OUT = sys.argv[2]
ts = json.load(open(f"{D}/tests.json"))["tests"]
ans = json.load(open(f"{D}/answers.json"))["commits"]
def best(secs):
    if not secs: return 0.0
    secs = sorted(secs, reverse=True); b = 1e9
    for k in range(1, 9):
        loads = [0] * k
        for s in secs: i = loads.index(min(loads)); loads[i] += s
        b = min(b, max(loads) + 2 * k)
    return b
tot = otot = 0; miss = 0
for l in open(OUT):
    d = json.loads(l); ws = d['workers']; c = d['commit']
    if c not in ans: continue
    cost = (max(sum(round(ts[n]['seconds'] * 1000) for n in w) for w in ws) + 2000 * len(ws)) / 1000 if ws else 0
    o = best([ts[n]['seconds'] for n in ans[c]['failing']])
    sel = set(n for w in ws for n in w); fail = set(ans[c]['failing'])
    if fail - sel: miss += 1; print("MISS", c, ans[c]['kind'], [ts[n]['id'] for n in fail - sel][:5])
    extra = [(ts[n]['id'].split('::')[-1], ts[n]['seconds']) for n in sorted(sel - fail)]
    tot += cost; otot += o
    if cost - o > 0.01: print(c, ans[c]['kind'], round(cost, 2), round(o, 2), '+%.2f' % (cost - o), extra[:4])
print('total', round(tot, 2), 'oracle', round(otot, 2), 'excess', round(tot - otot, 2), 'misses', miss)
