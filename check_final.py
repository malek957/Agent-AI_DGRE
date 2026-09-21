import sqlite3

conn = sqlite3.connect("data/registry.sqlite3")
rows = conn.execute(
    "SELECT name, status FROM downloaded_products"
).fetchall()

for r in rows:
    print(r)

conn.close()