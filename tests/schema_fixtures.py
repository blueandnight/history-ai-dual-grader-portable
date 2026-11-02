import sqlite3


def rebuild_legacy_submissions_table(conn: sqlite3.Connection, omitted_columns: set[str]) -> None:
    """Rebuild a synthetic old schema using SQLite features available in 3.26."""
    table_info = conn.execute("PRAGMA table_info(submissions)").fetchall()
    retained = [row for row in table_info if str(row[1]) not in omitted_columns]
    retained_names = {str(row[1]) for row in retained}
    definitions = []
    for row in retained:
        name = str(row[1]).replace('"', '""')
        declared_type = str(row[2] or "BLOB")
        definition = f'"{name}" {declared_type}'
        if row[3]:
            definition += " NOT NULL"
        if row[4] is not None:
            definition += f" DEFAULT {row[4]}"
        if row[5]:
            definition += " PRIMARY KEY"
        definitions.append(definition)

    foreign_keys = conn.execute("PRAGMA foreign_key_list(submissions)").fetchall()
    grouped_foreign_keys: dict[int, list[sqlite3.Row]] = {}
    for row in foreign_keys:
        grouped_foreign_keys.setdefault(int(row[0]), []).append(row)
    for rows in grouped_foreign_keys.values():
        rows.sort(key=lambda row: int(row[1]))
        if not all(str(row[3]) in retained_names for row in rows):
            continue
        source = ", ".join(f'"{str(row[3]).replace(chr(34), chr(34) * 2)}"' for row in rows)
        target = ", ".join(f'"{str(row[4]).replace(chr(34), chr(34) * 2)}"' for row in rows)
        table = str(rows[0][2]).replace('"', '""')
        definitions.append(f'FOREIGN KEY ({source}) REFERENCES "{table}" ({target})')

    indexes_to_restore = []
    for index in conn.execute("PRAGMA index_list(submissions)").fetchall():
        index_name = str(index[1])
        escaped_index = index_name.replace('"', '""')
        columns = {
            str(item[2])
            for item in conn.execute(f'PRAGMA index_info("{escaped_index}")').fetchall()
            if item[2] is not None
        }
        index_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (index_name,)
        ).fetchone()
        if index_sql and index_sql[0] and columns.issubset(retained_names):
            indexes_to_restore.append(str(index_sql[0]))

    quoted_columns = ", ".join(f'"{str(row[1]).replace(chr(34), chr(34) * 2)}"' for row in retained)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(f"CREATE TABLE submissions_legacy ({', '.join(definitions)})")
    conn.execute(
        f"INSERT INTO submissions_legacy ({quoted_columns}) SELECT {quoted_columns} FROM submissions"
    )
    conn.execute("DROP TABLE submissions")
    conn.execute("ALTER TABLE submissions_legacy RENAME TO submissions")
    for index_sql in indexes_to_restore:
        conn.execute(index_sql)
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
