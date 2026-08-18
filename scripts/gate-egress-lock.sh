#!/usr/bin/env bash
# brains gate — OS-level egress lock (the ENFORCEMENT floor under the PATH-shim gate).
#
# The PATH-shim action gate (brains.exec.gate) is the *cooperative* approval layer:
# it intercepts `git push`, `vercel deploy`, etc. and blocks for operator approval.
# A NON-cooperative agent could bypass it (call a binary by absolute path, or open a
# raw socket from Python). The hard enforcement for that is at the OS level: deny all
# outbound network from the agent's worktree except an explicit allowlist.
#
# This script locks outbound traffic for a dedicated UNIX user the executor runs as
# (BRAINS_EXEC_USER), allowing only DNS + loopback + an operator-provided allowlist of
# hosts/CIDRs (e.g. your package mirror). Run it ONCE at box provisioning, as root.
# Requires nftables. Idempotent (replaces the brains-egress table).
#
# Usage:
#   sudo BRAINS_EXEC_USER=brains-exec ALLOW_CIDRS="140.82.112.0/20" ./gate-egress-lock.sh
#
# The agent then runs as BRAINS_EXEC_USER; even a direct `/usr/bin/git push` fails
# because the TCP connection to the remote is dropped unless the host is allowlisted
# (and you allowlist only what you intend, e.g. nothing, or a read-only mirror).
set -euo pipefail

EXEC_USER="${BRAINS_EXEC_USER:?set BRAINS_EXEC_USER to the unix user the executor runs as}"
ALLOW_CIDRS="${ALLOW_CIDRS:-}"   # space/comma-separated extra-allowed destination CIDRs

if ! command -v nft >/dev/null 2>&1; then
  echo "nftables (nft) is required" >&2; exit 1
fi
UID_NUM="$(id -u "$EXEC_USER")"

# Build the allowlist set elements.
elements=""
for c in ${ALLOW_CIDRS//,/ }; do elements="${elements}${c}, "; done

nft delete table inet brains_egress 2>/dev/null || true
nft -f - <<EOF
table inet brains_egress {
  set allow_dst {
    type ipv4_addr
    flags interval
    ${elements:+elements = { ${elements%, } }}
  }
  chain out {
    type filter hook output priority 0; policy accept;
    # Only police the executor user; everyone else is untouched.
    meta skuid != $UID_NUM accept
    # Allow loopback (brains gateway / local services).
    oifname "lo" accept
    ip daddr 127.0.0.0/8 accept
    ip daddr 10.0.0.0/8 accept
    # DNS so name resolution still works.
    udp dport 53 accept
    tcp dport 53 accept
    # Operator allowlist (e.g. a package mirror).
    ip daddr @allow_dst accept
    # Everything else from the executor user is DROPPED — the hard floor.
    reject with icmpx type admin-prohibited
  }
}
EOF

echo "brains egress lock installed for user $EXEC_USER (uid $UID_NUM)."
echo "Allowed: loopback + private-10 + DNS${ALLOW_CIDRS:+ + [$ALLOW_CIDRS]}. All other egress DROPPED."
echo "Remove with: sudo nft delete table inet brains_egress"
