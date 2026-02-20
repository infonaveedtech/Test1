import os
import oracledb
from dotenv import load_dotenv

# Load .env file automatically
load_dotenv()

def _connect():
    dsn = os.getenv("ORACLE_DSN")
    user = os.getenv("ORACLE_USER")
    pwd = os.getenv("ORACLE_PASSWORD")
    owner = os.getenv("ORACLE_OWNER", user)

    if not dsn or not user or not pwd:
        raise RuntimeError(
            "Missing Oracle environment variables. "
            "Ensure ORACLE_DSN / ORACLE_USER / ORACLE_PASSWORD are set in .env"
        )

    conn = oracledb.connect(
        user=user,
        password=pwd,
        dsn=dsn
    )

    # Set default schema (optional but recommended)
    cur = conn.cursor()
    cur.execute(f"ALTER SESSION SET CURRENT_SCHEMA = {owner}")

    return conn


def run_sql(sql: str, row_limit: int = 200):
    sql = sql.strip().rstrip(";")   # oracle hates trailing semicolons

    conn = _connect()
    cur = conn.cursor()

    cur.execute(sql)

    # Fetch rows (preview only)
    rows = cur.fetchmany(row_limit)

    # Extract column names
    columns = [d[0] for d in cur.description]

    cur.close()
    conn.close()

    return {
        "columns": columns,
        "rows": rows,
        "row_limit": row_limit
    }
