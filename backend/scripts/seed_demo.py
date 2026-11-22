from src.models.database import connect
from src.utils.passwords import hash_password

VERSIONS = [
    ('YA-01', '原版 · 图录用语', '青釉刻划纹执壶',
     '北宋中晚期青釉刻划纹执壶。器表以刻划法于坯体施饰，罩釉入窑，经高温还原焰烧成。可见细密纹样随釉层厚薄呈现深浅变化。', 16, 1.35),
    ('YA-02', '样张 · 易读改写', '青釉刻划纹执壶',
     '这把青釉壶烧制于北宋。\n工匠先在未烧的壶身上刻出花纹，再涂上青色釉料，把壶放进窑里烧。\n看一看花纹凹进去的地方，釉色更深。', 24, 1.60),
    ('YA-03', '原版 · 釉色说明', '青釉刻划纹执壶',
     '釉面玻璃质感明显，积釉处色泽深沉。刻划线条深刻利落，与釉层相互映衬，为北宋青釉器常见装饰手法。', 16, 1.35),
    ('YA-04', '样张 · 釉色改写', '青釉刻划纹执壶',
     '壶身有一层亮亮的青釉。花纹凹陷的地方釉更厚，颜色也就更深。\n刻线越深，烧成后深浅变化越清楚。', 24, 1.60),
]
# Adjacent participant codes have opposite read orders. Each outcome occurs in both orders.
OUTCOMES = [(True, True)] * 16 + [(True, False)] * 2 + [(False, True)] * 12 + [(False, False)] * 4
OUTCOMES += [(None, True), (True, None)]

# The second batch measures the same hall-two visitors on a different label, with
# its own outcomes (correctness never enters roster selection). Both runs end up
# with 17 complete pairs per reading order and share the whole pool, so a combined
# roster can pick 8 per order per batch and the two batches compete for the rest.
SECOND = [(True, True)] * 14 + [(True, False)] * 4 + [(False, True)] * 10 + [(False, False)] * 6
SECOND += [(None, False), (False, None)]


def seed():
    with connect() as db:
        db.execute('SELECT pg_advisory_xact_lock(520301)')
        for username, name in [('analyst', '沈砚'), ('curator', '许禾'), ('viewer', '周简')]:
            if not db.execute('SELECT 1 FROM accounts WHERE username=%s', (username,)).fetchone():
                db.execute('INSERT INTO accounts VALUES (%s,%s,%s,%s)',
                           (username, name, username, hash_password('Proof5203!')))
        for version in VERSIONS:
            db.execute('''INSERT INTO label_versions(code,title,exhibit,body,type_pt,line_height)
                          VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (code) DO NOTHING''', version)
        studies = (
            ('PD-1121', '青釉执壶展签复验', '常设展 · 二厅', 'YA-01', 'YA-02',
             '阅读后口述花纹是在上釉前还是上釉后刻出的，答「上釉前」记为成功。', OUTCOMES),
            ('PD-1122', '青釉执壶釉色说明复验', '常设展 · 二厅', 'YA-03', 'YA-04',
             '阅读后口述花纹凹处釉色是深还是浅，答「更深」记为成功。', SECOND),
        )
        for code, title, station, old_code, new_code, task, outcomes in studies:
            study = db.execute('''INSERT INTO studies(code,title,station,reading_task,old_id,new_id,revision)
                                  SELECT %s, %s, %s, %s, a.id, b.id, 1
                                  FROM label_versions a, label_versions b
                                  WHERE a.code=%s AND b.code=%s
                                  ON CONFLICT (code) DO NOTHING RETURNING id''',
                               (code, title, station, task, old_code, new_code)).fetchone()
            if study:
                with db.cursor() as cursor:
                    cursor.executemany('INSERT INTO reading_pairs VALUES (%s,%s,%s,%s,%s,now())',
                                       [(study['id'], f'PD-{i:02}',
                                         'old_first' if i % 2 else 'new_first', old, new)
                                        for i, (old, new) in enumerate(outcomes, 1)])
        for table in ('accounts', 'label_versions', 'studies', 'reading_pairs', 'analysis_runs', 'signoffs', 'audit_events'):
            print(f'{table}={db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]}')


if __name__ == '__main__':
    seed()
