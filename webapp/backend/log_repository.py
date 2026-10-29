"""Durable workspace indexes. Raw source messages are never stored here."""
import json


class LogRepository:
    def __init__(self, db):
        self.db = db
        with db.transaction() as conn:
            schema = '''
                CREATE TABLE IF NOT EXISTS log_tasks (
                    id TEXT PRIMARY KEY, body TEXT NOT NULL, locator TEXT NOT NULL,
                    selected INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS log_reports (
                    id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS log_meta (id TEXT PRIMARY KEY, body TEXT NOT NULL);
            '''
            for statement in schema.split(';'):
                if statement.strip():
                    conn.execute(statement)

    def tasks(self):
        return [{**json.loads(r['body']), 'selected': bool(r['selected']), 'revision': r['revision']}
                for r in self.db.query('SELECT * FROM log_tasks ORDER BY rowid')]

    def task(self, key):
        row = self.db.query_one('SELECT * FROM log_tasks WHERE id=?', (key,))
        if row is None:
            return None
        return {**json.loads(row['body']), 'selected': bool(row['selected']),
                'revision': row['revision'], '_locator': json.loads(row['locator'])}

    def upsert(self, item, locator):
        self.db.write('''INSERT INTO log_tasks(id, body, locator) VALUES(?,?,?)
            ON CONFLICT(id) DO UPDATE SET body=excluded.body, locator=excluded.locator''',
                      (item['id'], json.dumps(item, ensure_ascii=False), json.dumps(locator)))

    def select(self, ids, selected, revision):
        with self.db.transaction() as conn:
            rows = [conn.execute('SELECT revision FROM log_tasks WHERE id=?', (key,)).fetchone() for key in ids]
            if any(row is None or row['revision'] != revision for row in rows):
                return False
            for key in ids:
                conn.execute('UPDATE log_tasks SET selected=?, revision=revision+1 WHERE id=?', (selected, key))
        return True

    def remove(self, ids):
        with self.db.transaction() as conn:
            for key in ids:
                conn.execute('DELETE FROM log_tasks WHERE id=?', (key,))

    def report(self, key):
        row = self.db.query_one('SELECT body FROM log_reports WHERE id=?', (key,))
        return json.loads(row['body']) if row else None

    def put_report(self, key, body):
        self.db.write('INSERT INTO log_reports VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                      (key, json.dumps(body, ensure_ascii=False)))

    def meta(self, key, value=None):
        if value is not None:
            self.db.write('INSERT INTO log_meta VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                          (key, json.dumps(value)))
        row = self.db.query_one('SELECT body FROM log_meta WHERE id=?', (key,))
        return json.loads(row['body']) if row else None
