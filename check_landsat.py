import sqlite3

conn = sqlite3.connect("data/registry.sqlite3")
rows = conn.execute(
    "SELECT name, collection, status FROM downloaded_products WHERE collection LIKE 'LANDSAT%'"
).fetchall()

for r in rows:
    print(r)

print(f"\nTotal Landsat en base : {len(rows)}")
conn.close()