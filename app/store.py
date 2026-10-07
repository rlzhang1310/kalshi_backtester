import sqlite3
import uuid
import json
from datetime import datetime, timezone

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, config):
        self.config = config
        self.revision = 0
        self.on_change = None
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(config.db_path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders (
                environment TEXT NOT NULL, account_key TEXT NOT NULL, request_id TEXT NOT NULL,
                client_order_id TEXT NOT NULL, exchange_order_id TEXT,
                ticker TEXT NOT NULL, outcome TEXT NOT NULL, mode TEXT NOT NULL,
                limit_price TEXT NOT NULL, requested_quantity TEXT NOT NULL,
                filled_quantity TEXT NOT NULL, remaining_quantity TEXT NOT NULL,
                canceled_quantity TEXT, status TEXT NOT NULL, raw_status TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                expires_unix INTEGER NOT NULL, average_fill_price TEXT, actual_fees TEXT,
                error_code TEXT, message TEXT,
                pair_id TEXT, leg_index INTEGER, pair_intent TEXT,
                PRIMARY KEY (environment, account_key, request_id),
                UNIQUE (environment, account_key, client_order_id)
            );
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(orders)")}
        for name, kind in (("pair_id", "TEXT"), ("leg_index", "INTEGER"), ("pair_intent", "TEXT")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE orders ADD COLUMN {name} {kind}")
        self.db.execute("DROP INDEX IF EXISTS one_active_market")
        self.db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_active_leg
            ON orders(environment, account_key, ticker, outcome)
            WHERE status IN ('submitting','resting','cancel_pending','unknown')""")
        self.db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_pair_leg
            ON orders(environment, account_key, pair_id, leg_index) WHERE pair_id IS NOT NULL""")
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('instance_id', ?)", (str(uuid.uuid4()),))
        self.instance_id = self.db.execute("SELECT value FROM meta WHERE key='instance_id'").fetchone()[0]

    def close(self):
        self.db.close()

    def _row(self, row):
        return dict(row) if row else None

    def get(self, request_id: str):
        return self._row(self.db.execute(
            "SELECT * FROM orders WHERE environment=? AND account_key=? AND request_id=?",
            (self.config.environment, self.config.account_key, request_id)
        ).fetchone())

    def pair(self, pair_id: str):
        return [self._row(row) for row in self.db.execute(
            """SELECT * FROM orders WHERE environment=? AND account_key=? AND pair_id=? ORDER BY leg_index""",
            (self.config.environment, self.config.account_key, pair_id)
        ).fetchall()]

    def find_owned(self, client_order_id: str | None, exchange_order_id: str | None, ticker: str):
        return self._row(self.db.execute(
            """SELECT * FROM orders WHERE environment=? AND account_key=? AND ticker=?
               AND (client_order_id=? OR exchange_order_id=?) LIMIT 1""",
            (self.config.environment, self.config.account_key, ticker, client_order_id, exchange_order_id)
        ).fetchone())

    def list(self):
        scope = (self.config.environment, self.config.account_key)
        active = self.db.execute(
            "SELECT * FROM orders WHERE environment=? AND account_key=? AND status NOT IN ('filled','canceled','expired','rejected') ORDER BY created_at DESC", scope
        ).fetchall()
        terminal = self.db.execute(
            "SELECT * FROM orders WHERE environment=? AND account_key=? AND status IN ('filled','canceled','expired','rejected') ORDER BY created_at DESC LIMIT 20", scope
        ).fetchall()
        pair_ids = {row["pair_id"] for row in (*active, *terminal) if row["pair_id"]}
        mates = []
        if pair_ids:
            placeholders = ",".join("?" for _ in pair_ids)
            mates = self.db.execute(
                f"SELECT * FROM orders WHERE environment=? AND account_key=? AND pair_id IN ({placeholders}) ORDER BY created_at DESC, leg_index",
                (*scope, *pair_ids)
            ).fetchall()
        unique = {row["request_id"]: row for row in (*active, *mates, *terminal)}
        return [self._row(row) for row in unique.values()]

    def unresolved(self):
        return [self._row(row) for row in self.db.execute(
            "SELECT * FROM orders WHERE environment=? AND account_key=? AND status NOT IN ('filled','canceled','expired','rejected') ORDER BY created_at",
            (self.config.environment, self.config.account_key)
        ).fetchall()]

    def pending_accounting(self):
        return [self._row(row) for row in self.db.execute(
            """SELECT * FROM orders WHERE environment=? AND account_key=?
               AND status IN ('filled','canceled','expired') AND filled_quantity != '0'
               AND (average_fill_price IS NULL OR actual_fees IS NULL)
               AND julianday(created_at) >= julianday('now', '-15 minutes')
               ORDER BY updated_at DESC LIMIT 20""",
            (self.config.environment, self.config.account_key)
        ).fetchall()]

    def insert(self, request, expiry):
        request_id = str(request.request_id)
        client_id = str(uuid.uuid5(uuid.UUID(self.instance_id), request_id))
        now = utcnow()
        values = (
            self.config.environment, self.config.account_key, request_id, client_id,
            request.ticker, request.outcome, request.mode, request.price,
            "1", "0", "1", "submitting", now, now, expiry.isoformat(), int(expiry.timestamp())
        )
        try:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.get(request_id)
            if existing:
                self.db.execute("COMMIT")
                return existing, False
            self.db.execute("""
                INSERT INTO orders (environment, account_key, request_id, client_order_id,
                    ticker, outcome, mode, limit_price, requested_quantity, filled_quantity,
                    remaining_quantity, status, created_at, updated_at, expires_at, expires_unix)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, values)
            self.db.execute("COMMIT")
            self.revision += 1
            if self.on_change:
                self.on_change()
            return self.get(request_id), True
        except sqlite3.IntegrityError:
            self.db.execute("ROLLBACK")
            existing = self.get(request_id)
            if existing:
                return existing, False
            raise
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def insert_pair(self, pair_request, legs, expiry):
        pair_id = str(pair_request.pair_id)
        pair_intent = json.dumps(pair_request.model_dump(mode="json"), sort_keys=True)
        now = utcnow()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.pair(pair_id)
            if existing:
                self.db.execute("COMMIT")
                return existing, False
            for index, request in enumerate(legs):
                request_id = str(request.request_id)
                client_id = str(uuid.uuid5(uuid.UUID(self.instance_id), request_id))
                self.db.execute("""INSERT INTO orders (environment, account_key, request_id, client_order_id,
                    ticker, outcome, mode, limit_price, requested_quantity, filled_quantity,
                    remaining_quantity, status, created_at, updated_at, expires_at, expires_unix,
                    pair_id, leg_index, pair_intent)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (self.config.environment, self.config.account_key, request_id, client_id,
                     request.ticker, request.outcome, request.mode, request.price,
                     "1", "0", "1", "submitting", now, now, expiry.isoformat(), int(expiry.timestamp()),
                     pair_id, index, pair_intent))
            self.db.execute("COMMIT")
            self.revision += 1
            if self.on_change:
                self.on_change()
            return self.pair(pair_id), True
        except sqlite3.IntegrityError:
            self.db.execute("ROLLBACK")
            existing = self.pair(pair_id)
            if existing:
                return existing, False
            raise
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def update(self, request_id: str, **fields):
        if not fields:
            return self.get(request_id)
        fields["updated_at"] = utcnow()
        allowed = {"exchange_order_id", "filled_quantity", "remaining_quantity", "canceled_quantity",
                   "status", "raw_status", "average_fill_price", "actual_fees", "error_code", "message", "updated_at"}
        if not set(fields) <= allowed:
            raise ValueError("Invalid update fields")
        assignments = ", ".join(f"{name}=?" for name in fields)
        self.db.execute(
            f"UPDATE orders SET {assignments} WHERE environment=? AND account_key=? AND request_id=?",
            (*fields.values(), self.config.environment, self.config.account_key, request_id)
        )
        self.revision += 1
        if self.on_change:
            self.on_change()
        return self.get(request_id)
