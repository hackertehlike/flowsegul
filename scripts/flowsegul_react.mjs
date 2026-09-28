#!/usr/bin/env node
// flowsegul React mode: a static map of a React + TypeScript app.
//
//   node flowsegul_react.mjs ROOT [--tag NAME]   → JSON on stdout
//   node flowsegul_react.mjs --check [ROOT]      → exit 0 when the TypeScript compiler can be found
//
// Uses the TypeScript compiler API (its TypeChecker resolves names across files, imports,
// re-exports and destructured props). `typescript` is looked up in the app first, then next to
// this script, then in the global npm folder; FLOWSEGUL_TS can point at a folder that has it.
//
// What it finds, all tied to real source lines ("rows"):
//  - components, custom hooks, and module functions reached from them ("units")
//  - every useState / useReducer, with every line that sets it, following the setter when it is
//    passed down as a prop under another name, through wrappers (`const h = v => setX(v)`,
//    useCallback), custom hook returns and context values
//  - user actions (event handler props on DOM elements, `useEffect(..., [])` as page load) and,
//    for each, the ordered path through the code: handler → prop hops up the tree → setter →
//    state → components that rerun → effects that depend on it → fetch calls → further setters
// Anything it can't resolve for sure (spread props, context) is marked `unc` and never claimed.

import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));

export function loadTypeScript(root) {
  const tries = [];
  const env = process.env.FLOWSEGUL_TS;
  if (env) {
    tries.push(() => createRequire(path.join(path.resolve(env), 'noop.js'))('typescript'));
    tries.push(() => createRequire(path.join(HERE, 'noop.js'))(path.resolve(env)));
  }
  if (root) tries.push(() => createRequire(path.join(path.resolve(root), 'package.json'))('typescript'));
  tries.push(() => createRequire(import.meta.url)('typescript'));
  tries.push(() => {
    const g = execSync('npm root -g', { stdio: ['ignore', 'pipe', 'ignore'], timeout: 20000 }).toString().trim();
    return createRequire(path.join(g, 'noop.js'))(path.join(g, 'typescript'));
  });
  for (const t of tries) {
    try { const ts = t(); if (ts && ts.createProgram) return ts; } catch (e) { /* try the next place */ }
  }
  return null;
}

const TS_MISSING = 'flowsegul: React mode needs the TypeScript compiler (npm package "typescript"). '
  + 'Add it to the app (npm i -D typescript) or globally (npm i -g typescript), or set FLOWSEGUL_TS to a folder that has node_modules/typescript.';

// ── CLI ──
function main() {
  const argv = process.argv.slice(2);
  const check = argv.includes('--check');
  const tagAt = argv.indexOf('--tag');
  const tag = tagAt >= 0 ? argv[tagAt + 1] : '';
  const root = argv.filter((a, i) => !a.startsWith('--') && argv[i - 1] !== '--tag')[0];
  const ts = loadTypeScript(root);
  if (!ts) { console.error(TS_MISSING); process.exit(3); }
  if (check) { console.log('typescript ' + ts.version); return; }
  if (!root || !fs.existsSync(root)) { console.error('flowsegul: usage: flowsegul_react.mjs ROOT [--tag NAME]'); process.exit(2); }
  const out = analyze(ts, path.resolve(root), { tag });
  process.stdout.write(JSON.stringify(out));
}

// ── files ──
const SKIP_DIRS = new Set(['node_modules', 'dist', 'build', 'out', 'coverage', 'vendor', 'public', 'static',
  '__tests__', '__mocks__', 'test', 'tests', 'e2e', 'cypress', 'playwright', 'storybook-static', 'tmp']);
const SRC_RE = /\.(tsx|ts|jsx|js|mjs)$/;
const SKIP_FILE_RE = /(\.d\.ts|\.(test|spec|stories|story|e2e)\.[cm]?[jt]sx?|\.config\.[cm]?[jt]s|\.min\.js)$/;

function listFiles(root) {
  const out = [];
  const walk = (dir) => {
    let ents;
    try { ents = fs.readdirSync(dir, { withFileTypes: true }); } catch (e) { return; }
    ents.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
    for (const e of ents) {
      const p = path.join(dir, e.name);
      if (e.isDirectory()) {
        if (e.name.startsWith('.') || SKIP_DIRS.has(e.name)) continue;
        walk(p);
      } else if (e.isFile() && SRC_RE.test(e.name) && !SKIP_FILE_RE.test(e.name)) {
        try { if (fs.statSync(p).size < 400000) out.push(p); } catch (err) { /* unreadable */ }
      }
    }
  };
  walk(root);
  return out;
}

function compilerOptions(ts, root) {
  let opts = {};
  const cfg = ts.findConfigFile(root, ts.sys.fileExists, 'tsconfig.json');
  const read = (file) => {
    try {
      const r = ts.readConfigFile(file, ts.sys.readFile);
      return ts.parseJsonConfigFileContent(r.config || {}, ts.sys, path.dirname(file));
    } catch (e) { return null; }
  };
  if (cfg) {
    const p = read(cfg);
    if (p) {
      opts = p.options || {};
      // a "solution" tsconfig (Vite's template) keeps paths/baseUrl in the configs it references
      for (const ref of p.projectReferences || []) {
        if (opts.paths) break;
        const file = ref.path.endsWith('.json') ? ref.path : path.join(ref.path, 'tsconfig.json');
        const q = fs.existsSync(file) ? read(file) : null;
        if (q && q.options) {
          for (const k of ['paths', 'baseUrl', 'jsx', 'moduleResolution', 'module', 'target']) {
            if (q.options[k] != null && opts[k] == null) opts[k] = q.options[k];
          }
        }
      }
    }
  }
  opts = Object.assign({}, opts, {
    allowJs: true, checkJs: false, noEmit: true, skipLibCheck: true, types: [],
    esModuleInterop: true, allowSyntheticDefaultImports: true,
    composite: false, incremental: false, declaration: false, emitDeclarationOnly: false,
    tsBuildInfoFile: undefined, noResolve: false,
  });
  if (opts.jsx == null) opts.jsx = ts.JsxEmit.Preserve;
  if (opts.moduleResolution == null) {
    const k = ts.ModuleResolutionKind;
    opts.moduleResolution = k.Bundler != null ? k.Bundler : (k.Node10 != null ? k.Node10 : k.NodeJs);
    opts.module = ts.ModuleKind.ESNext;
  }
  if (opts.target == null) opts.target = ts.ScriptTarget.ES2020;
  return opts;
}

// ── analysis ──
export function analyze(ts, root, { tag = '' } = {}) {
  const t0 = Date.now();
  const files = listFiles(root);
  const program = ts.createProgram(files, compilerOptions(ts, root));
  const checker = program.getTypeChecker();
  const fileSet = new Set(files.map((f) => path.resolve(f)));
  const sources = program.getSourceFiles().filter((sf) => fileSet.has(path.resolve(sf.fileName)))
    .sort((a, b) => (a.fileName < b.fileName ? -1 : 1));
  const rel = (sf) => path.relative(root, sf.fileName).split(path.sep).join('/');
  const P = tag ? tag + ':' : '';

  // ── small helpers ──
  const lineOf = (sf, pos) => sf.getLineAndCharacterOfPosition(pos).line;
  const startLine = (n) => lineOf(n.getSourceFile(), n.getStart());
  const endLine = (n) => lineOf(n.getSourceFile(), n.getEnd());
  const isFnLike = (n) => n && (ts.isArrowFunction(n) || ts.isFunctionExpression(n) || ts.isFunctionDeclaration(n)
    || ts.isMethodDeclaration(n) || ts.isGetAccessor(n) || ts.isSetAccessor(n) || ts.isConstructorDeclaration(n));
  const isWrapper = (n) => n && (ts.isParenthesizedExpression(n) || ts.isAsExpression(n) || ts.isNonNullExpression(n)
    || ts.isTypeAssertionExpression(n) || (ts.isSatisfiesExpression && ts.isSatisfiesExpression(n)));
  const skipOuter = (e) => { while (e && isWrapper(e)) e = e.expression; return e; };
  const upOuter = (n) => { while (n.parent && isWrapper(n.parent)) n = n.parent; return n; };
  const calleeName = (call) => {
    const e = skipOuter(call.expression);
    if (ts.isIdentifier(e)) return e.text;
    if (ts.isPropertyAccessExpression(e)) return e.name.text;
    return '';
  };
  const symOf = (node) => {
    let s;
    try { s = checker.getSymbolAtLocation(node); } catch (e) { return undefined; }
    if (s && (s.flags & ts.SymbolFlags.Alias)) { try { s = checker.getAliasedSymbol(s); } catch (e) { /* keep */ } }
    return s;
  };
  const enclosingFn = (n) => { let p = n.parent; while (p && !isFnLike(p)) p = p.parent; return p; };
  const attrName = (a) => (a.name && a.name.text) || (a.name && a.name.getText()) || '';
  const EFFECTS = new Set(['useEffect', 'useLayoutEffect', 'useInsertionEffect']);

  // ── units: components, hooks, module functions ──
  const units = [];
  const unitByFn = new Map();
  const unitBySym = new Map();
  const baseName = (sf) => {
    const b = path.basename(sf.fileName).replace(/\.[^.]+$/, '');
    return b === 'index' ? path.basename(path.dirname(sf.fileName)) : b;
  };
  const containsJsx = (fn) => {
    let found = false;
    const v = (n) => { if (found) return; if (ts.isJsxElement(n) || ts.isJsxSelfClosingElement(n) || ts.isJsxFragment(n)) { found = true; return; } ts.forEachChild(n, v); };
    if (fn.body) v(fn.body);
    return found;
  };
  // memo(...) / forwardRef(...) / observer(...) around a function, or around a name
  const WRAPS = new Set(['memo', 'forwardRef', 'observer']);
  function unwrapFn(e) {
    e = skipOuter(e);
    if (!e) return {};
    if (ts.isArrowFunction(e) || ts.isFunctionExpression(e)) return { fn: e, memo: false };
    if (ts.isCallExpression(e) && WRAPS.has(calleeName(e)) && e.arguments[0]) {
      const r = unwrapFn(e.arguments[0]);
      const memo = calleeName(e) === 'memo';
      if (r.fn) return { fn: r.fn, memo: r.memo || memo };
      const a = skipOuter(e.arguments[0]);
      if (ts.isIdentifier(a)) return { ref: a, memo: r.memo || memo };
    }
    return {};
  }
  function addUnit(fn, sf, name, memo, stmt, nameNode) {
    if (unitByFn.has(fn) || !fn.body) return null;
    const kind = /^use[A-Z0-9]/.test(name) ? 'hook' : (/^[A-Z]/.test(name) && containsJsx(fn) ? 'component' : 'fn');
    const u = { id: P + 'u' + units.length, idx: units.length, name, kind, fn, sf, file: rel(sf), stmt, memo: !!memo, rows: [],
      renders: [], parents: [], hookCalls: [], states: [], effects: [], props: null, depth: null };
    units.push(u); unitByFn.set(fn, u);
    if (nameNode) { const s = symOf(nameNode); if (s) unitBySym.set(s, u); }
    return u;
  }
  for (const sf of sources) {
    for (const st of sf.statements) {
      if (ts.isFunctionDeclaration(st) && st.body) {
        const isDefault = (ts.getCombinedModifierFlags(st) & ts.ModifierFlags.Default) !== 0;
        const u = addUnit(st, sf, st.name ? st.name.text : baseName(sf), false, st, st.name);
        if (u && isDefault && !st.name) { const ds = defaultSym(sf); if (ds) unitBySym.set(ds, u); }
      } else if (ts.isVariableStatement(st)) {
        for (const d of st.declarationList.declarations) {
          if (!ts.isIdentifier(d.name) || !d.initializer) continue;
          const r = unwrapFn(d.initializer);
          if (r.fn) addUnit(r.fn, sf, d.name.text, r.memo, st, d.name);
        }
      } else if (ts.isExportAssignment(st)) {
        const r = unwrapFn(st.expression);
        if (r.fn) {
          const u = addUnit(r.fn, sf, (r.fn.name && r.fn.name.text) || baseName(sf), r.memo, st, null);
          const ds = defaultSym(sf); if (u && ds) unitBySym.set(ds, u);
        }
      }
    }
  }
  function defaultSym(sf) {
    const s = checker.getSymbolAtLocation(sf);
    return s && s.exports && s.exports.get('default');
  }
  // a function reached from a path that isn't top level (a class method, an object's arrow): its own box
  function ensureUnit(fn) {
    if (!fn || !fn.body || unitOf(fn)) return;
    const sf = fn.getSourceFile();
    if (!fileSet.has(path.resolve(sf.fileName))) return;
    let name = fn.name ? fn.name.getText() : '', stmt = fn;
    const up = upOuter(fn).parent;
    if (!name && up && (ts.isPropertyAssignment(up) || ts.isVariableDeclaration(up))) { name = up.name.getText(); stmt = up; }
    let owner = ts.isMethodDeclaration(fn) ? fn.parent : up && ts.isPropertyAssignment(up) ? up.parent : null;
    if (owner && ts.isClassLike(owner) && owner.name) name = owner.name.text + '.' + name;
    else if (owner && ts.isObjectLiteralExpression(owner)) { const vd = upOuter(owner).parent; if (vd && ts.isVariableDeclaration(vd)) name = vd.name.getText() + '.' + name; }
    const u = addUnit(fn, sf, name || 'function', false, stmt, null);
    if (u) u.kind = 'fn';
  }
  const unitOf = (node) => { let n = node; while (n) { const u = unitByFn.get(n); if (u) return u; n = n.parent; } return null; };

  // a name → the unit it stands for, and whether memo() wraps it on the way
  const unitRefCache = new Map();
  function unitFromSym(sym, depth = 0) {
    if (!sym || depth > 6) return null;
    if (unitRefCache.has(sym)) return unitRefCache.get(sym);
    unitRefCache.set(sym, null);
    let res = null;
    const direct = unitBySym.get(sym);
    if (direct) res = { unit: direct, memo: direct.memo };
    for (const d of sym.declarations || []) {
      if (res) break;
      let e = null;
      if (ts.isVariableDeclaration(d) && d.initializer) e = d.initializer;
      else if (ts.isExportAssignment(d)) e = d.expression;
      else if (ts.isFunctionDeclaration(d) && unitByFn.get(d)) res = { unit: unitByFn.get(d), memo: false };
      if (!e) continue;
      const r = unwrapFn(e);
      if (r.fn && unitByFn.get(r.fn)) res = { unit: unitByFn.get(r.fn), memo: r.memo };
      else if (r.ref) { const inner = unitFromSym(symOf(r.ref), depth + 1); if (inner) res = { unit: inner.unit, memo: inner.memo || r.memo }; }
      else { const x = skipOuter(e); if (ts.isIdentifier(x)) res = unitFromSym(symOf(x), depth + 1); }
    }
    unitRefCache.set(sym, res);
    return res;
  }

  // JSX tag → {intrinsic} | {unit, memo} | {provider, ctx} | {external}
  const tagCache = new Map();
  function resolveTag(op) {
    if (tagCache.has(op)) return tagCache.get(op);
    const tn = op.tagName;
    let r;
    if (ts.isIdentifier(tn) && (/^[a-z]/.test(tn.text) || tn.text.includes('-'))) r = { intrinsic: true, name: tn.text };
    else if (ts.isPropertyAccessExpression(tn) && (tn.name.text === 'Provider' || tn.name.text === 'Consumer')) {
      r = { provider: tn.name.text === 'Provider', ctx: symOf(tn.expression), name: tn.getText() };
    } else {
      const u = unitFromSym(symOf(tn));
      r = u && u.unit.kind === 'component' ? { unit: u.unit, memo: u.memo, name: tn.getText() } : { external: true, name: tn.getText() };
    }
    tagCache.set(op, r);
    return r;
  }

  // ── rows: real source lines ──
  const rowsByUnit = new Map();
  const allRows = [];
  const lineText = (sf, L) => {
    const starts = sf.getLineStarts();
    const s = starts[L], e = L + 1 < starts.length ? starts[L + 1] : sf.text.length;
    return sf.text.slice(s, e).replace(/\r?\n$/, '');
  };
  const leadWidth = (s) => { let w = 0; for (const ch of s) { if (ch === ' ') w += 1; else if (ch === '\t') w += 2; else break; } return w; };
  // lines a..b, each trimmed, joined by one space; plus a map from source positions to text offsets
  function joinLines(sf, a, b) {
    const offs = [], leads = [], lens = [];
    let text = '';
    for (let L = a; L <= b; L++) {
      const raw = lineText(sf, L), t = raw.trim();
      if (text && t) text += ' ';
      offs.push(text.length); leads.push(raw.length - raw.trimStart().length); lens.push(t.length);
      text += t;
    }
    const map = (pos) => {
      const lc = sf.getLineAndCharacterOfPosition(pos);
      if (lc.line < a || lc.line > b) return -1;
      const i = lc.line - a;
      return offs[i] + Math.max(0, Math.min(lens[i], lc.character - leads[i]));
    };
    return { text, map };
  }
  // first multi-line function body inside `node`, as a line: rows stop there, the body's own lines get their own rows
  function firstBodyLine(node) {
    let found = null;
    // a block body, or an expression body over several lines (render={({ field }) => (…)})
    const multi = (n) => isFnLike(n) && n.body && startLine(n.body) !== endLine(n.body);
    const v = (n) => {
      if (found != null) return;
      if (multi(n)) { found = startLine(n.body); return; }
      ts.forEachChild(n, v);
    };
    ts.forEachChild(node, v);
    if (found == null && multi(node)) found = startLine(node.body);
    return found;
  }
  function elemRange(op) {
    const a = startLine(op);
    let b = endLine(op);
    for (const p of op.attributes.properties) {
      if (ts.isJsxAttribute(p) && p.initializer && ts.isJsxExpression(p.initializer) && p.initializer.expression) {
        const cut = firstBodyLine(p.initializer);
        if (cut != null && cut < b) { b = cut; break; }
      }
    }
    if (b - a > 7) b = a + 7;
    if (ts.isJsxOpeningElement(op) && b === endLine(op)) {
      const eb = endLine(op.parent);
      if (eb - a <= 2) b = eb;   // `<button …>Apply</button>` reads as one line
    }
    return [a, b];
  }
  function enclosingOpening(node) {
    let n = node;
    while (n && !ts.isSourceFile(n)) {
      if (ts.isJsxOpeningElement(n) || ts.isJsxSelfClosingElement(n)) return n;
      if (n.parent && ts.isJsxElement(n.parent) && n !== n.parent.openingElement) return null;   // in children
      if (unitByFn.has(n)) return null;
      n = n.parent;
    }
    return null;
  }
  function mkRow(node, a, b) {
    const u = unitOf(node);
    if (!u) return null;
    const list = rowsByUnit.get(u) || [];
    for (const r of list) if (r.a <= a && r.b >= b) return r;
    const sf = node.getSourceFile();
    const r = { id: P + 'r' + allRows.length, u, sf, a, b, marks: [], api: [], kids: [], unc: false };
    list.push(r); rowsByUnit.set(u, list); allRows.push(r);
    return r;
  }
  function rowFor(node) {
    if (!node) return null;
    const op = enclosingOpening(node);
    if (op) {
      const [a, b] = elemRange(op);
      const L = startLine(node);
      if (L >= a && L <= b) {
        const r = mkRow(node, a, b);
        // shown as at most 8 lines; where the tag's props really end, so a diff there is seen too
        if (r) r.pe = Math.max(r.pe || r.b, endLine(op));
        return r;
      }
    }
    const a = startLine(node);
    let b = endLine(node);
    const cut = firstBodyLine(node);
    if (cut != null && cut < b) b = Math.max(a, cut);
    if (b - a > 2) b = a + 2;
    return mkRow(node, a, b);
  }

  // ── identifier index: every name, resolved once ──
  const refs = new Map();        // symbol → [expression nodes that use it]
  const idsByFile = new Map();   // file → [{node, sym, decl}] in source order
  const calls = [];              // every call expression in the app's files
  const isDeclName = (n) => {
    const p = n.parent;
    if (!p) return false;
    if ((ts.isVariableDeclaration(p) || ts.isBindingElement(p) || ts.isParameter(p) || ts.isFunctionDeclaration(p)
      || ts.isFunctionExpression(p) || ts.isClassDeclaration(p) || ts.isPropertyAssignment(p) || ts.isPropertyDeclaration(p)
      || ts.isMethodDeclaration(p) || ts.isPropertySignature(p) || ts.isMethodSignature(p) || ts.isEnumMember(p)
      || ts.isTypeAliasDeclaration(p) || ts.isInterfaceDeclaration(p) || ts.isImportSpecifier(p) || ts.isImportClause(p)
      || ts.isNamespaceImport(p) || ts.isExportSpecifier(p) || ts.isJsxAttribute(p) || ts.isEnumDeclaration(p)
      || ts.isModuleDeclaration(p) || ts.isGetAccessor(p) || ts.isSetAccessor(p) || ts.isLabeledStatement(p)) && p.name === n) return true;
    if (ts.isBindingElement(p) && p.propertyName === n) return true;
    if (ts.isPropertyAccessExpression(p) && p.name === n) return true;
    if (ts.isQualifiedName(p)) return true;
    return false;
  };
  for (const sf of sources) {
    const list = [];
    const v = (n) => {
      if (ts.isTypeNode(n) && !ts.isExpressionWithTypeArguments(n)) return;
      if (ts.isImportDeclaration(n) || ts.isExportDeclaration(n)) return;
      if (ts.isCallExpression(n)) calls.push(n);
      if (ts.isIdentifier(n)) {
        const p = n.parent;
        if (ts.isPropertyAccessExpression(p) && p.name === n) list.push({ node: n, pa: p });
        else if (isDeclName(n)) {
          if (ts.isVariableDeclaration(p) || ts.isBindingElement(p) || ts.isParameter(p)) list.push({ node: n, sym: symOf(n), decl: true });
        } else {
          const s = ts.isShorthandPropertyAssignment(p) && p.name === n ? checker.getShorthandAssignmentValueSymbol(p) : symOf(n);
          if (s) {
            list.push({ node: n, sym: s });
            let arr = refs.get(s); if (!arr) refs.set(s, arr = []); arr.push(n);
          }
        }
      }
      ts.forEachChild(n, v);
    };
    v(sf);
    idsByFile.set(sf.fileName, list);
  }

  // ── per unit: props, states, effects, JSX, hook calls ──
  function readProps(u) {
    const p = u.fn.parameters && u.fn.parameters[0];
    if (!p) return null;
    const out = { bind: new Map(), rest: null, ident: null, elems: new Map() };
    if (ts.isObjectBindingPattern(p.name)) {
      for (const el of p.name.elements) {
        if (!ts.isIdentifier(el.name)) continue;
        if (el.dotDotDotToken) { out.rest = symOf(el.name); continue; }
        const key = el.propertyName ? (el.propertyName.text || el.propertyName.getText()) : el.name.text;
        const s = symOf(el.name);
        if (s) { out.bind.set(key, s); out.elems.set(key, el); }
      }
    } else if (ts.isIdentifier(p.name)) out.ident = symOf(p.name);
    return out;
  }
  const propOwner = new Map();   // prop binding symbol / props object symbol → {unit, key|null}
  for (const u of units) {
    u.props = readProps(u);
    if (u.props) {
      for (const [k, s] of u.props.bind) propOwner.set(s, { unit: u, key: k });
      if (u.props.rest) propOwner.set(u.props.rest, { unit: u, key: null, rest: true });
      if (u.props.ident) propOwner.set(u.props.ident, { unit: u, key: null, obj: true });
    }
  }

  const states = [];
  const stateBySetter = new Map();
  const stateByValue = new Map();
  const renderSites = new Map();   // child unit → [{parent unit, op, memo}]
  const providers = new Map();     // context symbol → [opening elements]
  const ctxCalls = new Map();      // context symbol → [useContext calls]
  const hookCallSites = new Map(); // hook unit → [calls]
  const httpOf = new Map();        // call → {m, u, id, via}

  for (const u of units) {
    const v = (n) => {
      if (n !== u.fn && unitByFn.has(n)) return;
      if (ts.isVariableDeclaration(n) && ts.isArrayBindingPattern(n.name) && n.initializer) {
        const init = skipOuter(n.initializer);
        const cn = ts.isCallExpression(init) ? calleeName(init) : '';
        if (cn === 'useState' || cn === 'useReducer') {
          const [ve, se] = n.name.elements;
          const val = ve && ts.isBindingElement(ve) && ts.isIdentifier(ve.name) ? ve.name : null;
          const set = se && ts.isBindingElement(se) && ts.isIdentifier(se.name) ? se.name : null;
          const stmt = n.parent && n.parent.parent && ts.isVariableStatement(n.parent.parent) ? n.parent.parent : n;
          const S = { id: P + 's' + states.length, unit: u, decl: n, kind: cn, name: val ? val.text : '', setter: set ? set.text : '',
            valueSym: val && symOf(val), setterSym: set && symOf(set), row: rowFor(stmt) };
          states.push(S); u.states.push(S);
          if (S.setterSym) stateBySetter.set(S.setterSym, S);
          if (S.valueSym) stateByValue.set(S.valueSym, S);
        }
      }
      if (ts.isCallExpression(n)) {
        const cn = calleeName(n);
        if (EFFECTS.has(cn) && n.arguments[0] && isFnLike(skipOuter(n.arguments[0]))) {
          u.effects.push({ call: n, cb: skipOuter(n.arguments[0]), deps: n.arguments[1] && ts.isArrayLiteralExpression(skipOuter(n.arguments[1])) ? skipOuter(n.arguments[1]) : null, unit: u });
        }
      }
      if (ts.isJsxOpeningElement(n) || ts.isJsxSelfClosingElement(n)) {
        const t = resolveTag(n);
        if (t.unit) {
          let arr = renderSites.get(t.unit); if (!arr) renderSites.set(t.unit, arr = []);
          arr.push({ parent: u, op: n, memo: t.memo });
          u.renders.push({ child: t.unit, op: n, memo: t.memo });
        } else if (t.provider && t.ctx) {
          let arr = providers.get(t.ctx); if (!arr) providers.set(t.ctx, arr = []);
          arr.push(n);
        }
      }
      ts.forEachChild(n, v);
    };
    v(u.fn);
  }
  for (const c of calls) {
    const cn = calleeName(c);
    if (cn === 'useContext' && c.arguments[0]) {
      const s = symOf(skipOuter(c.arguments[0]));
      if (s) { let arr = ctxCalls.get(s); if (!arr) ctxCalls.set(s, arr = []); arr.push(c); }
      continue;
    }
    const callee = skipOuter(c.expression);
    if (ts.isIdentifier(callee) && /^use[A-Z0-9]/.test(callee.text)) {
      const r = unitFromSym(symOf(callee));
      if (r && r.unit.kind === 'hook') {
        let arr = hookCallSites.get(r.unit); if (!arr) hookCallSites.set(r.unit, arr = []);
        arr.push(c);
        const caller = unitOf(c); if (caller) caller.hookCalls.push({ hook: r.unit, call: c });
      }
    }
  }
  // render tree: parents, depth
  for (const [child, sites] of renderSites) {
    for (const s of sites) if (!child.parents.includes(s.parent)) child.parents.push(s.parent);
  }
  {
    const comps = units.filter((u) => u.kind === 'component');
    const q = comps.filter((u) => !u.parents.length);
    q.forEach((u) => { u.depth = 0; });
    for (let i = 0; i < q.length; i++) {
      for (const r of q[i].renders) if (r.child.depth == null) { r.child.depth = q[i].depth + 1; q.push(r.child); }
    }
    comps.forEach((u) => { if (u.depth == null) u.depth = 0; });
    // hooks sit right of the components that call them
    for (let pass = 0; pass < 4; pass++) {
      for (const u of units) {
        if (u.kind !== 'hook') continue;
        const ds = (hookCallSites.get(u) || []).map((c) => unitOf(c)).filter((x) => x && x.depth != null).map((x) => x.depth + 1);
        if (ds.length) u.depth = Math.min(...ds);
      }
    }
  }
  // components that own a hook's state: whoever calls the hook, through other hooks too
  function hookOwners(h, seen = new Set()) {
    if (seen.has(h)) return [];
    seen.add(h);
    const out = [];
    for (const c of hookCallSites.get(h) || []) {
      const u = unitOf(c);
      if (!u) continue;
      if (u.kind === 'hook') out.push(...hookOwners(u, seen));
      else out.push({ unit: u, call: c });
    }
    return out;
  }

  // ── requests: the method, URL and query keys each call sends ──
  // A URL is read as parts: text, a value only known at run time ('dyn', with its source), a
  // variable from the environment ('env'), or a parameter of the function the call sits in
  // ('param'). A parameter makes that function a wrapper, and its callers supply the URL:
  // `api('/items')` where `api = (path) => fetch(BASE + path)`. The same goes for a module-level
  // function that makes one request (a generated client's `ItemsService.readItems(...)`): its
  // callers get the request too, with what they pass (`{ query: { skip } }`) filled in.
  const METHODS = new Set(['get', 'post', 'put', 'patch', 'delete', 'head', 'options']);
  const BODY_ARG = new Set(['post', 'put', 'patch']);
  const envVals = readEnv(root);
  const valSym = (e) => (e.parent && ts.isShorthandPropertyAssignment(e.parent) && e.parent.name === e
    ? checker.getShorthandAssignmentValueSymbol(e.parent) : symOf(e));
  function constInit(sym) {
    for (const d of (sym && sym.declarations) || []) {
      if (ts.isVariableDeclaration(d) && d.initializer && ts.isIdentifier(d.name) && d.parent
        && ts.isVariableDeclarationList(d.parent) && (d.parent.flags & ts.NodeFlags.Const)) return d.initializer;
    }
    return null;
  }
  const paramDecl = (sym) => {
    const d = sym && sym.declarations && sym.declarations[0];
    return d && ts.isParameter(d) && ts.isIdentifier(d.name) ? d : null;
  };
  // an expression read as an object literal: {o, sub}, {none} when it is known to be absent, or null
  function objectOf(e, sub, depth = 0) {
    e = skipOuter(e);
    if (!e || depth > 8) return null;
    if (ts.isObjectLiteralExpression(e)) return { o: e, sub };
    if (ts.isIdentifier(e)) {
      if (e.text === 'undefined') return { none: true };
      const sym = valSym(e);
      if (!sym) return null;
      if (sub && sub.has(sym)) { const x = sub.get(sym); return x ? objectOf(x.e, x.sub, depth + 1) : { none: true }; }
      const init = constInit(sym);
      return init ? objectOf(init, null, depth + 1) : null;
    }
    if (ts.isPropertyAccessExpression(e)) {
      const p = propOf(e.expression, e.name.text, sub, depth + 1);
      return p && p.e ? objectOf(p.e, p.sub, depth + 1) : p && p.none ? { none: true } : null;
    }
    return null;
  }
  // property `key` of an object expression: {e, sub}, {none} when it has no such key, or {unknown}
  function propOf(objE, key, sub, depth = 0) {
    const o = objectOf(objE, sub, depth);
    if (!o) return { unknown: true };
    if (o.none) return { none: true };
    let found = { none: true };
    for (const p of o.o.properties) {
      if (ts.isSpreadAssignment(p)) {
        // a spread we can read wins over what came before it; one we can't only fills a gap
        const inner = propOf(p.expression, key, o.sub, depth + 1);
        if (inner.e || (inner.unknown && found.none)) found = inner;
        continue;
      }
      const k = p.name && (p.name.text != null ? p.name.text : p.name.getText());
      if (k !== key) continue;
      if (ts.isPropertyAssignment(p)) found = { e: p.initializer, sub: o.sub };
      else if (ts.isShorthandPropertyAssignment(p)) found = { e: p.name, sub: o.sub };
      else found = { unknown: true };
    }
    return found;
  }
  // the keys an object will have: a list, or null when a spread or a variable hides them
  function keysOf(e, sub, depth = 0) {
    const o = objectOf(e, sub, depth);
    if (!o) return null;
    if (o.none) return [];
    const out = [];
    for (const p of o.o.properties) {
      if (ts.isSpreadAssignment(p)) { const k = keysOf(p.expression, o.sub, depth + 1); if (!k) return null; out.push(...k); continue; }
      if (!p.name || ts.isComputedPropertyName(p.name)) return null;
      out.push(p.name.text != null ? p.name.text : p.name.getText());
    }
    return out;
  }
  function evalStr(e, sub, depth = 0) {
    e = skipOuter(e);
    if (!e || depth > 12) return [{ t: 'dyn', s: e ? e.getText() : '' }];
    if (ts.isStringLiteral(e) || ts.isNoSubstitutionTemplateLiteral(e)) return [{ t: 'lit', v: e.text }];
    if (ts.isTemplateExpression(e)) {
      let out = [{ t: 'lit', v: e.head.text }];
      for (const s of e.templateSpans) out = out.concat(evalStr(s.expression, sub, depth + 1), [{ t: 'lit', v: s.literal.text }]);
      return out;
    }
    if (ts.isBinaryExpression(e)) {
      const k = e.operatorToken.kind;
      if (k === ts.SyntaxKind.PlusToken) return evalStr(e.left, sub, depth + 1).concat(evalStr(e.right, sub, depth + 1));
      // `import.meta.env.VITE_API_URL ?? ''`: the left side is the one that's meant
      if (k === ts.SyntaxKind.QuestionQuestionToken || k === ts.SyntaxKind.BarBarToken) return evalStr(e.left, sub, depth + 1);
    }
    if (ts.isIdentifier(e)) {
      const sym = valSym(e);
      if (sym && sub && sub.has(sym)) { const x = sub.get(sym); return x ? evalStr(x.e, x.sub, depth + 1) : [{ t: 'dyn', s: e.text }]; }
      const p = paramDecl(sym);
      if (p) return [{ t: 'param', fn: p.parent, s: e.text, e }];
      const init = constInit(sym);
      if (init) return evalStr(init, null, depth + 1);
      return [{ t: 'dyn', s: e.text }];
    }
    if (ts.isPropertyAccessExpression(e)) {
      const env = /^(?:import\.meta\.env|process\.env)\.(\w+)$/.exec(e.getText());
      if (env) return envVals.has(env[1]) ? [{ t: 'lit', v: envVals.get(env[1]) }] : [{ t: 'env', s: env[1] }];
      const p = propOf(e.expression, e.name.text, sub, depth + 1);
      if (p.e) return evalStr(p.e, p.sub, depth + 1);
    }
    return [{ t: 'dyn', s: e.getText() }];
  }
  // the function a callee stands for: `api`, `ItemsService.readItems`, `http.get`
  function fnOfCallee(e, sub) {
    e = skipOuter(e);
    let sym = null;
    if (ts.isIdentifier(e)) {
      sym = valSym(e);
      if (sym && sub && sub.has(sym)) { const x = sub.get(sym); return x ? fnOfCallee(x.e, x.sub) : null; }
    } else if (ts.isPropertyAccessExpression(e)) sym = symOf(e.name);
    for (const d of (sym && sym.declarations) || []) {
      if (ts.isFunctionDeclaration(d) || ts.isMethodDeclaration(d)) return d.body ? d : null;
      const init = (ts.isVariableDeclaration(d) || ts.isPropertyAssignment(d) || ts.isPropertyDeclaration(d)) && d.initializer ? skipOuter(d.initializer) : null;
      if (init && (ts.isArrowFunction(init) || ts.isFunctionExpression(init))) return init;
    }
    return null;
  }
  // `axios.create({ baseURL })` behind a name: the base every call through it starts with
  function baseOf(e, sub) {
    e = skipOuter(e);
    if (ts.isBinaryExpression(e)) return baseOf(e.right, sub) || baseOf(e.left, sub);   // (options.client ?? client)
    if (!ts.isIdentifier(e)) return null;
    const init = constInit(valSym(e));
    const c = init && skipOuter(init);
    if (c && ts.isCallExpression(c) && calleeName(c) === 'create' && c.arguments[0]) {
      const b = propOf(c.arguments[0], 'baseURL', null);
      if (b.e) return evalStr(b.e, b.sub);
    }
    return null;
  }
  const CLIENTISH = /(^|[^\w$])(axios|ky|api|\$api|client|http|https|request|fetcher|instance|service)\w*$/i;
  const urlish = (parts) => {
    const p = parts[0];
    if (!p) return false;
    if (p.t === 'lit') return /^(\/|https?:\/\/)/.test(p.v) || (p.v === '' && parts.length > 1 && urlish(parts.slice(1)));
    return p.t === 'env' || p.t === 'param' || (p.t === 'dyn' && parts.length > 1 && parts[1].t === 'lit' && parts[1].v.startsWith('/'));
  };
  // the request a call makes by itself (fetch, axios, a client's .get, useSWR, request({ method, url }))
  function directReq(c, sub) {
    const callee = skipOuter(c.expression);
    const args = c.arguments;
    const lower = (s) => (s || '').toLowerCase();
    const mk = (m, urlE, cfg, extra) => Object.assign({ m, url: urlE ? { e: urlE, sub } : null, cfg: cfg ? { e: cfg, sub } : null }, extra || {});
    const name = calleeName(c);
    if (ts.isIdentifier(callee) && (name === 'fetch' || name === '$fetch' || name === 'ofetch') && args[0]) {
      return mk(args[1] ? { from: args[1], key: 'method' } : 'GET', args[0], args[1], { fetch: true, sure: true });
    }
    if (ts.isPropertyAccessExpression(callee) && name === 'fetch' && /^(window|globalThis|self)$/.test(skipOuter(callee.expression).getText()) && args[0]) {
      return mk(args[1] ? { from: args[1], key: 'method' } : 'GET', args[0], args[1], { fetch: true, sure: true });
    }
    if (ts.isIdentifier(callee) && (name === 'useSWR' || name === 'useSWRImmutable') && args[0]) return mk('GET', args[0], null);
    // openapi-react-query: $api.useQuery('get', '/users/{id}', { params })
    if (/^(useQuery|useSuspenseQuery|useMutation|queryOptions|useInfiniteQuery)$/.test(name) && ts.isPropertyAccessExpression(callee)
      && args[0] && args[1] && ts.isStringLiteral(skipOuter(args[0])) && METHODS.has(lower(skipOuter(args[0]).text))) {
      return mk(skipOuter(args[0]).text.toUpperCase(), args[1], args[2], { params: true });
    }
    if (ts.isIdentifier(callee) && name === 'axios' && args[0]) {
      const o = objectOf(args[0], sub);
      if (o && o.o) return mk({ from: args[0], key: 'method', dflt: 'GET' }, null, args[0], { urlKey: true, sure: true });
      return mk({ from: args[1], key: 'method', dflt: 'GET' }, args[0], args[1], { sure: true });
    }
    if (ts.isPropertyAccessExpression(callee) && (METHODS.has(lower(name)) || name === 'request') && args[0]) {
      const baseE = skipOuter(callee.expression);
      const clientish = (ts.isIdentifier(baseE) && baseE.text === 'axios') || CLIENTISH.test(baseE.getText()) || baseOf(baseE, sub);
      const base = baseOf(baseE, sub);
      const o = objectOf(args[0], sub);
      if (o && o.o) {   // client.get({ url: '/items', query }) (hey-api), client.request({ method, url })
        const u = propOf(args[0], 'url', sub);
        if (!u.e) return null;
        const m = name === 'request' ? { from: args[0], key: 'method', dflt: 'GET' } : name.toUpperCase();
        return mk(m, null, args[0], { urlKey: true, base });
      }
      if (name === 'request') return null;
      // a server's route, not a request: app.get('/items', (req, res) => …)
      if (args.slice(1).some((a) => isFnLike(skipOuter(a)))) return null;
      const parts = evalStr(args[0], sub);
      if (!clientish && !(parts[0] && parts[0].t === 'lit' && /^(\/|https?:\/\/)/.test(parts[0].v))) return null;
      const upper = name === name.toUpperCase();   // openapi-fetch: client.GET('/items/{id}', { params })
      return mk(name.toUpperCase(), args[0], upper ? args[1] : BODY_ARG.has(lower(name)) ? args[2] : args[1], { base, params: upper });
    }
    // request(OpenAPI, { method: 'POST', url: '/api/v1/items/' }) and the like
    for (const a of args) {
      const o = objectOf(a, sub);
      if (!o || !o.o) continue;
      const mp = propOf(a, 'method', sub), up = propOf(a, 'url', sub);
      if (mp.e && up.e) return mk({ from: a, key: 'method' }, null, a, { urlKey: true });
    }
    return null;
  }
  const wrappers = new Map();    // function → the call in it whose URL (or method) comes from its parameters
  const clientFns = new Map();   // module-level function → the one request it makes
  const outerCalls = new Map();  // client function → how many calls to it were found
  // a parameter that fills one value (`/orders/${id}`, `?page=${page}`) is data, not the URL's shape:
  // the function around it is not a wrapper, the request line itself is the one to show
  function valueParams(parts) {
    return parts.map((p, i) => {
      if (p.t !== 'param') return p;
      const before = parts[i - 1], after = parts[i + 1];
      const opens = before && before.t === 'lit' && /[/=]$/.test(before.v);
      const closes = !after || (after.t === 'lit' && (/^[/?&#]/.test(after.v) || after.v === ''));
      return opens && closes ? { t: 'dyn', s: p.s } : p;
    });
  }
  const hasParam = (parts) => parts.some((p) => p.t === 'param');
  // everything a call sends: {m, parts, base, q, via}, or null when it makes no request we can read
  function reqOf(c, sub, depth = 0) {
    if (depth > 6) return null;
    const d = directReq(c, sub);
    if (d) {
      let urlE = d.url;
      if (d.urlKey) { const u = propOf(d.cfg.e, 'url', d.cfg.sub); urlE = u.e ? u : null; }
      if (!urlE) return null;
      const parts = valueParams(evalStr(urlE.e, urlE.sub));
      let m = d.m;
      if (typeof m !== 'string') {
        const mp = m.from ? propOf(m.from, m.key, sub) : { none: true };
        if (mp.e) { const mv = evalStr(mp.e, mp.sub); m = mv.length === 1 && mv[0].t === 'lit' ? mv[0].v.toUpperCase() : mv.some((x) => x.t === 'param') ? mv : '?'; }
        else m = mp.none ? (m.dflt || 'GET') : '?';
      }
      // query keys: `?a=1&b=` in the URL, axios `params`, a client's `query`, openapi-fetch `params.query`
      let q = [];
      const cfg = d.cfg;
      if (cfg) {
        let qe = d.params ? propOf(cfg.e, 'params', cfg.sub) : null;
        if (qe && qe.e) qe = propOf(qe.e, 'query', qe.sub);
        else if (!d.params) { qe = propOf(cfg.e, 'params', cfg.sub); if (qe.none) qe = propOf(cfg.e, 'query', cfg.sub); }
        if (d.fetch) qe = null;
        if (qe && qe.e) q = keysOf(qe.e, qe.sub);
        else if (qe && qe.unknown) q = null;
      }
      return { m, parts, base: d.base || null, q, sure: !!d.sure, src: urlE.e };
    }
    const fn = fnOfCallee(c.expression, sub);
    const inner = fn && (wrappers.get(fn) || clientFns.get(fn));
    if (!inner) return null;
    const sub2 = new Map();
    fn.parameters.forEach((p, i) => { if (ts.isIdentifier(p.name)) { const s = symOf(p.name); if (s) sub2.set(s, c.arguments[i] ? { e: c.arguments[i], sub } : null); } });
    const r = reqOf(inner, sub2, depth + 1);
    return r && Object.assign({}, r, { via: fn, inner: r.inner || inner });
  }
  // a function with no function around it (a class or an object literal is fine): `export const api = {...}`
  // and one with a name to call it by: a function, a class method, `const f = () =>`, `const api = { f: () => }`
  const moduleLevel = (fn) => {
    for (let p = fn.parent; p; p = p.parent) if (isFnLike(p)) return false;
    if (ts.isFunctionDeclaration(fn) || ts.isMethodDeclaration(fn)) return true;
    const up = upOuter(fn).parent;
    if (up && ts.isVariableDeclaration(up)) return true;
    return !!(up && ts.isPropertyAssignment(up) && upOuter(up.parent).parent && ts.isVariableDeclaration(upOuter(up.parent).parent));
  };
  // parts → a path to match against routes: '/api/v1/items/{}', with what was left out
  function toPath(r) {
    let parts = r.parts;
    if (r.base && !(parts[0] && parts[0].t === 'lit' && /^https?:\/\//.test(parts[0].v))) {
      const b = r.base.slice(), last = b[b.length - 1];
      if (last && last.t === 'lit' && last.v.endsWith('/') && parts[0] && parts[0].t === 'lit' && parts[0].v.startsWith('/')) b[b.length - 1] = { t: 'lit', v: last.v.slice(0, -1) };
      else if (parts[0] && parts[0].t === 'lit' && !parts[0].v.startsWith('/') && !(last && last.t === 'lit' && last.v.endsWith('/'))) b.push({ t: 'lit', v: '/' });
      parts = b.concat(parts);
    }
    // one string, with \u0001 for each value known only at run time
    let s = '', shown = '', q = r.q, qdyn = false;
    for (const p of parts) {
      if (p.t === 'lit') { s += p.v; shown += p.v; }
      else { s += '\u0001'; shown += p.t === 'env' ? '\u0002' : '${' + (p.s || '…') + '}'; if (s.includes('?')) qdyn = true; }
    }
    const qi = s.indexOf('?');
    if (qi >= 0) {
      const qs = s.slice(qi + 1);
      s = s.slice(0, qi);
      const keys = [...qs.matchAll(/(?:^|&)([^=&\u0001]+)=/g)].map((m) => decodeURIComponent(m[1]));
      // `?${params}` hides the keys; `?page=${page}` doesn't
      const hidden = /(^|&)\u0001/.test(qs);
      q = hidden || q == null ? null : [...new Set(q.concat(keys))];
    } else if (qdyn) q = null;
    const hi = s.indexOf('#'); if (hi >= 0) s = s.slice(0, hi);
    let host = '', lead = false;
    const abs = /^(?:https?:)?\/\/([^/\u0001]*)/.exec(s);
    if (abs) { host = abs[1]; s = s.slice(abs[0].length) || '/'; }
    else if (s[0] === '\u0001') {   // `${API_URL}/items`: the value in front is the server
      lead = true;
      s = s.replace(/^\u0001+/, '');
    }
    if (s && s[0] !== '/') s = '/' + s;
    const unres = !s.replace(/[/\u0001]/g, '').length && s !== '/';
    const segs = s.split('/').map((g) => (g.includes('\u0001') || /^\{[^}]*\}$/.test(g) ? '{}' : g));
    let shownPath = shown.replace(/^[^/]*?\/\/[^/]*/, '').replace(/^\u0002+/, '');
    const qa = shownPath.indexOf('?'); if (qa > 0) shownPath = shownPath.slice(0, qa);
    return { p: segs.join('/'), host, lead, q, unres, shown: shownPath.replace(/\u0002/g, '') };
  }
  const reqAt = new Map();   // call → the request it makes (a wrapper's own inner call included)
  {
    const paramOfReq = (r) => r.parts.concat(Array.isArray(r.m) ? r.m : []).find((p) => p.t === 'param');
    for (let round = 0; round < 5; round++) {
      let grew = false;
      for (const c of calls) {
        const r = reqOf(c, null);
        if (!r) continue;
        if (!reqAt.has(c)) grew = true;
        reqAt.set(c, r);
        // a URL or method from a parameter makes the function around the call a wrapper
        const pf = paramOfReq(r);
        if (pf && pf.fn && pf.fn !== r.via && !wrappers.has(pf.fn)) { wrappers.set(pf.fn, c); grew = true; }
      }
      // a module-level function (not a component or hook) that makes exactly one request
      const perFn = new Map();
      for (const [c, r] of reqAt) {
        if (paramOfReq(r)) continue;
        const fn = enclosingFn(c);
        if (!fn || !fn.body || !moduleLevel(fn) || wrappers.has(fn)) continue;
        const u = unitOf(c);
        if (u && u.kind !== 'fn') continue;
        perFn.set(fn, perFn.has(fn) ? null : c);
      }
      for (const [fn, c] of perFn) if (c && !clientFns.has(fn)) { clientFns.set(fn, c); grew = true; }
      if (!grew) break;
    }
    for (const r of reqAt.values()) if (r.via) outerCalls.set(r.via, (outerCalls.get(r.via) || 0) + 1);
  }
  const proxy = readProxy(root);
  const httpOut = {};
  // the request a call makes, as the page shows it: {m, u, id}; null for a wrapper's own inner call
  function httpCall(c) {
    if (httpOf.has(c)) return httpOf.get(c);
    let out = null;
    const r = reqAt.get(c);
    if (r && !hasParam(r.parts) && !Array.isArray(r.m) && (r.sure || urlish(r.parts))) {
      const P2 = toPath(r);
      const id = P + 'h' + Object.keys(httpOut).length;
      const shown = (r.via ? P2.shown : urlText(r.src)) || P2.shown;
      out = { m: r.m, u: shown.length > 60 ? shown.slice(0, 59) + '…' : shown, id, via: r.via || null };
      const fn = enclosingFn(c);
      const alt = P2.lead || P2.host ? null : proxyPath(P2.p, proxy);
      httpOut[id] = Object.assign({ m: out.m, p: P2.p }, alt && alt !== P2.p ? { alt } : {}, P2.lead ? { lead: 1 } : {},
        P2.host ? { host: P2.host } : {}, P2.q ? { q: P2.q } : { qx: 1 }, P2.unres || !urlish(r.parts) ? { unres: 1 } : {},
        clientFns.get(fn) === c && outerCalls.get(fn) ? { inner: 1 } : {});
    }
    httpOf.set(c, out);
    return out;
  }
  // a client function that only makes its request: an action's path can stop at the call to it
  function thinClient(fn) {
    const req = clientFns.get(fn) || wrappers.get(fn);
    let other = false;
    const v = (n) => {
      if (other || n === req) return;
      if (ts.isCallExpression(n) && !/^(json|text|blob|then|catch|finally|stringify|parse|toString|get|set|append)$/.test(calleeName(n))) { other = true; return; }
      ts.forEachChild(n, v);
    };
    if (fn.body) v(fn.body);
    return !other;
  }
  // is a function named anywhere (passed as `queryFn: ItemsService.readItems`, say)
  function referenced(fn) {
    const decl = ts.isMethodDeclaration(fn) || ts.isFunctionDeclaration(fn) ? fn : upOuter(fn).parent;
    const nm = decl && decl.name;
    const sym = nm && symOf(nm);
    if (!sym) return false;
    if ((refs.get(sym) || []).length) return true;
    const txt = nm.getText();
    for (const list of idsByFile.values()) for (const x of list) if (x.pa && x.node.text === txt && symOf(x.node) === sym) return true;
    return false;
  }
  function urlText(e) {
    e = skipOuter(e);
    if (!e) return '';
    let t;
    if (ts.isStringLiteral(e) || ts.isNoSubstitutionTemplateLiteral(e)) t = e.text;
    else if (ts.isTemplateExpression(e)) {
      t = e.head.text + e.templateSpans.map((s) => '${' + s.expression.getText() + '}' + s.literal.text).join('');
    } else if (ts.isObjectLiteralExpression(e)) {
      const u = propOf(e, 'url', null);
      return u.e ? urlText(u.e) : '';
    } else t = e.getText();
    t = t.replace(/\s+/g, ' ');
    const q = t.indexOf('?');
    if (q > 0) t = t.slice(0, q);
    return t;
  }
  function noteApi(c) {
    const h = httpCall(c);
    if (!h) return null;
    const r = rowFor(c);
    if (r && !r.api.some((x) => x.id === h.id)) { r.api.push(h); httpOut[h.id].row = r.id; }
    return r;
  }
  // every request gets its row, not only the ones an action reaches, so a route can say who calls it
  for (const c of reqAt.keys()) {
    if (!httpCall(c)) continue;
    const fn = enclosingFn(c);
    if (clientFns.get(fn) === c && (outerCalls.get(fn) || !referenced(fn))) continue;   // its callers stand for it
    if (!unitOf(c) && fn) ensureUnit(fn);
    noteApi(c);
  }
  function readEnv(dir) {
    const out = new Map();
    for (const f of ['.env', '.env.development', '.env.local', '.env.development.local']) {
      let text;
      try { text = fs.readFileSync(path.join(dir, f), 'utf8'); } catch (e) { continue; }
      for (const line of text.split(/\r?\n/)) {
        const m = /^\s*(?:export\s+)?([A-Za-z_]\w*)\s*=\s*(.*?)\s*$/.exec(line);
        if (m) out.set(m[1], m[2].replace(/^(['"])(.*)\1$/, '$2'));
      }
    }
    return out;
  }
  // dev-server rewrites between the page and the API: Vite's server.proxy, Next's rewrites()
  function readProxy(dir) {
    const rules = [];
    let names = [];
    try { names = fs.readdirSync(dir); } catch (e) { return rules; }
    for (const f of names) {
      if (!/^(vite|next)\.config\.[cm]?[jt]s$/.test(f)) continue;
      let sf;
      try { sf = ts.createSourceFile(f, fs.readFileSync(path.join(dir, f), 'utf8'), ts.ScriptTarget.Latest, true); } catch (e) { continue; }
      const str = (e) => { e = e && skipOuter(e); return e && (ts.isStringLiteral(e) || ts.isNoSubstitutionTemplateLiteral(e)) ? e.text : null; };
      const keyOf = (p) => p.name && (p.name.text != null ? p.name.text : p.name.getText());
      const v = (n) => {
        if (ts.isPropertyAssignment(n) && keyOf(n) === 'proxy' && ts.isObjectLiteralExpression(skipOuter(n.initializer))) {
          for (const p of skipOuter(n.initializer).properties) {
            if (!ts.isPropertyAssignment(p)) continue;
            const from = keyOf(p).replace(/^\^/, '');
            if (!from.startsWith('/')) continue;
            let to = from;
            const val = skipOuter(p.initializer);
            const rw = ts.isObjectLiteralExpression(val) && val.properties.find((q) => keyOf(q) === 'rewrite');
            if (rw) {
              // rewrite: (path) => path.replace(/^\/api/, '')
              const m = /replace\(\s*\/\^?((?:\\\/|[^/])*)\/[a-z]*\s*,\s*(['"`])(.*?)\2\s*\)/.exec(rw.getText());
              to = m && m[1].replace(/\\\//g, '/') === from ? m[3] : null;
            }
            if (to != null) rules.push([from, to]);
          }
        }
        if (ts.isObjectLiteralExpression(n)) {   // { source: '/api/:path*', destination: 'http://localhost:8000/:path*' }
          const sp = n.properties.find((q) => keyOf(q) === 'source'), dp = n.properties.find((q) => keyOf(q) === 'destination');
          const sv = sp && ts.isPropertyAssignment(sp) && str(sp.initializer), dv = dp && ts.isPropertyAssignment(dp) && str(dp.initializer);
          if (sv && dv && sv.startsWith('/')) rules.push([sv.split(/[:(]/)[0], dv.replace(/^[a-z]+:\/\/[^/]*/i, '').split(/[:(]/)[0] || '/']);
        }
        ts.forEachChild(n, v);
      };
      v(sf);
    }
    return rules;
  }
  function proxyPath(p, rules) {
    for (const [from, to] of rules) {
      const f = from.replace(/\/$/, '');
      if (f && (p === f || p.startsWith(f + '/'))) return (to.replace(/\/$/, '') + p.slice(f.length)) || '/';
    }
    return null;
  }

  // ════════════════════════════════════════════════════════════════════════════════════════
  // Feature 1: every line that sets a state, following the setter under every name it takes
  // ════════════════════════════════════════════════════════════════════════════════════════
  // A carrier is a name that, when called, sets the state: the setter, a prop it was passed as,
  // a wrapper around it, a key of an object it travels in (props.onApply, a context value).
  function propCarrier(unit, key, unc) {
    const P2 = unit.props;
    if (!P2) return null;
    if (P2.bind.has(key)) return { sym: P2.bind.get(key), key: null, unc };
    if (P2.rest) return { sym: P2.rest, key, unc };
    if (P2.ident) return { sym: P2.ident, key, unc };
    return null;
  }
  const bindingFor = (pattern, key) => {
    if (ts.isObjectBindingPattern(pattern)) {
      for (const el of pattern.elements) {
        if (el.dotDotDotToken || !ts.isIdentifier(el.name)) continue;
        const k = el.propertyName ? (el.propertyName.text || el.propertyName.getText()) : el.name.text;
        if (k === key) return el.name;
      }
    } else if (ts.isArrayBindingPattern(pattern) && key && key[0] === '#') {
      const el = pattern.elements[+key.slice(1)];
      if (el && ts.isBindingElement(el) && ts.isIdentifier(el.name)) return el.name;
    }
    return null;
  };

  function setterFlow(S) {
    const out = { sites: new Map(), hops: new Set(), sigs: new Set(), syms: new Set(), keyed: [] };
    if (!S.setterSym) return out;
    const queue = [{ sym: S.setterSym, key: null, unc: false }];
    const seen = new Map();   // symbol → keys already followed
    const site = (row, unc) => { if (!row) return; const prev = out.sites.get(row); out.sites.set(row, prev === undefined ? unc : prev && unc); };
    const hop = (row) => { if (row) out.hops.add(row); };
    const push = (c) => { if (c && c.sym) queue.push(c); };
    const pushProp = (unit, key, unc) => { out.sigs.add(unit); push(propCarrier(unit, key, unc)); };

    function flowObject(objExpr, key, unc, depth = 0) {
      if (depth > 6) return false;
      const e = upOuter(objExpr), p = e.parent;
      if (!p) return false;
      if (ts.isVariableDeclaration(p) && p.initializer === e) {
        if (ts.isIdentifier(p.name)) { hop(rowFor(p)); push({ sym: symOf(p.name), key, unc }); return true; }
        const b = bindingFor(p.name, key);
        if (b) { hop(rowFor(p)); push({ sym: symOf(b), key: null, unc }); return true; }
        return false;
      }
      if (ts.isJsxExpression(p) && p.parent && ts.isJsxAttribute(p.parent)) {
        const op = p.parent.parent.parent, t = resolveTag(op);
        if (t.provider && attrName(p.parent) === 'value' && t.ctx) {
          hop(rowFor(op));
          let any = false;
          for (const c of ctxCalls.get(t.ctx) || []) any = flowObject(c, key, true, depth + 1) || any;
          return any;
        }
        return false;
      }
      if (ts.isReturnStatement(p) || (ts.isArrowFunction(p) && p.body === e)) {
        const fn = ts.isReturnStatement(p) ? enclosingFn(p) : p;
        const h = fn && unitByFn.get(fn);
        if (h && h.kind === 'hook') {
          hop(rowFor(e));
          let any = false;
          for (const c of hookCallSites.get(h) || []) any = flowObject(c, key, unc, depth + 1) || any;
          return any;
        }
        const mp = fn && upOuter(fn).parent;
        if (mp && ts.isCallExpression(mp) && calleeName(mp) === 'useMemo') return flowObject(mp, key, unc, depth + 1);
      }
      return false;
    }

    // a call of the carrier (or the carrier handed over as a callback): a place that sets the
    // state, unless it sits inside a named function or a prop — then that name carries it on
    function invoke(e, c) {
      const R = rowFor(e);
      const home = unitOf(e);
      let f = enclosingFn(e);
      while (f && (!home || f !== home.fn)) {
        const fo = upOuter(f), fp = fo.parent;
        if (ts.isFunctionDeclaration(f) && f.name) { hop(R); hop(rowFor(f)); push({ sym: symOf(f.name), key: null, unc: c.unc, pending: R }); return; }
        if (fp && ts.isVariableDeclaration(fp) && fp.initializer === fo && ts.isIdentifier(fp.name)) {
          hop(R); hop(rowFor(fp)); push({ sym: symOf(fp.name), key: null, unc: c.unc, pending: R }); return;
        }
        if (fp && ts.isCallExpression(fp) && fp.arguments.includes(fo)) {
          const cn = calleeName(fp);
          if (cn === 'useCallback' || cn === 'useMemo') {
            const vd = upOuter(fp).parent;
            if (vd && ts.isVariableDeclaration(vd) && ts.isIdentifier(vd.name)) { hop(R); hop(rowFor(vd)); push({ sym: symOf(vd.name), key: null, unc: c.unc, pending: R }); return; }
            break;
          }
          if (EFFECTS.has(cn)) break;
          f = enclosingFn(fp);   // a callback (then, setTimeout, map): look further out
          continue;
        }
        if (fp && ts.isJsxExpression(fp) && fp.parent && ts.isJsxAttribute(fp.parent)) {
          const op = fp.parent.parent.parent, t = resolveTag(op);
          if (t.unit) { hop(rowFor(op)); pushProp(t.unit, attrName(fp.parent), c.unc); return; }
          break;   // an event on a DOM element: this is where it gets set
        }
        if (fp && (ts.isPropertyAssignment(fp) || ts.isObjectLiteralExpression(fp) || ts.isMethodDeclaration(f))) {
          const pa = ts.isPropertyAssignment(fp) ? fp : f;
          const obj = pa.parent;
          if (obj && ts.isObjectLiteralExpression(obj) && pa.name) { hop(R); if (flowObject(obj, pa.name.getText(), c.unc)) return; }
          break;
        }
        break;
      }
      site(R, c.unc);
    }

    function classify(ref, c) {
      const e = upOuter(ref), p = e.parent;
      if (!p) return false;
      if (ts.isCallExpression(p) && p.expression === e) { invoke(e, c); return true; }
      if (ts.isCallExpression(p) && p.arguments.includes(e)) {
        const cn = calleeName(p);
        if (cn === 'useCallback' || cn === 'useMemo') {
          const vd = upOuter(p).parent;
          if (vd && ts.isVariableDeclaration(vd) && ts.isIdentifier(vd.name)) { hop(rowFor(vd)); push({ sym: symOf(vd.name), key: null, unc: c.unc }); return true; }
          return false;
        }
        invoke(e, c); return true;   // handed over as a callback: .then(setTotal)
      }
      if (ts.isJsxExpression(p) && p.parent && ts.isJsxAttribute(p.parent)) {
        const op = p.parent.parent.parent, t = resolveTag(op);
        if (t.unit) { hop(rowFor(op)); pushProp(t.unit, attrName(p.parent), c.unc); return true; }
        if (t.provider) {
          hop(rowFor(op));
          for (const call of ctxCalls.get(t.ctx) || []) {
            const vd = upOuter(call).parent;
            if (vd && ts.isVariableDeclaration(vd) && ts.isIdentifier(vd.name)) push({ sym: symOf(vd.name), key: null, unc: true });
          }
          return true;
        }
        site(rowFor(op), c.unc); return true;   // onClick={handleClick}
      }
      if (ts.isShorthandPropertyAssignment(p) || (ts.isPropertyAssignment(p) && p.initializer === e)) {
        return flowObject(p.parent, p.name.getText(), c.unc);
      }
      if (ts.isArrayLiteralExpression(p)) {
        const gp = upOuter(p).parent;
        if (gp && ts.isCallExpression(gp) && gp.arguments[1] === upOuter(p)) return false;   // a deps array
        return flowObject(p, '#' + p.elements.indexOf(e), c.unc);
      }
      if (ts.isVariableDeclaration(p) && p.initializer === e) {
        if (ts.isIdentifier(p.name)) { hop(rowFor(p)); push({ sym: symOf(p.name), key: null, unc: c.unc }); return true; }
        return false;
      }
      return false;
    }

    // uses of a keyed carrier: `props.onApply`, `{...props}`, `const { onApply } = props`, or the whole object moving on
    function classifyKeyed(ref, c) {
      const e = upOuter(ref), p = e.parent;
      if (!p) return false;
      if (ts.isPropertyAccessExpression(p) && p.expression === e) {
        if (p.name.text !== c.key) return false;
        out.keyed.push({ sym: c.sym, key: c.key });
        return classify(p, c);
      }
      if (ts.isJsxSpreadAttribute(p)) {
        const op = p.parent.parent, t = resolveTag(op);
        if (t.unit) { const r = rowFor(op); hop(r); if (r) r.unc = true; pushProp(t.unit, c.key, true); return true; }
        return false;
      }
      if (ts.isVariableDeclaration(p) && p.initializer === e && !ts.isIdentifier(p.name)) {
        const b = bindingFor(p.name, c.key);
        if (b) { hop(rowFor(p)); push({ sym: symOf(b), key: null, unc: c.unc }); return true; }
        return false;
      }
      return flowObject(e, c.key, c.unc);
    }

    let guard = 0;
    while (queue.length && guard++ < 500) {
      const c = queue.shift();
      if (!seen.has(c.sym)) seen.set(c.sym, new Set());
      if (seen.get(c.sym).has(c.key)) continue;
      seen.get(c.sym).add(c.key);
      if (c.key == null) out.syms.add(c.sym);
      let used = 0;
      for (const ref of refs.get(c.sym) || []) {
        if (c.key == null ? classify(ref, c) : classifyKeyed(ref, c)) used++;
      }
      // a wrapper nobody calls: its own call to the setter is the place that sets it
      if (!used && c.pending) { out.hops.delete(c.pending); site(c.pending, c.unc); }
    }
    return out;
  }

  // ════════════════════════════════════════════════════════════════════════════════════════
  // Values: where a state's value travels (props, derived consts, context), for reruns + effects
  // ════════════════════════════════════════════════════════════════════════════════════════
  function valueFlow(S) {
    const out = { syms: new Set(), keys: [], passEls: new Set(), readers: [], ctx: new Set() };
    if (!S.valueSym) return out;
    const queue = [{ sym: S.valueSym, key: null, unc: false }];
    const seen = new Map();
    const push = (c) => { if (c && c.sym) queue.push(c); };
    const pushNames = (name, unc) => {
      if (ts.isIdentifier(name)) push({ sym: symOf(name), key: null, unc });
      else if (ts.isObjectBindingPattern(name) || ts.isArrayBindingPattern(name)) {
        for (const el of name.elements) if (ts.isBindingElement(el)) pushNames(el.name, unc);
      }
    };
    const toReaders = (ctx) => {
      if (!ctx || out.ctx.has(ctx)) return;
      out.ctx.add(ctx);
      for (const call of ctxCalls.get(ctx) || []) {
        const u = unitOf(call);
        if (u) out.readers.push({ unit: u, call });
        const vd = upOuter(call).parent;
        if (vd && ts.isVariableDeclaration(vd)) pushNames(vd.name, true);
      }
    };
    function fromHookReturn(h, unc) {
      for (const call of hookCallSites.get(h) || []) {
        const vd = upOuter(call).parent;
        if (vd && ts.isVariableDeclaration(vd)) pushNames(vd.name, unc);
        else {   // the hook's result goes straight into a provider: value={useCart()}
          const jp = upOuter(call).parent;
          if (jp && ts.isJsxExpression(jp) && jp.parent && ts.isJsxAttribute(jp.parent)) {
            const t = resolveTag(jp.parent.parent.parent);
            if (t.provider) toReaders(t.ctx);
          }
        }
      }
    }
    function classify(ref, c) {
      let n = ref;
      if (c.key != null) {
        const e = upOuter(ref), p = e.parent;
        if (p && ts.isPropertyAccessExpression(p) && p.expression === e) {
          if (p.name.text !== c.key) return;
          out.keys.push({ sym: c.sym, key: c.key });
          n = p;
        } else if (p && ts.isJsxSpreadAttribute(p)) {
          const op = p.parent.parent, t = resolveTag(op);
          if (t.unit) { out.passEls.add(op); push(propCarrier(t.unit, c.key, true)); }
          return;
        }
      }
      // climb to what the value feeds: a prop, a derived const, a provider, a hook's result
      for (let x = n; x && x.parent; x = x.parent) {
        const p = x.parent;
        if (ts.isJsxAttribute(p)) {
          const op = p.parent.parent, t = resolveTag(op);
          if (t.unit) { out.passEls.add(op); push(propCarrier(t.unit, attrName(p), c.unc)); }
          else if (t.provider && attrName(p) === 'value') toReaders(t.ctx);
          return;
        }
        if (ts.isJsxSpreadAttribute(p)) {
          const op = p.parent.parent, t = resolveTag(op);
          if (t.unit) out.passEls.add(op);
          return;
        }
        if (ts.isVariableDeclaration(p) && p.initializer === x) { pushNames(p.name, c.unc); return; }
        if (ts.isReturnStatement(p) || (ts.isArrowFunction(p) && p.body === x)) {
          const fn = ts.isReturnStatement(p) ? enclosingFn(p) : p;
          const h = fn && unitByFn.get(fn);
          if (h && h.kind === 'hook') fromHookReturn(h, c.unc);
          if (!ts.isReturnStatement(p)) { x = p; continue; }
          if (fn && !unitByFn.get(fn)) { x = fn; continue; }
          return;
        }
        if (ts.isCallExpression(p) && p.arguments.includes(x)) {
          const callee = skipOuter(p.expression);
          const hr = ts.isIdentifier(callee) ? unitFromSym(symOf(callee)) : null;
          if (hr && hr.unit.kind === 'hook') {   // useFetch(url): the hook's parameter carries it on
            const prm = hr.unit.fn.parameters[p.arguments.indexOf(x)];
            if (prm) pushNames(prm.name, c.unc);
            return;
          }
        }
        if (isFnLike(p)) {
          const pp = upOuter(p).parent;
          if (pp && ts.isCallExpression(pp) && (calleeName(pp) === 'useMemo' || calleeName(pp) === 'useCallback')) { x = p; continue; }
          return;
        }
        if (ts.isBlock(p) || ts.isSourceFile(p)) return;
      }
    }
    let guard = 0;
    while (queue.length && guard++ < 500) {
      const c = queue.shift();
      if (!seen.has(c.sym)) seen.set(c.sym, new Set());
      if (seen.get(c.sym).has(c.key)) continue;
      seen.get(c.sym).add(c.key);
      if (c.key == null) out.syms.add(c.sym);
      for (const ref of refs.get(c.sym) || []) classify(ref, c);
    }
    return out;
  }

  // components that run again when S changes: its owner(s) and everything they render.
  // A memo() child that is handed none of the value is only maybe: marked unc, never claimed.
  function reruns(S, V) {
    const list = [];
    const seen = new Map();
    const add = (unit, row, unc) => {
      const prev = seen.get(unit);
      if (prev) { if (prev.unc && !unc) { prev.unc = false; prev.row = row; } return false; }
      const e = { unit, row, unc };
      seen.set(unit, e); list.push(e);
      return true;
    };
    const q = [];
    if (S.unit.kind === 'component') { add(S.unit, S.row, false); q.push(S.unit); }
    else for (const o of hookOwners(S.unit)) if (add(o.unit, rowFor(o.call), false)) q.push(o.unit);
    for (const r of V.readers) if (r.unit.kind === 'component' && add(r.unit, rowFor(r.call), true)) q.push(r.unit);
    for (let i = 0; i < q.length && list.length < 300; i++) {
      const u = q[i], uu = seen.get(u).unc;
      for (const r of u.renders) {
        const spread = r.op.attributes.properties.some((p) => ts.isJsxSpreadAttribute(p));
        const unc = uu || (r.memo && !V.passEls.has(r.op) && !spread);
        if (add(r.child, rowFor(r.op), unc)) q.push(r.child);
      }
    }
    return list;
  }

  // effects in those components (or their hooks) whose deps name the value
  function effectsOn(S, V, R) {
    const inR = new Set(R.map((e) => e.unit));
    const hooks = [];
    for (const e of R) for (const hc of e.unit.hookCalls) if (!hooks.includes(hc.hook)) hooks.push(hc.hook);
    if (S.unit.kind === 'hook' && !hooks.includes(S.unit)) hooks.unshift(S.unit);
    const units2 = R.map((e) => e.unit).concat(hooks.filter((h) => !inR.has(h)));
    const keyHit = (pa) => {
      const b = skipOuter(pa.expression);
      if (!ts.isIdentifier(b)) return false;
      const s = symOf(b);
      return V.keys.some((k) => k.sym === s && k.key === pa.name.text);
    };
    const out = [];
    for (const u of units2) {
      for (const ef of u.effects) {
        if (!ef.deps || !ef.deps.elements.length) continue;
        let hit = false, unc = false;
        for (const d0 of ef.deps.elements) {
          const d = skipOuter(d0);
          let root = d;
          while (ts.isPropertyAccessExpression(root)) { if (keyHit(root)) { hit = true; break; } root = skipOuter(root.expression); }
          if (hit) break;
          if (ts.isIdentifier(root) && V.syms.has(symOf(root))) { hit = true; break; }
        }
        if (hit) {
          const re = R.find((e) => e.unit === u);
          unc = !!(re && re.unc);
          out.push({ ef, unc });
        }
      }
    }
    return out;
  }

  // ════════════════════════════════════════════════════════════════════════════════════════
  // Feature 2: from a user action to what it runs
  // ════════════════════════════════════════════════════════════════════════════════════════
  // resolve an expression used as a function → [{kind:'setter', S} | {kind:'fn', fn, named}] each
  // with the hops (real rows) it took to get there, child first
  const resolving = new Set();
  function resolveValue(expr, depth = 0) {
    expr = skipOuter(expr);
    if (!expr || depth > 10 || resolving.has(expr)) return [];
    resolving.add(expr);
    try { return resolveInner(expr, depth); } finally { resolving.delete(expr); }
  }
  const withHop = (targets, row, unc) => targets.map((t) => Object.assign({}, t, { hops: [{ row, unc: !!unc }].concat(t.hops) }));
  function resolveInner(expr, depth) {
    if (ts.isArrowFunction(expr) || ts.isFunctionExpression(expr)) return [{ kind: 'fn', fn: expr, named: false, hops: [] }];
    if (ts.isConditionalExpression(expr)) return resolveValue(expr.whenTrue, depth + 1).concat(resolveValue(expr.whenFalse, depth + 1));
    if (ts.isBinaryExpression(expr) && [ts.SyntaxKind.BarBarToken, ts.SyntaxKind.QuestionQuestionToken, ts.SyntaxKind.AmpersandAmpersandToken].includes(expr.operatorToken.kind)) {
      return (expr.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken ? [] : resolveValue(expr.left, depth + 1)).concat(resolveValue(expr.right, depth + 1));
    }
    if (ts.isCallExpression(expr)) {
      const cn = calleeName(expr);
      if ((cn === 'useCallback' || cn === 'useMemo') && expr.arguments[0]) return cn === 'useCallback' ? resolveValue(expr.arguments[0], depth + 1) : [];
      return [];
    }
    if (ts.isPropertyAccessExpression(expr)) {
      const base = skipOuter(expr.expression), key = expr.name.text;
      if (ts.isIdentifier(base)) {
        const bs = symOf(base);
        const po = bs && propOwner.get(bs);
        if (po && !po.key) return viaProp(po.unit, key, depth);
        const via = bs && viaBinding(bs, key, depth);
        if (via) return via;
      }
      return declResolve(symOf(expr), depth);
    }
    if (ts.isIdentifier(expr)) return declResolve(symOf(expr), depth);
    return [];
  }
  // `x` came from `const x = useContext(C)` / `useHook()` / `props`: follow key through it
  // where a name's value was made: `const m = useMutation()` itself, or — for `const { m } = useAuth()` —
  // the declaration inside the hook that `m` is returned from
  function originDecls(sym, depth = 0) {
    if (!sym || depth > 6) return [];
    const out = [];
    for (const d of sym.declarations || []) {
      if (ts.isVariableDeclaration(d)) { out.push(d); continue; }
      if (!ts.isBindingElement(d) || !ts.isObjectBindingPattern(d.parent)) continue;
      const vd = d.parent.parent;
      if (!vd || !ts.isVariableDeclaration(vd) || !vd.initializer) continue;
      const key = d.propertyName ? (d.propertyName.text || d.propertyName.getText()) : (ts.isIdentifier(d.name) ? d.name.text : '');
      const init = skipOuter(vd.initializer);
      const callee = ts.isCallExpression(init) ? skipOuter(init.expression) : null;
      const hr = callee && ts.isIdentifier(callee) ? unitFromSym(symOf(callee)) : null;
      if (!hr || hr.unit.kind !== 'hook') continue;
      for (const r of returnsOf(hr.unit.fn)) {
        const o = skipOuter(r);
        if (!ts.isObjectLiteralExpression(o)) continue;
        for (const p of o.properties) {
          if (!p.name || (p.name.text || p.name.getText()) !== key) continue;
          const s2 = ts.isShorthandPropertyAssignment(p) ? checker.getShorthandAssignmentValueSymbol(p)
            : ts.isPropertyAssignment(p) && ts.isIdentifier(skipOuter(p.initializer)) ? symOf(skipOuter(p.initializer)) : null;
          out.push(...originDecls(s2, depth + 1));
        }
      }
    }
    return out;
  }
  function viaBinding(sym, key, depth) {
    for (const d of originDecls(sym)) {
      if (ts.isVariableDeclaration(d) && d.initializer) {
        const init = skipOuter(d.initializer);
        if (key === 'mutate' || key === 'mutateAsync') { const mc = mutationCall(init); if (mc) return mutationTargets(mc, depth); }
        if (ts.isCallExpression(init)) {
          if (calleeName(init) === 'useContext' && init.arguments[0]) return viaContext(symOf(skipOuter(init.arguments[0])), key, depth);
          const callee = skipOuter(init.expression);
          const hr = ts.isIdentifier(callee) ? unitFromSym(symOf(callee)) : null;
          if (hr && hr.unit.kind === 'hook') return viaHook(hr.unit, key, depth);
        }
        if (ts.isIdentifier(init)) {
          const po = propOwner.get(symOf(init));
          if (po && !po.key) return viaProp(po.unit, key, depth);
        }
      }
    }
    return null;
  }
  function declResolve(sym, depth) {
    if (!sym) return [];
    const S = stateBySetter.get(sym);
    if (S) return [{ kind: 'setter', S, hops: [] }];
    const po = propOwner.get(sym);
    if (po && po.key) return viaProp(po.unit, po.key, depth);
    for (const d of sym.declarations || []) {
      if (ts.isFunctionDeclaration(d) && d.body) return [{ kind: 'fn', fn: d, named: true, def: d, hops: [] }];
      if (ts.isVariableDeclaration(d) && d.initializer) {
        const init = skipOuter(d.initializer);
        if (ts.isArrowFunction(init) || ts.isFunctionExpression(init)) return [{ kind: 'fn', fn: init, named: true, def: d, hops: [] }];
        if (ts.isCallExpression(init) && calleeName(init) === 'useCallback' && init.arguments[0]) {
          const f = skipOuter(init.arguments[0]);
          if (ts.isArrowFunction(f) || ts.isFunctionExpression(f)) return [{ kind: 'fn', fn: f, named: true, def: d, hops: [] }];
          return resolveValue(f, depth + 1);
        }
        if (ts.isIdentifier(init) || ts.isPropertyAccessExpression(init)) return resolveValue(init, depth + 1);
        continue;
      }
      if (ts.isBindingElement(d)) {
        const pat = d.parent, vd = pat && pat.parent;
        const key = ts.isObjectBindingPattern(pat)
          ? (d.propertyName ? (d.propertyName.text || d.propertyName.getText()) : (ts.isIdentifier(d.name) ? d.name.text : ''))
          : '#' + pat.elements.indexOf(d);
        if (vd && ts.isVariableDeclaration(vd) && vd.initializer) {
          const init = skipOuter(vd.initializer);
          if (ts.isCallExpression(init)) {
            if (calleeName(init) === 'useContext' && init.arguments[0]) return viaContext(symOf(skipOuter(init.arguments[0])), key, depth);
            const callee = skipOuter(init.expression);
            const hr = ts.isIdentifier(callee) ? unitFromSym(symOf(callee)) : null;
            if (hr && hr.unit.kind === 'hook') return viaHook(hr.unit, key, depth);
          }
          if (ts.isIdentifier(init)) {
            const po2 = propOwner.get(symOf(init));
            if (po2 && !po2.key) return viaProp(po2.unit, key, depth);
          }
        }
        continue;
      }
      if (ts.isPropertyAssignment(d) && d.initializer) return resolveValue(d.initializer, depth + 1);
      if (ts.isShorthandPropertyAssignment(d)) return declResolve(checker.getShorthandAssignmentValueSymbol(d), depth + 1);
      if (ts.isMethodDeclaration(d) && d.body) return [{ kind: 'fn', fn: d, named: true, def: d, hops: [] }];
    }
    return [];
  }
  // a prop: what every parent passes for it
  function viaProp(unit, key, depth) {
    const out = [];
    for (const s of (renderSites.get(unit) || []).slice(0, 8)) {
      const row = rowFor(s.op);
      let found = false;
      for (const p of s.op.attributes.properties) {
        if (ts.isJsxAttribute(p) && attrName(p) === key) {
          found = true;
          if (p.initializer && ts.isJsxExpression(p.initializer) && p.initializer.expression) out.push(...withHop(resolveValue(p.initializer.expression, depth + 1), row, false));
        }
      }
      if (found) continue;
      for (const p of s.op.attributes.properties) {
        if (!ts.isJsxSpreadAttribute(p)) continue;
        const e = skipOuter(p.expression);
        let inner = [];
        if (ts.isIdentifier(e)) {
          const po = propOwner.get(symOf(e));
          if (po && !po.key) inner = viaProp(po.unit, key, depth + 1);
        } else if (ts.isObjectLiteralExpression(e)) inner = objectKey(e, key, depth + 1);
        if (inner.length) out.push(...withHop(inner, row, true).map((t) => Object.assign(t, { unc: true })));
      }
    }
    return out;
  }
  // `useMutation(...)` itself, or a custom hook that returns one (`useDeleteComment()`)
  function mutationCall(expr, depth = 0) {
    expr = skipOuter(expr);
    if (!expr || depth > 3 || !ts.isCallExpression(expr)) return null;
    if (calleeName(expr) === 'useMutation') return expr;
    const callee = skipOuter(expr.expression);
    const hr = ts.isIdentifier(callee) ? unitFromSym(symOf(callee)) : null;
    if (!hr || hr.unit.kind !== 'hook') return null;
    for (const r of returnsOf(hr.unit.fn)) {
      let e = skipOuter(r);
      if (ts.isIdentifier(e)) {
        const d = ((symOf(e) || {}).declarations || []).find((x) => ts.isVariableDeclaration(x) && x.initializer);
        e = d ? skipOuter(d.initializer) : e;
      }
      const m = mutationCall(e, depth + 1);
      if (m) return m;
    }
    return null;
  }
  // useMutation({ mutationFn, onSuccess }): what `mutate` runs, in order
  function mutationTargets(call, depth) {
    const o = call.arguments[0] && skipOuter(call.arguments[0]);
    if (!o || !ts.isObjectLiteralExpression(o)) return [];
    const out = [];
    for (const key of ['mutationFn', 'onMutate', 'onSuccess', 'onError', 'onSettled']) {
      const p = o.properties.find((q) => q.name && q.name.getText() === key);
      if (!p) continue;
      const row = rowFor(p);
      if (ts.isMethodDeclaration(p)) { out.push({ kind: 'fn', fn: p, named: false, hops: [{ row, unc: false }] }); continue; }
      const init = ts.isPropertyAssignment(p) ? skipOuter(p.initializer) : null;
      if (init && (ts.isArrowFunction(init) || ts.isFunctionExpression(init))) out.push({ kind: 'fn', fn: init, named: false, hops: [{ row, unc: false }] });
      else if (init) out.push(...withHop(resolveValue(init, depth + 1), row, false));
      else if (ts.isShorthandPropertyAssignment(p)) out.push(...withHop(declResolve(checker.getShorthandAssignmentValueSymbol(p), depth + 1), row, false));
    }
    return out;
  }
  // useQuery({ queryFn }) / useQuery(itemsQueryOptions()): the function it fetches with
  function queryFnOf(expr, depth = 0) {
    expr = skipOuter(expr);
    if (!expr || depth > 4) return null;
    if (ts.isObjectLiteralExpression(expr)) return expr.properties.find((q) => q.name && q.name.getText() === 'queryFn') || null;
    if (ts.isCallExpression(expr)) {
      const t = resolveValue(expr.expression).find((x) => x.kind === 'fn');
      if (t) for (const r of returnsOf(t.fn)) { const q = queryFnOf(r, depth + 1); if (q) return q; }
      return null;
    }
    if (ts.isIdentifier(expr)) {
      const sy = symOf(expr);
      for (const d of (sy && sy.declarations) || []) if (ts.isVariableDeclaration(d) && d.initializer) return queryFnOf(d.initializer, depth + 1);
    }
    return null;
  }
  function objectKey(obj, key, depth) {
    for (const p of obj.properties) {
      const k = p.name && (p.name.text || p.name.getText());
      if (k !== key) continue;
      if (ts.isPropertyAssignment(p)) return resolveValue(p.initializer, depth + 1);
      if (ts.isShorthandPropertyAssignment(p)) return declResolve(checker.getShorthandAssignmentValueSymbol(p), depth + 1);
      if (ts.isMethodDeclaration(p)) return [{ kind: 'fn', fn: p, named: true, def: p, hops: [] }];
    }
    return [];
  }
  // the value an expression has, read as an object, at `key`
  function valueAtKey(expr, key, depth) {
    expr = skipOuter(expr);
    if (!expr || depth > 10) return [];
    if (ts.isObjectLiteralExpression(expr)) return objectKey(expr, key, depth);
    if (ts.isArrayLiteralExpression(expr) && key[0] === '#') { const el = expr.elements[+key.slice(1)]; return el ? resolveValue(el, depth + 1) : []; }
    if (ts.isCallExpression(expr)) {
      const cn = calleeName(expr);
      if (cn === 'useMemo' && expr.arguments[0]) {
        const f = skipOuter(expr.arguments[0]);
        if (ts.isArrowFunction(f) && !ts.isBlock(f.body)) return valueAtKey(f.body, key, depth + 1);
        if (isFnLike(f) && f.body) return returnsOf(f).flatMap((r) => valueAtKey(r, key, depth + 1));
        return [];
      }
      if (cn === 'useContext' && expr.arguments[0]) return viaContext(symOf(skipOuter(expr.arguments[0])), key, depth);
      const callee = skipOuter(expr.expression);
      const hr = ts.isIdentifier(callee) ? unitFromSym(symOf(callee)) : null;
      if (hr && hr.unit.kind === 'hook') return viaHook(hr.unit, key, depth);
      return [];
    }
    if (ts.isIdentifier(expr)) {
      const s = symOf(expr);
      for (const d of (s && s.declarations) || []) {
        if (ts.isVariableDeclaration(d) && d.initializer && ts.isIdentifier(d.name)) return valueAtKey(d.initializer, key, depth + 1);
      }
      const po = s && propOwner.get(s);
      if (po && !po.key) return viaProp(po.unit, key, depth + 1);
    }
    return [];
  }
  function returnsOf(fn) {
    if (!fn.body) return [];
    if (!ts.isBlock(fn.body)) return [fn.body];
    const out = [];
    const v = (n) => { if (isFnLike(n)) return; if (ts.isReturnStatement(n) && n.expression) out.push(n.expression); ts.forEachChild(n, v); };
    ts.forEachChild(fn.body, v);
    return out;
  }
  function viaContext(ctx, key, depth) {
    const out = [];
    for (const op of (providers.get(ctx) || []).slice(0, 4)) {
      for (const p of op.attributes.properties) {
        if (!ts.isJsxAttribute(p) || attrName(p) !== 'value' || !p.initializer || !ts.isJsxExpression(p.initializer)) continue;
        out.push(...withHop(valueAtKey(p.initializer.expression, key, depth + 1), rowFor(op), true).map((t) => Object.assign(t, { unc: true })));
      }
    }
    return out;
  }
  function viaHook(h, key, depth) {
    const out = [];
    for (const r of returnsOf(h.fn)) out.push(...withHop(valueAtKey(r, key, depth + 1), rowFor(r), false));
    return out;
  }

  // ── state consequences, cached per state ──
  const flowCache = new Map();
  function flows(S) {
    let f = flowCache.get(S);
    if (!f) {
      const V = valueFlow(S);
      const R = reruns(S, V);
      f = { V, R, E: effectsOn(S, V, R) };
      flowCache.set(S, f);
    }
    return f;
  }

  // ── the walk ──
  function pathFor(entry) {
    const steps = [];
    const rowStep = new Map();
    const statesDone = new Set();
    const fnDone = new Set();
    const addStep = (rowsIn, extra = {}) => {
      const rs = [...new Set(rowsIn.filter(Boolean))];
      const fresh = rs.filter((r) => !rowStep.has(r));
      if (!fresh.length && !extra.rr) {
        // already on the path: carry the highlight onto the step that has it
        const at = rs.length ? rowStep.get(rs[0]) : null;
        if (at != null) { for (const k of ['tk', 'tv']) if (extra[k]) steps[at][k] = [...new Set((steps[at][k] || []).concat(extra[k]))]; }
        return;
      }
      const st = { r: fresh, tk: extra.tk || [], tv: extra.tv || [], rr: extra.rr || [], unc: !!extra.unc };
      fresh.forEach((r) => rowStep.set(r, steps.length));
      steps.push(st);
    };
    const MAXSTEPS = 60;

    function consequences(S, level, unc) {
      if (statesDone.has(S) || steps.length > MAXSTEPS) return;
      statesDone.add(S);
      const { R, E } = flows(S);
      const rr = R.slice(0, 40).map((e) => [e.unit, e.row, e.unc || unc]);
      // light the lines that make the nearest few rerun (the rest still get their chip)
      const childRows = R.slice(0, 9).filter((e) => e.row !== S.row && e.unit !== S.unit).map((e) => e.row);
      const freshChildren = childRows.filter((r) => !rowStep.has(r));
      if (!freshChildren.length) addStep([S.row], { tk: [S], rr, unc });
      else { addStep([S.row], { tk: [S], unc }); addStep(childRows, { rr, tv: [S], unc }); }
      if (level >= 3) return;
      for (const { ef, unc: eu } of E) {
        if (steps.length > MAXSTEPS) return;
        addStep([rowFor(ef.call), rowFor(ef.deps)], { tv: [S], unc: unc || eu });
        walkFn(ef.cb, level + 1, unc || eu);
      }
    }
    function handleTargets(targets, callRow, level, unc) {
      for (const t of targets.slice(0, 4)) {
        const u2 = unc || !!t.unc;
        if (t.kind === 'setter') {
          addStep([callRow], { tk: [t.S], unc: u2 });
          for (const h of t.hops) addStep([h.row], { tk: [t.S], unc: u2 || h.unc });
          consequences(t.S, level, u2);
        } else if (t.kind === 'fn') {
          if (fnDone.has(t.fn)) continue;
          ensureUnit(t.fn);
          addStep([callRow], { unc: u2 });
          for (const h of t.hops) addStep([h.row], { unc: u2 || h.unc });
          if (t.named) {
            const dr = rowFor(t.def || t.fn);
            // a change anywhere in the function's body is a change on this path
            if (dr && !unitByFn.has(t.fn)) dr.e = Math.max(dr.e || dr.b, endLine(t.fn));
            addStep([dr], { unc: u2 });
          }
          walkFn(t.fn, level, u2);
        }
      }
    }
    const CALLBACK_SKIP = new Set(['useCallback', 'useMemo', 'useEffect', 'useLayoutEffect', 'useState', 'useReducer', 'useRef']);
    function onCall(c, level, unc) {
      if (steps.length > MAXSTEPS) return;
      const api = httpCall(c);
      if (api) { addStep([noteApi(c)], { unc }); }
      const cn = calleeName(c);
      if (CALLBACK_SKIP.has(cn)) return;
      // a call to a client function that does more than its request still runs the rest
      const targets = api && (!api.via || thinClient(api.via)) ? [] : resolveValue(c.expression);
      if (targets.length) handleTargets(targets, rowFor(c), level, unc);
      const calleeIsSetter = targets.some((t) => t.kind === 'setter');
      for (const a0 of c.arguments) {
        const a = skipOuter(a0);
        if (isFnLike(a)) { if (!calleeIsSetter) walkFn(a, level, unc); continue; }
        if (calleeIsSetter || targets.length) continue;
        if (ts.isIdentifier(a) || ts.isPropertyAccessExpression(a)) {
          const ts2 = resolveValue(a).filter((t) => t.kind === 'setter' || t.named);
          if (ts2.length) handleTargets(ts2, rowFor(a), level, unc);
        }
      }
    }
    function visitExpr(node, level, unc) {
      const v = (n) => {
        if (isFnLike(n)) return;   // runs later, when something calls it
        ts.forEachChild(n, v);
        if (ts.isCallExpression(n)) onCall(n, level, unc);
      };
      v(node);
    }
    function walkFn(fn, level, unc) {
      if (!fn || fnDone.has(fn) || fnDone.size > 40) return;
      fnDone.add(fn);
      if (fn.body) visitExpr(fn.body, level, unc);
    }
    return { steps, addStep, walkFn, visitExpr, handleTargets, rowStep };
  }

  // ── actions ──
  const actions = [];
  const labelText = (s2) => {
    s2 = s2.replace(/\s+/g, ' ').trim();
    if (s2.length <= 40) return s2;
    const cut = s2.slice(0, 40).replace(/\s+\S*$/, '');
    return (cut.length > 20 ? cut : s2.slice(0, 39)) + '…';
  };
  const attrString = (op, name) => {
    for (const p of op.attributes.properties) {
      if (!ts.isJsxAttribute(p) || attrName(p) !== name || !p.initializer) continue;
      if (ts.isStringLiteral(p.initializer)) return p.initializer.text;
      if (ts.isJsxExpression(p.initializer) && p.initializer.expression) {
        const e = skipOuter(p.initializer.expression);
        if (ts.isStringLiteral(e) || ts.isNoSubstitutionTemplateLiteral(e)) return e.text;
      }
    }
    return '';
  };
  // the words a user sees on an element: its own text, and text in inline tags (<span>, <b>)
  const INLINE = new Set(['span', 'strong', 'b', 'em', 'i', 'small', 'u', 'code', 'kbd', 'abbr', 'mark', 's', 'sup', 'sub']);
  function childText(el, depth = 0) {
    if (!el || !ts.isJsxElement(el) || depth > 2) return '';
    let s2 = '';
    for (const ch of el.children) {
      if (ts.isJsxText(ch)) s2 += ' ' + ch.text;
      else if (ts.isJsxExpression(ch) && ch.expression) {
        const e = skipOuter(ch.expression);
        const str = (x) => { x = skipOuter(x); return x && (ts.isStringLiteral(x) || ts.isNoSubstitutionTemplateLiteral(x)) ? x.text : ''; };
        // {loading ? <Spinner /> : 'Load more'}: the words it shows when idle
        s2 += ' ' + (ts.isConditionalExpression(e) ? (str(e.whenFalse) || str(e.whenTrue)) : str(e));
      }
      else if (ts.isJsxElement(ch) && INLINE.has(ch.openingElement.tagName.getText())) s2 += ' ' + childText(ch, depth + 1);
    }
    return s2.replace(/\s+/g, ' ').trim();
  }
  // a form is named by its submit button
  function submitText(formEl) {
    let typed = null, first = null;
    const v = (n) => {
      if (typed) return;
      if (ts.isJsxOpeningElement(n) || ts.isJsxSelfClosingElement(n)) {
        const tn = n.tagName.getText();
        const txt = (ts.isJsxOpeningElement(n) ? childText(n.parent) : '') || attrString(n, 'aria-label') || attrString(n, 'value');
        if (attrString(n, 'type') === 'submit' && txt) { typed = txt; return; }
        if (!first && /^button$/i.test(tn.split('.').pop()) && txt) first = txt;
      }
      ts.forEachChild(n, v);
    };
    if (formEl) ts.forEachChild(formEl, v);
    return typed || first || '';
  }
  function labelObject(op, event) {
    const el = ts.isJsxOpeningElement(op) ? op.parent : null;
    let t = event === 'onSubmit' ? submitText(el) : '';
    if (t) return t;
    // a submit button outside the form points at it: <Button form="create-comment" type="submit">
    const fid = event === 'onSubmit' && attrString(op, 'id');
    if (fid) {
      const v = (n) => {
        if (t) return;
        if (ts.isJsxOpeningElement(n) && attrString(n, 'form') === fid) t = childText(n.parent) || attrString(n, 'aria-label');
        ts.forEachChild(n, v);
      };
      v(op.getSourceFile());
      if (t) return t;
    }
    t = childText(el);
    if (t) return t;
    t = attrString(op, 'aria-label'); if (t) return t;
    // <label htmlFor="id">, or a <label> wrapped around it
    const id = attrString(op, 'id');
    const u = unitOf(op);
    if (id && u) {
      let found = '';
      const v = (n) => { if (found) return; if (ts.isJsxOpeningElement(n) && /^label$/i.test(n.tagName.getText().split('.').pop()) && attrString(n, 'htmlFor') === id) found = childText(n.parent); ts.forEachChild(n, v); };
      v(u.fn);
      if (found) return found;
    }
    for (let p = (el || op).parent; p && !isFnLike(p); p = p.parent) {
      if (ts.isJsxElement(p) && /^label$/i.test(p.openingElement.tagName.getText().split('.').pop())) { const s2 = childText(p); if (s2) return s2; break; }
    }
    for (const a of ['placeholder', 'name', 'title']) { t = attrString(op, a); if (t) return t; }
    return '';
  }
  function verbFor(event, tag0, op) {
    const e = event.slice(2).toLowerCase();
    const tag = tag0.split('.').pop().toLowerCase();
    const type = attrString(op, 'type');
    if (e === 'click') return 'click';
    if (e === 'change' || e === 'input') {
      if (tag === 'select') return 'choose in';
      if (tag === 'input' && /^(checkbox|radio)$/.test(type)) return 'click';
      if (tag === 'input' || tag === 'textarea') return 'type in';
      return e;
    }
    if (e === 'submit') return 'submit';
    if (e === 'keydown' || e === 'keyup' || e === 'keypress') return 'press key in';
    return e;
  }
  // ── one action per key: `if (e.key === 'Enter') … else if (e.key === 'ArrowUp') …` in a key handler ──
  // is what the user does, so each key gets its own path instead of one onKeyDown that the first
  // branch fills up. Follows thin wrappers (`(e) => handleKeyDown(e)`) down to the function that branches.
  const isKeyProp = (x) => { x = skipOuter(x); return ts.isPropertyAccessExpression(x) && /^(key|code)$/.test(x.name.text); };
  const keyLit = (x) => { x = skipOuter(x); return ts.isStringLiteral(x) || ts.isNoSubstitutionTemplateLiteral(x) ? x.text : null; };
  function keysIn(cond) {
    const out = [];
    const v = (n) => {
      n = skipOuter(n);
      if (ts.isBinaryExpression(n)) {
        const op = n.operatorToken.kind;
        if (op === ts.SyntaxKind.EqualsEqualsEqualsToken || op === ts.SyntaxKind.EqualsEqualsToken) {
          const k = isKeyProp(n.left) ? keyLit(n.right) : isKeyProp(n.right) ? keyLit(n.left) : null;
          if (k != null) out.push(k);
        } else if (op === ts.SyntaxKind.BarBarToken || op === ts.SyntaxKind.AmpersandAmpersandToken) { v(n.left); v(n.right); }
      } else if (ts.isCallExpression(n) && ts.isPropertyAccessExpression(n.expression) && n.expression.name.text === 'includes'
          && n.arguments[0] && isKeyProp(n.arguments[0]) && ts.isArrayLiteralExpression(skipOuter(n.expression.expression))) {
        for (const e of skipOuter(n.expression.expression).elements) { const k = keyLit(e); if (k != null) out.push(k); }
      }
    };
    v(cond);
    return out;
  }
  function keyBranches(fn) {
    const out = [];
    const onStmt = (st) => {
      if (ts.isIfStatement(st)) {
        const keys = keysIn(st.expression);
        if (keys.length) out.push({ keys, at: st, body: st.thenStatement });
        if (st.elseStatement) onStmt(st.elseStatement);
      } else if (ts.isSwitchStatement(st) && isKeyProp(st.expression)) {
        // `case 'ArrowDown': case 'ArrowUp': …` falls through: one action for the keys sharing a body
        let keys = [], first = null;
        for (const c of st.caseBlock.clauses) {
          const k = ts.isCaseClause(c) ? keyLit(c.expression) : null;
          if (k == null) { keys = []; first = null; continue; }   // `default:` or a non-literal case
          keys.push(k); first = first || c;
          if (c.statements.length) { out.push({ keys, at: first, body: c }); keys = []; first = null; }
        }
      } else if (ts.isBlock(st)) st.statements.forEach(onStmt);
    };
    if (fn.body && ts.isBlock(fn.body)) fn.body.statements.forEach(onStmt);
    return out;
  }
  function keySplit(V) {
    const pre = [];
    let fn = isFnLike(V) ? V : null;
    for (let d = 0; d < 3; d++) {
      if (!fn) {
        const t = resolveValue(V).find((x) => x.kind === 'fn');
        if (!t) return null;
        for (const h of t.hops) pre.push(h.row);
        if (t.named) pre.push(rowFor(t.def || t.fn));
        fn = t.fn;
      }
      const br = keyBranches(fn);
      if (br.length) return { pre, br };
      const calls2 = [];
      const v = (n) => { if (n !== fn && isFnLike(n)) return; if (ts.isCallExpression(n)) calls2.push(n); ts.forEachChild(n, v); };
      if (fn.body) v(fn.body);
      if (calls2.length !== 1) return null;
      pre.push(rowFor(calls2[0]));
      V = calls2[0].expression; fn = null;
    }
    return null;
  }
  const keyName = (k) => (k === ' ' ? 'Space' : k);
  const groupOf = (u) => { const d = path.posix.dirname(u.file); return d === '.' ? '' : d; };
  const seenAction = new Set();
  function addAction(o) {
    const key = o.row.id + '|' + o.label + '|' + o.event;
    if (seenAction.has(key)) return;
    seenAction.add(key);
    actions.push(Object.assign({ id: P + 'a' + actions.length }, o));
  }
  const isEvent = (name) => /^on[A-Z]/.test(name);
  // does a component read prop `key` itself ('self'), pass its props on with a spread ('spread'), or neither
  function consumes(unit, key, depth = 0) {
    const P2 = unit.props;
    if (!P2 || depth > 5) return 'none';
    if (P2.bind.has(key)) return 'self';
    const obj = P2.rest || P2.ident;
    if (!obj) return 'none';
    let self = false, spread = false;
    for (const ref of refs.get(obj) || []) {
      const e = upOuter(ref), p = e.parent;
      if (!p) continue;
      if (ts.isPropertyAccessExpression(p) && p.expression === e) { if (p.name.text === key) self = true; }
      else if (ts.isJsxSpreadAttribute(p)) {
        // spread onto another of the app's components: that one decides
        const t = resolveTag(p.parent.parent);
        const r = t.unit ? consumes(t.unit, key, depth + 1) : 'spread';
        if (r === 'self') self = true; else if (r === 'spread') spread = true;
      } else if (ts.isSpreadAssignment(p) || ts.isSpreadElement(p)) spread = true;
      else if (ts.isVariableDeclaration(p) && p.initializer === e && !ts.isIdentifier(p.name) && bindingFor(p.name, key)) self = true;
    }
    return self ? 'self' : spread ? 'spread' : 'none';
  }

  for (const u of units) {
    if (u.kind !== 'component') continue;
    const opens = [];
    const v = (n) => { if (n !== u.fn && unitByFn.has(n)) return; if (ts.isJsxOpeningElement(n) || ts.isJsxSelfClosingElement(n)) opens.push(n); ts.forEachChild(n, v); };
    v(u.fn);
    for (const op of opens) {
      const t = resolveTag(op);
      if (t.provider) continue;
      for (const p of op.attributes.properties) {
        if (!ts.isJsxAttribute(p) || !isEvent(attrName(p)) || !p.initializer || !ts.isJsxExpression(p.initializer) || !p.initializer.expression) continue;
        // a repo component that reads the prop itself has its own action inside; one that only
        // spreads its props onto a DOM element (<Button {...props}>) makes this line the action
        if (t.unit && consumes(t.unit, attrName(p)) !== 'spread') continue;
        const event = attrName(p), V = skipOuter(p.initializer.expression);
        const row = rowFor(op);
        const verb = verbFor(event, t.name, op);
        const code = `${event} on <${t.name}>`;
        // a pure pass-through (`<button onClick={onClick}>`): each parent that passes it is its own action
        // `form.handleSubmit(onSubmit)` (react-hook-form) hands the prop on the same way
        const VP = ts.isCallExpression(V) && calleeName(V) === 'handleSubmit' && V.arguments[0] ? skipOuter(V.arguments[0]) : V;
        const bs = (ts.isIdentifier(VP) ? symOf(VP) : ts.isPropertyAccessExpression(VP) && ts.isIdentifier(skipOuter(VP.expression)) && propOwner.get(symOf(skipOuter(VP.expression))) ? symOf(skipOuter(VP.expression)) : null);
        const po = bs && propOwner.get(bs);
        const passKey = po ? (po.key || (ts.isPropertyAccessExpression(VP) ? VP.name.text : null)) : null;
        const sites = passKey ? (renderSites.get(u) || []).filter((s) => s.op.attributes.properties.some((q) => ts.isJsxAttribute(q) && attrName(q) === passKey)) : [];
        if (passKey && sites.length) {
          for (const s of sites.slice(0, 12)) {
            const obj = labelObject(s.op, event) || labelObject(op, event);
            const W = pathFor();
            W.addStep([row]);
            const srow = rowFor(s.op);
            for (const q of s.op.attributes.properties) {
              if (ts.isJsxAttribute(q) && attrName(q) === passKey && q.initializer && ts.isJsxExpression(q.initializer) && q.initializer.expression) {
                const e2 = skipOuter(q.initializer.expression);
                W.addStep([srow]);
                if (isFnLike(e2)) W.walkFn(e2, 0, false);
                else { W.visitExpr(e2, 0, false); W.handleTargets(resolveValue(e2), srow, 0, false); }
              }
            }
            addAction({ label: obj ? verb + ' ' + labelText(obj) : code, sub: s.parent.name, group: groupOf(s.parent), u: s.parent, owner: s.parent, row: srow, event, steps: W.steps });
          }
          continue;
        }
        const obj = labelObject(op, event);
        const split = /^onKey(Down|Up|Press)$/.test(event) && keySplit(V);
        if (split) {
          for (const b of split.br) {
            const W = pathFor();
            W.addStep([row]);
            for (const r of split.pre) W.addStep([r]);
            W.addStep([rowFor(b.at)]);
            W.visitExpr(b.body, 0, false);
            const keys = b.keys.map(keyName).join(' or ');
            addAction({ label: 'press ' + keys + (obj ? ' in ' + labelText(obj) : ''), sub: u.name, group: groupOf(u), u, owner: u, row, event, steps: W.steps });
          }
          continue;
        }
        const W = pathFor();
        W.addStep([row]);
        if (isFnLike(V)) W.walkFn(V, 0, false);
        else { W.visitExpr(V, 0, false); W.handleTargets(resolveValue(V), row, 0, false); }
        addAction({ label: obj ? verb + ' ' + labelText(obj) : code, sub: u.name, group: groupOf(u), u, owner: u, row, event, steps: W.steps });
      }
    }
  }
  // queries run when the component mounts: page load too
  const QUERIES = new Set(['useQuery', 'useSuspenseQuery', 'useInfiniteQuery', 'useSuspenseInfiniteQuery']);
  for (const c of calls) {
    if (!QUERIES.has(calleeName(c)) || !c.arguments[0]) continue;
    const u = unitOf(c);
    if (!u || u.kind === 'fn') continue;
    const q = queryFnOf(c.arguments[0]);
    if (!q) continue;
    const owner = u.kind === 'hook' ? (hookOwners(u)[0] || {}).unit : u;
    const W = pathFor();
    const row = rowFor(c);
    W.addStep([row]);
    const qrow = rowFor(q);
    const init = ts.isPropertyAssignment(q) ? skipOuter(q.initializer) : q;
    if (ts.isMethodDeclaration(init) || ts.isArrowFunction(init) || ts.isFunctionExpression(init)) { W.addStep([qrow]); W.walkFn(init, 1, false); }
    else if (init) W.handleTargets(resolveValue(init), qrow, 1, false);
    addAction({ label: 'page load', sub: (owner || u).name, group: groupOf(owner || u), u, owner: owner || u, row, event: calleeName(c), steps: W.steps });
  }
  // page load: effects with an empty deps list
  for (const u of units) {
    for (const ef of u.effects) {
      if (!ef.deps || ef.deps.elements.length) continue;
      const owner = u.kind === 'hook' ? (hookOwners(u)[0] || {}).unit : u;
      if (u.kind === 'fn') continue;
      const W = pathFor();
      const row = rowFor(ef.call);
      W.addStep([row, rowFor(ef.deps)]);
      W.walkFn(ef.cb, 1, false);
      addAction({ label: 'page load', sub: (owner || u).name, group: groupOf(owner || u), u, owner: owner || u, row, event: 'useEffect', steps: W.steps });
    }
  }

  // ── setter sites for every state ──
  const setterData = new Map();
  for (const S of states) setterData.set(S, setterFlow(S));

  // ── marks on rows: component tags, setter names, the state's value ──
  const symStates = new Map();   // symbol → [state ids] (names that set it)
  const keyStates = [];          // [{sym, key, S}] (props.onApply)
  const valStates = new Map();   // symbol → [state ids] (names that hold it)
  const valKeyStates = [];
  const addTo = (m, k, v) => { let a = m.get(k); if (!a) m.set(k, a = []); if (!a.includes(v)) a.push(v); };
  for (const S of states) {
    const d = setterData.get(S);
    for (const s of d.syms) addTo(symStates, s, S.id);
    for (const k of d.keyed) keyStates.push({ sym: k.sym, key: k.key, id: S.id });
    const { V } = flows(S);
    for (const s of V.syms) addTo(valStates, s, S.id);
    for (const k of V.keys) valKeyStates.push({ sym: k.sym, key: k.key, id: S.id });
  }
  const keyHits = (list, pa) => {
    const b = skipOuter(pa.expression);
    if (!ts.isIdentifier(b)) return [];
    const s = symOf(b);
    return list.filter((k) => k.sym === s && k.key === pa.name.text).map((k) => k.id);
  };
  function idsIn(sf, p0, p1) {
    const list = idsByFile.get(sf.fileName) || [];
    let lo = 0, hi = list.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (list[mid].node.getStart() < p0) lo = mid + 1; else hi = mid; }
    const out = [];
    for (let i = lo; i < list.length; i++) { const s = list[i].node.getStart(); if (s >= p1) break; out.push(list[i]); }
    return out;
  }
  function marksFor(sf, p0, p1, map, textLen, sigUnit) {
    const marks = [];
    for (const it of idsIn(sf, p0, p1)) {
      const n = it.node, off = map(n.getStart());
      if (off < 0) continue;
      const end = Math.min(textLen, off + n.text.length);
      const p = n.parent;
      if (!it.decl && !it.pa && (ts.isJsxOpeningElement(p) || ts.isJsxSelfClosingElement(p)) && p.tagName === n) {
        const t = resolveTag(p);
        if (t.unit) { marks.push([Math.max(0, off - 1), end, 'c', t.unit.id]); continue; }
      }
      let setIds = [], valIds = [];
      if (it.pa) { setIds = keyHits(keyStates, it.pa); valIds = keyHits(valKeyStates, it.pa); }
      else if (it.sym) { setIds = symStates.get(it.sym) || []; valIds = valStates.get(it.sym) || []; }
      if (sigUnit && it.decl && ts.isBindingElement(p) && sigUnit.props) {
        const key = p.propertyName ? (p.propertyName.text || p.propertyName.getText()) : n.text;
        if (sigUnit.props.bind.get(key) === it.sym) { marks.push([off, end, 'p', key, setIds.join(' '), valIds.join(' ')]); continue; }
      }
      if (setIds.length) marks.push([off, end, 's', setIds.join(' ')]);
      else if (valIds.length) marks.push([off, end, 'v', valIds.join(' ')]);
    }
    return marks;
  }
  function finishRow(r) {
    const sf = r.sf;
    const { text, map } = joinLines(sf, r.a, r.b);
    const starts = sf.getLineStarts();
    const p0 = starts[r.a], p1 = r.b + 1 < starts.length ? starts[r.b + 1] : sf.text.length;
    let t = text, marks = marksFor(sf, p0, p1, map, text.length, null);
    const CAP = 360;
    if (t.length > CAP) { t = t.slice(0, CAP - 1) + '…'; marks = marks.filter((m) => m[1] <= CAP - 1); }
    r.text = t; r.marks = marks;
    r.ind = leadWidth(lineText(sf, r.a));
  }
  allRows.forEach(finishRow);

  // component signature line: `function CouponField({ onApply })`
  function sigOf(u) {
    const sf = u.sf, stmt = u.stmt;
    const start = stmt.getStart(), bodyPos = u.fn.body.getStart();
    const a = lineOf(sf, start);
    let b = lineOf(sf, bodyPos);
    if (b - a > 8) b = a + 8;
    const { text, map } = joinLines(sf, a, b);
    let cut = map(bodyPos);
    if (cut < 0) cut = text.length;
    const shift = Math.max(0, map(start));
    let t = text.slice(shift, cut);
    let marks = marksFor(sf, start, bodyPos, map, cut, u).map((m) => [m[0] - shift, m[1] - shift].concat(m.slice(2)));
    // `export default` adds nothing to read here
    const ex = /^export\s+(default\s+)?/.exec(t);
    if (ex) { t = t.slice(ex[0].length); marks = marks.map((m) => [m[0] - ex[0].length, m[1] - ex[0].length].concat(m.slice(2))); }
    t = t.replace(/\s*=>\s*$/, '').replace(/\s+$/, '');
    marks = marks.filter((m) => m[0] >= 0 && m[1] <= t.length);
    if (t.length > 300) { t = t.slice(0, 299) + '…'; marks = marks.filter((m) => m[1] <= 299); }
    return { t, m: marks };
  }

  // ── output ──
  const used = new Set();
  for (const r of allRows) used.add(r.u);
  for (const u of units) if (u.kind === 'component') used.add(u);
  const unitList = units.filter((u) => used.has(u));
  // fn boxes: right of whoever reaches them
  for (const u of unitList) if (u.depth == null) u.depth = 0;
  for (const u of unitList) for (const r of u.renders) { const row = rowFor(r.op); if (row && !row.kids.includes(r.child.id)) row.kids.push(r.child.id); }
  // rows created just now (render rows) need text too
  allRows.forEach((r) => { if (r.text == null) finishRow(r); });

  // a hook box sits beside the line that calls it
  const hookRows = new Map();
  for (const u of unitList) {
    if (u.kind !== 'hook') continue;
    const rs = [];
    for (const c of hookCallSites.get(u) || []) { const cu = unitOf(c); if (cu && used.has(cu)) { const r = rowFor(c); if (r && !rs.includes(r)) rs.push(r); } }
    hookRows.set(u, rs);
  }
  allRows.forEach((r) => { if (r.text == null) finishRow(r); });
  const outUnits = unitList.map((u) => {
    const rowsHere = (rowsByUnit.get(u) || []).slice().sort((x, y) => x.a - y.a || x.b - y.b);
    const propRows = {};
    for (const s of renderSites.get(u) || []) {
      for (const p of s.op.attributes.properties) {
        if (!ts.isJsxAttribute(p)) continue;
        const r = rowFor(s.op);
        (propRows[attrName(p)] = propRows[attrName(p)] || []).push(r.id);
      }
    }
    const code = u.stmt.getText();
    return {
      id: u.id, name: u.name, kind: u.kind, file: u.file, line: startLine(u.stmt) + 1, memo: u.memo,
      depth: u.depth, parents: u.parents.map((p) => p.id), sig: sigOf(u), rows: rowsHere.map((r) => r.id),
      propRows, code: code.length > 60000 ? code.slice(0, 60000) : code, codeLine: startLine(u.stmt) + 1,
      callRows: (hookRows.get(u) || []).map((r) => r.id),
    };
  });
  // late rows (from sig or render pass) get finished too
  allRows.forEach((r) => { if (r.text == null) finishRow(r); });
  const rowsOut = {};
  for (const r of allRows) {
    rowsOut[r.id] = { u: r.u.id, a: r.a + 1, b: r.b + 1, t: r.text, i: r.ind, m: r.marks, ...(r.e > r.b ? { e: r.e + 1 } : {}), ...(r.pe > r.b ? { pe: r.pe + 1 } : {}) };
    if (r.api.length) rowsOut[r.id].api = r.api.map((x) => [x.m, x.u, x.id]);
    if (r.kids.length) rowsOut[r.id].kids = r.kids;
    if (r.unc) rowsOut[r.id].unc = 1;
  }
  const stateOut = states.filter((S) => S.row).map((S) => {
    const d = setterData.get(S);
    const siteRows = [...d.sites.keys()].filter(Boolean);
    const rank = (r) => [r.u.depth || 0, r.u.file, r.a];
    siteRows.sort((x, y) => { const a = rank(x), b = rank(y); return a[0] - b[0] || (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0) || a[2] - b[2]; });
    return {
      id: S.id, u: S.unit.id, row: S.row.id, name: S.name, setter: S.setter, kind: S.kind,
      sites: siteRows.map((r) => r.id), unc: siteRows.filter((r) => d.sites.get(r)).map((r) => r.id),
      hops: [...d.hops].filter((r) => r && !d.sites.has(r) && r !== S.row).map((r) => r.id),
      sigs: [...d.sigs].map((u) => u.id),
    };
  });
  const actOut = actions.map((a) => ({
    id: a.id, label: a.label, sub: a.sub, group: a.group, u: a.u.id, row: a.row.id, event: a.event,
    steps: a.steps.filter((s) => s.r.length || s.rr.length).map((s) => {
      const o = { r: s.r.map((r) => r.id) };
      if (s.tk.length) o.tk = [...new Set(s.tk.map((S) => S.id))];
      if (s.tv.length) o.tv = [...new Set(s.tv.map((S) => S.id))];
      if (s.rr.length) o.rr = s.rr.map(([u, r, unc]) => [u.id, r ? r.id : null, unc ? 1 : 0]);
      if (s.unc) o.unc = 1;
      return o;
    }),
  }));
  // an element with two handlers where one does nothing we can follow (onSelect={(e) => e.preventDefault()})
  // next to one that does: keep the one that leads somewhere
  {
    const richRows = new Set(actOut.filter((a) => a.steps.length > 1).map((a) => a.row));
    for (let i = actOut.length - 1; i >= 0; i--) if (actOut[i].steps.length <= 1 && richRows.has(actOut[i].row)) actOut.splice(i, 1);
  }
  // sidebar groups read as short folder names: drop the folders every group shares and a leading src/
  {
    const parts = actOut.map((a) => (a.group ? a.group.split('/') : []));
    let common = parts.length ? Math.min(...parts.map((p) => p.length)) : 0;
    for (let i = 0; i < common; i++) if (parts.some((p) => p[i] !== parts[0][i])) { common = i; break; }
    actOut.forEach((a, k) => {
      let p = parts[k].slice(common);
      if (p[0] === 'src') p = p.slice(1);
      a.group = p.join('/');
    });
  }
  const treeRank = new Map();
  {
    const visit = (u) => { if (treeRank.has(u)) return; treeRank.set(u, treeRank.size); for (const r of u.renders) visit(r.child); };
    units.filter((u) => u.kind === 'component' && !u.parents.length).forEach(visit);
    units.forEach((u) => { if (!treeRank.has(u)) treeRank.set(u, treeRank.size); });
  }
  const order = new Map(actions.map((a) => [a.id, [treeRank.get(a.owner || a.u), a.u.file, a.row.a]]));
  actOut.sort((x, y) => {
    if (x.group !== y.group) return x.group < y.group ? -1 : 1;
    const a = order.get(x.id), b = order.get(y.id);
    return a[0] - b[0] || (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0) || a[2] - b[2];
  });
  return {
    version: 1, root: path.basename(root), tag,
    units: outUnits, rows: rowsOut, states: stateOut, actions: actOut,
    http: Object.fromEntries(Object.entries(httpOut).filter(([, h]) => h.row)),
    stats: { files: sources.length, components: units.filter((u) => u.kind === 'component').length, ms: Date.now() - t0, typescript: ts.version },
  };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
