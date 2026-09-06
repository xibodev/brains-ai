import type { ComponentProps, ReactNode } from "react";
import { NavLink, useNavigate } from "react-router-dom";

declare const coreHrefBrand: unique symbol;

/** A destination that has passed the supported SPA route boundary. */
export type CoreHref = string & { readonly [coreHrefBrand]: true };

export const CORE_ROUTE_PATTERNS = [
  "/command-center",
  "/workspaces",
  "/workspaces/:slug",
  "/coordination",
  "/governance",
  "/operations",
  "/operations/config",
  "/operations/config/:section",
  "/act",
  "/inbox",
  "/config",
  "*",
] as const;

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

const FALLBACK = "/command-center" as CoreHref;

/** Bound a runtime-provided navigation target to the supported SPA surface. */
export function coreHref(candidate: string): CoreHref {
  if (!candidate.startsWith("/") || candidate.startsWith("//")) return FALLBACK;
  let parsed: URL;
  try {
    parsed = new URL(candidate, "http://brains.invalid");
  } catch {
    return FALLBACK;
  }
  if (parsed.origin !== "http://brains.invalid") return FALLBACK;
  const allowed = CORE_ROUTE_PREFIXES.some(
    (prefix) => parsed.pathname === prefix || parsed.pathname.startsWith(`${prefix}/`),
  );
  return allowed ? (candidate as CoreHref) : FALLBACK;
}

export function workspaceHref(slug: string): CoreHref {
  return coreHref(`/workspaces/${encodeURIComponent(slug)}`);
}

export function configHref(section: string): CoreHref {
  return coreHref(`/operations/config/${encodeURIComponent(section)}`);
}

export function actHref(params?: URLSearchParams | Record<string, string>): CoreHref {
  const query = params instanceof URLSearchParams ? params : new URLSearchParams(params);
  const suffix = query.size ? `?${query.toString()}` : "";
  return coreHref(`/act${suffix}`);
}

/** The only imperative navigation adapter exposed to reachable SPA consumers. */
export function useCoreNavigation() {
  const navigate = useNavigate();
  return {
    open(candidate: CoreHref | string) {
      navigate(coreHref(candidate));
    },
    back() {
      navigate(-1);
    },
  } as const;
}

type CoreNavLinkProps = {
  to: CoreHref | string;
  children?: ReactNode;
  className?: ComponentProps<typeof NavLink>["className"];
  title?: string;
  ariaLabel?: string;
};

/** Declarative in-product navigation with the sanitized target applied last. */
export function CoreNavLink({ to, children, className, title, ariaLabel }: CoreNavLinkProps) {
  return (
    <NavLink
      to={coreHref(to)}
      className={className}
      title={title}
      aria-label={ariaLabel}
    >
      {children}
    </NavLink>
  );
}

type ExternalLinkProps = {
  href: string;
  children?: ReactNode;
  className?: string;
  title?: string;
  ariaLabel?: string;
};

/** Explicit external navigation; relative and non-HTTP(S) targets are refused. */
export function ExternalLink({ href, children, className, title, ariaLabel }: ExternalLinkProps) {
  let safe: string | undefined;
  try {
    const parsed = new URL(href);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") safe = parsed.href;
  } catch {
    safe = undefined;
  }
  if (!safe) {
    return <span className={className} title={title} aria-label={ariaLabel}>{children}</span>;
  }
  return (
    <a
      href={safe}
      target="_blank"
      rel="noopener noreferrer"
      className={className}
      title={title}
      aria-label={ariaLabel}
    >
      {children}
    </a>
  );
}
