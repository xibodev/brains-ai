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
const REACT_MODULES = new Set(["react", "react/jsx-runtime", "react/jsx-dev-runtime"]);
const REVIEWED_REACT_IMPORTS = new Set([
  "StrictMode", "ComponentProps", "CSSProperties", "ReactNode", "RefObject",
  "createContext", "useCallback", "useContext", "useEffect", "useMemo", "useRef", "useState",
]);
const REVIEWED_OBJECT_METHODS = new Set(["entries", "fromEntries", "keys", "values"]);
const READ_ONLY_ROUTER = new Set(["Outlet", "useLocation", "useParams", "useSearchParams"]);
const APP_ROUTER = new Set(["BrowserRouter", "Route", "Routes"]), BOUNDARY_ROUTER = new Set(["NavLink", "useNavigate"]);
const BOUNDARY_EXPORTS = new Set(["CoreHref", "CORE_ROUTE_PATTERNS", "coreHref", "workspaceHref", "configHref", "actHref", "useCoreNavigation", "CoreNavLink", "ExternalLink"]);
const ROUTE_PATTERNS = new Set(["/command-center", "/workspaces", "/workspaces/:slug", "/coordination", "/governance", "/operations", "/operations/config", "/operations/config/:section", "/act", "/inbox", "/config", "*"]);
const CORE_PREFIXES = ["/act", "/command-center", "/config", "/coordination", "/governance", "/inbox", "/operations", "/workspaces"];
const DOM_NAV_MEMBERS = new Map([
  ["Location", null], ["History", null],
  ["Navigation", new Set(["navigate", "reload", "traverseTo", "back", "forward"])],
  ["HTMLFormElement", new Set(["action", "submit", "requestSubmit", "setAttribute", "setAttributeNS"])],
  ["HTMLButtonElement", new Set(["formAction", "setAttribute", "setAttributeNS"])],
  ["HTMLInputElement", new Set(["formAction", "setAttribute", "setAttributeNS"])],
  ["HTMLAnchorElement", new Set(["href", "click", "setAttribute", "setAttributeNS"])],
  ["HTMLAreaElement", new Set(["href", "click", "setAttribute", "setAttributeNS"])],
  ["HTMLFrameElement", new Set(["src", "contentWindow", "contentDocument"])],
  ["HTMLIFrameElement", new Set(["src", "srcdoc", "contentWindow", "contentDocument", "getSVGDocument"])],
  ["HTMLObjectElement", new Set(["data", "contentWindow", "contentDocument", "getSVGDocument"])],
  ["HTMLEmbedElement", new Set(["src", "getSVGDocument"])],
]);
const SAFE_GLOBAL_MEMBERS = new Map([
  ["document", new Set(["activeElement", "addEventListener", "removeEventListener", "baseURI"])],
  ["window", new Set(["addEventListener", "removeEventListener", "setTimeout", "setInterval", "dispatchEvent", "requestAnimationFrame", "cancelAnimationFrame"])],
  ["globalThis", new Set()],
  ["self", new Set()],
  ["top", new Set()],
  ["parent", new Set()],
]);
const SAFE_DOM_LOCALS = new Map([
  ["components/useDialogFocus.ts", new Set(["opener", "dialog", "target", "first", "last"])],
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
function unsafeReactLocalBinding(file, identifier) {
  if (!ts.isIdentifier(identifier)) return false;
  for (const statement of file.statements) {
    if (!ts.isImportDeclaration(statement) || !REACT_MODULES.has(statement.moduleSpecifier.text)) continue;
    const clause = statement.importClause;
    if (!clause) continue;
    if (clause.name?.text === identifier.text) return true;
    if (clause.namedBindings && ts.isNamespaceImport(clause.namedBindings) && clause.namedBindings.name.text === identifier.text) return true;
    if (clause.namedBindings && ts.isNamedImports(clause.namedBindings)) {
      const item = clause.namedBindings.elements.find((candidate) => candidate.name.text === identifier.text);
      if (item) {
        const imported = item.propertyName?.text || item.name.text;
        return statement.moduleSpecifier.text !== "react" || !REVIEWED_REACT_IMPORTS.has(imported);
      }
    }
  }
  return false;
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
    if (!allowed.has(imported) || item.name.text !== imported) fail(file, item, "router-import");
  }
}
function ambientGlobal(identifier, names) {
  if (!ts.isIdentifier(identifier)) return false;
  const parent = identifier.parent;
  const isMemberName = Boolean(parent) && ts.isPropertyAccessExpression(parent) && parent.name === identifier;
  return !isMemberName && names.includes(identifier.text) &&
    (!checker.getSymbolAtLocation(identifier) || libraryGlobal(identifier, names));
}
function inspectFiniteSyntax(file, node) {
  if (ts.isJsxOpeningLikeElement(node) && node.attributes.properties.some(ts.isJsxSpreadAttribute)) {
    fail(file, node, "jsx-attribute-spread");
  }
  if (ts.isImportDeclaration(node)) {
    const module = node.moduleSpecifier.text;
    if (module === "react/jsx-runtime" || module === "react/jsx-dev-runtime") {
      fail(file, node, "react-jsx-runtime");
    }
    if (module === "react" && node.importClause) {
      const clause = node.importClause;
      if (clause.name || (clause.namedBindings && ts.isNamespaceImport(clause.namedBindings))) {
        fail(file, node, "react-factory-acquisition");
      }
      if (clause.namedBindings && ts.isNamedImports(clause.namedBindings)) {
        for (const item of clause.namedBindings.elements) {
          const imported = item.propertyName?.text || item.name.text;
          if (!REVIEWED_REACT_IMPORTS.has(imported) || item.name.text !== imported) {
            fail(file, item, "react-import-outside-reviewed-set");
          }
        }
      }
    }
  }
  if (ts.isExportDeclaration(node)) {
    if (node.moduleSpecifier && REACT_MODULES.has(node.moduleSpecifier.text)) {
      fail(file, node, "react-reexport");
    } else if (!node.moduleSpecifier && node.exportClause && ts.isNamedExports(node.exportClause)) {
      for (const item of node.exportClause.elements) {
        const local = item.propertyName || item.name;
        if (unsafeReactLocalBinding(file, local)) {
          fail(file, item, "react-reexport");
        }
      }
    }
  }
  if (ts.isExportAssignment(node)) {
    const expression = unwrap(node.expression);
    if (ts.isIdentifier(expression) && unsafeReactLocalBinding(file, expression)) {
      fail(file, node, "react-reexport");
    }
  }
  if (ambientGlobal(node, ["eval", "Function", "Proxy", "Reflect", "DOMParser"])) {
    fail(file, node, "dynamic-language-capability");
  }
  if (ts.isIdentifier(node) && libraryGlobal(node, ["Object"])) {
    const member = node.parent;
    const directMember = (ts.isPropertyAccessExpression(member) || ts.isElementAccessExpression(member)) && member.expression === node;
    const directCall = directMember && ts.isCallExpression(member.parent) && member.parent.expression === member;
    if (!directCall || !REVIEWED_OBJECT_METHODS.has(memberName(member))) {
      fail(file, node, "object-meta-outside-reviewed-set");
    }
  }
  if ((ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) &&
      ts.isIdentifier(unwrap(node.expression)) && libraryGlobal(unwrap(node.expression), ["Object"]) &&
      memberName(node) === "defineProperty") {
    fail(file, node, "dynamic-language-capability");
  }
  if (ts.isIdentifier(node) && ambientGlobal(node, ["setTimeout", "setInterval"])) {
    const parent = node.parent;
    if (!ts.isCallExpression(parent) || parent.expression !== node || !validTimerCall(parent)) {
      fail(file, node, "unsafe-timer");
    }
  }
  if (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) {
    const base = unwrap(node.expression), name = memberName(node);
    const ambientObject = ts.isIdentifier(base) && libraryGlobal(base, ["Object"]);
    const ambientWindow = ts.isIdentifier(base) && libraryGlobal(base, ["window"]);
    if (ambientObject) {
      const directCall = ts.isCallExpression(node.parent) && node.parent.expression === node;
      if (!REVIEWED_OBJECT_METHODS.has(name) || !directCall) fail(file, node, "object-meta-outside-reviewed-set");
    }
    if (name === "constructor" && constructorRisk(base, false)) {
      fail(file, node, "dynamic-constructor");
    }
    if (ts.isElementAccessExpression(node) && name === null && constructorRisk(base, true) && !safeComputedData(base, node.argumentExpression)) {
      fail(file, node, "dynamic-constructor");
    }
    if (["setTimeout", "setInterval"].includes(name) && (ambientWindow || typeNames(base).has("Window"))) {
      const parent = node.parent;
      if (!ts.isCallExpression(parent) || parent.expression !== node || !validTimerCall(parent)) {
        fail(file, node, "unsafe-timer");
      }
    }
  }
  if (ts.isMetaProperty(node) && node.keywordToken === ts.SyntaxKind.ImportKeyword) {
    fail(file, node, "import-meta");
  }
  if (ts.isCallExpression(node)) {
    const expression = unwrap(node.expression);
    if (expression.kind === ts.SyntaxKind.ImportKeyword) fail(file, node, "dynamic-import");
    if (ts.isIdentifier(expression) && expression.text === "require") fail(file, node, "dynamic-require");
  }
}
function intrinsicTag(node) { return ts.isIdentifier(node) && node.text === node.text.toLowerCase() ? node.text : null; }
function stringTypeValues(node) {
  const type = checker.getTypeAtLocation(node);
  const parts = type.isUnion() ? type.types : [type];
  const values = [];
  for (const part of parts) {
    if (part.flags & ts.TypeFlags.StringLiteral) values.push(part.value);
    else if (part.flags & (ts.TypeFlags.Any | ts.TypeFlags.Unknown | ts.TypeFlags.String)) return null;
  }
  return values;
}
function isDomCapability(node) {
  const seen = new Set();
  function visit(type) {
    if (!type || seen.has(type)) return false;
    seen.add(type);
    const symbol = type.symbol || type.aliasSymbol;
    const name = symbol?.name || "";
    const capabilityName = name === "Window" || name === "Document" || DOM_NAV_MEMBERS.has(name) ||
      ["DOMImplementation", "DOMParser", "DocumentFragment", "Element", "HTMLElement", "Range"].includes(name) ||
      /^HTML.*Element$/.test(name) || /^SVG.*Element$/.test(name);
    const domDeclaration = (symbol?.declarations || []).some((declaration) => declaration.getSourceFile().fileName.replaceAll("\\", "/").endsWith("/lib.dom.d.ts"));
    if (capabilityName && domDeclaration) return true;
    return (type.types || []).some(visit) || (type.getBaseTypes?.() || []).some(visit);
  }
  return visit(checker.getTypeAtLocation(node));
}
function safeDomLocal(file, node) {
  return ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) &&
    SAFE_DOM_LOCALS.get(relative(file.fileName))?.has(node.name.text);
}
function safeDomArgument(file, call, argument, index) {
  if (relative(file.fileName) !== "main.tsx" || index !== 0) return false;
  const expression = unwrap(call.expression);
  const info = ts.isIdentifier(expression) ? importInfo(checker.getSymbolAtLocation(expression)) : null;
  if (info?.module !== "react-dom/client" || info.name !== "createRoot") return false;
  const source = unwrap(argument);
  if (!ts.isCallExpression(source)) return false;
  const receiver = unwrap(source.expression);
  return (ts.isPropertyAccessExpression(receiver) || ts.isElementAccessExpression(receiver)) &&
    ts.isIdentifier(unwrap(receiver.expression)) && libraryGlobal(unwrap(receiver.expression), ["document"]) &&
    memberName(receiver) === "getElementById";
}
function safeMountRoot(file, node) {
  if (relative(file.fileName) !== "main.tsx" || node.text !== "document") return false;
  const member = node.parent;
  if (!ts.isPropertyAccessExpression(member) || member.expression !== node || member.name.text !== "getElementById") return false;
  const sourceCall = member.parent;
  if (!ts.isCallExpression(sourceCall) || sourceCall.expression !== member) return false;
  let argument = sourceCall;
  while (argument.parent && [ts.SyntaxKind.NonNullExpression, ts.SyntaxKind.ParenthesizedExpression, ts.SyntaxKind.AsExpression, ts.SyntaxKind.TypeAssertionExpression].includes(argument.parent.kind)) argument = argument.parent;
  const outer = argument.parent;
  return ts.isCallExpression(outer) && outer.arguments.some((item, index) => item === argument && safeDomArgument(file, outer, item, index));
}
function staticallyCallable(node) {
  node = unwrap(node);
  if (!node) return false;
  if (ts.isArrowFunction(node) || ts.isFunctionExpression(node)) return true;
  const type = checker.getTypeAtLocation(node);
  return !(type.flags & (ts.TypeFlags.Any | ts.TypeFlags.Unknown | ts.TypeFlags.StringLike)) && type.getCallSignatures().length > 0;
}
function validTimerCall(node) {
  return ts.isCallExpression(node) && node.arguments.length > 0 && staticallyCallable(node.arguments[0]);
}
function safeWindowDispatch(file, node) {
  if (relative(file.fileName) !== "components/TopBar.tsx" || !ts.isPropertyAccessExpression(node) || node.name.text !== "dispatchEvent") return false;
  const base = unwrap(node.expression);
  if (!ts.isIdentifier(base) || !libraryGlobal(base, ["window"])) return false;
  const call = node.parent;
  if (!ts.isCallExpression(call) || call.expression !== node || call.arguments.length !== 1) return false;
  const event = unwrap(call.arguments[0]);
  if (!ts.isNewExpression(event) || event.arguments?.length !== 1 || staticLiteral(event.arguments[0]) !== "brains:open-command-palette") return false;
  return ts.isIdentifier(event.expression) && libraryGlobal(event.expression, ["Event"]);
}
function constructorRisk(node, unresolved) {
  const type = checker.getTypeAtLocation(node);
  if (type.flags & (ts.TypeFlags.Any | ts.TypeFlags.Unknown)) return true;
  if (type.getCallSignatures().length > 0) return true;
  if (!(type.flags & ts.TypeFlags.Object)) return false;
  if (!unresolved) return true;
  return !type.getStringIndexType() && !type.getNumberIndexType();
}
function safeComputedData(base, argument) {
  const type = checker.getTypeAtLocation(base);
  if (type.getStringIndexType() || type.getNumberIndexType()) return true;
  const values = stringTypeValues(argument);
  const properties = new Set(type.getProperties().map((property) => property.name));
  return Boolean(values?.length) && values.every((value) => value !== "constructor" && properties.has(value));
}
function intrinsicCandidates(node) {
  const direct = intrinsicTag(node);
  if (direct) return { unknown: false, tags: [direct] };
  const values = stringTypeValues(node);
  return values === null
    ? { unknown: true, tags: [] }
    : { unknown: false, tags: values.map((value) => value.toLowerCase()) };
}
function inspectIntrinsic(file, node) {
  const attributes = node.attributes.properties;
  if (attributes.some((item) => ts.isJsxAttribute(item) && item.name.text === "dangerouslySetInnerHTML")) {
    fail(file, node, "dangerous-html");
  }
  if (relative(file.fileName) === BOUNDARY) return;
  const candidate = intrinsicCandidates(node.tagName);
  const targets = new Set(candidate.tags.flatMap((tag) => [...(INTRINSIC_TARGETS.get(tag) || [])]));
  if ((candidate.unknown && (attributes.some(ts.isJsxSpreadAttribute) || attributes.some((item) => ts.isJsxAttribute(item) && [...INTRINSIC_TARGETS.values()].some((names) => names.has(item.name.text))))) ||
      (targets.size && (attributes.some(ts.isJsxSpreadAttribute) || attributes.some((item) => ts.isJsxAttribute(item) && targets.has(item.name.text))))) {
    fail(file, node, "intrinsic-navigation");
  }
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
  if (!factory) return;
  if (node.arguments.length > 1 && objectHasTargets(node.arguments[1], new Set(["dangerouslySetInnerHTML"]))) {
    fail(file, node, "dangerous-html");
  }
  if (relative(file.fileName) === BOUNDARY) return;
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
  const candidate = intrinsicCandidates(component);
  const targets = new Set(candidate.tags.flatMap((tag) => [...(INTRINSIC_TARGETS.get(tag) || [])]));
  if ((candidate.unknown && objectHasTargets(node.arguments[1], all)) || (targets.size && objectHasTargets(node.arguments[1], targets))) fail(file, node, "react-navigation-factory");
}
function inspectDom(file, node) {
  if (relative(file.fileName) === BOUNDARY) return;
  if (ts.isIdentifier(node) && libraryGlobal(node, ["location", "history", "open", "navigation", "frames", "opener", "frameElement"])) return fail(file, node, "global-navigation");
  if (ts.isIdentifier(node) && libraryGlobal(node, [...SAFE_GLOBAL_MEMBERS.keys()])) {
    const parent = node.parent;
    const safe = ts.isPropertyAccessExpression(parent) && parent.expression === node &&
      SAFE_GLOBAL_MEMBERS.get(node.text)?.has(parent.name.text);
    if (!safe && !safeMountRoot(file, node)) return fail(file, node, "global-dom-root-escape");
  }
  if (ts.isAsExpression(node) || ts.isTypeAssertionExpression(node)) {
    const target = checker.getTypeAtLocation(node);
    if (isDomCapability(node.expression) && target.flags & (ts.TypeFlags.Any | ts.TypeFlags.Unknown)) return fail(file, node, "dom-capability-cast");
  }
  if ((ts.isVariableDeclaration(node) || ts.isParameter(node) || ts.isPropertyDeclaration(node)) && node.initializer && isDomCapability(node.initializer)) {
    if (!safeDomLocal(file, node)) return fail(file, node, "dom-capability-alias");
  }
  if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken && isDomCapability(node.right)) {
    return fail(file, node, "dom-capability-assignment");
  }
  if (ts.isPropertyAssignment(node) && isDomCapability(node.initializer)) {
    return fail(file, node, "dom-capability-store");
  }
  if (ts.isShorthandPropertyAssignment(node) && isDomCapability(node.name)) {
    return fail(file, node, "dom-capability-store");
  }
  if (ts.isArrayLiteralExpression(node)) {
    for (const item of node.elements) if (isDomCapability(item)) return fail(file, item, "dom-capability-store");
  }
  if (ts.isCallExpression(node)) {
    node.arguments.forEach((argument, index) => {
      if (isDomCapability(argument) && !safeDomArgument(file, node, argument, index)) fail(file, argument, "dom-capability-argument");
    });
  }
  if (ts.isReturnStatement(node) && node.expression && isDomCapability(node.expression)) {
    return fail(file, node, "dom-capability-return");
  }
  if (ts.isYieldExpression(node) && node.expression && isDomCapability(node.expression)) {
    return fail(file, node, "dom-capability-yield");
  }
  if (ts.isArrowFunction(node) && !ts.isBlock(node.body) && isDomCapability(node.body)) {
    return fail(file, node, "dom-capability-return");
  }
  if (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) {
    const base = unwrap(node.expression), name = memberName(node), names = typeNames(base);
    const baseType = checker.getTypeAtLocation(base);
    const erasedBase = Boolean(baseType.flags & (ts.TypeFlags.Any | ts.TypeFlags.Unknown));
    const globalWindow = ts.isIdentifier(base) && libraryGlobal(base, ["window", "globalThis", "self", "top", "parent"]);
    if (ts.isElementAccessExpression(node) && name === null && (isDomCapability(base) || erasedBase)) return fail(file, node, "computed-dom-member");
    if (["innerHTML", "outerHTML", "insertAdjacentHTML"].includes(name) && (isDomCapability(base) || erasedBase)) return fail(file, node, "html-navigation-injection");
    if (["append", "prepend", "replaceChildren", "insertAdjacentElement", "createContextualFragment", "createHTMLDocument", "parseFromString"].includes(name) && (isDomCapability(base) || erasedBase)) return fail(file, node, "dom-markup-construction");
    if (["click", "submit", "requestSubmit", "dispatchEvent"].includes(name) && (isDomCapability(base) || erasedBase) && !safeWindowDispatch(file, node)) return fail(file, node, "dom-programmatic-activation");
    if (["setAttribute", "setAttributeNS", "removeAttribute", "removeAttributeNS", "toggleAttribute"].includes(name) && (isDomCapability(base) || erasedBase)) return fail(file, node, "dom-attribute-mutation");
    if ((globalWindow || names.has("Window")) && (name === null || ["location", "history", "open", "navigation", "frames", "opener", "frameElement", "parent", "top", "self"].includes(name))) return fail(file, node, "window-navigation");
    if (names.has("Document") && (name === null || ["forms", "location", "write", "writeln"].includes(name))) return fail(file, node, "document-navigation");
    for (const [typeName, members] of DOM_NAV_MEMBERS) if (names.has(typeName) && (members === null || name === null || members.has(name))) return fail(file, node, "dom-navigation");
  }
  if (ts.isBindingElement(node)) {
    const parent = node.parent?.parent;
    const initializer = ts.isVariableDeclaration(parent) ? parent.initializer : ts.isBinaryExpression(parent) ? parent.right : null;
    if (initializer) {
      const names = typeNames(initializer), key = propertyName(node.propertyName || node.name);
      if (isDomCapability(initializer)) return fail(file, node, "dom-capability-destructure");
      if ((names.has("Window") && (key === null || ["location", "history", "open"].includes(key))) || [...DOM_NAV_MEMBERS].some(([typeName, members]) => names.has(typeName) && (members === null || key === null || members.has(key)))) fail(file, node, "destructured-navigation");
    }
  }
}
function inspectDynamic(file, node) {
  if (relative(file.fileName) === BOUNDARY) return;
  if (ts.isIdentifier(node) && libraryGlobal(node, ["eval", "Function"])) fail(file, node, "dynamic-code-navigation");
  if (!ts.isCallExpression(node)) return;
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
      } else if (!["key", "className", "title", "ariaLabel"].includes(item.name.text)) fail(file, item, "core-link-prop");
    }
    if (!found) fail(file, node, "core-link-target");
  }
  if (ts.isJsxOpeningLikeElement(node) && boundaryDeclaration(checker.getSymbolAtLocation(node.tagName), "ExternalLink")) {
    let found = false;
    for (const item of node.attributes.properties) {
      if (ts.isJsxSpreadAttribute(item)) fail(file, item, "external-link-spread");
      else if (item.name.text === "href") found = true;
      else if (!["className", "title", "ariaLabel"].includes(item.name.text)) fail(file, item, "external-link-prop");
    }
    if (!found) fail(file, node, "external-link-target");
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
      const names = attributes.map((item) => ts.isJsxSpreadAttribute(item) ? null : item.name.text);
      if (JSON.stringify(names) !== JSON.stringify(["to", "className", "title", "aria-label"])) fail(file, node, "boundary-link-grammar");
    }
    if (intrinsicTag(node.tagName) === "a") {
      const attributes = node.attributes.properties;
      const names = attributes.map((item) => ts.isJsxSpreadAttribute(item) ? null : item.name.text);
      if (JSON.stringify(names) !== JSON.stringify(["href", "target", "rel", "className", "title", "aria-label"])) fail(file, node, "boundary-external-link-grammar");
    }
    if (intrinsicTag(node.tagName) === "span") {
      const attributes = node.attributes.properties;
      const names = attributes.map((item) => ts.isJsxSpreadAttribute(item) ? null : item.name.text);
      if (JSON.stringify(names) !== JSON.stringify(["className", "title", "aria-label"])) fail(file, node, "boundary-fallback-grammar");
    }
  }
}
function inspectAppGrammar(file, node) {
  if (relative(file.fileName) !== APP || !ts.isIdentifier(node)) return;
  const info = importInfo(checker.getSymbolAtLocation(node));
  if (info?.module !== "react-router-dom" || !APP_ROUTER.has(info.name)) return;
  const parent = node.parent;
  const directImport = ts.isImportSpecifier(parent) && parent.name === node && !parent.propertyName && node.text === info.name;
  const directTag = ((ts.isJsxOpeningLikeElement(parent) || ts.isJsxClosingElement(parent)) && parent.tagName === node && node.text === info.name);
  if (!directImport && !directTag) fail(file, node, "app-router-alias");
}
function appRouterTag(node, name) {
  return Boolean(node) && ts.isIdentifier(node) && node.text === name &&
    importInfo(checker.getSymbolAtLocation(node))?.module === "react-router-dom";
}
function exactElementAttribute(item) {
  return Boolean(item) && ts.isJsxAttribute(item) && item.name.text === "element" &&
    ts.isJsxExpression(item.initializer) && ts.isJsxSelfClosingElement(item.initializer.expression);
}
function inspectApp(file, node, appRoutes) {
  if (!ts.isJsxOpeningLikeElement(node) || !ts.isIdentifier(node.tagName) || node.tagName.text !== "Route") return;
  if (importInfo(checker.getSymbolAtLocation(node.tagName))?.module !== "react-router-dom") return;
  const attributes = node.attributes.properties;
  const names = attributes.map((item) => ts.isJsxSpreadAttribute(item) ? null : item.name.text);
  const element = ts.isJsxOpeningElement(node) && ts.isJsxElement(node.parent) ? node.parent : node;
  const parent = element.parent;
  const parentTag = ts.isJsxElement(parent) ? parent.openingElement.tagName : null;
  const outer = JSON.stringify(names) === JSON.stringify(["element"]);
  const index = JSON.stringify(names) === JSON.stringify(["index", "element"]);
  const path = JSON.stringify(names) === JSON.stringify(["path", "element"]);
  const correctParent = outer ? appRouterTag(parentTag, "Routes") : (index || path) ? appRouterTag(parentTag, "Route") : false;
  const correctShape = outer ? ts.isJsxOpeningElement(node) : ts.isJsxSelfClosingElement(node);
  const elementAttribute = attributes.find((item) => ts.isJsxAttribute(item) && item.name.text === "element");
  const indexAttribute = attributes.find((item) => ts.isJsxAttribute(item) && item.name.text === "index");
  if (!correctParent || !correctShape || !exactElementAttribute(elementAttribute) || (index && indexAttribute.initializer)) {
    fail(file, node, "route-declaration-grammar");
  }
  for (const item of attributes) if (ts.isJsxAttribute(item) && item.name.text === "path") {
    if (!ts.isStringLiteral(item.initializer)) fail(file, item, "dynamic-route-path");
    else { appRoutes.push(item.initializer.text); marker(file, item, "route", item.initializer.text); }
  }
}

const appRoutes = [];
for (const file of sourceFiles) {
  function visit(node) {
    inspectFiniteSyntax(file, node);
    if (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) inspectRouterDeclaration(file, node);
    if (ts.isJsxOpeningLikeElement(node)) inspectIntrinsic(file, node);
    if (ts.isCallExpression(node)) inspectReactFactory(file, node);
    inspectDom(file, node);
    inspectDynamic(file, node);
    inspectBoundaryUsage(file, node);
    inspectBoundaryGrammar(file, node);
    inspectAppGrammar(file, node);
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
