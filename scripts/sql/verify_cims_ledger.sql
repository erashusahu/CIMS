-- Read-only reconciliation for every tenant visible to the connected role.
-- Expected result: mismatch_count = 0. Run with an authorized reporting role.
WITH ledger_totals AS (
    SELECT tenant_id, item_id, location_id,
           COALESCE(SUM(signed_quantity) FILTER (WHERE stock_state = 'PHYSICAL'), 0) AS physical,
           COALESCE(SUM(signed_quantity) FILTER (WHERE stock_state = 'ACCOUNTING'), 0) AS accounting,
           COALESCE(SUM(signed_quantity) FILTER (WHERE stock_state = 'COMMITTED'), 0) AS committed,
           COALESCE(SUM(signed_quantity) FILTER (WHERE stock_state = 'INCOMING'), 0) AS incoming
    FROM cims.stock_ledger_entry
    GROUP BY tenant_id, item_id, location_id
)
SELECT COUNT(*) AS mismatch_count
FROM cims.stock_balance AS balance
FULL JOIN ledger_totals AS ledger
  ON ledger.tenant_id = balance.tenant_id
 AND ledger.item_id = balance.item_id
 AND ledger.location_id = balance.location_id
WHERE COALESCE(balance.physical_on_hand, 0) <> COALESCE(ledger.physical, 0)
   OR COALESCE(balance.accounting_on_hand, 0) <> COALESCE(ledger.accounting, 0)
   OR COALESCE(balance.committed, 0) <> COALESCE(ledger.committed, 0)
   OR COALESCE(balance.incoming, 0) <> COALESCE(ledger.incoming, 0);
