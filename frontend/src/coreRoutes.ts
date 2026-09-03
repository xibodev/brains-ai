const CORE_ROUTE_PREFIXES = [
  "/act",
  "/command-center",
  "/config",
  "/coordination",
  "/governance",
  "/inbox",
  "/operations",
  "/workspaces",
] as const;

/** Bound a runtime-provided navigation target to the supported SPA surface. */
export function coreRoute(target: string): string {
  if (!target.startsWith("/") || target.startsWith("//")) return "/command-center";
  let path: string;
  try {
    path = new URL(target, "http://brains.invalid").pathname;
  } catch {
    return "/command-center";
  }
  return CORE_ROUTE_PREFIXES.some((prefix) => path === prefix || path.startsWith(`${prefix}/`))
    ? target
    : "/command-center";
}
