-- BL-P0-01 - make the Org membership an existing install already relied on
-- explicit (postgresql).
--
-- The Postgres implementation of 130_org_member_backfill.py. Both grant every
-- pre-existing operator an explicit membership of the ``default`` Org - the
-- one an org-less install resolves to - so enforcing Org scope does not lock
-- an existing operator out of the store it has been using. ``admin`` joins as
-- ``owner``; ``daemon-*`` operators minted by pre-BL-P0-01 enrollment are
-- deliberately excluded and are reported by ``brains-ai credentials doctor``
-- instead of being promoted to Org members.
--
-- Idempotent: an operator that already has a membership row keeps it.

INSERT INTO org_members (org_id, operator_id, role, created_at)
SELECT o.id,
       op.id,
       CASE WHEN op.slug = 'admin' THEN 'owner' ELSE 'member' END,
       CURRENT_TIMESTAMP
FROM orgs o
CROSS JOIN operators op
WHERE o.slug = 'default'
  AND op.slug NOT LIKE 'daemon-%'
  AND NOT EXISTS (
      SELECT 1 FROM org_members m
      WHERE m.org_id = o.id AND m.operator_id = op.id
  );
