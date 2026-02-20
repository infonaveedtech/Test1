WITH recent_trades AS (
  SELECT t.BUYER_CLIENT_ID AS CLIENT_ID, t.SYMBOL_ID, t.ENTRY_DATETIME
  FROM ATS.TRADES t
  WHERE t.ENTRY_DATETIME >= (SELECT MAX(ENTRY_DATETIME) - INTERVAL '50' DAY FROM ATS.TRADES)
  UNION ALL
  SELECT t.SELLER_CLIENT_ID AS CLIENT_ID, t.SYMBOL_ID, t.ENTRY_DATETIME
  FROM ATS.TRADES t
  WHERE t.ENTRY_DATETIME >= (SELECT MAX(ENTRY_DATETIME) - INTERVAL '50' DAY FROM ATS.TRADES)
),
client_symbol_counts AS (
  SELECT rt.CLIENT_ID, rt.SYMBOL_ID, COUNT(*) AS trade_count
  FROM recent_trades rt
  GROUP BY rt.CLIENT_ID, rt.SYMBOL_ID
),
max_trades_per_client_symbol AS (
  SELECT CLIENT_ID, SYMBOL_ID, trade_count
  FROM client_symbol_counts csc
  WHERE trade_count = (
    SELECT MAX(trade_count)
    FROM client_symbol_counts csc2
    WHERE csc2.CLIENT_ID = csc.CLIENT_ID
  )
),
client_with_max_symbol AS (
  SELECT CLIENT_ID, SYMBOL_ID
  FROM max_trades_per_client_symbol ms
  WHERE trade_count > 1
)
SELECT ec.NAME AS client_name, s.SYMBOL AS symbol, mts.trade_count
FROM client_with_max_symbol mts
JOIN ATS.EDS_CLIENTS ec ON mts.CLIENT_ID = ec.CLIENT_ID
JOIN ATS.SYMBOLS s ON mts.SYMBOL_ID = s.SYMBOL_ID
ORDER BY mts.trade_count DESC
FETCH FIRST 1 ROWS ONLY;
