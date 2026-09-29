#!/usr/bin/env node
/**
 * Self-check for the three pieces of template.html that carry real logic: the top-down tree
 * packing, the render arrow that draws the props edge, and the helper-folding pass.
 *
 * Each is pulled straight out of the template by brace-matching and run against stubs, so this
 * tests the shipped code rather than a copy of it that can drift.
 * Run: node scripts/test_viewer.mjs
 */
import assert from 'node:assert';
import fs from 'node:fs';
import path from 'node:path';

const here = path.dirname(new URL(import.meta.url).pathname);
const tpl = fs.readFileSync(path.join(here, 'template.html'), 'utf8');

/** The source of one brace-balanced block of the template, located by its opening line. */
function grab(mark) {
    const start = tpl.indexOf(mark);
    assert.ok(start > 0, `${mark} not found in template.html`);
    let depth = 0;
    for (let i = start + mark.length - 1; i < tpl.length; i++) {
        if (tpl[i] === '{') depth++;
        else if (tpl[i] === '}' && --depth === 0) return tpl.slice(start, i + 1);
    }
    assert.fail(`unbalanced braces after ${mark}`);
}

const layoutTree = new Function('curEp', 'nodes', 'padTop', 'place', 'drawEdges',
    grab('if(curEp.nodes.some(n=>n.row!==undefined)){'));

// A ⟨renders B, C⟩; B ⟨renders D, E⟩; C ⟨renders F⟩.
// K is a helper with ONE caller (B) — it stays inline on B's line, and Q chains off it.
// H is a helper with TWO callers (A and C) — it belongs to neither, so it must go to the shelf.
const W = 330, H = 120;
const g = {
    A: [['render', 'B'], ['render', 'C'], ['call', 'H']],
    B: [['render', 'D'], ['render', 'E'], ['call', 'K']],
    C: [['render', 'F'], ['call', 'H']],
    H: [['call', 'P']],
    K: [['call', 'Q']],
    D: [], E: [], F: [], P: [], Q: [],
};
const curEp = {
    nodes: Object.entries(g).map(([id, steps]) => ({
        id, row: 0, depth: 0, entry: id === 'A',
        steps: steps.map(([kind, target]) => ({ kind, target })),
    })),
};
const nodes = {};
for (const id of Object.keys(g)) nodes[id] = { el: { offsetWidth: W, offsetHeight: H }, x: 0, y: 0 };

layoutTree(curEp, nodes, 24, () => {}, () => {});

const box = id => ({ id, x: nodes[id].x, y: nodes[id].y, cx: nodes[id].x + W / 2 });

// 1. everything landed somewhere
for (const id of Object.keys(g)) assert.ok(Number.isFinite(nodes[id].x) && Number.isFinite(nodes[id].y), `${id} unplaced`);

// 2. no two cards sharing a band overlap horizontally — the whole point of the frontier
const bands = {};
for (const id of Object.keys(g)) (bands[nodes[id].y] ||= []).push(id);
for (const [y, ids] of Object.entries(bands)) {
    const sorted = ids.map(box).sort((a, b) => a.x - b.x);
    for (let i = 1; i < sorted.length; i++) {
        assert.ok(sorted[i].x >= sorted[i - 1].x + W,
            `band ${y}: ${sorted[i - 1].id} and ${sorted[i].id} overlap`);
    }
}

// 3. a rendered child is strictly BELOW its parent — this is what makes it top-down
for (const [parent, steps] of Object.entries(g)) {
    for (const [kind, child] of steps) {
        if (kind === 'render') assert.ok(nodes[child].y > nodes[parent].y, `${child} not below ${parent}`);
    }
}

// 4. a SINGLE-caller chain stays on its owner's band, to its right — a hook is inside a
//    component, not under it
assert.strictEqual(nodes.K.y, nodes.B.y, 'helper K left B\'s band');
assert.ok(nodes.K.x > nodes.B.x, 'helper K not right of B');
assert.strictEqual(nodes.Q.y, nodes.B.y, 'Q left the chain band');
assert.ok(nodes.Q.x > nodes.K.x, 'Q not right of K');

// 4b. a MULTI-caller helper belongs to no caller's line, so it goes to a shelf below the whole
//     tree — that is what keeps both of its arrows pointing down instead of one running backwards
const treeY = Math.max(...['A', 'B', 'C', 'D', 'E', 'F', 'K', 'Q'].map(id => nodes[id].y));
assert.ok(nodes.H.y > treeY, 'shared helper H was not shelved below the tree');
assert.ok(nodes.H.y > nodes.A.y && nodes.H.y > nodes.C.y, 'H sits above one of its callers');
assert.strictEqual(nodes.P.y, nodes.H.y, 'H\'s own chain left the shelf');
assert.ok(nodes.P.x > nodes.H.x, 'P not right of H on the shelf');

// 5. a parent is centred over its children
assert.strictEqual(box('A').cx, (box('B').cx + box('C').cx) / 2, 'A not centred over B,C');
assert.strictEqual(box('B').cx, (box('D').cx + box('E').cx) / 2, 'B not centred over D,E');

// 6. B's chain must not sit on top of C's subtree
assert.ok(nodes.C.x >= nodes.K.x + W, 'B\'s chain overlaps sibling C');

// ── the render arrow ──────────────────────────────────────────────────────────────────────────
// Same trick: run the template's own render-edge branch, with the accumulators it appends to
// declared around it and the early `return` contained in an inner function.
const bez = new Function('return ' + grab('function bez(') )();
const drawRender = new Function('g', 'a', 'b', 'i', 'gc', 'esc', 'bez',
    "let defs='',fwd='',labels='';(function(){" + grab('if(g.kind===\'render\'){') + "})();return {defs,fwd,labels};");

const card = (x, y) => ({ x, y, el: { offsetWidth: W, offsetHeight: H } });
const draw = (label, nprops, cond = '') => drawRender(
    { kind: 'render', label, cond, items: [{ nprops }] },
    card(0, 0), card(400, 300), 0, 'ge ge0', String, bez);

// 7. the arrow leaves the parent's bottom edge and lands on the child's top edge
const one = draw('name', 1);
const d = one.fwd.match(/ d="M([\d.-]+),([\d.-]+) C[^"]*? ([\d.-]+),([\d.-]+)"/);
assert.ok(d, 'no render path emitted');
assert.strictEqual(+d[1], W / 2, 'arrow does not start at the parent\'s horizontal centre');
assert.strictEqual(+d[2], H, 'arrow does not start at the parent\'s bottom edge');
assert.strictEqual(+d[3], 400 + W / 2, 'arrow does not end at the child\'s horizontal centre');
assert.ok(+d[4] < 300, 'arrow does not end above the child\'s top edge');
assert.ok(+d[4] < +d[2] + 300, 'arrow does not run downward');

// 8. few props read as names, many collapse to a count — the anti-crowding rule
assert.match(draw('name', 1).labels, />name</, 'a single prop should be named on the arrow');
assert.match(draw('a, b, c', 3).labels, />a, b, c</, 'three props should still be named');
assert.match(draw('a, b, c, d', 4).labels, />4 props</, 'four props should collapse to a count');
assert.doesNotMatch(draw('a, b, c, d', 4).labels, />a, b, c, d</, 'the long list must not be drawn');

// 9. …but every name stays hoverable, so a collapsed edge can still be traced
const many = draw('alpha, beta, gamma, delta', 4);
assert.match(many.labels, /data-vars="alpha beta gamma delta"/, 'collapsed edge lost its prop names');

// 10. a conditional child renders as a DASHED arrow naming its guard; an unconditional one does not
const guarded = draw('userDetails, compact', 2, 'isMobile');
assert.match(guarded.fwd, /stroke-dasharray/, 'a guarded render edge should be dashed');
assert.match(guarded.labels, />if isMobile</, 'the guard should be named on the edge');
assert.doesNotMatch(draw('a', 1).fwd, /stroke-dasharray/, 'an unguarded render edge must stay solid');
assert.doesNotMatch(draw('a', 1).labels, />if /, 'an unguarded edge must not claim a condition');

// 11. guard and props chips must not sit on top of each other when an edge carries both
const ys = [...guarded.labels.matchAll(/<text x="[\d.-]+" y="([\d.-]+)"/g)].map(m => +m[1]);
assert.strictEqual(ys.length, 2, 'expected both a guard chip and a props chip');
assert.ok(Math.abs(ys[0] - ys[1]) >= 18, 'guard and props chips overlap');

// ── helper folding ────────────────────────────────────────────────────────────────────────────
// E ─ calls H1 (3 callers, no subtree), H2 (2 callers, subtree of 1), BIG (2 callers, subtree of 5)
// and SOLO (one caller, no subtree — a helper only in shape, so it must never fold).
const mkFold = new Function('CHIP_MAX', 'collapsed', grab('function fold(src){') + '; return fold;');
const makeFold = (t, hidden = []) => mkFold(t, new Set(hidden));
const node = (id, col, steps, extra = {}) => ({
    id, col, title: id, fnKey: `k:${id}`, steps: steps.map(t => ({ target: t, expr: `${t}()` })), ...extra,
});
const SRC = {
    nodes: [
        node('E', 0, ['A', 'B', 'BIG'], { entry: true }),
        node('A', 1, ['H1', 'H2', 'SOLO']),
        node('B', 1, ['H1', 'H2']),
        node('C', 1, ['H1']),
        node('H1', 2, []),
        node('H2', 2, ['H2KID']),
        node('H2KID', 3, []),
        node('SOLO', 2, []),
        node('BIG', 1, ['G1']),
        node('G1', 2, ['G2']), node('G2', 3, ['G3']), node('G3', 4, ['G4']), node('G4', 5, []),
        node('DEEP', 4, ['BIG']),          // sits past BIG's column → would draw a backward arc
    ],
};
SRC.nodes.find(n => n.id === 'G3').steps.push({ target: 'DEEP', expr: 'DEEP()' });
const ids = ep => new Set(ep.nodes.map(n => n.id));
const stepsOf = (ep, id) => ep.nodes.find(n => n.id === id).steps;

// 12. off means off — and a card nothing calls (the generator's "who calls this" ghosts) survives
assert.deepStrictEqual([...ids(makeFold(-1)(SRC))], [...ids(SRC)], 'threshold off must change nothing');
assert.ok(ids(makeFold(3)(SRC)).has('C'), 'C is called by nobody — a root card must never be pruned');

// 13. at 0 only the leaf helper folds — H2 has a subtree, SOLO has one caller
const f0 = makeFold(0)(SRC);
assert.ok(!ids(f0).has('H1'), 'H1 (3 callers, leaf) should fold at 0');
assert.ok(ids(f0).has('H2'), 'H2 has a subtree — must not fold at 0');
assert.ok(ids(f0).has('SOLO'), 'SOLO has ONE caller — it is flow, not a helper, and must never fold');

// 14. at 1 H2 folds and takes its exclusive child with it
const f1 = makeFold(1)(SRC);
assert.ok(!ids(f1).has('H2'), 'H2 (2 callers, subtree of 1) should fold at 1');
assert.ok(!ids(f1).has('H2KID'), 'H2\'s exclusive child should be dropped with it');

// 15b. a COMPONENT rendered from two parents is shared, but it is the tree itself — never fold it
const TREE = { nodes: [
    { id:'App', col:0, entry:true, fnKey:'k:App', title:'App',
      steps:[{target:'Row',kind:'render',expr:'<Row>'},{target:'Panel',kind:'render',expr:'<Panel>'}] },
    { id:'Row', col:0, fnKey:'k:Row', title:'Row', steps:[{target:'Icon',kind:'render',expr:'<Icon>'}] },
    { id:'Panel', col:0, fnKey:'k:Panel', title:'Panel', steps:[{target:'Icon',kind:'render',expr:'<Icon>'}] },
    { id:'Icon', col:0, fnKey:'k:Icon', title:'Icon', steps:[] },
] };
for (const t of [0, 3, 5]) assert.ok(new Set(makeFold(t)(TREE).nodes.map(n => n.id)).has('Icon'),
    `a shared leaf COMPONENT was folded out of the render tree at ${t}`);

// 15. a big shared flow keeps its card at every sane threshold
for (const t of [0, 1, 2, 3]) assert.ok(makeFold(t)(SRC).nodes.some(n => n.id === 'BIG'), `BIG folded at ${t}`);

// 16. a folded call keeps its row, loses its arrow, and gains a chip that can open the source
const chip = stepsOf(f0, 'A').find(s => s.chip === 'H1');
assert.ok(chip, 'the call to H1 lost its row entirely');
assert.strictEqual(chip.target, null, 'a folded call must not still draw an arrow');
assert.strictEqual(chip.chipKey, 'k:H1', 'the chip cannot open a source it has no key for');

// 17. the caller that would reach BACKWARDS to BIG gets a reference chip, not an arc
const back = stepsOf(f0, 'DEEP').find(s => s.chip === 'BIG');
assert.ok(back, 'DEEP → BIG should have become a back-reference');
assert.strictEqual(back.target, null, 'a back-reference must not also draw an arc');
assert.strictEqual(back.backref, 'BIG', 'the chip has nothing to jump to');
// ...while the caller that reaches FORWARD keeps its ordinary arrow
assert.strictEqual(stepsOf(f0, 'E').find(s => s.chip === 'BIG'), undefined, 'a forward arc must be left alone');
assert.ok(stepsOf(f0, 'E').some(s => s.target === 'BIG'), 'E lost its forward arrow to BIG');

// 18. nothing left behind: no surviving step may point at a node that was dropped
for (const t of [0, 1, 2, 3]) {
    const f = makeFold(t)(SRC), live = ids(f);
    for (const n of f.nodes) for (const s of n.steps)
        assert.ok(!s.target || live.has(s.target), `at ${t}, ${n.id} still points at dropped ${s.target}`);
}

// ── collapsing a node's downstream ────────────────────────────────────────────────────────────
// 19. the collapsed node stays; what hangs off it goes
const cBig = makeFold(3, ['BIG'])(SRC);
assert.ok(ids(cBig).has('BIG'), 'the collapsed node itself must stay — you still need to see it');
for (const g of ['G1', 'G2', 'G3', 'G4'])
    assert.ok(!ids(cBig).has(g), `${g} hangs off BIG and should be hidden with it`);

// 20. only what hangs off it EXCLUSIVELY — a node another live path still reaches must survive
const cA = makeFold(-1, ['A'])(SRC);
assert.ok(ids(cA).has('A'), 'collapsed A should still be drawn');
assert.ok(!ids(cA).has('SOLO'), 'SOLO is reachable only through A and should be hidden');
assert.ok(ids(cA).has('H1'), 'H1 is still reached by B and C — collapsing A must not take it');

// 21. collapsing nothing changes nothing
assert.deepStrictEqual([...ids(makeFold(3, [])(SRC))], [...ids(makeFold(3)(SRC))], 'empty collapse set changed the graph');

// 22. the ↳ count is what would actually vanish, not the raw subtree
const big = makeFold(3)(SRC).nodes.find(n => n.id === 'BIG');
assert.strictEqual(big.down, 5, 'BIG should report its 5 downstream nodes (G1-G4 + DEEP)');
// DEEP calls back into BIG, so the walk goes round the cycle. Everything it reaches is genuinely
// downstream of it — except itself, which is the one thing the count must never include.
assert.strictEqual(makeFold(3)(SRC).nodes.find(n => n.id === 'G3').down, 5,
    'G3 reaches G4 and DEEP, then round through BIG, G1, G2 — five, and never itself');
assert.strictEqual(makeFold(3)(SRC).nodes.find(n => n.id === 'G4').down, 0, 'a leaf should report nothing downstream');
assert.strictEqual(cBig.nodes.find(n => n.id === 'BIG').collapsed, true, 'the card cannot draw the un-collapse switch');

console.log('helper folding OK — folds by subtree size, never folds single-caller flow, no dangling targets');
console.log('collapsing OK — hides only what hangs off the node exclusively, keeps the node and an honest count');
console.log('tree layout OK —', Object.keys(g).length, 'nodes,', Object.keys(bands).length, 'bands');
console.log('render arrow OK — vertical, count-collapsed past 3 props, dashed when guarded');

// ── data-flow visualization ───────────────────────────────────────────────────────────────────
// The three helpers are pulled out of the template the same way as the layout code above, so
// these run the shipped implementations rather than copies of them.
const renderFlowBadge = new Function('bindings', 'colors', 'badgeColor',
    grab('function renderFlowBadge(bindings,colors){').replace(/^function [^{]+\{/, '').replace(/\}$/, ''));
const badgeColor = new Function('name', 'colors',
    grab('function badgeColor(name,colors){').replace(/^function [^{]+\{/, '').replace(/\}$/, ''));
const traceValue = new Function('startVar', 'epNodes', 'maxDepth', 'SPOT_DEPTH',
    grab('function traceValue(startVar,epNodes,maxDepth){').replace(/^function [^{]+\{/, '').replace(/\}$/, ''));
const renderLineageRows = new Function('chain', 'LINEAGE_MAX_ROWS',
    grab('function renderLineageRows(chain){').replace(/^function [^{]+\{/, '').replace(/\}$/, ''));

const SPOT_DEPTH = +tpl.match(/const SPOT_DEPTH=(\d+)/)[1];
const LINEAGE_MAX_ROWS = +tpl.match(/const LINEAGE_MAX_ROWS=(\d+)/)[1];
const badge = (b, pv) => renderFlowBadge(b, pv, badgeColor);
const trace  = (v, n) => traceValue(v, n, SPOT_DEPTH, SPOT_DEPTH);
const hotSet = (v, n) => new Set(trace(v, n).map(s => s.node_id + '\u0000' + s.var_name));
const linRows = c => renderLineageRows(c, LINEAGE_MAX_ROWS);

// deterministic PRNG so a failure is reproducible
let _seed = 12345;
const rnd = () => (_seed = (_seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
const pick = a => a[Math.floor(rnd() * a.length)];

// Feature: data-flow-visualization, Property 5: flow badge presence determined by non-trivial bindings
for (let iter = 0; iter < 200; iter++) {
    const n = Math.floor(rnd() * 6);
    const kind = iter % 3;            // 0 = all trivial, 1 = all renames, 2 = mixed
    const bindings = [];
    for (let k = 0; k < n; k++) {
        const arg = 'v' + k;
        const trivial = kind === 0 || (kind === 2 && rnd() < 0.5);
        bindings.push({ arg_expr: arg, param_name: trivial ? arg : 'p' + k });
    }
    if (iter % 7 === 0) bindings.push({ arg_expr: 'db', param_name: '?' });  // skipped param
    const anyRename = bindings.some(b => b.arg_expr !== b.param_name && b.param_name !== '?');
    const out = badge(bindings, {});
    assert.strictEqual(out.lines.length > 0, anyRename,
        `bindings ${JSON.stringify(bindings)} -> ${JSON.stringify(out)}`);
    // a badge exists only to show a rename; every line it draws must be one
    out.lines.forEach(l => {
        assert.notStrictEqual(l.arg, l.param);
        assert.notStrictEqual(l.param, '?', 'a binding with no destination name must not be drawn');
    });
    if (!anyRename) assert.strictEqual(out.title, '', 'no renames must produce no title text');
}
// an empty / absent list is never a badge
assert.strictEqual(badge([], {}).lines.length, 0);
assert.strictEqual(badge(undefined, {}).lines.length, 0);

// Feature: data-flow-visualization, Property 8: flow badge truncation and argument coloring
for (let iter = 0; iter < 200; iter++) {
    const N = 1 + Math.floor(rnd() * 20);
    const bindings = [], pvcolor = {};
    for (let k = 0; k < N; k++) {
        const arg = 'a' + k;
        bindings.push({ arg_expr: arg, param_name: 'p' + k });   // all non-trivial
        if (rnd() < 0.5) pvcolor[arg] = '#0' + (k % 10) + '0000';
    }
    const out = badge(bindings, pvcolor);
    assert.strictEqual(out.lines.length, Math.min(N, 2), `N=${N}`);
    assert.strictEqual(out.more, N > 2 ? N - 2 : 0, `N=${N} overflow`);
    // the title carries every binding, so "+N more" is never a dead end
    assert.strictEqual(out.title.split('\n').length, N, `N=${N} title completeness`);
    out.lines.forEach(l => {
        assert.strictEqual(l.color, pvcolor[l.arg] || 'var(--ink)',
            `${l.arg} should use its chip colour when it has one, else the default ink`);
        assert.strictEqual(l.paramColor, pvcolor[l.param] || 'var(--ink)',
            `${l.param} must be coloured by the PARAM, not by the arrow`);
    });
}
// one value, one colour, wherever it is named — this is what makes three arrows read as one thread
{
    const colors = { username: '#9c36b5', auth_data: '#1098ad' };
    const b = badge([{ arg_expr: 'auth_data.username', param_name: 'username' }], colors);
    assert.strictEqual(b.lines[0].paramColor, colors.username, 'param keeps the value colour');
    assert.strictEqual(b.lines[0].color, colors.auth_data, 'a dotted name borrows its root colour');
    // and the same name on a later arrow gets the identical colour
    const b2 = badge([{ arg_expr: 'username', param_name: 'who' }], colors);
    assert.strictEqual(b2.lines[0].color, colors.username);
    assert.strictEqual(badge([{ arg_expr: 'x.y', param_name: 'z' }], {}).lines[0].color, 'var(--ink)');
}
// a binding with no destination name never earns a badge on its own
assert.strictEqual(badge([{ arg_expr: 'db', param_name: '?' }], {}).lines.length, 0);

// Feature: data-flow-visualization, Property 7: cross-node spotlight propagates transitively up to depth 10
for (let iter = 0; iter < 200; iter++) {
    // a straight chain n0 -> n1 -> ... renaming the value at every hop, longer than the cap
    const len = 2 + Math.floor(rnd() * 16);
    const nodes = [];
    for (let k = 0; k < len; k++) {
        const here = k === 0 ? 'seed' : 'x' + k;
        const next = 'x' + (k + 1);
        nodes.push({
            id: 'n' + k, params: [here],
            steps: k + 1 < len
                ? [{ target: 'n' + (k + 1), bindings: [{ arg_expr: here, param_name: next }] }]
                : [],
        });
    }
    const hot = hotSet('seed', nodes);
    // every hop within the cap is reachable...
    for (let k = 0; k <= Math.min(len - 1, SPOT_DEPTH); k++) {
        const nm = k === 0 ? 'seed' : 'x' + k;
        assert.ok(hot.has('n' + k + '\u0000' + nm), `depth ${k} should be hot (len=${len})`);
    }
    // ...and nothing beyond it is, or the "trace" becomes the whole chart
    for (let k = SPOT_DEPTH + 1; k < len; k++) {
        assert.ok(!hot.has('n' + k + '\u0000x' + k), `depth ${k} must be past the cap (len=${len})`);
    }
}
// a value nobody knows lights nothing; no start variable lights nothing
assert.strictEqual(hotSet('nope', [{ id: 'n0', params: ['a'], steps: [] }]).size, 0);
assert.strictEqual(hotSet(null, [{ id: 'n0', params: ['a'], steps: [] }]).size, 0);

// a cycle must not spin: A -> B -> A under the same name terminates
{
    const cyc = [
        { id: 'A', params: ['v'], steps: [{ target: 'B', bindings: [{ arg_expr: 'v', param_name: 'w' }] }] },
        { id: 'B', params: ['w'], steps: [{ target: 'A', bindings: [{ arg_expr: 'w', param_name: 'v' }] }] },
    ];
    assert.deepStrictEqual([...hotSet('v', cyc)].sort(), ['A\u0000v', 'B\u0000w']);
}

// Feature: data-flow-visualization, Property 6: lineage panel row count and rename display
for (let iter = 0; iter < 200; iter++) {
    const N = Math.floor(rnd() * 200);
    const names = ['a', 'b', 'c'];
    const chain = [];
    for (let k = 0; k < N; k++) {
        // a fan-out: stop k arrives from an ARBITRARY earlier stop, not necessarily k-1
        chain.push({ node_id: 'n' + k, var_name: pick(names), depth: k ? 1 : 0,
                     from: k ? chain[Math.floor(rnd() * k)].var_name : null });
    }
    const rows = linRows(chain);
    assert.strictEqual(rows.length, Math.min(N, LINEAGE_MAX_ROWS), `N=${N}`);
    rows.forEach((r, i) => {
        // the rename is against where the value CAME FROM, never against the row above it
        const prev = chain[i].from;
        const renamed = prev !== null && prev !== chain[i].var_name;
        assert.strictEqual(r.renamed, renamed, `row ${i} rename flag`);
        assert.strictEqual(r.name.includes('\u2192'), renamed, `row ${i}: arrow iff renamed`);
        if (renamed) assert.ok(r.name.startsWith(prev + ' '), `row ${i} must point from ${prev}`);
    });
    // the first stop is where the value is introduced — it has no predecessor to point from
    if (rows.length) assert.strictEqual(rows[0].renamed, false);
}
assert.deepStrictEqual(linRows([]), []);
assert.deepStrictEqual(linRows(undefined), []);

// Feature: data-flow-visualization, Property 9: lineage variable lookup is case-insensitive
const findLineage = new Function('ep', 'varName', 'loose', 'namesIn', 'traceValue',
    grab('function findLineage(ep,varName,loose){').replace(/^function [^{]+\{/, '').replace(/\}$/, ''));
const namesIn = new Function('ep',
    grab('function namesIn(ep){').replace(/^function [^{]+\{/, '').replace(/\}$/, ''));
const findLin = (ep, v, loose) => findLineage(ep, v, loose, namesIn, (a,b)=>trace(a,b));
// a two-card chart that hands `base` along, so the lookup has something real to resolve against
const chartFor = base => ({ nodes: [
    { id: 'n0', params: [base], steps: [{ target: 'n1', bindings: [{ arg_expr: base, param_name: 'inner' }] }] },
    { id: 'n1', params: ['inner'], steps: [] }] });
// resolves to the chart's own spelling of the name, or null when the chart does not know it
const lookup = (base, varName) => { const g = findLin(chartFor(base), varName, true); return g && g.var; };
for (let iter = 0; iter < 200; iter++) {
    const base = pick(['userId', 'TENANT', 'order_ref', 'Xy']);
    const variants = [base.toUpperCase(), base.toLowerCase(),
        [...base].map((c, i) => (i % 2 ? c.toUpperCase() : c.toLowerCase())).join('')];
    variants.forEach(v => assert.strictEqual(lookup(base, v), base, `${v} should find ${base}`));
    // a genuinely different name still misses — case-insensitive, not fuzzy
    assert.strictEqual(lookup(base, base + '_other'), null);
}
// A value handed on under a new name must trace back to the card that introduced it: clicking
// `username` in the service has to show the endpoint passing `auth_data.username`.
{
    const ep = { nodes: [
        { id: 'ctrl', params: ['auth_data'], steps: [{ target: 'svc',
            bindings: [{ arg_expr: 'auth_data.username', param_name: 'username' }] }] },
        { id: 'svc', params: ['username'], steps: [{ target: 'repo',
            bindings: [{ arg_expr: 'username', param_name: 'username' }] }] },
        { id: 'repo', params: ['username'], steps: [] }] };
    const got = findLin(ep, 'username');
    assert.strictEqual(got.chain.length, 3, 'the trail must reach back to the controller');
    assert.strictEqual(got.chain[0].node_id, 'ctrl');
    assert.strictEqual(got.chain[0].var_name, 'auth_data.username');
    assert.strictEqual(got.chain[1].from, 'auth_data.username', 'the rename is recorded at the hop');
    assert.strictEqual(findLin(ep, 'USERNAME', true).chain.length, 3);
    assert.strictEqual(findLin(ep, 'nope'), null);
    // every stop names a card that is actually in this chart — the bug that made the panel useless
    const ids = new Set(ep.nodes.map(n => n.id));
    got.chain.forEach(s => assert.ok(ids.has(s.node_id), `${s.node_id} is not in the chart`));
}
// folding removes cards; a trail computed against the folded chart can never point outside it
{
    const folded = { nodes: [
        { id: 'a', params: ['v'], steps: [{ target: null, bindings: [{ arg_expr: 'v', param_name: 'w' }] }] }] };
    trace('v', folded.nodes).forEach(s => assert.strictEqual(s.node_id, 'a'));
}

console.log('flow badge OK — renames only, two lines then +N more, chip colours, full title');
console.log('cross-node spotlight OK — follows renames, caps at depth', SPOT_DEPTH + ', terminates on cycles');
console.log('lineage rows OK — capped at', LINEAGE_MAX_ROWS + ', arrow shown exactly on a rename');
console.log('lineage lookup OK — case-insensitive exact match');

// Every `$('#foo')` in the script must name an element that actually exists in the markup.
// The overview panel shipped as CSS + JS with no <div id="ovwpanel">, so buildOverview() threw
// on boot and a --changed run rendered nothing at all.
const markupIds = new Set([...tpl.matchAll(/\bid=["']([^"']+)["']/g)].map(m => m[1]));
const missing = [...new Set([...tpl.matchAll(/\$\(\s*'#([A-Za-z0-9_-]+)'\s*\)/g)].map(m => m[1]))]
    .filter(id => !markupIds.has(id));
assert.deepStrictEqual(missing, [], `$('#id') with no element in the markup: ${missing.join(', ')}`);

console.log('id wiring OK — every $(\'#id\') resolves to an element in the markup');
