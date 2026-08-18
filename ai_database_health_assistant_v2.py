
import io
import re
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

try:
    import pyodbc
    PYODBC_AVAILABLE = True
except Exception:
    pyodbc = None
    PYODBC_AVAILABLE = False


st.set_page_config(
    page_title="AI Database Health Assistant",
    page_icon="🗄️",
    layout="wide",
)

st.title("🗄️ AI Database Health Assistant")
st.caption(
    "Read-only SQL Server diagnostics for storage, indexes, blocking, "
    "query performance, backups, and database health."
)


# ============================================================
# CONFIG
# ============================================================

DEFAULT_SERVER = r"localhost\SQLEXPRESS"
DEFAULT_DATABASE = "SPC"
DEFAULT_DRIVER = "ODBC Driver 17 for SQL Server"


# ============================================================
# DEMO DATA
# ============================================================

@st.cache_data
def demo_data():
    now = datetime.now()

    summary = {
        "Server": r"localhost\SQLEXPRESS",
        "Database": "SPC",
        "Database Size GB": 84.6,
        "Data File Used %": 91.8,
        "Log File Used %": 63.4,
        "Free Data Space GB": 7.6,
        "Recovery Model": "FULL",
        "Compatibility Level": 160,
        "Last Full Backup": now - timedelta(hours=18),
        "Last Log Backup": now - timedelta(minutes=12),
        "Connection Status": "Demo Mode",
    }

    files = pd.DataFrame([
        ["SPC", "ROWS", "SPC.mdf", 78.4, 72.1, 6.3, 91.96, "512 MB"],
        ["SPC_log", "LOG", "SPC_log.ldf", 6.2, 3.9, 2.3, 62.90, "256 MB"],
    ], columns=[
        "Logical Name", "Type", "File Name",
        "Size GB", "Used GB", "Free GB", "Used %", "Growth"
    ])

    tables = pd.DataFrame([
        ["dbo.VDATA", 18_421_550, 31.8],
        ["dbo.VDATA_AUX", 9_314_820, 17.2],
        ["dbo.EVENTS", 6_822_011, 12.5],
        ["dbo.INSPECTION_RESULTS", 11_204_111, 14.1],
        ["dbo.TRACE_DATA", 4_221_042, 7.1],
        ["dbo.VSTDS", 1_241_540, 5.6],
    ], columns=["Table", "Rows", "Size GB"])

    fragmentation = pd.DataFrame([
        ["dbo.VDATA", "IX_VDATA_PART", 61.2, 184221],
        ["dbo.VDATA_AUX", "IX_AUX_TRACE", 74.8, 141122],
        ["dbo.EVENTS", "IX_EVENTS_CODE", 39.1, 88100],
        ["dbo.VSTDS", "IX_STDS_PART", 3.1, 15112],
    ], columns=["Table", "Index", "Fragmentation %", "Page Count"])

    missing_indexes = pd.DataFrame([
        ["dbo.VDATA", "[PARTNO], [DATETIME]", "[VALUE]", 91.2],
        ["dbo.VDATA_AUX", "[PARTNOAUX], [DATETIMEAUX]", "[UDL7], [UDL8]", 78.5],
        ["dbo.EVENTS", "[EVENTCODE], [DATETIME]", "[PARTNO], [MACHINE]", 42.3],
    ], columns=[
        "Table", "Suggested Key Columns", "Suggested Include Columns", "Impact Score"
    ])

    slow_queries = pd.DataFrame([
        [
            "SELECT ... FROM VDATA WHERE PARTNO=@p AND DATETIME BETWEEN ...",
            1280, 3840000, 92110, 146.2
        ],
        [
            "SELECT ... FROM VDATA_AUX WHERE PARTNOAUX=@p ...",
            970, 2211090, 50203, 104.1
        ],
        [
            "SELECT ... FROM EVENTS WHERE EVENTCODE=@e ...",
            510, 1020884, 10020, 71.7
        ],
    ], columns=[
        "Query", "Avg Duration ms", "Logical Reads", "Executions", "CPU sec"
    ])

    blocking = pd.DataFrame([
        [57, 61, "LCK_M_S", 144.2, "SELECT ... FROM VDATA ..."],
        [61, 0, "RUNNING", 0.0, "UPDATE ..."],
    ], columns=[
        "Session ID", "Blocking Session ID", "Wait Type",
        "Wait Seconds", "SQL Text"
    ])

    growth = pd.DataFrame({
        "Date": pd.date_range(end=pd.Timestamp.today().normalize(), periods=30, freq="D"),
        "Database Size GB": np.linspace(79.1, 84.6, 30) + np.sin(np.arange(30)/3)*0.2
    })

    return (
        summary, files, tables, fragmentation,
        missing_indexes, slow_queries, blocking, growth
    )


# ============================================================
# CONNECTION
# ============================================================

def build_connection_string(
    server,
    database,
    driver,
    auth_mode,
    username="",
    password="",
):
    base = (
        f"DRIVER={{{driver}}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        "TrustServerCertificate=yes;"
        "ApplicationIntent=ReadOnly;"
    )

    if auth_mode == "Windows Authentication":
        return base + "Trusted_Connection=yes;"

    return (
        base
        + f"UID={username};"
        + f"PWD={password};"
    )


def connect_sql_server(
    server,
    database,
    driver,
    auth_mode,
    username="",
    password="",
):
    conn_str = build_connection_string(
        server,
        database,
        driver,
        auth_mode,
        username,
        password,
    )

    return pyodbc.connect(
        conn_str,
        timeout=8,
        autocommit=True,
    )


def read_sql(conn, sql):
    return pd.read_sql(sql, conn)


# ============================================================
# LIVE SQL SERVER QUERIES
# ============================================================

def get_database_summary(conn, server, database):
    db_sql = """
    SELECT
        DB_NAME() AS DatabaseName,
        recovery_model_desc AS RecoveryModel,
        compatibility_level AS CompatibilityLevel
    FROM sys.databases
    WHERE name = DB_NAME();
    """

    file_sql = """
    SELECT
        name AS [Logical Name],
        type_desc AS [Type],
        physical_name AS [File Name],
        CAST(size * 8.0 / 1024 / 1024 AS decimal(12,2)) AS [Size GB],
        CAST(FILEPROPERTY(name, 'SpaceUsed') * 8.0 / 1024 / 1024 AS decimal(12,2)) AS [Used GB],
        CAST(
            (size - FILEPROPERTY(name, 'SpaceUsed')) * 8.0 / 1024 / 1024
            AS decimal(12,2)
        ) AS [Free GB],
        CAST(
            CASE
                WHEN size > 0
                THEN FILEPROPERTY(name, 'SpaceUsed') * 100.0 / size
                ELSE 0
            END
            AS decimal(8,2)
        ) AS [Used %],
        CASE
            WHEN is_percent_growth = 1
                THEN CAST(growth AS varchar(20)) + '%'
            ELSE
                CAST(CAST(growth * 8.0 / 1024 AS decimal(12,0)) AS varchar(20)) + ' MB'
        END AS [Growth]
    FROM sys.database_files;
    """

    backup_sql = """
    SELECT
        MAX(CASE WHEN type = 'D' THEN backup_finish_date END) AS LastFullBackup,
        MAX(CASE WHEN type = 'L' THEN backup_finish_date END) AS LastLogBackup
    FROM msdb.dbo.backupset
    WHERE database_name = DB_NAME();
    """

    db = read_sql(conn, db_sql)
    files = read_sql(conn, file_sql)

    try:
        backups = read_sql(conn, backup_sql)
    except Exception:
        backups = pd.DataFrame()

    summary = {
        "Server": server,
        "Database": database,
        "Database Size GB": float(files["Size GB"].sum()) if not files.empty else np.nan,
        "Data File Used %": np.nan,
        "Log File Used %": np.nan,
        "Free Data Space GB": np.nan,
        "Recovery Model": db["RecoveryModel"].iloc[0] if not db.empty else "Unknown",
        "Compatibility Level": int(db["CompatibilityLevel"].iloc[0]) if not db.empty else np.nan,
        "Last Full Backup": None,
        "Last Log Backup": None,
        "Connection Status": "Connected - Read Only",
    }

    if not files.empty:
        rows_files = files[files["Type"] == "ROWS"]
        log_files = files[files["Type"] == "LOG"]

        if not rows_files.empty:
            data_size = rows_files["Size GB"].sum()
            data_used = rows_files["Used GB"].sum()
            summary["Data File Used %"] = (
                data_used / data_size * 100 if data_size > 0 else np.nan
            )
            summary["Free Data Space GB"] = rows_files["Free GB"].sum()

        if not log_files.empty:
            log_size = log_files["Size GB"].sum()
            log_used = log_files["Used GB"].sum()
            summary["Log File Used %"] = (
                log_used / log_size * 100 if log_size > 0 else np.nan
            )

    if not backups.empty:
        summary["Last Full Backup"] = backups["LastFullBackup"].iloc[0]
        summary["Last Log Backup"] = backups["LastLogBackup"].iloc[0]

    return summary, files


def get_largest_tables(conn):
    sql = """
    SELECT TOP 100
        s.name + '.' + t.name AS [Table],
        SUM(p.rows) AS [Rows],
        CAST(SUM(a.total_pages) * 8.0 / 1024 / 1024 AS decimal(12,2)) AS [Size GB]
    FROM sys.tables t
    JOIN sys.schemas s
      ON t.schema_id = s.schema_id
    JOIN sys.indexes i
      ON t.object_id = i.object_id
    JOIN sys.partitions p
      ON i.object_id = p.object_id
     AND i.index_id = p.index_id
    JOIN sys.allocation_units a
      ON p.partition_id = a.container_id
    WHERE i.index_id IN (0,1)
    GROUP BY s.name, t.name
    ORDER BY [Size GB] DESC;
    """
    return read_sql(conn, sql)


def get_fragmentation(conn):
    sql = """
    SELECT TOP 200
        OBJECT_SCHEMA_NAME(ips.object_id) + '.' +
        OBJECT_NAME(ips.object_id) AS [Table],
        i.name AS [Index],
        CAST(ips.avg_fragmentation_in_percent AS decimal(8,2)) AS [Fragmentation %],
        ips.page_count AS [Page Count]
    FROM sys.dm_db_index_physical_stats(
        DB_ID(), NULL, NULL, NULL, 'LIMITED'
    ) ips
    JOIN sys.indexes i
      ON ips.object_id = i.object_id
     AND ips.index_id = i.index_id
    WHERE ips.index_id > 0
      AND ips.page_count >= 100
    ORDER BY ips.avg_fragmentation_in_percent DESC;
    """
    return read_sql(conn, sql)


def get_missing_indexes(conn):
    sql = """
    SELECT TOP 100
        OBJECT_SCHEMA_NAME(mid.object_id, DB_ID()) + '.' +
        OBJECT_NAME(mid.object_id, DB_ID()) AS [Table],
        LTRIM(RTRIM(
            COALESCE(mid.equality_columns,'') +
            CASE
                WHEN mid.equality_columns IS NOT NULL
                 AND mid.inequality_columns IS NOT NULL
                THEN ', '
                ELSE ''
            END +
            COALESCE(mid.inequality_columns,'')
        )) AS [Suggested Key Columns],
        COALESCE(mid.included_columns,'') AS [Suggested Include Columns],
        CAST(
            migs.avg_total_user_cost
            * migs.avg_user_impact
            * (migs.user_seeks + migs.user_scans)
            AS decimal(18,2)
        ) AS [Impact Score]
    FROM sys.dm_db_missing_index_group_stats migs
    JOIN sys.dm_db_missing_index_groups mig
      ON migs.group_handle = mig.index_group_handle
    JOIN sys.dm_db_missing_index_details mid
      ON mig.index_handle = mid.index_handle
    WHERE mid.database_id = DB_ID()
    ORDER BY [Impact Score] DESC;
    """
    return read_sql(conn, sql)


def get_slow_queries(conn):
    sql = """
    SELECT TOP 50
        LEFT(
            REPLACE(REPLACE(st.text, CHAR(10), ' '), CHAR(13), ' '),
            500
        ) AS [Query],
        CAST(
            qs.total_elapsed_time * 1.0 /
            NULLIF(qs.execution_count, 0) / 1000
            AS decimal(18,2)
        ) AS [Avg Duration ms],
        qs.total_logical_reads AS [Logical Reads],
        qs.execution_count AS [Executions],
        CAST(qs.total_worker_time / 1000000.0 AS decimal(18,2)) AS [CPU sec]
    FROM sys.dm_exec_query_stats qs
    CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
    WHERE st.dbid = DB_ID()
    ORDER BY [Avg Duration ms] DESC;
    """
    return read_sql(conn, sql)


def get_blocking(conn):
    sql = """
    SELECT
        r.session_id AS [Session ID],
        r.blocking_session_id AS [Blocking Session ID],
        r.wait_type AS [Wait Type],
        CAST(r.wait_time / 1000.0 AS decimal(12,2)) AS [Wait Seconds],
        LEFT(
            REPLACE(REPLACE(st.text, CHAR(10), ' '), CHAR(13), ' '),
            500
        ) AS [SQL Text]
    FROM sys.dm_exec_requests r
    CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) st
    WHERE r.session_id <> @@SPID
      AND (
            r.blocking_session_id <> 0
            OR r.wait_type LIKE 'LCK%'
          )
    ORDER BY r.wait_time DESC;
    """
    return read_sql(conn, sql)


# ============================================================
# HEALTH SCORE
# ============================================================

def score_health(summary, fragmentation, missing_indexes, slow_queries, blocking):
    storage = 100
    indexes = 100
    performance = 100
    maintenance = 100
    concurrency = 100

    data_used = summary.get("Data File Used %", np.nan)

    if pd.notna(data_used):
        if data_used >= 95:
            storage -= 50
        elif data_used >= 90:
            storage -= 30
        elif data_used >= 80:
            storage -= 15

    if not fragmentation.empty:
        severe = int((fragmentation["Fragmentation %"] >= 50).sum())
        medium = int(
            (
                (fragmentation["Fragmentation %"] >= 30)
                & (fragmentation["Fragmentation %"] < 50)
            ).sum()
        )
        indexes -= min(55, severe * 10 + medium * 5)

    if not missing_indexes.empty:
        high = int((missing_indexes["Impact Score"] >= 100000).sum())
        med = int(
            (
                (missing_indexes["Impact Score"] >= 10000)
                & (missing_indexes["Impact Score"] < 100000)
            ).sum()
        )
        indexes -= min(35, high * 8 + med * 4)

    if not slow_queries.empty:
        slow = int((slow_queries["Avg Duration ms"] >= 1000).sum())
        medium = int(
            (
                (slow_queries["Avg Duration ms"] >= 500)
                & (slow_queries["Avg Duration ms"] < 1000)
            ).sum()
        )
        performance -= min(55, slow * 8 + medium * 4)

    if not blocking.empty:
        concurrency -= min(60, len(blocking) * 15)

    last_backup = summary.get("Last Full Backup")

    if last_backup is not None and not pd.isna(last_backup):
        age_hours = (
            datetime.now() - pd.Timestamp(last_backup).to_pydatetime()
        ).total_seconds() / 3600

        if age_hours > 168:
            maintenance -= 50
        elif age_hours > 72:
            maintenance -= 30
        elif age_hours > 24:
            maintenance -= 15

    scores = {
        "Storage": max(0, round(storage)),
        "Indexes": max(0, round(indexes)),
        "Performance": max(0, round(performance)),
        "Blocking": max(0, round(concurrency)),
        "Maintenance": max(0, round(maintenance)),
    }

    overall = round(np.mean(list(scores.values())))
    return overall, scores


def health_label(score):
    if score >= 90:
        return "Healthy"
    if score >= 75:
        return "Attention Needed"
    if score >= 60:
        return "At Risk"
    return "Critical"


# ============================================================
# ISSUE LIST
# ============================================================

def build_issues(summary, fragmentation, missing_indexes, slow_queries, blocking):
    rows = []

    used = summary.get("Data File Used %", np.nan)

    if pd.notna(used):
        if used >= 95:
            rows.append(["CRITICAL", "Database storage", f"Data files are {used:.1f}% used."])
        elif used >= 90:
            rows.append(["HIGH", "Database storage", f"Data files are {used:.1f}% used."])
        elif used >= 80:
            rows.append(["MEDIUM", "Database storage", f"Data files are {used:.1f}% used."])

    if not fragmentation.empty:
        for _, r in fragmentation.head(10).iterrows():
            frag = float(r["Fragmentation %"])

            if frag >= 50:
                rows.append([
                    "HIGH",
                    f"Index fragmentation - {r['Table']}",
                    f"{r['Index']} is {frag:.1f}% fragmented."
                ])
            elif frag >= 30:
                rows.append([
                    "MEDIUM",
                    f"Index fragmentation - {r['Table']}",
                    f"{r['Index']} is {frag:.1f}% fragmented."
                ])

    if not missing_indexes.empty:
        for _, r in missing_indexes.head(10).iterrows():
            impact = float(r["Impact Score"])

            severity = (
                "HIGH" if impact >= 100000
                else "MEDIUM" if impact >= 10000
                else "LOW"
            )

            rows.append([
                severity,
                f"Missing index opportunity - {r['Table']}",
                f"Suggested key columns: {r['Suggested Key Columns']}"
            ])

    if not slow_queries.empty:
        for _, r in slow_queries.head(5).iterrows():
            duration = float(r["Avg Duration ms"])

            if duration >= 1000:
                rows.append([
                    "HIGH",
                    "Slow query",
                    f"Average duration is {duration:.0f} ms."
                ])
            elif duration >= 500:
                rows.append([
                    "MEDIUM",
                    "Slow query",
                    f"Average duration is {duration:.0f} ms."
                ])

    if not blocking.empty:
        for _, r in blocking.head(10).iterrows():
            rows.append([
                "HIGH",
                f"Blocking session {r['Session ID']}",
                f"Blocked by session {r['Blocking Session ID']} for {r['Wait Seconds']:.1f} seconds."
            ])

    sev_order = {
        "CRITICAL": 0,
        "HIGH": 1,
        "MEDIUM": 2,
        "LOW": 3,
    }

    result = pd.DataFrame(
        rows,
        columns=["Severity", "Issue", "Description"]
    )

    if not result.empty:
        result["_order"] = result["Severity"].map(sev_order)
        result = (
            result
            .sort_values("_order")
            .drop(columns="_order")
            .reset_index(drop=True)
        )

    return result


# ============================================================
# NATURAL LANGUAGE
# ============================================================

def parse_question(question):
    q = question.lower().strip()

    if any(x in q for x in ["largest table", "biggest table", "table size"]):
        return "largest_tables"

    if any(x in q for x in ["missing index", "missing indexes", "index opportunity"]):
        return "missing_indexes"

    if "fragment" in q:
        return "fragmentation"

    if any(x in q for x in ["slow query", "slow queries", "why is my database slow", "query performance"]):
        return "slow_queries"

    if any(x in q for x in ["blocking", "blocked", "lock", "locks"]):
        return "blocking"

    if any(x in q for x in ["space", "storage", "database size", "free space"]):
        return "storage"

    if any(x in q for x in ["backup", "maintenance"]):
        return "maintenance"

    if any(x in q for x in ["health", "worry", "attention", "biggest problem", "fix first"]):
        return "health"

    return "summary"


def answer_question(
    intent,
    summary,
    tables,
    fragmentation,
    missing_indexes,
    slow_queries,
    blocking,
    issues,
):
    if intent == "largest_tables":
        return (
            "These are the largest tables in the database.",
            tables.sort_values("Size GB", ascending=False).head(10)
        )

    if intent == "missing_indexes":
        return (
            "These are the highest-impact missing-index opportunities currently visible to SQL Server.",
            missing_indexes.head(10)
        )

    if intent == "fragmentation":
        return (
            "These indexes currently have the highest fragmentation.",
            fragmentation.sort_values("Fragmentation %", ascending=False).head(10)
        )

    if intent == "slow_queries":
        return (
            "These cached queries currently have the highest average duration.",
            slow_queries.sort_values("Avg Duration ms", ascending=False).head(10)
        )

    if intent == "blocking":
        if blocking.empty:
            return (
                "No active blocking sessions were detected at the time of analysis.",
                pd.DataFrame()
            )

        return (
            "These sessions are currently involved in blocking or lock waits.",
            blocking
        )

    if intent == "storage":
        text = (
            f"Database size is {summary.get('Database Size GB', np.nan):.2f} GB. "
            f"Data files are "
            f"{summary.get('Data File Used %', np.nan):.1f}% used with "
            f"approximately {summary.get('Free Data Space GB', np.nan):.2f} GB "
            f"free inside the data files."
        )
        return text, pd.DataFrame()

    if intent == "maintenance":
        return (
            f"Last full backup: {summary.get('Last Full Backup', 'Unknown')}. "
            f"Last log backup: {summary.get('Last Log Backup', 'Unknown')}.",
            pd.DataFrame()
        )

    if intent == "health":
        if issues.empty:
            return (
                "No major database-health issues were detected.",
                pd.DataFrame()
            )

        return (
            "These are the highest-priority issues I would investigate first.",
            issues.head(10)
        )

    return (
        "Here is the current database-health summary.",
        issues.head(10)
    )


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:
    st.header("Connection")

    mode = st.radio(
        "Mode",
        ["Demo Database", "SQL Server"]
    )

    if mode == "SQL Server":
        driver = st.selectbox(
            "ODBC Driver",
            (
                pyodbc.drivers()
                if PYODBC_AVAILABLE
                else [DEFAULT_DRIVER]
            ),
            index=(
                pyodbc.drivers().index(DEFAULT_DRIVER)
                if PYODBC_AVAILABLE
                and DEFAULT_DRIVER in pyodbc.drivers()
                else 0
            )
        )

        server = st.text_input(
            "Server",
            value=DEFAULT_SERVER
        )

        database = st.text_input(
            "Database",
            value=DEFAULT_DATABASE
        )

        auth_mode = st.selectbox(
            "Authentication",
            [
                "Windows Authentication",
                "SQL Server Authentication",
            ],
            key="db_auth_mode",
        )

        username = ""
        password = ""

        if auth_mode == "SQL Server Authentication":
            username = st.text_input(
                "Username",
                key="db_sql_username",
            )

            password = st.text_input(
                "Password",
                type="password",
                key="db_sql_password",
            )

        connect_button = st.button(
            "Connect / Re-analyze",
            use_container_width=True
        )

    else:
        connect_button = True
        auth_mode = "Windows Authentication"
        username = ""
        password = ""

    st.divider()

    st.info(
        "Read-only mode. This application does not execute CREATE, ALTER, "
        "UPDATE, DELETE, DROP, DBCC, index rebuilds, or other maintenance commands."
    )


# ============================================================
# LOAD DATA
# ============================================================

if "db_analysis_cache" not in st.session_state:
    st.session_state.db_analysis_cache = None

if mode == "Demo Database":

    (
        summary,
        files,
        tables,
        fragmentation,
        missing_indexes,
        slow_queries,
        blocking,
        growth,
    ) = demo_data()

else:

    current_signature = (
        str(driver),
        str(server).strip(),
        str(database).strip(),
        str(auth_mode),
        str(username).strip(),
    )

    cached = st.session_state.db_analysis_cache

    # Connect only when the user explicitly clicks the button.
    if connect_button:

        if not PYODBC_AVAILABLE:
            st.error(
                "pyodbc is not installed. Install it with: pip install pyodbc"
            )
            st.stop()

        if not server.strip() or not database.strip():
            st.error("Server and Database are required.")
            st.stop()

        if auth_mode == "SQL Server Authentication":
            if not username.strip():
                st.error("Username is required for SQL Server Authentication.")
                st.stop()

            if not password:
                st.error("Password is required for SQL Server Authentication.")
                st.stop()

        try:
            conn = connect_sql_server(
                server,
                database,
                driver,
                auth_mode,
                username,
                password,
            )

            summary, files = get_database_summary(
                conn,
                server,
                database,
            )

            tables = get_largest_tables(conn)
            fragmentation = get_fragmentation(conn)
            missing_indexes = get_missing_indexes(conn)

            try:
                slow_queries = get_slow_queries(conn)
            except Exception:
                slow_queries = pd.DataFrame(
                    columns=[
                        "Query",
                        "Avg Duration ms",
                        "Logical Reads",
                        "Executions",
                        "CPU sec",
                    ]
                )

            try:
                blocking = get_blocking(conn)
            except Exception:
                blocking = pd.DataFrame(
                    columns=[
                        "Session ID",
                        "Blocking Session ID",
                        "Wait Type",
                        "Wait Seconds",
                        "SQL Text",
                    ]
                )

            growth = pd.DataFrame(
                columns=["Date", "Database Size GB"]
            )

            conn.close()

            # Persist the successful analysis across Streamlit reruns.
            st.session_state.db_analysis_cache = {
                "signature": current_signature,
                "summary": summary,
                "files": files,
                "tables": tables,
                "fragmentation": fragmentation,
                "missing_indexes": missing_indexes,
                "slow_queries": slow_queries,
                "blocking": blocking,
                "growth": growth,
                "analyzed_at": datetime.now(),
            }

            cached = st.session_state.db_analysis_cache

        except Exception as exc:
            st.error(
                f"Could not connect/analyze SQL Server:\n\n{exc}"
            )
            st.stop()

    # After the first successful analysis, reuse the snapshot on chat/widget reruns.
    if cached is None:
        st.info(
            "Confirm the server/database information and click Connect & Analyze."
        )
        st.stop()

    # If connection settings were changed, require another explicit analysis.
    if cached["signature"] != current_signature:
        st.warning(
            "The connection settings have changed. Click Connect & Analyze "
            "to analyze the new server/database."
        )
        st.stop()

    summary = cached["summary"]
    files = cached["files"]
    tables = cached["tables"]
    fragmentation = cached["fragmentation"]
    missing_indexes = cached["missing_indexes"]
    slow_queries = cached["slow_queries"]
    blocking = cached["blocking"]
    growth = cached["growth"]

    st.sidebar.success(
        "Analysis loaded"
    )
    st.sidebar.caption(
        "Last analyzed: "
        + cached["analyzed_at"].strftime("%Y-%m-%d %H:%M:%S")
    )

# ============================================================
# DASHBOARD
# ============================================================

overall, scores = score_health(
    summary,
    fragmentation,
    missing_indexes,
    slow_queries,
    blocking,
)

issues = build_issues(
    summary,
    fragmentation,
    missing_indexes,
    slow_queries,
    blocking,
)

st.subheader("Database Health")

c1, c2, c3, c4 = st.columns(4)

c1.metric(
    "Health Score",
    f"{overall}/100",
    health_label(overall),
)

c2.metric(
    "Database Size",
    (
        "N/A"
        if pd.isna(summary.get("Database Size GB", np.nan))
        else f"{summary['Database Size GB']:.2f} GB"
    ),
)

c3.metric(
    "Data File Used",
    (
        "N/A"
        if pd.isna(summary.get("Data File Used %", np.nan))
        else f"{summary['Data File Used %']:.1f}%"
    ),
)

c4.metric(
    "Open Issues",
    len(issues),
)

score_df = pd.DataFrame({
    "Area": list(scores.keys()),
    "Score": list(scores.values()),
})

fig = px.bar(
    score_df,
    x="Area",
    y="Score",
    range_y=[0, 100],
    title="Health by Area",
)

st.plotly_chart(
    fig,
    use_container_width=True,
    key="health_scores",
)


# ============================================================
# TABS
# ============================================================

tabs = st.tabs([
    "Issues",
    "Storage",
    "Tables",
    "Indexes",
    "Queries",
    "Blocking",
    "Ask Database",
])

with tabs[0]:
    st.subheader("Issues Requiring Attention")

    if issues.empty:
        st.success(
            "No major database-health issues were detected."
        )
    else:
        st.dataframe(
            issues,
            use_container_width=True,
            hide_index=True,
        )


with tabs[1]:
    st.subheader("Database Files")

    st.dataframe(
        files,
        use_container_width=True,
        hide_index=True,
    )

    if not files.empty:
        file_fig = px.bar(
            files,
            x="Logical Name",
            y=["Used GB", "Free GB"],
            barmode="stack",
            title="Database File Usage",
        )

        st.plotly_chart(
            file_fig,
            use_container_width=True,
            key="file_usage",
        )

    if not growth.empty:
        st.subheader("Database Growth")

        growth_fig = px.line(
            growth,
            x="Date",
            y="Database Size GB",
            markers=True,
            title="Database Size Trend",
        )

        st.plotly_chart(
            growth_fig,
            use_container_width=True,
            key="growth_chart",
        )

    st.markdown("### Database Settings")

    settings_rows = [
        ["Server", summary.get("Server")],
        ["Database", summary.get("Database")],
    ]

    if mode == "SQL Server":
        settings_rows.append(["Authentication", auth_mode])
        if auth_mode == "SQL Server Authentication":
            settings_rows.append(["Login", username])

    settings_rows.extend([
        ["Recovery Model", summary.get("Recovery Model")],
        ["Compatibility Level", summary.get("Compatibility Level")],
        ["Last Full Backup", summary.get("Last Full Backup")],
        ["Last Log Backup", summary.get("Last Log Backup")],
        ["Connection Status", summary.get("Connection Status")],
    ])

    settings_df = pd.DataFrame(
        settings_rows,
        columns=["Setting", "Value"]
    )

    st.dataframe(
        settings_df,
        use_container_width=True,
        hide_index=True,
    )


with tabs[2]:
    st.subheader("Largest Tables")

    st.dataframe(
        tables,
        use_container_width=True,
        hide_index=True,
    )

    if not tables.empty:
        top = tables.sort_values(
            "Size GB",
            ascending=False
        ).head(15)

        table_fig = px.bar(
            top,
            x="Table",
            y="Size GB",
            title="Largest Tables",
        )

        st.plotly_chart(
            table_fig,
            use_container_width=True,
            key="table_size_chart",
        )


with tabs[3]:
    st.subheader("Index Fragmentation")

    st.dataframe(
        fragmentation,
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("Missing Index Opportunities")

    st.dataframe(
        missing_indexes,
        use_container_width=True,
        hide_index=True,
    )

    if not missing_indexes.empty:

        selected_index = st.selectbox(
            "Generate suggested SQL for",
            missing_indexes.index,
            format_func=lambda i: (
                f"{missing_indexes.loc[i, 'Table']} - "
                f"{missing_indexes.loc[i, 'Suggested Key Columns']}"
            ),
        )

        row = missing_indexes.loc[selected_index]

        table_name = str(row["Table"])
        key_columns = str(
            row["Suggested Key Columns"]
        ).strip()

        include_columns = str(
            row["Suggested Include Columns"]
        ).strip()

        index_name = re.sub(
            r"[^A-Za-z0-9_]+",
            "_",
            f"IX_AI_{table_name}_{key_columns}",
        )[:120]

        sql = (
            f"CREATE NONCLUSTERED INDEX [{index_name}]\n"
            f"ON {table_name} ({key_columns})"
        )

        if include_columns:
            sql += f"\nINCLUDE ({include_columns})"

        sql += ";"

        st.code(
            sql,
            language="sql",
        )

        st.warning(
            "Recommendation only. Review execution plans, existing indexes, "
            "column order, and write overhead before applying."
        )


with tabs[4]:
    st.subheader("Slow / Expensive Queries")

    if slow_queries.empty:
        st.info(
            "No query-performance DMV data is available."
        )
    else:
        st.dataframe(
            slow_queries,
            use_container_width=True,
            hide_index=True,
        )

        query_fig = px.bar(
            slow_queries.head(20),
            x="Avg Duration ms",
            y="Query",
            orientation="h",
            title="Queries by Average Duration",
        )

        st.plotly_chart(
            query_fig,
            use_container_width=True,
            key="query_chart",
        )


with tabs[5]:
    st.subheader("Blocking / Lock Waits")

    if blocking.empty:
        st.success(
            "No active blocking sessions were detected."
        )
    else:
        st.dataframe(
            blocking,
            use_container_width=True,
            hide_index=True,
        )


with tabs[6]:
    st.subheader("Ask Database")

    st.caption(
        "Examples: Why is my database slow? • What are my largest tables? • "
        "Do I have missing indexes? • Is anything blocking? • "
        "How much space is left? • What should I fix first?"
    )

    if "db_health_messages" not in st.session_state:
        st.session_state.db_health_messages = []

    for msg in st.session_state.db_health_messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    question = st.chat_input(
        "Ask about SQL Server health..."
    )

    if question:

        st.session_state.db_health_messages.append({
            "role": "user",
            "content": question,
        })

        with st.chat_message("user"):
            st.markdown(question)

        intent = parse_question(question)

        answer, result_df = answer_question(
            intent,
            summary,
            tables,
            fragmentation,
            missing_indexes,
            slow_queries,
            blocking,
            issues,
        )

        with st.chat_message("assistant"):
            st.markdown(answer)

            if (
                result_df is not None
                and not result_df.empty
            ):
                st.dataframe(
                    result_df,
                    use_container_width=True,
                    hide_index=True,
                )

        st.session_state.db_health_messages.append({
            "role": "assistant",
            "content": answer,
        })


# ============================================================
# EXPORT
# ============================================================

st.divider()
st.subheader("Export Health Report")

output = io.BytesIO()

with pd.ExcelWriter(
    output,
    engine="openpyxl",
) as writer:

    pd.DataFrame(
        list(summary.items()),
        columns=["Metric", "Value"],
    ).to_excel(
        writer,
        sheet_name="Summary",
        index=False,
    )

    pd.DataFrame(
        list(scores.items()),
        columns=["Area", "Score"],
    ).to_excel(
        writer,
        sheet_name="Health Scores",
        index=False,
    )

    issues.to_excel(
        writer,
        sheet_name="Issues",
        index=False,
    )

    files.to_excel(
        writer,
        sheet_name="Database Files",
        index=False,
    )

    tables.to_excel(
        writer,
        sheet_name="Tables",
        index=False,
    )

    fragmentation.to_excel(
        writer,
        sheet_name="Fragmentation",
        index=False,
    )

    missing_indexes.to_excel(
        writer,
        sheet_name="Missing Indexes",
        index=False,
    )

    slow_queries.to_excel(
        writer,
        sheet_name="Queries",
        index=False,
    )

    blocking.to_excel(
        writer,
        sheet_name="Blocking",
        index=False,
    )

output.seek(0)

st.download_button(
    "Download Database Health Report",
    data=output,
    file_name="database_health_report.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    use_container_width=True,
)

with st.expander("What this application checks"):
    st.markdown(
        """
- Database size and file utilization
- Data/log file growth configuration
- Largest tables
- Index fragmentation
- SQL Server missing-index DMV recommendations
- Slow cached queries
- Logical reads and CPU usage
- Active blocking and lock waits
- Backup timestamps
- Recovery model
- Compatibility level
- Overall health score
- Natural-language database questions

The application is intentionally read-only. Generated index SQL is displayed for review only and is never executed automatically.
"""
    )

st.caption(
    "AI Database Health Assistant — SQL Server infrastructure diagnostics."
)
