import sqlite3

conn = sqlite3.connect("data/registry.sqlite3")
rows = conn.execute(
    "SELECT name, collection FROM downloaded_products WHERE status='rejected_corrupt'"
).fetchall()

for r in rows:
    print(r)

conn.close()