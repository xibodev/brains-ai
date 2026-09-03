import fs from "node:fs";
import path from "node:path";
import ts from "../frontend/node_modules/typescript/lib/typescript.js";

const sourceRoot = path.resolve(process.argv[2] || "frontend/src");
const frontendRoot = path.dirname(sourceRoot);
const configPath = path.join(frontendRoot, "tsconfig.json");
const config = ts.readConfigFile(configPath, ts.sys.readFile);
if (config.error) throw new Error("frontend tsconfig is unreadable");
const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, frontendRoot);
if (parsed.errors.length) throw new Error("frontend tsconfig is invalid");
const aliasPatterns = Object.keys(parsed.options.paths || {}).map((pattern) => pattern.split("*", 1)[0]);

const extensions = ["", ".ts", ".tsx", ".js", ".jsx", ".css"];
const relative = (file) => path.relative(sourceRoot, file).split(path.sep).join("/");
const withinRoot = (file) => file === sourceRoot || file.startsWith(`${sourceRoot}${path.sep}`);

function resolveLocal(specifier, importer) {
  const resolved = ts.resolveModuleName(specifier, importer, parsed.options, ts.sys).resolvedModule;
  if (resolved && withinRoot(path.resolve(resolved.resolvedFileName))) {
    return path.resolve(resolved.resolvedFileName);
  }
  if (!specifier.startsWith(".")) {
    if (aliasPatterns.some((prefix) => specifier.startsWith(prefix))) {
      throw new Error("configured frontend alias could not be resolved");
    }
    return null;
  }
  const base = path.resolve(path.dirname(importer), specifier);
  const candidates = [
    ...extensions.map((extension) => `${base}${extension}`),
    ...extensions.slice(1).map((extension) => path.join(base, `index${extension}`)),
  ];
  for (const candidate of candidates) {
    if (withinRoot(candidate) && fs.existsSync(candidate) && fs.statSync(candidate).isFile()) return candidate;
  }
  throw new Error("relative frontend import could not be resolved");
}

function moduleSpecifiers(sourceFile) {
  const found = [];
  function visit(node) {
    if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier) {
      found.push(node.moduleSpecifier.text);
    } else if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword) {
      if (node.arguments.length !== 1 || !ts.isStringLiteralLike(node.arguments[0])) {
        throw new Error("dynamic frontend import target is unresolved");
      }
      found.push(node.arguments[0].text);
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return found;
}

const entry = path.join(sourceRoot, "main.tsx");
if (!fs.existsSync(entry)) throw new Error("frontend entry main.tsx is unavailable");
const pending = [entry];
const files = new Map();
const graph = {};
while (pending.length) {
  const file = path.resolve(pending.pop());
  if (files.has(file)) continue;
  const text = fs.readFileSync(file, "utf8");
  const kind = file.endsWith(".tsx") ? ts.ScriptKind.TSX : file.endsWith(".ts") ? ts.ScriptKind.TS : ts.ScriptKind.Unknown;
  const sourceFile = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, kind);
  files.set(file, sourceFile);
  const dependencies = [];
  for (const specifier of moduleSpecifiers(sourceFile)) {
    const dependency = resolveLocal(specifier, file);
    if (dependency) {
      dependencies.push(dependency);
      pending.push(dependency);
    }
  }
  graph[relative(file)] = [...new Set(dependencies.map(relative))].sort();
}
if (!files.has(path.join(sourceRoot, "App.tsx"))) throw new Error("frontend entry does not reach App.tsx");

function unwrap(node) {
  if (!node) return node;
  while (
    ts.isParenthesizedExpression(node) ||
    ts.isAsExpression(node) ||
    ts.isTypeAssertionExpression(node) ||
    ts.isNonNullExpression(node) ||
    ts.isSatisfiesExpression(node)
  ) node = node.expression;
  return node;
}

function staticValues(node, seen = new Set(), coreRouteAliases = new Set()) {
  node = unwrap(node);
  if (ts.isStringLiteralLike(node)) return [node.text];
  if (ts.isTemplateExpression(node)) {
    let values = [node.head.text];
    for (const span of node.templateSpans) {
      const expressionValues = staticValues(span.expression, seen, coreRouteAliases);
      const substitutions = expressionValues.length ? expressionValues : ["${dynamic}"];
      values = values.flatMap((prefix) => substitutions.map((value) => `${prefix}${value}${span.literal.text}`));
    }
    return values;
  }
  if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
    const left = staticValues(node.left, seen, coreRouteAliases);
    const right = staticValues(node.right, seen, coreRouteAliases);
    const leftValues = left.length ? left : ["${dynamic}"];
    const rightValues = right.length ? right : ["${dynamic}"];
    return leftValues.flatMap((lhs) => rightValues.map((rhs) => `${lhs}${rhs}`));
  }
  if (ts.isConditionalExpression(node)) {
    return [...new Set([...staticValues(node.whenTrue, seen, coreRouteAliases), ...staticValues(node.whenFalse, seen, coreRouteAliases)])];
  }
  if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && coreRouteAliases.has(node.expression.text)) {
    return ["@core-route-guard"];
  }
  if (ts.isIdentifier(node) && !seen.has(node.text)) {
    const nextSeen = new Set(seen).add(node.text);
    const declaration = node.getSourceFile().statements.find(
      (statement) =>
        ts.isVariableStatement(statement) &&
        statement.declarationList.declarations.some(
          (candidate) => ts.isIdentifier(candidate.name) && candidate.name.text === node.text && candidate.initializer,
        ),
    );
    if (declaration) {
      const candidate = declaration.declarationList.declarations.find(
        (item) => ts.isIdentifier(item.name) && item.name.text === node.text,
      );
      if (candidate?.initializer) return staticValues(candidate.initializer, nextSeen, coreRouteAliases);
    }
  }
  return [];
}

function jsxTag(node) {
  return ts.isIdentifier(node.tagName) ? node.tagName.text : node.tagName.getText();
}

const sites = [];
const unresolved = [];
function record(sourceFile, node, kind, expression, coreRouteAliases) {
  const values = staticValues(expression, new Set(), coreRouteAliases);
  const line = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
  if (!values.length || values.some((value) => value.startsWith("${dynamic}"))) {
    unresolved.push({ file: `frontend/src/${relative(sourceFile.fileName)}`, kind, line });
    return;
  }
  for (const target of values) sites.push({ file: `frontend/src/${relative(sourceFile.fileName)}`, kind, line, target });
}

function isHistoryDelta(node) {
  node = unwrap(node);
  if (ts.isNumericLiteral(node)) return true;
  return ts.isPrefixUnaryExpression(node) &&
    [ts.SyntaxKind.PlusToken, ts.SyntaxKind.MinusToken].includes(node.operator) &&
    ts.isNumericLiteral(node.operand);
}

for (const [file, sourceFile] of files) {
  if (!file.endsWith(".ts") && !file.endsWith(".tsx")) continue;
  const routerAliases = new Map([["Route", "Route"], ["Navigate", "Navigate"], ["Link", "Link"], ["NavLink", "NavLink"]]);
  const routerImportedAliases = new Set(routerAliases.keys());
  const routerNamespaces = new Set();
  const navigateFactories = new Set(["useNavigate"]);
  const navigateAliases = new Set(["navigate", "redirect"]);
  const coreRouteAliases = new Set();
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement)) continue;
    const specifier = statement.moduleSpecifier.text;
    const bindings = statement.importClause?.namedBindings;
    if (specifier === "react-router-dom" && bindings && ts.isNamespaceImport(bindings)) {
      routerNamespaces.add(bindings.name.text);
    }
    for (const element of statement.importClause?.namedBindings?.elements || []) {
      const imported = element.propertyName?.text || element.name.text;
      if (specifier === "react-router-dom") {
        routerImportedAliases.add(element.name.text);
        if (["Route", "Navigate", "Link", "NavLink"].includes(imported)) routerAliases.set(element.name.text, imported);
        if (imported === "useNavigate") navigateFactories.add(element.name.text);
        if (imported === "redirect") navigateAliases.add(element.name.text);
      }
      const importedFile = resolveLocal(specifier, file);
      if (imported === "coreRoute" && importedFile && relative(importedFile) === "coreRoutes.ts") {
        coreRouteAliases.add(element.name.text);
      }
    }
  }
  const navigateFunctions = new Set(navigateAliases);
  const openFunctions = new Set(["open"]);
  const locationAliases = new Set(["location"]);
  const locationFunctions = new Set();
  const historyAliases = new Set(["history"]);
  const historyFunctions = new Set();
  const formActivatorAliases = new Set();
  const memberName = (node) => {
    if (!node) return null;
    if (ts.isPropertyAccessExpression(node)) return node.name.text;
    if (ts.isElementAccessExpression(node) && node.argumentExpression && ts.isStringLiteral(unwrap(node.argumentExpression))) {
      return unwrap(node.argumentExpression).text;
    }
    return null;
  };
  const memberOwner = (node) =>
    node && (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) ? node.expression : null;
  const isGlobalObject = (node) => {
    node = unwrap(node);
    return Boolean(node) && ts.isIdentifier(node) && ["window", "globalThis"].includes(node.text);
  };
  const isLocationObject = (node) => {
    node = unwrap(node);
    if (!node) return false;
    if (ts.isIdentifier(node)) return locationAliases.has(node.text);
    const owner = memberOwner(node);
    return owner &&
      ts.isIdentifier(unwrap(owner)) &&
      ["window", "globalThis", "document"].includes(unwrap(owner).text) &&
      memberName(node) === "location";
  };
  const isHistoryObject = (node) => {
    node = unwrap(node);
    if (!node) return false;
    if (ts.isIdentifier(node)) return historyAliases.has(node.text);
    const owner = memberOwner(node);
    return owner && ts.isIdentifier(owner) && ["window", "globalThis"].includes(owner.text) && memberName(node) === "history";
  };
  const namespacedRouterMember = (node) =>
    node && ts.isPropertyAccessExpression(node) && ts.isIdentifier(node.expression) &&
    routerNamespaces.has(node.expression.text) ? node.name.text : null;
  const isNavigateFunction = (node) => {
    node = unwrap(node);
    if (!node) return false;
    return (ts.isIdentifier(node) && navigateFunctions.has(node.text)) ||
      namespacedRouterMember(node) === "redirect";
  };
  const isOpenFunction = (node) => {
    node = unwrap(node);
    if (!node) return false;
    return (ts.isIdentifier(node) && openFunctions.has(node.text)) ||
      (memberName(node) === "open" && isGlobalObject(memberOwner(node)));
  };
  const isLocationFunction = (node) => {
    node = unwrap(node);
    if (!node) return false;
    return (ts.isIdentifier(node) && locationFunctions.has(node.text)) ||
      (["assign", "replace"].includes(memberName(node)) && isLocationObject(memberOwner(node)));
  };
  const isDocumentMember = (node, name) => {
    node = unwrap(node);
    if (!node) return false;
    const owner = memberOwner(node);
    return memberName(node) === name && owner &&
      ts.isIdentifier(unwrap(owner)) && unwrap(owner).text === "document";
  };
  const isFormActivatorObject = (node) => {
    node = unwrap(node);
    if (!node) return false;
    if (ts.isIdentifier(node)) return formActivatorAliases.has(node.text);
    if (ts.isCallExpression(node) && isDocumentMember(node.expression, "createElement")) {
      const tag = node.arguments[0] && staticValues(node.arguments[0]);
      return tag?.length === 1 && ["form", "button", "input"].includes(tag[0].toLowerCase());
    }
    if (ts.isCallExpression(node) && isDocumentMember(node.expression, "querySelector")) {
      const selectors = node.arguments[0] && staticValues(node.arguments[0]);
      if (selectors?.length !== 1) return false;
      const selector = selectors[0].toLowerCase();
      return ["form", "button", "input"].some((tag) =>
        selector === tag || [".", "#", "[", ":"].some((suffix) => selector.startsWith(`${tag}${suffix}`)));
    }
    if (memberName(node) === "forms") return isDocumentMember(node, "forms");
    const owner = memberOwner(node);
    if (owner && isFormActivatorObject(owner)) return true;
    return false;
  };
  const propertyName = (node) => {
    if (ts.isIdentifier(node) || ts.isStringLiteralLike(node) || ts.isNumericLiteral(node)) return node.text;
    if (ts.isComputedPropertyName(node) && ts.isStringLiteralLike(unwrap(node.expression))) {
      return unwrap(node.expression).text;
    }
    return null;
  };
  const objectValue = (node, key) => {
    node = unwrap(node);
    if (!ts.isObjectLiteralExpression(node)) return null;
    for (const property of node.properties) {
      if (ts.isShorthandPropertyAssignment(property) && property.name.text === key) return property.name;
      if (ts.isPropertyAssignment(property) && propertyName(property.name) === key) return property.initializer;
    }
    return null;
  };
  const addAlias = (target, expression, aliases, recognizes) => {
    if (!ts.isIdentifier(target) || !expression || !recognizes(expression)) return;
    aliases.add(target.text);
  };
  const propagateBinding = (binding, expression, aliases, recognizes) => {
    expression = expression && unwrap(expression);
    if (!expression) return;
    if (ts.isIdentifier(binding)) {
      addAlias(binding, expression, aliases, recognizes);
      return;
    }
    if (ts.isObjectBindingPattern(binding)) {
      for (const element of binding.elements) {
        if (!ts.isIdentifier(element.name)) continue;
        const key = element.propertyName?.getText(sourceFile) || element.name.text;
        propagateBinding(element.name, objectValue(expression, key), aliases, recognizes);
      }
    } else if (ts.isArrayBindingPattern(binding) && ts.isArrayLiteralExpression(expression)) {
      binding.elements.forEach((element, index) => {
        if (ts.isBindingElement(element)) {
          propagateBinding(element.name, expression.elements[index], aliases, recognizes);
        }
      });
    }
  };
  const propagateAssignment = (target, expression, aliases, recognizes) => {
    target = unwrap(target);
    expression = unwrap(expression);
    if (!target || !expression) return;
    if (ts.isIdentifier(target)) {
      addAlias(target, expression, aliases, recognizes);
      return;
    }
    if (ts.isArrayLiteralExpression(target) && ts.isArrayLiteralExpression(expression)) {
      target.elements.forEach((element, index) =>
        propagateAssignment(element, expression.elements[index], aliases, recognizes));
    } else if (ts.isObjectLiteralExpression(target)) {
      for (const property of target.properties) {
        if (ts.isShorthandPropertyAssignment(property)) {
          propagateAssignment(property.name, objectValue(expression, property.name.text), aliases, recognizes);
        } else if (ts.isPropertyAssignment(property) && ts.isIdentifier(property.initializer)) {
          const key = propertyName(property.name);
          if (key === null) continue;
          propagateAssignment(property.initializer, objectValue(expression, key), aliases, recognizes);
        }
      }
    }
  };
  function discover(node) {
    if (
      ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer &&
      ts.isIdentifier(unwrap(node.initializer)) && routerNamespaces.has(unwrap(node.initializer).text)
    ) routerNamespaces.add(node.name.text);
    if (
      ts.isVariableDeclaration(node) && ts.isObjectBindingPattern(node.name) && node.initializer &&
      ts.isIdentifier(unwrap(node.initializer)) && routerNamespaces.has(unwrap(node.initializer).text)
    ) {
      for (const element of node.name.elements) {
        if (!ts.isIdentifier(element.name)) continue;
        const imported = element.propertyName?.getText(sourceFile) || element.name.text;
        if (imported === "redirect") navigateFunctions.add(element.name.text);
        if (imported === "useNavigate") navigateFactories.add(element.name.text);
        if (["Route", "Navigate", "Link", "NavLink"].includes(imported)) {
          routerAliases.set(element.name.text, imported);
          routerImportedAliases.add(element.name.text);
        }
      }
    }
    if (
      ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer &&
      ts.isCallExpression(node.initializer) && ts.isIdentifier(node.initializer.expression) &&
      navigateFactories.has(node.initializer.expression.text)
    ) navigateFunctions.add(node.name.text);
    if (
      ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer &&
      ts.isCallExpression(node.initializer) &&
      namespacedRouterMember(node.initializer.expression) === "useNavigate"
    ) navigateFunctions.add(node.name.text);
    if (
      ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer &&
      isLocationObject(node.initializer)
    ) locationAliases.add(node.name.text);
    if (
      ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer &&
      isHistoryObject(node.initializer)
    ) historyAliases.add(node.name.text);
    if (
      ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer &&
      ["pushState", "replaceState"].includes(memberName(unwrap(node.initializer))) &&
      isHistoryObject(memberOwner(unwrap(node.initializer)))
    ) historyFunctions.add(node.name.text);
    if (
      ts.isVariableDeclaration(node) && ts.isObjectBindingPattern(node.name) && node.initializer &&
      isHistoryObject(node.initializer)
    ) {
      for (const element of node.name.elements) {
        if (!ts.isIdentifier(element.name)) continue;
        const method = element.propertyName?.getText(sourceFile) || element.name.text;
        if (["pushState", "replaceState"].includes(method)) historyFunctions.add(element.name.text);
      }
    }
    if (ts.isVariableDeclaration(node) && node.initializer) {
      propagateBinding(node.name, node.initializer, navigateFunctions, isNavigateFunction);
      propagateBinding(node.name, node.initializer, openFunctions, isOpenFunction);
      propagateBinding(node.name, node.initializer, locationAliases, isLocationObject);
      propagateBinding(node.name, node.initializer, locationFunctions, isLocationFunction);
      propagateBinding(node.name, node.initializer, formActivatorAliases, isFormActivatorObject);
    }
    if (ts.isVariableDeclaration(node) && ts.isObjectBindingPattern(node.name) && node.initializer && isGlobalObject(node.initializer)) {
      for (const element of node.name.elements) {
        if (!ts.isIdentifier(element.name)) continue;
        const key = element.propertyName?.getText(sourceFile) || element.name.text;
        if (key === "open") openFunctions.add(element.name.text);
        if (key === "location") locationAliases.add(element.name.text);
      }
    }
    if (ts.isVariableDeclaration(node) && ts.isObjectBindingPattern(node.name) && node.initializer && isLocationObject(node.initializer)) {
      for (const element of node.name.elements) {
        if (!ts.isIdentifier(element.name)) continue;
        const key = element.propertyName?.getText(sourceFile) || element.name.text;
        if (["assign", "replace"].includes(key)) locationFunctions.add(element.name.text);
      }
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken) {
      propagateAssignment(node.left, node.right, navigateFunctions, isNavigateFunction);
      propagateAssignment(node.left, node.right, openFunctions, isOpenFunction);
      propagateAssignment(node.left, node.right, locationAliases, isLocationObject);
      propagateAssignment(node.left, node.right, locationFunctions, isLocationFunction);
      propagateAssignment(node.left, node.right, formActivatorAliases, isFormActivatorObject);
      const target = unwrap(node.left);
      if (ts.isObjectLiteralExpression(target) && isGlobalObject(node.right)) {
        for (const property of target.properties) {
          const key = ts.isShorthandPropertyAssignment(property)
            ? property.name.text
            : ts.isPropertyAssignment(property) ? propertyName(property.name) : null;
          const alias = ts.isShorthandPropertyAssignment(property)
            ? property.name
            : ts.isPropertyAssignment(property) ? unwrap(property.initializer) : null;
          if (key === "open" && alias && ts.isIdentifier(alias)) openFunctions.add(alias.text);
          if (key === "location" && alias && ts.isIdentifier(alias)) locationAliases.add(alias.text);
        }
      }
      if (ts.isObjectLiteralExpression(target) && isLocationObject(node.right)) {
        for (const property of target.properties) {
          const key = ts.isShorthandPropertyAssignment(property)
            ? property.name.text
            : ts.isPropertyAssignment(property) ? propertyName(property.name) : null;
          const alias = ts.isShorthandPropertyAssignment(property)
            ? property.name
            : ts.isPropertyAssignment(property) ? unwrap(property.initializer) : null;
          if (["assign", "replace"].includes(key) && alias && ts.isIdentifier(alias)) {
            locationFunctions.add(alias.text);
          }
        }
      }
    }
  }
  const nodes = [];
  function collect(node) {
    nodes.push(node);
    ts.forEachChild(node, collect);
  }
  collect(sourceFile);
  let previousSize = -1;
  while (previousSize !== navigateFunctions.size + openFunctions.size + locationAliases.size + locationFunctions.size + historyAliases.size + historyFunctions.size + formActivatorAliases.size) {
    previousSize = navigateFunctions.size + openFunctions.size + locationAliases.size + locationFunctions.size + historyAliases.size + historyFunctions.size + formActivatorAliases.size;
    for (const node of nodes) discover(node);
  }
  function visit(node) {
    if (ts.isJsxOpeningLikeElement(node)) {
      const tag = jsxTag(node);
      const namespaceTag = namespacedRouterMember(node.tagName);
      const importedTag = routerAliases.get(tag) ||
        (namespaceTag && ["Route", "Navigate", "Link", "NavLink"].includes(namespaceTag) ? namespaceTag : null);
      const intrinsicTag = ts.isIdentifier(node.tagName) && node.tagName.text === node.tagName.text.toLowerCase();
      const formAttribute = intrinsicTag
        ? node.attributes.properties.find(
            (property) => ts.isJsxAttribute(property) &&
              (property.name.text === "formAction" || (tag === "form" && property.name.text === "action")),
          )
        : null;
      const attributeName = importedTag === "Route" ? "path" : importedTag ? "to" : tag === "a" ? "href" : null;
      if (attributeName) {
        const attribute = node.attributes.properties.find(
          (property) => ts.isJsxAttribute(property) && property.name.text === attributeName,
        );
        if (attribute?.initializer) {
          const expression = ts.isStringLiteral(attribute.initializer)
            ? attribute.initializer
            : ts.isJsxExpression(attribute.initializer)
              ? attribute.initializer.expression
              : null;
          if (expression) record(sourceFile, attribute, importedTag === "Route" ? "route" : "navigation", expression, coreRouteAliases);
        }
      } else if (formAttribute) {
        const expression = formAttribute.initializer && (
          ts.isStringLiteral(formAttribute.initializer)
            ? formAttribute.initializer
            : ts.isJsxExpression(formAttribute.initializer)
              ? formAttribute.initializer.expression
              : null
        );
        if (expression) record(sourceFile, formAttribute, "form-action", expression, coreRouteAliases);
        else {
          const line = sourceFile.getLineAndCharacterOfPosition(formAttribute.getStart(sourceFile)).line + 1;
          unresolved.push({ file: `frontend/src/${relative(sourceFile.fileName)}`, kind: "form-action", line });
        }
      } else if (
        (namespaceTag || routerImportedAliases.has(tag)) &&
        node.attributes.properties.some(
          (property) => ts.isJsxAttribute(property) && ["path", "to", "href"].includes(property.name.text),
        )
      ) {
        const line = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
        unresolved.push({ file: `frontend/src/${relative(sourceFile.fileName)}`, kind: "unknown-router-jsx", line });
      }
    } else if (
      ts.isCallExpression(node) && ts.isIdentifier(node.expression) &&
      navigateFunctions.has(node.expression.text) && node.arguments.length
    ) {
      if (isHistoryDelta(node.arguments[0])) {
        const line = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
        sites.push({ file: `frontend/src/${relative(sourceFile.fileName)}`, kind: "history", line, target: "@history-delta" });
      } else {
        record(sourceFile, node, "navigate", node.arguments[0], coreRouteAliases);
      }
    } else if (
      ts.isCallExpression(node) && namespacedRouterMember(node.expression) === "redirect" &&
      node.arguments.length
    ) {
      record(sourceFile, node, "navigate", node.arguments[0], coreRouteAliases);
    } else if (
      ts.isCallExpression(node) &&
      isLocationFunction(node.expression) && node.arguments.length
    ) {
      record(sourceFile, node, "location", node.arguments[0], coreRouteAliases);
    } else if (ts.isCallExpression(node) && isOpenFunction(node.expression) && node.arguments.length) {
      record(sourceFile, node, "window-open", node.arguments[0], coreRouteAliases);
    } else if (
      ts.isCallExpression(node) &&
      ((ts.isIdentifier(node.expression) && historyFunctions.has(node.expression.text)) ||
        (["pushState", "replaceState"].includes(memberName(node.expression)) &&
          isHistoryObject(memberOwner(node.expression))))
    ) {
      if (node.arguments.length >= 3) {
        record(sourceFile, node, "history-state", node.arguments[2], coreRouteAliases);
      } else {
        const line = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
        unresolved.push({ file: `frontend/src/${relative(sourceFile.fileName)}`, kind: "history-state", line });
      }
    } else if (ts.isBinaryExpression(node)) {
      const assignment = node.operatorToken.kind === ts.SyntaxKind.EqualsToken;
      const directLocation = isLocationObject(node.left);
      const locationProperty = memberOwner(node.left) &&
        isLocationObject(memberOwner(node.left)) &&
        ["href", "pathname", "search", "hash"].includes(memberName(node.left));
      const locationAliasAssignment = assignment &&
        ts.isIdentifier(unwrap(node.left)) &&
        unwrap(node.left).text !== "location" &&
        isLocationObject(node.right);
      if ((directLocation || locationProperty) && !locationAliasAssignment) {
        if (assignment) record(sourceFile, node, "location", node.right, coreRouteAliases);
        else {
          const line = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
          unresolved.push({ file: `frontend/src/${relative(sourceFile.fileName)}`, kind: "location-write", line });
        }
      } else if (
        memberOwner(node.left) &&
        ["action", "formAction"].includes(memberName(node.left)) &&
        isFormActivatorObject(memberOwner(node.left))
      ) {
        if (assignment) record(sourceFile, node, "form-action", node.right, coreRouteAliases);
        else {
          const line = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
          unresolved.push({ file: `frontend/src/${relative(sourceFile.fileName)}`, kind: "form-action-write", line });
        }
      }
    } else if (
      ts.isPropertyAssignment(node) &&
      ((ts.isIdentifier(node.name) && node.name.text === "to") || (ts.isStringLiteral(node.name) && node.name.text === "to"))
    ) {
      record(sourceFile, node, "declarative-target", node.initializer, coreRouteAliases);
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
}
if (unresolved.length) throw new Error(`unresolved reachable navigation target count=${unresolved.length}`);
sites.sort((a, b) => a.file.localeCompare(b.file) || a.line - b.line || a.kind.localeCompare(b.kind) || a.target.localeCompare(b.target));
console.log(JSON.stringify({ modules: [...files.keys()].map(relative).sort(), graph, navigation: sites }));
