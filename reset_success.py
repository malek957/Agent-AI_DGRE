import sqlite3

conn = sqlite3.connect("data/registry.sqlite3")
rows = conn.execute(
    "SELECT name, status, watch_name, raw_dir FROM downloaded_products "
    "WHERE raw_dir IS NOT NULL"
).fetchall()

for r in rows:
    print(r)

conn.close()