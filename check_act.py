import sqlite3
from pathlib import Path

# 1. Find "act" claim in source files
print("=== Source files mentioning 'act' claim ===")
mirror = Path("mirror/identity-inbound-auth-oauth")
if mirror.exists():
    for f in mirror.rglob("*.java"):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
            if '"act"' in text or 'ACT_CLAIM' in text or 'ACT_TOKEN' in text:
                lines = text.splitlines()
                for i, line in enumerate(lines, 1):
                    if '"act"' in line or 'ACT_CLAIM' in line or 'ACT_TOKEN' in line:
                        rel = str(f.relative_to(mirror))
                        print(f"  {rel}:{i}  {line.strip()}")
        except Exception:
            continue
else:
    print("  mirror/identity-inbound-auth-oauth not found")

# 2. Check DB for nodes with 'act' in name (short, not Activator)
print("\n=== DB nodes with short 'act' pattern ===")
with sqlite3.connect('data/nexus_graph.db') as conn:
    rows = conn.execute("""
        SELECT fqn FROM nodes
        WHERE (fqn LIKE '%.act%' OR fqn LIKE '%ActClaim%' OR fqn LIKE '%ActToken%')
        AND fqn NOT LIKE '%Activat%'
        AND fqn NOT LIKE '%AbstractO%'
        AND fqn NOT LIKE '%Interact%'
        AND fqn NOT LIKE '%Transaction%'
        ORDER BY fqn LIMIT 30
    """).fetchall()
    for r in rows:
        print(" ", r[0])
