from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.core.config import REPO_ROOT, load_risk_limits
from src.ingest.csv_loader import CANONICAL_COLUMNS, read_positions_csv


DEFAULT_DB_PATH = REPO_ROOT / "data" / "portfolio.sqlite3"
DEFAULT_RAW_DIR = REPO_ROOT / "data" / "raw"


class PortfolioStore:
    """Small local SQLite store for V0 normalized positions and import records."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        """Open a SQLite connection with row dictionaries enabled."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        """Create V0 import and positions tables if they do not exist."""
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS raw_imports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    imported_at TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    original_path TEXT NOT NULL,
                    raw_path TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS positions_snapshot (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    import_id INTEGER,
                    as_of TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    account_code TEXT,
                    account_name TEXT,
                    owner_ratio REAL,
                    symbol TEXT,
                    asset_id TEXT,
                    asset_name TEXT,
                    asset_type TEXT,
                    currency TEXT,
                    quantity REAL,
                    price REAL,
                    market_value REAL,
                    cost REAL,
                    fx_rate_to_cny REAL,
                    market_value_cny REAL,
                    cost_cny REAL,
                    is_cash INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(import_id) REFERENCES raw_imports(id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_positions_as_of ON positions_snapshot(as_of)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_positions_source_as_of ON positions_snapshot(source_type, as_of)")
            conn.commit()

    def write_positions(
        self,
        positions: pd.DataFrame,
        source_type: str,
        original_path: str | Path,
        raw_path: str | Path,
        replace_source_as_of: bool = True,
    ) -> dict[str, Any]:
        """Write normalized positions and record the raw file lineage."""
        self.init_schema()
        now = datetime.now(UTC).isoformat(timespec="seconds")
        df = positions.copy()
        df["as_of"] = df["as_of"].astype(str)
        df["is_cash"] = df["is_cash"].astype(int)

        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO raw_imports (imported_at, source_type, original_path, raw_path, row_count, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (now, source_type, str(original_path), str(raw_path), len(df), "ok"),
            )
            import_id = int(cur.lastrowid)

            if replace_source_as_of:
                for as_of in sorted(df["as_of"].dropna().unique()):
                    conn.execute(
                        "DELETE FROM positions_snapshot WHERE source_type = ? AND as_of = ?",
                        (source_type, str(as_of)),
                    )

            insert_cols = [
                "import_id",
                *CANONICAL_COLUMNS,
                "created_at",
            ]
            rows = []
            for record in df[CANONICAL_COLUMNS].where(pd.notna(df[CANONICAL_COLUMNS]), None).to_dict("records"):
                rows.append([import_id, *[record.get(col) for col in CANONICAL_COLUMNS], now])

            placeholders = ",".join("?" for _ in insert_cols)
            conn.executemany(
                f"INSERT INTO positions_snapshot ({','.join(insert_cols)}) VALUES ({placeholders})",
                rows,
            )
            conn.commit()

        return {
            "import_id": import_id,
            "source_type": source_type,
            "row_count": len(df),
            "raw_path": str(raw_path),
            "db_path": str(self.db_path),
        }

    def latest_positions(self, as_of: str | None = None) -> pd.DataFrame:
        """Load positions for the requested date, or the latest date in the local store."""
        self.init_schema()
        with self.connect() as conn:
            if as_of is None:
                row = conn.execute("SELECT MAX(as_of) AS as_of FROM positions_snapshot").fetchone()
                as_of = row["as_of"] if row else None
            if not as_of:
                return pd.DataFrame(columns=CANONICAL_COLUMNS)
            rows = conn.execute(
                """
                SELECT as_of, source_type, account_code, account_name, owner_ratio, symbol,
                       asset_id, asset_name, asset_type, currency, quantity, price,
                       market_value, cost, fx_rate_to_cny, market_value_cny, cost_cny, is_cash
                FROM positions_snapshot
                WHERE as_of = ?
                ORDER BY market_value_cny DESC
                """,
                (as_of,),
            ).fetchall()
        df = pd.DataFrame([dict(r) for r in rows])
        if not df.empty:
            df["is_cash"] = df["is_cash"].astype(bool)
        return df

    def list_imports(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent raw import records."""
        self.init_schema()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, imported_at, source_type, original_path, raw_path, row_count, status
                FROM raw_imports
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def save_raw_file(path: str | Path, source_type: str, raw_dir: str | Path = DEFAULT_RAW_DIR) -> Path:
    """Copy an imported CSV into data/raw with a timestamped file name."""
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(f"CSV file not found: {src}")
    raw_root = Path(raw_dir)
    raw_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    safe_name = src.name.replace(" ", "_")
    dst = raw_root / f"{stamp}_{source_type}_{safe_name}"
    shutil.copy2(src, dst)
    return dst


def import_positions_csv(
    path: str | Path,
    source_type: str,
    db_path: str | Path = DEFAULT_DB_PATH,
    raw_dir: str | Path = DEFAULT_RAW_DIR,
    risk_limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Save a raw CSV, normalize it, and write cleaned positions to the local store."""
    risk_limits = risk_limits or load_risk_limits()
    raw_path = save_raw_file(path, source_type, raw_dir)
    positions = read_positions_csv(raw_path, source_type, risk_limits)
    store = PortfolioStore(db_path)
    return store.write_positions(positions, source_type, path, raw_path)

