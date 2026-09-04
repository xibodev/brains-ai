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
      ["glob", "globEager"].includes(node.expression.name.text)) throw new Error("import.meta glob frontend reachability is unresolved");
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
const hasBoundary = discovered.has(path.join(sourceRoot, "coreRoutes.tsx"));
const program = ts.createProgram({ rootNames: [...discovered.keys()].filter((file) => /\.[cm]?[jt]sx?$/.test(file)), options: parsed.options });
const checker = program.getTypeChecker();
const sourceFiles = [...discovered.keys()].map((file) => program.getSourceFile(file)).filter(Boolean);

const APP = "App.tsx", BOUNDARY = "coreRoutes.tsx";
const READ_ONLY_ROUTER = new Set(["Outlet", "useLocation", "useParams", "useSearchParams"]);
const APP_ROUTER = new Set(["BrowserRouter", "Route", "Routes"]), BOUNDARY_ROUTER = new Set(["NavLink", "useNavigate"]);
const BOUNDARY_EXPORTS = new Set(["CoreHref", "CORE_ROUTE_PATTERNS", "coreHref", "workspaceHref", "configHref", "actHref", "useCoreNavigation", "CoreNavLink", "ExternalLink"]);
const ROUTE_PATTERNS = new Set(["/command-center", "/workspaces", "/workspaces/:slug", "/coordination", "/governance", "/operations", "/operations/config", "/operations/config/:section", "/act", "/inbox", "/config", "*"]);
const CORE_PREFIXES = ["/act", "/command-center", "/config", "/coordination", "/governance", "/inbox", "/operations", "/workspaces"];
const DOM_NAV_MEMBERS = new Map([
  ["Location", null], ["History", null],
  ["HTMLFormElement", new Set(["action", "submit", "requestSubmit", "setAttribute", "setAttributeNS"])],
  ["HTMLButtonElement", new Set(["formAction", "setAttribute", "setAttributeNS"])],
  ["HTMLInputElement", new Set(["formAction", "setAttribute", "setAttributeNS"])],
  ["HTMLAnchorElement", new Set(["href", "click", "setAttribute", "setAttributeNS"])],
  ["HTMLAreaElement", new Set(["href", "click", "setAttribute", "setAttributeNS"])],
]);
const INTRINSIC_TARGETS = new Map([
  ["a", new Set(["href", "xlinkHref"])], ["area", new Set(["href"])], ["iframe", new Set(["src", "srcDoc"])],
  ["object", new Set(["data"])], ["embed", new Set(["src"])], ["base", new Set(["href"])],
  ["meta", new Set(["httpEquiv", "content"])], ["form", new Set(["action"])],
  ["button", new Set(["formAction"])], ["input", new Set(["formAction"])],
]);

function unwrap(node) {
  while (node && (ts.isParenthesizedExpression(node) || ts.isAsExpression(node) || ts.isTypeAssertionExpression(node) || ts.isNonNullExpression(node) || ts.isSatisfiesExpression(node))) node = node.expression;
  return node;
}
function staticLiteral(node) { node = unwrap(node); return node && (ts.isStringLiteralLike(node) || ts.isNumericLiteral(node)) ? node.text : null; }
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
function libraryGlobal(identifier, names) {
  if (!ts.isIdentifier(identifier) || !names.includes(identifier.text)) return false;
  const symbol = checker.getSymbolAtLocation(identifier);
  const declarations = symbol?.declarations || [];
  if (identifier.text === "globalThis" && !declarations.length) return Boolean(symbol && symbol.flags & ts.SymbolFlags.Module);
  return declarations.length > 0 && declarations.every((declaration) => declaration.getSourceFile().isDeclarationFile);
}
function typeNames(node) {
  const found = new Set(), seen = new Set();
  function add(type) {
    if (!type || seen.has(type)) return;
    seen.add(type);
    if (type.symbol?.name) found.add(type.symbol.name);
    if (type.aliasSymbol?.name) found.add(type.aliasSymbol.name);
    for (const part of type.types || []) add(part);
    for (const base of type.getBaseTypes?.() || []) add(base);
  }
  add(checker.getTypeAtLocation(node));
  return found;
}
function importInfo(symbol) {
  if (!symbol) return null;
  for (const declaration of symbol.declarations || []) {
    if (ts.isImportSpecifier(declaration)) return { module: declaration.parent.parent.parent.moduleSpecifier.text, name: declaration.propertyName?.text || declaration.name.text };
    if (ts.isNamespaceImport(declaration)) return { module: declaration.parent.parent.moduleSpecifier.text, name: "*" };
    if (ts.isImportClause(declaration)) return { module: declaration.parent.moduleSpecifier.text, name: "default" };
    if (ts.isExportSpecifier(declaration) && declaration.parent.parent.moduleSpecifier) return { module: declaration.parent.parent.moduleSpecifier.text, name: declaration.propertyName?.text || declaration.name.text };
  }
  if (symbol.flags & ts.SymbolFlags.Alias) {
    const target = checker.getAliasedSymbol(symbol);
    if (target && target !== symbol) return importInfo(target);
  }
  return null;
}

const sites = [], unresolved = [];
const lineOf = (file, node) => file.getLineAndCharacterOfPosition(node.getStart(file)).line + 1;
function fail(file, node, kind) { unresolved.push({ file: `frontend/src/${relative(file.fileName)}`, kind, line: lineOf(file, node) }); }
function marker(file, node, kind, target) { sites.push({ file: `frontend/src/${relative(file.fileName)}`, kind, line: lineOf(file, node), target }); }
function sanitize(candidate) {
  if (!candidate.startsWith("/") || candidate.startsWith("//")) return "/command-center";
  try {
    const parsed = new URL(candidate, "http://brains.invalid");
    if (parsed.origin !== "http://brains.invalid") return "/command-center";
    return CORE_PREFIXES.some((prefix) => parsed.pathname === prefix || parsed.pathname.startsWith(`${prefix}/`)) ? candidate : "/command-center";
  } catch { return "/command-center"; }
}
function staticValues(node) {
  node = unwrap(node);
  if (!node) return null;
  if (ts.isStringLiteralLike(node)) return [node.text];
  if (ts.isConditionalExpression(node)) {
    const yes = staticValues(node.whenTrue), no = staticValues(node.whenFalse);
    return yes && no ? [...new Set([...yes, ...no])] : null;
  }
  if (ts.isCallExpression(node)) {
    const symbol = checker.getSymbolAtLocation(ts.isPropertyAccessExpression(node.expression) ? node.expression.name : node.expression);
    if ((symbol?.declarations || []).some((declaration) => relative(declaration.getSourceFile().fileName) === BOUNDARY)) return ["@core-route-guard"];
  }
  return null;
}
function recordGuarded(file, node, expression, kind = "navigate") {
  const values = staticValues(expression);
  if (!values) return marker(file, node, kind, "@core-route-guard");
  for (const value of values) marker(file, node, kind, value === "@core-route-guard" ? value : sanitize(value));
}
function inspectRouterDeclaration(file, node) {
  if (!node.moduleSpecifier || node.moduleSpecifier.text !== "react-router-dom") return;
  const fileName = relative(file.fileName);
  if (ts.isExportDeclaration(node)) return fail(file, node, "router-reexport");
  const clause = node.importClause;
  if (!clause || clause.name || !clause.namedBindings || ts.isNamespaceImport(clause.namedBindings)) return fail(file, node, "router-import");
  for (const item of clause.namedBindings.elements) {
    if (clause.isTypeOnly || item.isTypeOnly) continue;
    const imported = item.propertyName?.text || item.name.text;
    const allowed = fileName === APP ? APP_ROUTER : fileName === BOUNDARY ? BOUNDARY_ROUTER : READ_ONLY_ROUTER;
    if (!allowed.has(imported) || ((fileName === APP || fileName === BOUNDARY) && item.name.text !== imported)) fail(file, item, "router-import");
  }
}
function intrinsicTag(node) { return ts.isIdentifier(node) && node.text === node.text.toLowerCase() ? node.text : null; }
function inspectIntrinsic(file, node) {
  const tag = intrinsicTag(node.tagName), targets = tag && INTRINSIC_TARGETS.get(tag);
  if (!targets || relative(file.fileName) === BOUNDARY) return;
  const attributes = node.attributes.properties;
  if (attributes.some(ts.isJsxSpreadAttribute) || attributes.some((item) => ts.isJsxAttribute(item) && targets.has(item.name.text))) fail(file, node, "intrinsic-navigation");
}
function reactFactory(node) {
  const expression = unwrap(node.expression);
  if (ts.isIdentifier(expression)) {
    const info = importInfo(checker.getSymbolAtLocation(expression));
    return info?.module === "react" && ["createElement", "cloneElement"].includes(info.name) ? info.name : null;
  }
  if (ts.isPropertyAccessExpression(expression)) {
    const info = importInfo(checker.getSymbolAtLocation(expression.expression));
    return info?.module === "react" && ["*", "default"].includes(info.name) && ["createElement", "cloneElement"].includes(expression.name.text) ? expression.name.text : null;
  }
  return null;
}
function objectHasTargets(node, targets) {
  node = unwrap(node);
  if (!node || node.kind === ts.SyntaxKind.NullKeyword) return false;
  if (!ts.isObjectLiteralExpression(node)) return true;
  return node.properties.some((item) => ts.isSpreadAssignment(item) || targets.has(propertyName(item.name)));
}
function inspectReactFactory(file, node) {
  const factory = reactFactory(node);
  if (!factory || relative(file.fileName) === BOUNDARY) return;
  const all = new Set(["href", "to", "action", "formAction", "src", "srcDoc", "data", "httpEquiv", "content"]);
  if (factory === "cloneElement") {
    if (objectHasTargets(node.arguments[1], all)) fail(file, node, "react-navigation-factory");
    return;
  }
  const component = unwrap(node.arguments[0]);
  const componentInfo = ts.isIdentifier(component) ? importInfo(checker.getSymbolAtLocation(component)) : null;
  if (componentInfo?.module === "react-router-dom" && !READ_ONLY_ROUTER.has(componentInfo.name)) {
    fail(file, node, "react-router-factory");
    return;
  }
  const targets = ts.isStringLiteralLike(component) ? INTRINSIC_TARGETS.get(component.text.toLowerCase()) : null;
  if (targets && objectHasTargets(node.arguments[1], targets)) fail(file, node, "react-navigation-factory");
}
function inspectDom(file, node) {
  if (relative(file.fileName) === BOUNDARY) return;
  if (ts.isIdentifier(node) && libraryGlobal(node, ["location", "history", "open"])) return fail(file, node, "global-navigation");
  if (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) {
    const base = unwrap(node.expression), name = memberName(node), names = typeNames(base);
    const globalWindow = ts.isIdentifier(base) && libraryGlobal(base, ["window", "globalThis", "self", "top", "parent"]);
    if ((globalWindow || names.has("Window")) && (name === null || ["location", "history", "open"].includes(name))) return fail(file, node, "window-navigation");
    if (names.has("Document") && (name === null || ["forms", "write", "writeln"].includes(name))) return fail(file, node, "document-navigation");
    for (const [typeName, members] of DOM_NAV_MEMBERS) if (names.has(typeName) && (members === null || name === null || members.has(name))) return fail(file, node, "dom-navigation");
    const domElement = [...names].some((value) => value === "Element" || value === "HTMLElement" || /^HTML.*Element$/.test(value) || /^SVG.*Element$/.test(value));
    if (domElement && ["innerHTML", "outerHTML"].includes(name)) return fail(file, node, "html-navigation-injection");
  }
  if (ts.isBindingElement(node)) {
    const parent = node.parent?.parent;
    const initializer = ts.isVariableDeclaration(parent) ? parent.initializer : ts.isBinaryExpression(parent) ? parent.right : null;
    if (initializer) {
      const names = typeNames(initializer), key = propertyName(node.propertyName || node.name);
      if ((names.has("Window") && (key === null || ["location", "history", "open"].includes(key))) || [...DOM_NAV_MEMBERS].some(([typeName, members]) => names.has(typeName) && (members === null || key === null || members.has(key)))) fail(file, node, "destructured-navigation");
    }
  }
}
function inspectDynamic(file, node) {
  if (relative(file.fileName) === BOUNDARY) return;
  if (ts.isIdentifier(node) && libraryGlobal(node, ["eval", "Function"])) fail(file, node, "dynamic-code-navigation");
  if (!ts.isCallExpression(node)) return;
  if (memberName(node.expression) === "insertAdjacentHTML") {
    const owner = ts.isPropertyAccessExpression(node.expression) || ts.isElementAccessExpression(node.expression) ? node.expression.expression : null;
    const names = owner ? typeNames(owner) : new Set();
    if ([...names].some((value) => value === "Element" || value === "HTMLElement" || /^HTML.*Element$/.test(value) || /^SVG.*Element$/.test(value))) fail(file, node, "html-navigation-injection");
  }
  const expression = unwrap(node.expression), target = staticLiteral(node.arguments[0]);
  if (ts.isIdentifier(expression) && expression.text === "require" && (target === null || target === "react-router-dom")) fail(file, node, target === null ? "dynamic-require" : "router-require");
  if (expression.kind === ts.SyntaxKind.ImportKeyword && target === "react-router-dom") fail(file, node, "router-dynamic-import");
  if (ts.isPropertyAccessExpression(expression) || ts.isElementAccessExpression(expression)) {
    const owner = expression.expression, ownerTypes = typeNames(owner), name = memberName(expression);
    if (ownerTypes.has("Document") && name === "createElement") {
      const tag = target?.toLowerCase();
      if (!tag || INTRINSIC_TARGETS.has(tag)) fail(file, node, "dom-navigation-construction");
    }
    const element = [...ownerTypes].some((value) => value === "Element" || value === "HTMLElement" || /^HTML.*Element$/.test(value) || /^SVG.*Element$/.test(value));
    if (element && ["setAttribute", "setAttributeNS"].includes(name)) {
      const offset = name === "setAttributeNS" ? 1 : 0;
      const attribute = staticLiteral(node.arguments[offset])?.toLowerCase();
      if (!attribute || ["href", "xlink:href", "action", "formaction", "src", "srcdoc", "data", "http-equiv", "content"].includes(attribute)) fail(file, node, "dom-navigation-attribute");
    }
  }
}
function boundaryDeclaration(symbol, name = null) {
  if (!symbol) return false;
  if (symbol.flags & ts.SymbolFlags.Alias) symbol = checker.getAliasedSymbol(symbol);
  return (symbol?.declarations || []).some((declaration) => relative(declaration.getSourceFile().fileName) === BOUNDARY && (!name || declaration.name?.text === name));
}
function inspectBoundaryUsage(file, node) {
  if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)) {
    const symbol = checker.getSymbolAtLocation(node.expression.name);
    if (boundaryDeclaration(symbol)) {
      if (node.expression.name.text === "open") return node.arguments[0] ? recordGuarded(file, node, node.arguments[0]) : fail(file, node, "core-navigation-call");
      if (node.expression.name.text === "back") return marker(file, node, "history", "@history-delta");
    }
  }
  if (ts.isJsxOpeningLikeElement(node) && boundaryDeclaration(checker.getSymbolAtLocation(node.tagName), "CoreNavLink")) {
    let found = false;
    for (const item of node.attributes.properties) {
      if (ts.isJsxSpreadAttribute(item)) fail(file, item, "core-link-spread");
      else if (item.name.text === "to") {
        found = true;
        const expression = ts.isStringLiteral(item.initializer) ? item.initializer : ts.isJsxExpression(item.initializer) ? item.initializer.expression : null;
        expression ? recordGuarded(file, item, expression, "navigation") : fail(file, item, "core-link-target");
      }
    }
    if (!found) fail(file, node, "core-link-target");
  }
}
function hasExportModifier(node) { return Boolean(node.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword)); }
function inspectBoundaryGrammar(file, node) {
  if (relative(file.fileName) !== BOUNDARY) return;
  if (hasExportModifier(node) && (ts.isFunctionDeclaration(node) || ts.isVariableStatement(node) || ts.isTypeAliasDeclaration(node))) {
    const names = ts.isVariableStatement(node)
      ? node.declarationList.declarations.map((declaration) => ts.isIdentifier(declaration.name) ? declaration.name.text : null)
      : [node.name?.text || null];
    if (names.some((name) => !name || !BOUNDARY_EXPORTS.has(name))) fail(file, node, "boundary-raw-export");
  }
  if (ts.isIdentifier(node)) {
    const info = importInfo(checker.getSymbolAtLocation(node));
    if (info?.module === "react-router-dom" && info.name === "useNavigate" && !ts.isImportSpecifier(node.parent)) {
      const call = node.parent;
      const declaration = call?.parent;
      const valid = ts.isCallExpression(call) && call.expression === node && ts.isVariableDeclaration(declaration) &&
        ts.isIdentifier(declaration.name) && declaration.name.text === "navigate";
      if (!valid) fail(file, node, "boundary-raw-router-use");
    }
  }
  if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === "navigate") {
    const argument = unwrap(node.arguments[0]);
    const guarded = ts.isCallExpression(argument) && ts.isIdentifier(unwrap(argument.expression)) && unwrap(argument.expression).text === "coreHref";
    const back = ts.isPrefixUnaryExpression(argument) && argument.operator === ts.SyntaxKind.MinusToken && staticLiteral(argument.operand) === "1";
    if (node.arguments.length !== 1 || (!guarded && !back)) fail(file, node, "boundary-raw-navigate");
  }
  if (ts.isJsxOpeningLikeElement(node)) {
    if (ts.isIdentifier(node.tagName) && node.tagName.text === "NavLink") {
      const attributes = node.attributes.properties;
      if (attributes.length !== 2 || !ts.isJsxSpreadAttribute(attributes[0]) || !ts.isJsxAttribute(attributes[1]) || attributes[1].name.text !== "to") fail(file, node, "boundary-link-order");
    }
    if (intrinsicTag(node.tagName) === "a") {
      const attributes = node.attributes.properties;
      const names = attributes.map((item) => ts.isJsxSpreadAttribute(item) ? null : item.name.text);
      if (JSON.stringify(names) !== JSON.stringify([null, "href", "target", "rel"])) fail(file, node, "boundary-external-link-order");
    }
  }
}
function inspectApp(file, node, appRoutes) {
  if (!ts.isJsxOpeningLikeElement(node) || !ts.isIdentifier(node.tagName) || node.tagName.text !== "Route") return;
  if (!boundaryDeclaration(checker.getSymbolAtLocation(node.tagName)) && importInfo(checker.getSymbolAtLocation(node.tagName))?.module !== "react-router-dom") return;
  if (node.attributes.properties.some(ts.isJsxSpreadAttribute)) fail(file, node, "route-spread");
  for (const item of node.attributes.properties) if (ts.isJsxAttribute(item) && item.name.text === "path") {
    if (!ts.isStringLiteral(item.initializer)) fail(file, item, "dynamic-route-path");
    else { appRoutes.push(item.initializer.text); marker(file, item, "route", item.initializer.text); }
  }
}

const appRoutes = [];
for (const file of sourceFiles) {
  function visit(node) {
    if (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) inspectRouterDeclaration(file, node);
    if (ts.isJsxOpeningLikeElement(node)) inspectIntrinsic(file, node);
    if (ts.isCallExpression(node)) inspectReactFactory(file, node);
    inspectDom(file, node);
    inspectDynamic(file, node);
    inspectBoundaryUsage(file, node);
    inspectBoundaryGrammar(file, node);
    if (relative(file.fileName) === APP) inspectApp(file, node, appRoutes);
    ts.forEachChild(node, visit);
  }
  visit(file);
}
if (hasBoundary && (new Set(appRoutes).size !== appRoutes.length || appRoutes.some((route) => !ROUTE_PATTERNS.has(route)) || [...ROUTE_PATTERNS].some((route) => !appRoutes.includes(route)))) {
  const appFile = sourceFiles.find((file) => relative(file.fileName) === APP);
  fail(appFile, appFile, "route-registry-mismatch");
}
if (unresolved.length) {
  const counts = Object.fromEntries([...new Set(unresolved.map((item) => item.kind))].sort().map((kind) => [kind, unresolved.filter((item) => item.kind === kind).length]));
  throw new Error(`navigation boundary violation count=${unresolved.length} kinds=${JSON.stringify(counts)} sites=${JSON.stringify(unresolved)}`);
}
sites.sort((a, b) => a.file.localeCompare(b.file) || a.line - b.line || a.kind.localeCompare(b.kind) || a.target.localeCompare(b.target));
console.log(JSON.stringify({ modules: [...discovered.keys()].map(relative).sort(), graph, navigation: sites }));
