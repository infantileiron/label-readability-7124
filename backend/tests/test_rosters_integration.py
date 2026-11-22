"""End-to-end roster checks against a real PostgreSQL.

These tests need a migrated database and the application dependencies.  They are
skipped automatically when DATABASE_URL is absent or unreachable, so the pure
allocation tests still run anywhere.

    DATABASE_URL=postgresql://proof@127.0.0.1:17772/proof JWT_SECRET=test \
        python -m unittest discover -s backend/tests -t backend
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DATABASE_URL = os.getenv('DATABASE_URL')
HAS_DB = False
if DATABASE_URL:
    try:
        import psycopg
        with psycopg.connect(DATABASE_URL, connect_timeout=3) as probe:
            probe.execute('SELECT 1 FROM schema_migrations LIMIT 1')
        HAS_DB = True
    except Exception:
        HAS_DB = False

if HAS_DB:
    os.environ.setdefault('JWT_SECRET', 'test')
    os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:6399/0')
    from src.models.database import connect
    from src.services.records import save_pair
    from src.services.rosters import build, load
    from src.services.runs import enqueue
    from src.worker import process as worker_process


def run_pending(db):
    while worker_process(db):
        pass


def clean_studies(db):
    db.execute("DELETE FROM roster_members")
    db.execute("DELETE FROM roster_items")
    db.execute("DELETE FROM rosters")
    db.execute("DELETE FROM signoffs")
    db.execute("DELETE FROM analysis_runs")
    db.execute("DELETE FROM reading_pairs")
    db.execute("DELETE FROM studies")
    db.execute("DELETE FROM label_versions")
    db.execute("UPDATE accounts SET password_hash = password_hash")


def add_batch(db, code, exhibit, old_code, new_code, pairs, start_revision=0):
    versions = {}
    for version_code, size in ((old_code, 16), (new_code, 24)):
        row = db.execute('''INSERT INTO label_versions(code,title,exhibit,body,type_pt,line_height)
                            VALUES (%s,%s,%s,%s,%s,%s) RETURNING id''',
                         (version_code, f'{version_code} 标题', exhibit, '这是一段足够长的展签说明文字。',
                          size, 1.4)).fetchone()
        versions[version_code] = row['id']
    study = db.execute('''INSERT INTO studies(code,title,station,reading_task,old_id,new_id,revision)
                          VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
                       (code, f'{code} 名称', '常设展 · 二厅', '阅读后口述判定题，答对记为成功。',
                        versions[old_code], versions[new_code], start_revision)).fetchone()
    for visitor, order, old_ok, new_ok in pairs:
        db.execute('''INSERT INTO reading_pairs(study_id, visitor_code, reading_order, old_ok, new_ok)
                      VALUES (%s,%s,%s,%s,%s)''', (study['id'], visitor, order, old_ok, new_ok))
    return study['id']


def finished_run(db, study_id, confidence=95):
    enqueue(db, study_id, confidence, 'analyst')
    run_pending(db)
    return db.execute('''SELECT id FROM analysis_runs WHERE study_id = %s AND status = 'succeeded'
                         ORDER BY id DESC LIMIT 1''', (study_id,)).fetchone()['id']


@unittest.skipUnless(HAS_DB, 'needs DATABASE_URL pointing at a migrated database')
class RosterIntegrationTest(unittest.TestCase):
    def setUp(self):
        with connect() as db:
            clean_studies(db)

    def tearDown(self):
        with connect() as db:
            clean_studies(db)

    def build_fixture(self, db):
        # Two batches over the same pool, plus one batch with its own visitors.
        shared = [f'V{index:02}' for index in range(20)]
        a = add_batch(db, 'BA-01', '展品甲', 'AA-01', 'AA-02',
                      [(code, 'old_first' if index % 2 else 'new_first', True, True)
                       for index, code in enumerate(shared[:16])])
        b = add_batch(db, 'BA-02', '展品乙', 'BA-01V', 'BA-02V',
                      [(code, 'new_first' if index % 2 else 'old_first', True, False)
                       for index, code in enumerate(shared[:14])]
                      + [(code, 'old_first', True, True) for code in shared[14:16]])
        c = add_batch(db, 'BA-03', '展品丙', 'CA-01', 'CA-02',
                      [(f'W{index:02}', 'old_first' if index % 2 else 'new_first', True, True)
                       for index in range(10)])
        return a, b, c

    def test_quota_per_order_and_one_batch_per_visitor(self):
        with connect() as db:
            a, b, c = self.build_fixture(db)
            runs = [finished_run(db, study) for study in (a, b, c)]
            roster, created = build(db, runs, 'analyst')
        self.assertTrue(created)
        # A and B share the same 8 old-order and 8 new-order visitors, so together
        # they can seat four per order each; C alone could seat five.
        self.assertEqual(roster['result']['k'], 4)
        codes = [row['visitor_code'] for row in roster['members']]
        self.assertEqual(len(codes), len(set(codes)))
        for item in roster['items']:
            for order in ('old_first', 'new_first'):
                self.assertEqual(sum(1 for row in roster['members']
                                     if row['study_id'] == item['study_id']
                                     and row['reading_order'] == order), 4)
        self.assertEqual(roster['result']['reason'], 'shared_pool')

    def test_same_inputs_do_not_add_a_roster(self):
        with connect() as db:
            a, b, c = self.build_fixture(db)
            runs = [finished_run(db, study) for study in (a, b, c)]
            first, created = build(db, runs, 'analyst')
            second, again = build(db, runs, 'analyst')
            third, hinted = build(db, runs, 'analyst', previous_id=first['id'])
            total = db.execute('SELECT count(*) AS n FROM rosters').fetchone()['n']
        self.assertTrue(created)
        self.assertFalse(again)
        self.assertFalse(hinted)
        self.assertEqual({first['id'], second['id'], third['id']}, {first['id']})
        self.assertEqual(total, 1)

    def test_stale_run_is_rejected_until_source_updates(self):
        from fastapi import HTTPException
        with connect() as db:
            a, b, c = self.build_fixture(db)
            runs = [finished_run(db, study) for study in (a, b, c)]
            first, _ = build(db, runs, 'analyst')
            # A source sample changes.  The old run is now a historical revision.
            save_pair(db, a, 'V00', {'reading_order': 'old_first', 'old_ok': True, 'new_ok': True},
                      'analyst', expected_revision=0)
            with self.assertRaises(HTTPException) as caught:
                build(db, [runs[0], runs[1], runs[2]], 'analyst')
            self.assertEqual(caught.exception.status_code, 409)
            self.assertIn('R1', caught.exception.detail)
            # Building with the new run recomputes and yields a different roster.
            fresh = finished_run(db, a)
            second, created = build(db, [fresh, runs[1], runs[2]], 'analyst')
            count = db.execute('SELECT count(*) AS n FROM rosters').fetchone()['n']
            first_again = load(db, first['id'])
        self.assertTrue(created)
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(second['items'][0]['run_id'], fresh)
        self.assertEqual(count, 2)
        # The old roster now reports the stale batch when it is reopened.
        self.assertEqual([row['code'] for row in first_again['stale']], ['BA-01'])
        self.assertIn('stale', {note['kind'] for note in first_again['findings']})

    def test_previous_roster_keeps_batches_when_tied(self):
        with connect() as db:
            a, b = add_batch(db, 'BA-01', '展品甲', 'AA-01', 'AA-02',
                             [(f'V{index:02}', 'old_first' if index % 2 else 'new_first', True, True)
                              for index in range(12)]), \
                   add_batch(db, 'BA-02', '展品乙', 'BA-01V', 'BA-02V',
                             [(f'V{index:02}', 'new_first' if index % 2 else 'old_first', True, True)
                              for index in range(12)])
            runs = [finished_run(db, a), finished_run(db, b)]
            first, _ = build(db, runs, 'analyst')
            second, _ = build(db, runs, 'analyst', previous_id=first['id'])
            retention = second['result']['retention']
        self.assertEqual(first['members'], second['members'])
        self.assertEqual(retention['dropped'], 0)
        self.assertEqual(retention['moved'], 0)

    def test_partial_previous_roster_still_works(self):
        with connect() as db:
            a, b, c = self.build_fixture(db)
            first, _ = build(db, [finished_run(db, a), finished_run(db, c)], 'analyst')
            second, created = build(db, [finished_run(db, a), finished_run(db, b),
                                          finished_run(db, c)], 'analyst', previous_id=first['id'])
        self.assertTrue(created)
        # The previous roster did not include BA-02, so its chosen people get no bonus.
        self.assertGreaterEqual(second['result']['retention']['kept'], 0)
        self.assertLessEqual(second['result']['retention']['kept'], first['result']['people'])

    def test_export_carries_source_run(self):
        with connect() as db:
            a, b, c = self.build_fixture(db)
            runs = [finished_run(db, study) for study in (a, b, c)]
            roster, _ = build(db, runs, 'analyst')
            from src.services.exports import roster_csv
            text = roster_csv(load(db, roster['id']))
        self.assertIn('run_id', text.splitlines()[0])
        self.assertIn(runs[0].__str__(), text)
        self.assertEqual(len(text.strip().splitlines()), roster['result']['k'] * 6 + 1)

    def test_shortage_and_contention_are_reported(self):
        with connect() as db:
            a = add_batch(db, 'BA-01', '展品甲', 'AA-01', 'AA-02',
                          [(f'V{index:02}', 'old_first' if index % 2 else 'new_first', True, True)
                           for index in range(10)])
            b = add_batch(db, 'BA-02', '展品乙', 'BA-01V', 'BA-02V',
                          [(f'V{index:02}', 'new_first' if index % 2 else 'old_first', True, True)
                           for index in range(4)])
            runs = [finished_run(db, a), finished_run(db, b)]
            roster, _ = build(db, runs, 'analyst')
        diagnostics = roster['result']
        self.assertEqual(diagnostics['reason'], 'shortage')
        self.assertEqual([row['code'] for row in diagnostics['shortage']], ['BA-02'])
        kinds = {note['kind'] for note in roster['findings']}
        self.assertIn('shortage', kinds)

    def test_run_must_be_finished(self):
        from fastapi import HTTPException
        with connect() as db:
            a, b, c = self.build_fixture(db)
            runs = [finished_run(db, study) for study in (a, b, c)]
            db.execute("UPDATE analysis_runs SET status='queued', finished_at=NULL WHERE id=%s", (runs[2],))
            with self.assertRaises(HTTPException) as caught:
                build(db, runs, 'analyst')
        self.assertEqual(caught.exception.status_code, 409)

    def test_export_json_matches_snapshot_checksum(self):
        with connect() as db:
            a, b, c = self.build_fixture(db)
            runs = [finished_run(db, study) for study in (a, b, c)]
            roster, _ = build(db, runs, 'analyst')
            exported = load(db, roster['id'])
        self.assertEqual(exported['checksum'], roster['checksum'])
        for item in exported['items']:
            self.assertIn(item['run_id'], runs)


if __name__ == '__main__':
    unittest.main()
