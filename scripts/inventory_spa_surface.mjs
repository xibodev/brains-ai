import fs from "node:fs";
import path from "node:path";
import ts from "../frontend/node_modules/typescript/lib/typescript.js";

const sourceRoot = path.resolve(process.argv[2] || "frontend/src");
const frontendRoot = path.dirname(sourceRoot);
const config = ts.readConfigFile(path.join(frontendRoot, "tsconfig.json"), ts.sys.readFile);
if (config.error) throw new Error("frontend tsconfig is unreadable");
const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, frontendRoot);
if (parsed.errors.length) throw new Error("frontend tsconfig is invalid");
const aliases = Object.keys(parsed.options.paths || {}).map((value) => value.split("*", 1)[0]);
const extensions = ["", ".ts", ".tsx", ".js", ".jsx", ".css"];
const relative = (file) => path.relative(sourceRoot, file).split(path.sep).join("/");
const within = (file) => file === sourceRoot || file.startsWith(`${sourceRoot}${path.sep}`);

function resolveLocal(specifier, importer) {
  const resolved = ts.resolveModuleName(specifier, importer, parsed.options, ts.sys).resolvedModule;
  if (resolved && within(path.resolve(resolved.resolvedFileName))) return path.resolve(resolved.resolvedFileName);
  if (!specifier.startsWith(".")) {
    if (aliases.some((prefix) => specifier.startsWith(prefix))) throw new Error("configured frontend alias could not be resolved");
    return null;
  }
  const base = path.resolve(path.dirname(importer), specifier);
  const candidates = [...extensions.map((ext) => `${base}${ext}`), ...extensions.slice(1).map((ext) => path.join(base, `index${ext}`))];
  for (const candidate of candidates) if (within(candidate) && fs.existsSync(candidate) && fs.statSync(candidate).isFile()) return candidate;
  throw new Error("relative frontend import could not be resolved");
}

function dependencies(sourceFile) {
  const found = [];
  function visit(node) {
    if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier) found.push(node.moduleSpecifier.text);
    else if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword) {
      if (node.arguments.length !== 1 || !ts.isStringLiteralLike(node.arguments[0])) throw new Error("dynamic frontend import target is unresolved");
      found.push(node.arguments[0].text);
    } else if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression) &&
      ts.isMetaProperty(node.expression.expression) && node.expression.expression.keywordToken === ts.SyntaxKind.ImportKeyword &&
      ["glob", "globEager"].includes(node.expression.name.text)) {
      throw new Error("import.meta glob frontend reachability is unresolved");
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return found;
}

const entry = path.join(sourceRoot, "main.tsx");
if (!fs.existsSync(entry)) throw new Error("frontend entry main.tsx is unavailable");
const pending = [entry], discovered = new Map(), graph = {};
while (pending.length) {
  const file = path.resolve(pending.pop());
  if (discovered.has(file)) continue;
  const kind = file.endsWith(".tsx") ? ts.ScriptKind.TSX : file.endsWith(".ts") ? ts.ScriptKind.TS : ts.ScriptKind.Unknown;
  const sourceFile = ts.createSourceFile(file, fs.readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true, kind);
  discovered.set(file, sourceFile);
  const local = [];
  for (const specifier of dependencies(sourceFile)) {
    const dependency = resolveLocal(specifier, file);
    if (dependency) { local.push(dependency); pending.push(dependency); }
  }
  graph[relative(file)] = [...new Set(local.map(relative))].sort();
}
if (!discovered.has(path.join(sourceRoot, "App.tsx"))) throw new Error("frontend entry does not reach App.tsx");
const program = ts.createProgram({ rootNames: [...discovered.keys()].filter((file) => /\.[cm]?[jt]sx?$/.test(file)), options: parsed.options });
const checker = program.getTypeChecker();
const sourceFiles = [...discovered.keys()].map((file) => program.getSourceFile(file)).filter(Boolean);

function unwrap(node) {
  while (node && (ts.isParenthesizedExpression(node) || ts.isAsExpression(node) || ts.isTypeAssertionExpression(node) || ts.isNonNullExpression(node) || ts.isSatisfiesExpression(node))) node = node.expression;
  return node;
}
function staticLiteral(node) {
  node = unwrap(node);
  return node && (ts.isStringLiteralLike(node) || ts.isNumericLiteral(node)) ? node.text : null;
}
function propertyName(node) {
  node = unwrap(node);
  if (node && (ts.isIdentifier(node) || ts.isStringLiteralLike(node) || ts.isNumericLiteral(node))) return node.text;
  return node && ts.isComputedPropertyName(node) ? staticLiteral(node.expression) : null;
}
function memberName(node) {
  node = unwrap(node);
  if (node && ts.isPropertyAccessExpression(node)) return node.name.text;
  return node && ts.isElementAccessExpression(node) ? staticLiteral(node.argumentExpression) : null;
}
function owner(node) {
  node = unwrap(node);
  return node && (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) ? node.expression : null;
}
function scope(node) {
  for (let cursor = node?.parent; cursor; cursor = cursor.parent) if (ts.isFunctionLike(cursor)) return cursor;
  return null;
}
function scopeContains(outer, inner) {
  if (!outer) return true;
  for (let cursor = inner; cursor; cursor = scope(cursor.parent)) if (cursor === outer) return true;
  return false;
}
function conditional(node, boundary) {
  for (let cursor = node.parent; cursor && cursor !== boundary && !ts.isSourceFile(cursor); cursor = cursor.parent) {
    if (ts.isIfStatement(cursor) || ts.isConditionalExpression(cursor) || ts.isSwitchStatement(cursor) ||
      ts.isForStatement(cursor) || ts.isForInStatement(cursor) || ts.isForOfStatement(cursor) ||
      ts.isWhileStatement(cursor) || ts.isDoStatement(cursor) || ts.isTryStatement(cursor)) return true;
  }
  return false;
}
function importInfo(symbol) {
  if (!symbol) return null;
  for (const declaration of symbol.declarations || []) {
    if (ts.isImportSpecifier(declaration)) {
      const module = declaration.parent.parent.parent.moduleSpecifier.text;
      if (module.startsWith(".") && symbol.flags & ts.SymbolFlags.Alias) {
        const target = checker.getAliasedSymbol(symbol);
        if (target && target !== symbol) {
          const targetInfo = importInfo(target);
          if (targetInfo && !targetInfo.module.startsWith(".")) return targetInfo;
        }
        const importedFile = resolveLocal(module, declaration.getSourceFile().fileName);
        const importedSource = importedFile && program.getSourceFile(importedFile);
        const importedName = declaration.propertyName?.text || declaration.name.text;
        for (const statement of importedSource?.statements || []) {
          if (!ts.isExportDeclaration(statement) || !statement.moduleSpecifier || !ts.isNamedExports(statement.exportClause)) continue;
          const exported = statement.exportClause.elements.find((item) => item.name.text === importedName);
          if (exported) return { module: statement.moduleSpecifier.text, name: exported.propertyName?.text || exported.name.text };
        }
      }
      return { module, name: declaration.propertyName?.text || declaration.name.text };
    }
    if (ts.isNamespaceImport(declaration)) return { module: declaration.parent.parent.moduleSpecifier.text, name: "*" };
    if (ts.isImportClause(declaration)) return { module: declaration.parent.moduleSpecifier.text, name: "default" };
    if (ts.isExportSpecifier(declaration)) {
      const exportDeclaration = declaration.parent.parent;
      if (exportDeclaration.moduleSpecifier) return { module: exportDeclaration.moduleSpecifier.text, name: declaration.propertyName?.text || declaration.name.text };
    }
  }
  if (symbol.flags & ts.SymbolFlags.Alias) {
    const target = checker.getAliasedSymbol(symbol);
    if (target && target !== symbol) return importInfo(target);
  }
  return null;
}
function libraryGlobal(identifier, names) {
  if (!ts.isIdentifier(identifier) || !names.includes(identifier.text)) return false;
  const symbol = checker.getSymbolAtLocation(identifier);
  const declarations = symbol?.declarations || [];
  if (identifier.text === "globalThis" && !declarations.length) {
    return Boolean(symbol && symbol.flags & ts.SymbolFlags.Module);
  }
  return declarations.length > 0 && declarations.every((declaration) => declaration.getSourceFile().isDeclarationFile);
}

const writes = new Map();
function addWrite(identifier, expression, selector, origin, symbolOverride = null) {
  if (!ts.isIdentifier(identifier)) return;
  const symbol = symbolOverride || checker.getSymbolAtLocation(identifier);
  if (!symbol) return;
  const lexicalScope = scope(origin);
  const records = writes.get(symbol) || [];
  records.push({ expression, selector, pos: origin.getStart(), scope: lexicalScope, conditional: conditional(origin, lexicalScope) });
  writes.set(symbol, records);
}
function bind(pattern, expression, selector, origin, assignment = false) {
  pattern = unwrap(pattern);
  if (ts.isIdentifier(pattern)) return addWrite(pattern, expression, selector, origin);
  const objectPattern = assignment ? ts.isObjectLiteralExpression(pattern) : ts.isObjectBindingPattern(pattern);
  const arrayPattern = assignment ? ts.isArrayLiteralExpression(pattern) : ts.isArrayBindingPattern(pattern);
  if (objectPattern) for (const item of assignment ? pattern.properties : pattern.elements) {
    if (ts.isBindingElement(item)) { const key = propertyName(item.propertyName || item.name); if (key !== null) bind(item.name, expression, [...selector, key], origin); }
    else if (ts.isShorthandPropertyAssignment(item)) addWrite(
      item.name,
      expression,
      [...selector, item.name.text],
      origin,
      checker.getShorthandAssignmentValueSymbol(item),
    );
    else if (ts.isPropertyAssignment(item)) { const key = propertyName(item.name); if (key !== null) bind(item.initializer, expression, [...selector, key], origin, true); }
  }
  if (arrayPattern) pattern.elements.forEach((item, index) => {
    if (ts.isBindingElement(item)) bind(item.name, expression, [...selector, index], origin);
    else if (item && !ts.isOmittedExpression(item)) bind(item, expression, [...selector, index], origin, true);
  });
}
for (const sourceFile of sourceFiles) {
  function index(node) {
    if (ts.isVariableDeclaration(node)) bind(node.name, node.initializer || null, [], node);
    else if (ts.isParameter(node)) bind(node.name, node.initializer || null, [], node);
    else if (ts.isFunctionDeclaration(node) && node.name) addWrite(node.name, node, [], node);
    else if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken) bind(node.left, node.right, [], node, true);
    ts.forEachChild(node, index);
  }
  index(sourceFile);
}
for (const records of writes.values()) records.sort((a, b) => a.pos - b.pos);

const NON = "non-nav", UNKNOWN = "unknown", MAYBE = "maybe-navigation";
const capabilities = new Set(["window", "document", "location", "history", "open-fn", "location-fn", "history-fn", "navigate-fn", "navigate-factory", "router-ns", "route", "navigate-component", "link", "router-form", "route-factory", "core-route", "form", "form-collection", "form-factory", "button", "input", "anchor"]);
const callable = new Set(["open-fn", "location-fn", "history-fn", "navigate-fn"]);
const isCapability = (kind) => capabilities.has(kind) || kind === MAYBE;
function routerKind(name) {
  return ({ useNavigate: "navigate-factory", redirect: "navigate-fn", Route: "route", Navigate: "navigate-component", Link: "link", NavLink: "link", Form: "router-form", createBrowserRouter: "route-factory", useRoutes: "route-factory", createRoutesFromElements: "route-factory" })[name] || NON;
}
function memberKind(base, name) {
  if (base === MAYBE) return MAYBE;
  if (base === UNKNOWN) return NON;
  if (base === "router-ns") return routerKind(name);
  if (base === "window") return ["window", "self", "top", "parent"].includes(name) ? "window" : ({ document: "document", location: "location", history: "history", open: "open-fn" })[name] || NON;
  if (base === "document") return name === "location" ? "location" : name === "forms" ? "form-collection" : NON;
  if (base === "form-collection") return /^\d+$/.test(name || "") ? "form" : ["item", "namedItem"].includes(name) ? "form-factory" : NON;
  if (base === "location" && ["assign", "replace"].includes(name)) return "location-fn";
  if (base === "history" && ["pushState", "replaceState"].includes(name)) return "history-fn";
  if (callable.has(base) && name === "bind") return base;
  if (["form", "button", "input"].includes(base) && name === "form") return "form";
  if (["form", "button", "input", "anchor"].includes(base) && ["setAttribute", "setAttributeNS"].includes(name)) return `${base}-setattr`;
  if (base === "form" && ["submit", "requestSubmit"].includes(name)) return "form-submit";
  if (base === "anchor" && name === "click") return "anchor-click";
  return NON;
}
function applicable(symbol, use) {
  const useScope = scope(use);
  return (writes.get(symbol) || []).filter((record) => scopeContains(record.scope, useScope) && (record.pos <= use.getStart() || (!record.scope && useScope)));
}
function selectKind(expression, selector, use, seen) {
  let current = unwrap(expression), kind = null;
  for (const key of selector) {
    current = unwrap(current);
    if (current && ts.isObjectLiteralExpression(current)) {
      const item = current.properties.find((value) => (ts.isPropertyAssignment(value) || ts.isShorthandPropertyAssignment(value)) && propertyName(value.name) === String(key));
      if (!item) return NON;
      if (ts.isShorthandPropertyAssignment(item)) {
        const valueSymbol = checker.getShorthandAssignmentValueSymbol(item);
        return valueSymbol ? symbolKind(valueSymbol, use, seen) : UNKNOWN;
      }
      current = item.initializer;
    } else if (current && ts.isArrayLiteralExpression(current) && typeof key === "number") current = current.elements[key];
    else { kind ||= kindOf(current, use, seen); return memberKind(kind, String(key)); }
  }
  return kindOf(current, use, seen);
}
function symbolKind(symbol, use, seen) {
  if (seen.has(symbol)) return UNKNOWN;
  const next = new Set(seen).add(symbol), records = applicable(symbol, use);
  if (!records.length) return NON;
  const latest = records.at(-1);
  const value = latest.expression ? selectKind(latest.expression, latest.selector, use, next) : UNKNOWN;
  const priorCapability = records.slice(0, -1).some((record) => record.expression && isCapability(selectKind(record.expression, record.selector, use, next)));
  return (latest.conditional || value === UNKNOWN) && (isCapability(value) || priorCapability) ? MAYBE : value;
}
function coreRouteSymbol(symbol) {
  if (!symbol) return false;
  if (symbol.flags & ts.SymbolFlags.Alias) symbol = checker.getAliasedSymbol(symbol);
  return (symbol?.declarations || []).some((declaration) => relative(declaration.getSourceFile().fileName) === "coreRoutes.ts" && declaration.name?.text === "coreRoute");
}
function kindOf(node, use = node, seen = new Set()) {
  node = unwrap(node);
  if (!node) return UNKNOWN;
  if (ts.isIdentifier(node)) {
    const symbol = checker.getSymbolAtLocation(node), info = importInfo(symbol);
    if (coreRouteSymbol(symbol)) return "core-route";
    if (info?.module === "react-router-dom") return info.name === "*" ? "router-ns" : routerKind(info.name);
    if (libraryGlobal(node, ["window", "globalThis", "self", "top", "parent"])) return "window";
    if (libraryGlobal(node, ["document"])) return "document";
    if (libraryGlobal(node, ["location"])) return "location";
    if (libraryGlobal(node, ["history"])) return "history";
    if (libraryGlobal(node, ["open"])) return "open-fn";
    return symbol ? symbolKind(symbol, use, seen) : UNKNOWN;
  }
  if (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) {
    const literalOwner = unwrap(node.expression), name = memberName(node);
    if (ts.isObjectLiteralExpression(literalOwner) && name !== null) {
      const item = literalOwner.properties.find((value) =>
        (ts.isPropertyAssignment(value) || ts.isShorthandPropertyAssignment(value)) && propertyName(value.name) === name);
      if (item) return kindOf(ts.isShorthandPropertyAssignment(item) ? item.name : item.initializer, use, seen);
    }
    const base = kindOf(node.expression, use, seen);
    return name === null && isCapability(base) ? MAYBE : memberKind(base, name);
  }
  if (ts.isCallExpression(node)) {
    const callee = kindOf(node.expression, node, seen);
    if (callee === "navigate-factory") return "navigate-fn";
    if (callee === "form-factory") return "form";
    if (callable.has(callee) && memberName(node.expression) === "bind") return callee;
    const base = owner(node.expression) ? kindOf(owner(node.expression), node, seen) : NON, name = memberName(node.expression);
    if (base === "document" && name === "createElement") return ({ form: "form", button: "button", input: "input", a: "anchor", area: "anchor" })[staticLiteral(node.arguments[0])?.toLowerCase()] || NON;
    if (base === "document" && ["querySelector", "getElementById"].includes(name)) {
      const selector = staticLiteral(node.arguments[0])?.toLowerCase();
      if (!selector) return MAYBE;
      for (const [tag, kind] of [["form", "form"], ["button", "button"], ["input", "input"], ["a", "anchor"], ["area", "anchor"]]) if (selector === tag || [".", "#", "[", ":"].some((suffix) => selector.startsWith(`${tag}${suffix}`))) return kind;
    }
    return NON;
  }
  return NON;
}

function selectStatic(expression, selector, use, seen) {
  let current = unwrap(expression);
  for (const key of selector) {
    current = unwrap(current);
    if (current && ts.isObjectLiteralExpression(current)) {
      const item = current.properties.find((value) => (ts.isPropertyAssignment(value) || ts.isShorthandPropertyAssignment(value)) && propertyName(value.name) === String(key));
      if (!item) return null;
      if (ts.isShorthandPropertyAssignment(item)) {
        const valueSymbol = checker.getShorthandAssignmentValueSymbol(item);
        if (!valueSymbol || seen.has(valueSymbol)) return null;
        const records = applicable(valueSymbol, use), latest = records.at(-1);
        if (!latest || latest.conditional || !latest.expression) return null;
        return selectStatic(latest.expression, latest.selector, use, new Set(seen).add(valueSymbol));
      }
      current = item.initializer;
    } else if (current && ts.isArrayLiteralExpression(current) && typeof key === "number") current = current.elements[key];
    else return null;
  }
  return staticValues(current, use, seen);
}
function staticValues(node, use = node, seen = new Set()) {
  node = unwrap(node);
  if (!node) return null;
  if (ts.isStringLiteralLike(node)) return [node.text];
  if (ts.isTemplateExpression(node)) {
    let values = [node.head.text];
    for (const span of node.templateSpans) { const part = staticValues(span.expression, use, seen); if (!part) return null; values = values.flatMap((left) => part.map((right) => `${left}${right}${span.literal.text}`)); }
    return values;
  }
  if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
    const left = staticValues(node.left, use, seen), right = staticValues(node.right, use, seen);
    return left && right ? left.flatMap((a) => right.map((b) => `${a}${b}`)) : null;
  }
  if (ts.isConditionalExpression(node)) {
    const yes = staticValues(node.whenTrue, use, seen), no = staticValues(node.whenFalse, use, seen);
    return yes && no ? [...new Set([...yes, ...no])] : null;
  }
  if (ts.isCallExpression(node) && kindOf(node.expression, node) === "core-route") return ["@core-route-guard"];
  if (ts.isIdentifier(node)) {
    const symbol = checker.getSymbolAtLocation(node);
    if (!symbol || seen.has(symbol)) return null;
    const records = applicable(symbol, use), latest = records.at(-1);
    if (!latest || latest.conditional || !latest.expression) return null;
    return selectStatic(latest.expression, latest.selector, use, new Set(seen).add(symbol));
  }
  return null;
}
const staticSingle = (node) => { const values = node ? staticValues(node) : null; return values?.length === 1 ? values[0] : null; };
const sites = [], unresolved = [];
const lineOf = (file, node) => file.getLineAndCharacterOfPosition(node.getStart(file)).line + 1;
function fail(file, node, kind) { unresolved.push({ file: `frontend/src/${relative(file.fileName)}`, kind, line: lineOf(file, node) }); }
function record(file, node, kind, expression) {
  const values = staticValues(expression, node);
  if (!values) return fail(file, node, kind);
  for (const target of values) sites.push({ file: `frontend/src/${relative(file.fileName)}`, kind, line: lineOf(file, node), target });
}
function marker(file, node, kind, target) { sites.push({ file: `frontend/src/${relative(file.fileName)}`, kind, line: lineOf(file, node), target }); }
function historyDelta(node) { node = unwrap(node); return node && (ts.isNumericLiteral(node) || (ts.isPrefixUnaryExpression(node) && [ts.SyntaxKind.PlusToken, ts.SyntaxKind.MinusToken].includes(node.operator) && ts.isNumericLiteral(node.operand))); }
function jsxTarget(node, name) {
  for (const item of node.attributes.properties) {
    if (ts.isJsxAttribute(item) && item.name.text === name) return { node: item, expression: ts.isStringLiteral(item.initializer) ? item.initializer : ts.isJsxExpression(item.initializer) ? item.initializer.expression : null };
    if (ts.isJsxSpreadAttribute(item)) {
      const value = unwrap(item.expression);
      if (!ts.isObjectLiteralExpression(value)) return { node: item, expression: null };
      const match = value.properties.find((property) => (ts.isPropertyAssignment(property) || ts.isShorthandPropertyAssignment(property)) && propertyName(property.name) === name);
      if (match) return { node: match, expression: ts.isShorthandPropertyAssignment(match) ? match.name : match.initializer };
    }
  }
  return null;
}
function routeObjects(file, node) {
  node = unwrap(node);
  if (ts.isArrayLiteralExpression(node)) return node.elements.forEach((item) => routeObjects(file, item));
  if (!ts.isObjectLiteralExpression(node)) return fail(file, node, "route-config");
  for (const item of node.properties) {
    if (ts.isSpreadAssignment(item)) fail(file, item, "route-config");
    else if (ts.isPropertyAssignment(item) && propertyName(item.name) === "path") record(file, item, "route", item.initializer);
  }
}
function reactCreateElement(node) {
  const expression = unwrap(node.expression);
  if (!ts.isPropertyAccessExpression(expression) || expression.name.text !== "createElement") return false;
  const info = ts.isIdentifier(expression.expression) ? importInfo(checker.getSymbolAtLocation(expression.expression)) : null;
  return info?.module === "react" && ["*", "default"].includes(info.name);
}

for (const file of sourceFiles) {
  function visit(node) {
    if (ts.isJsxOpeningLikeElement(node)) {
      const intrinsic = ts.isIdentifier(node.tagName) && node.tagName.text === node.tagName.text.toLowerCase();
      const kind = intrinsic ? null : kindOf(node.tagName, node);
      let name = null, siteKind = "navigation";
      if (kind === "route") { name = "path"; siteKind = "route"; }
      else if (["navigate-component", "link"].includes(kind)) name = "to";
      else if (kind === "router-form") { name = "action"; siteKind = "form-action"; }
      else if (intrinsic && ["a", "area"].includes(node.tagName.text)) name = "href";
      else if (intrinsic && node.tagName.text === "form") { name = "action"; siteKind = "form-action"; }
      else if (intrinsic && ["button", "input"].includes(node.tagName.text)) { name = "formAction"; siteKind = "form-action"; }
      if (name) {
        const target = jsxTarget(node, name);
        if (target) target.expression ? record(file, target.node, siteKind, target.expression) : fail(file, target.node, siteKind);
        else if (kind && node.attributes.properties.some((item) => ts.isJsxAttribute(item) && ["path", "to", "href", "action", "formAction"].includes(item.name.text))) fail(file, node, "unknown-router-jsx");
      }
      else if (kind === MAYBE) fail(file, node, "unknown-router-jsx");
    } else if (ts.isCallExpression(node)) {
      const kind = kindOf(node.expression, node);
      if (reactCreateElement(node)) {
        const component = kindOf(node.arguments[0], node), props = unwrap(node.arguments[1]);
        const targetName = component === "route" ? "path" : ["navigate-component", "link"].includes(component) ? "to" : component === "router-form" ? "action" : null;
        if (targetName) {
          if (!ts.isObjectLiteralExpression(props)) fail(file, node, "create-element-navigation");
          else {
            const target = props.properties.find((item) => (ts.isPropertyAssignment(item) || ts.isShorthandPropertyAssignment(item)) && propertyName(item.name) === targetName);
            if (!target) fail(file, node, "create-element-navigation");
            else record(file, target, component === "route" ? "route" : component === "router-form" ? "form-action" : "navigation", ts.isShorthandPropertyAssignment(target) ? target.name : target.initializer);
          }
        }
      } else if (kind === "navigate-fn") node.arguments.length ? (historyDelta(node.arguments[0]) ? marker(file, node, "history", "@history-delta") : record(file, node, "navigate", node.arguments[0])) : fail(file, node, "navigate");
      else if (["open-fn", "location-fn"].includes(kind)) node.arguments.length ? record(file, node, kind === "open-fn" ? "window-open" : "location", node.arguments[0]) : fail(file, node, kind);
      else if (kind === "history-fn") node.arguments.length >= 3 ? record(file, node, "history-state", node.arguments[2]) : fail(file, node, "history-state");
      else if (kind === "route-factory") node.arguments.length ? routeObjects(file, node.arguments[0]) : fail(file, node, "route-config");
      else if (kind.endsWith?.("-setattr")) {
        const offset = memberName(node.expression) === "setAttributeNS" ? 1 : 0, attribute = staticSingle(node.arguments[offset]), base = kind.slice(0, -8), allowed = base === "anchor" ? ["href"] : ["action", "formaction"];
        if (attribute === null) fail(file, node, "dom-attribute");
        else if (allowed.includes(attribute.toLowerCase())) node.arguments[offset + 1] ? record(file, node, base === "anchor" ? "navigation" : "form-action", node.arguments[offset + 1]) : fail(file, node, "dom-attribute");
      } else if (kind === "form-submit") marker(file, node, "form-submit", "@form-submit");
      else if (kind === "anchor-click") marker(file, node, "anchor-click", "@anchor-click");
      else if (kind === MAYBE) fail(file, node, "maybe-navigation");
      else for (const argument of node.arguments) if (isCapability(kindOf(argument, node))) fail(file, argument, "navigation-capability-escape");
    } else if (ts.isBinaryExpression(node)) {
      const left = unwrap(node.left), base = owner(left) ? kindOf(owner(left), node) : NON, name = memberName(left), assignment = node.operatorToken.kind === ts.SyntaxKind.EqualsToken;
      const location = (ts.isIdentifier(left) && libraryGlobal(left, ["location"])) || (["window", "document"].includes(base) && name === "location") || (base === "location" && ["href", "pathname", "search", "hash"].includes(name));
      const form = ["form", "button", "input"].includes(base) && ["action", "formAction"].includes(name), anchor = base === "anchor" && name === "href";
      if (location || form || anchor) assignment ? record(file, node, form ? "form-action" : anchor ? "navigation" : "location", node.right) : fail(file, node, "navigation-write");
      else if (owner(left) && isCapability(base) && name === null) fail(file, node, "computed-navigation-write");
    }
    ts.forEachChild(node, visit);
  }
  visit(file);
}
if (unresolved.length) {
  const counts = Object.fromEntries([...new Set(unresolved.map((item) => item.kind))].sort().map(
    (kind) => [kind, unresolved.filter((item) => item.kind === kind).length],
  ));
  throw new Error(`unresolved reachable navigation target count=${unresolved.length} kinds=${JSON.stringify(counts)} sites=${JSON.stringify(unresolved)}`);
}
sites.sort((a, b) => a.file.localeCompare(b.file) || a.line - b.line || a.kind.localeCompare(b.kind) || a.target.localeCompare(b.target));
console.log(JSON.stringify({ modules: [...discovered.keys()].map(relative).sort(), graph, navigation: sites }));
