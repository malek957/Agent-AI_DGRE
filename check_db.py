import sqlite3

conn = sqlite3.connect("data/registry.sqlite3")
rows = conn.execute(
    "SELECT name, processed_dir, indices_computed, raw_dir, cloud_cover_scl "
    "FROM downloaded_products WHERE status='success'"
).fetchall()

for r in rows:
    print(r)

conn.close()