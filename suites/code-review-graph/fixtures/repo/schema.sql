CREATE TABLE orders (
    id            INTEGER PRIMARY KEY,
    customer_id   INTEGER NOT NULL,
    status        TEXT NOT NULL,
    discount_percent REAL DEFAULT 0.0,
    total_cents   INTEGER NOT NULL
);
