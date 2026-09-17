"""Physical safety identity, independent of the OAuth route and its status.

These are fixed SQL fragments, never caller-supplied identifiers. Resolve the
exact stored reference first; do not select a credential or a 'primary'
connection. Retired routes still carry durable brakes, ledger and reservations.
"""

PHYSICAL_ACCOUNT_IDS_SQL = """
    SELECT sibling.id
      FROM platform_accounts sibling JOIN platform_accounts chosen
        ON sibling.business_id = chosen.business_id
       AND sibling.platform = chosen.platform
       AND sibling.external_account_id = chosen.external_account_id
     WHERE chosen.id = :platform_account_id
"""

PHYSICAL_ENTITY_REFS_SQL = """
    SELECT sibling.entity_ref
      FROM ads_execution_targets chosen
      JOIN platform_accounts chosen_account ON chosen_account.id = chosen.platform_account_id
      JOIN platform_accounts sibling_account
        ON sibling_account.business_id = chosen_account.business_id
       AND sibling_account.platform = chosen_account.platform
       AND sibling_account.external_account_id = chosen_account.external_account_id
      JOIN ads_execution_targets sibling ON sibling.platform_account_id = sibling_account.id
       AND sibling.business_id = chosen.business_id
       AND sibling.platform = chosen.platform
       AND sibling.level = chosen.level AND sibling.external_id = chosen.external_id
     WHERE chosen.entity_ref = :entity_ref
       AND chosen.business_id = chosen_account.business_id
       AND chosen.platform = chosen_account.platform
"""

# Same key as SqlUnitOfWork gates/settlement; no lock is held over provider I/O.
PHYSICAL_ACCOUNT_LOCK_SQL = """
    SELECT pg_advisory_xact_lock(hashtext('ads-account:' || a.business_id::text || ':' ||
                                        a.platform || ':' || a.external_account_id))
      FROM platform_accounts a WHERE a.id = :platform_account_id
"""

BRAKE_SCOPE_MATCH_SQL = f"""
    scope_kind = :scope_kind
      AND business_id IS NOT DISTINCT FROM :business_id
      AND ((:scope_kind <> 'platform_account' AND platform_account_id IS NULL)
           OR (:scope_kind = 'platform_account' AND platform_account_id IN (
               {PHYSICAL_ACCOUNT_IDS_SQL})))
"""
