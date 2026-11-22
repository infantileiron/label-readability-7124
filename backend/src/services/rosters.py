"""Combined rosters: several finished runs sharing one visitor pool.

A roster freezes one succeeded run per batch.  Only visitors fully measured in
that run's snapshot are eligible, each visitor joins at most one batch, every
batch receives the same number of visitors in each reading order, and that
common number is as large as the data allows.  Ties keep as many people as
possible in the batch they had on the previous roster.  Both reading orders get
equal counts, and whether a visitor answered correctly never matters.

The input checksum covers the selected runs, their eligible pools and the exact
membership chosen.  It is unique in the database, so a rerun that lands on the
same people (even with a different tie-break hint) returns the stored roster
instead of adding a new one.  A source sample that changed after its run makes
that run stale; the caller must pick the newer run, and the recomputed roster
hashes apart.
"""

import hashlib
import json

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from src.constants.enums import COMBINED_STUDY_LIMIT, ROSTER_METHOD, RunStatus
from src.services.allocation import allocate
from src.services.audit import record


def _digest(payload):
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _eligible(snapshot):
    pools = {'old_first': set(), 'new_first': set()}
    for row in snapshot['pairs']:
        if row['old_ok'] is None or row['new_ok'] is None:
            continue
        pools[row['reading_order']].add(row['visitor_code'])
    return pools['old_first'], pools['new_first']


def _item(run):
    old_first, new_first = _eligible(run['snapshot'])
    return {'study_id': run['study_id'], 'code': run['code'], 'run_id': run['id'],
            'old_first': old_first, 'new_first': new_first}


def _load_inputs(db, run_ids):
    if len(run_ids) < 2:
        raise HTTPException(422, '请至少选择两个批次一起复验')
    if len(run_ids) > COMBINED_STUDY_LIMIT:
        raise HTTPException(422, f'一次最多合并 {COMBINED_STUDY_LIMIT} 个批次')
    if len(set(run_ids)) != len(run_ids):
        raise HTTPException(422, '同一复验运行不能重复选择')
    runs = db.execute('''SELECT r.id, r.study_id, r.status, r.revision, r.snapshot, s.code,
                                s.revision AS study_revision
                         FROM analysis_runs r JOIN studies s ON s.id = r.study_id
                         WHERE r.id = ANY(%s)''', (list(run_ids),)).fetchall()
    if len(runs) != len(run_ids):
        raise HTTPException(404, '所选复验运行不存在')
    by_id = {run['id']: run for run in runs}
    ordered = [by_id[run_id] for run_id in run_ids]
    for run in ordered:
        if run['status'] != RunStatus.SUCCEEDED:
            raise HTTPException(409, f'批次 {run["code"]} 的运行尚未完成')
        if run['revision'] != run['snapshot']['revision']:
            raise HTTPException(409, f'批次 {run["code"]} 的快照修订与运行不一致')
        if run['study_revision'] != run['revision']:
            raise HTTPException(
                409,
                f'批次 {run["code"]} 的样本已更新到 R{run["study_revision"]}，'
                f'所选复验用 R{run["revision"]} 计算；请换用最新修订的复验重算',
            )
    if len({run['study_id'] for run in ordered}) != len(ordered):
        raise HTTPException(422, '同一批次只能选择一个运行')
    return ordered, [_item(run) for run in ordered]


def _freshness(items):
    """Batches whose sample revision has moved past the run used for this roster."""
    return [{'study_id': item['study_id'], 'code': item['code'],
             'run_revision': item['run_revision'], 'current_revision': item['current_revision']}
            for item in items if item['run_revision'] != item['current_revision']]


def findings(diagnostics, stale):
    """Plain-language notes explaining the quota and what would raise it."""
    notes = []
    for row in diagnostics['shortage']:
        notes.append({
            'kind': 'shortage', 'level': 'batch',
            'codes': [row['code']],
            'text': (f"{row['code']} 在单个读序内只有 {min(row['eligible_old'], row['eligible_new'])} "
                     f"位完整测读的观众，比达到 {row['needed_per_order']} 人每读序少 {row['short_by']} 人。"),
        })
    contention = diagnostics['contention']
    if contention:
        groups = '、'.join(
            f"{group['code']}（{'/'.join('先读旧版' if order == 'old_first' else '先读样张' for order in group['orders'])}）"
            for group in contention['groups'])
        notes.append({
            'kind': 'contention', 'level': 'group',
            'codes': contention['codes'],
            'text': (f"{'、'.join(contention['codes'])} 的可选观众被同一批人卡住：这些位置共需 "
                     f"{contention['needed']} 人，合计只有 {contention['pool']} 位可选观众，"
                     f"缺 {contention['shortfall']} 人。涉及 {groups}。"),
        })
    if diagnostics['reason'] == 'pool_bound':
        notes.append({
            'kind': 'pool_bound', 'level': 'all',
            'codes': [row['code'] for row in diagnostics['per_study']],
            'text': (f"全部批次合计只有 {diagnostics['visitor_pool']} 位可选观众，"
                     f"每多一位每批每读序的人，需要多 {2 * diagnostics['batches']} 位新观众。"),
        })
    for row in stale:
        notes.append({
            'kind': 'stale', 'level': 'batch', 'codes': [row['code']],
            'text': (f"{row['code']} 的样本已更新到 R{row['current_revision']}，"
                     f"本名单用的是 R{row['run_revision']} 的复验快照；需要换用新复验重算。"),
        })
    if not notes:
        notes.append({'kind': 'limit', 'level': 'none', 'codes': [],
                      'text': '已达到数据允许的最大人数；再增加需要补测新的完整配对。'})
    return notes


def _checksum(items, members):
    """Identity of a roster: the frozen runs plus the exact chosen membership.

    Two builds that land on the same people in the same batches return the same
    roster even if the tie-break hint differed, so a rerun never adds a list.
    A genuinely different assignment (or a different source run) hashes apart.
    """
    return _digest({
        'method': ROSTER_METHOD,
        'items': [{'study_id': item['study_id'], 'run_id': item['run_id'],
                   'old_first': sorted(item['old_first']),
                   'new_first': sorted(item['new_first'])} for item in items],
        'members': [[member['study_id'], member['reading_order'], member['visitor_code']]
                    for member in members],
    })


def build(db, run_ids, actor, previous_id=None):
    """Allocate and persist once; identical inputs return the existing roster."""
    # Serialise roster creation so two identical requests cannot both insert.
    db.execute('SELECT pg_advisory_xact_lock(520311)')
    check_reference(db, previous_id)
    previous = previous_map(db, previous_id)
    _, items = _load_inputs(db, run_ids)
    allocation = allocate(items, previous)
    checksum = _checksum(items, allocation['members'])
    existing = db.execute('SELECT id FROM rosters WHERE checksum = %s', (checksum,)).fetchone()
    if existing:
        return load(db, existing['id']), False
    diagnostics = allocation['diagnostics']
    roster = db.execute('''INSERT INTO rosters(method, checksum, previous_id, result, requested_by)
                           VALUES (%s, %s, %s, %s, %s) RETURNING id''',
                        (ROSTER_METHOD, checksum, previous_id, Jsonb(diagnostics), actor)).fetchone()
    for position, item in enumerate(items):
        db.execute('''INSERT INTO roster_items(roster_id, position, study_id, run_id)
                      VALUES (%s, %s, %s, %s)''',
                   (roster['id'], position, item['study_id'], item['run_id']))
    for member in allocation['members']:
        db.execute('''INSERT INTO roster_members(roster_id, visitor_code, study_id, run_id, reading_order)
                      VALUES (%s, %s, %s, %s, %s)''',
                   (roster['id'], member['visitor_code'], member['study_id'], member['run_id'],
                    member['reading_order']))
    record(db, actor, 'roster.created', str(roster['id']),
           {'checksum': checksum, 'k': allocation['k'], 'batches': len(items)})
    return load(db, roster['id']), True


def load(db, roster_id):
    roster = db.execute('SELECT * FROM rosters WHERE id = %s', (roster_id,)).fetchone()
    if not roster:
        raise HTTPException(404, '合并名单不存在')
    items = db.execute('''SELECT i.position, i.study_id, i.run_id, s.code, s.title, s.station,
                                 s.revision AS current_revision, r.revision AS run_revision,
                                 r.finished_at
                          FROM roster_items i
                          JOIN studies s ON s.id = i.study_id
                          JOIN analysis_runs r ON r.id = i.run_id
                          WHERE i.roster_id = %s ORDER BY i.position''', (roster_id,)).fetchall()
    members = db.execute('''SELECT visitor_code, study_id, run_id, reading_order
                            FROM roster_members WHERE roster_id = %s
                            ORDER BY study_id, reading_order, visitor_code''', (roster_id,)).fetchall()
    codes = {item['study_id']: item['code'] for item in items}
    stale = _freshness(items)
    return {**roster, 'items': items,
            'members': [{**row, 'study_code': codes[row['study_id']]} for row in members],
            'stale': stale,
            'findings': findings(roster['result'], stale)}


def available_runs(db):
    """Runs the roster picker may use: finished and matching the current sample revision."""
    return db.execute('''SELECT r.id, r.study_id, s.code AS study_code, s.title AS study_title,
                                s.station, r.revision AS run_revision, s.revision AS current_revision,
                                r.confidence, r.finished_at
                         FROM analysis_runs r JOIN studies s ON s.id = r.study_id
                         WHERE r.status = 'succeeded' AND r.revision = s.revision
                         ORDER BY s.code, r.id DESC''').fetchall()


def stale_runs(db):
    """Finished runs left behind by a sample revision; they need a re-run first."""
    return db.execute('''SELECT r.id, r.study_id, s.code AS study_code, s.revision AS current_revision,
                                r.revision AS run_revision, r.finished_at
                         FROM analysis_runs r JOIN studies s ON s.id = r.study_id
                         WHERE r.status = 'succeeded' AND r.revision <> s.revision
                         ORDER BY s.code, r.id DESC''').fetchall()


def list_rosters(db):
    return db.execute('''SELECT id, method, requested_by, created_at, checksum,
                                (result->>'k')::int AS k,
                                (result->>'batches')::int AS batches,
                                (result->>'people')::int AS people,
                                result->>'reason' AS reason
                         FROM rosters ORDER BY id DESC''').fetchall()


def previous_map(db, roster_id):
    """Membership of the preceding roster; an empty roster simply gives no bonus."""
    if roster_id is None:
        return {}
    rows = db.execute('SELECT visitor_code, study_id FROM roster_members WHERE roster_id = %s',
                      (roster_id,)).fetchall()
    return {row['visitor_code']: row['study_id'] for row in rows}


def check_reference(db, previous_id):
    """The previous roster must exist; batches it did not cover simply give no bonus."""
    if previous_id is None:
        return
    if not db.execute('SELECT 1 FROM rosters WHERE id = %s', (previous_id,)).fetchone():
        raise HTTPException(404, '上一份名单不存在')


def export_roster(db, roster_id):
    roster = load(db, roster_id)
    codes = {item['study_id']: item['code'] for item in roster['items']}
    return {**roster,
            'members': [{**row, 'study_code': codes[row['study_id']]} for row in roster['members']]}
