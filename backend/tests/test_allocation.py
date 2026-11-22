"""Tests for the combined-roster allocation engine (standard library only).

Run with:  python -m unittest discover -s backend/tests -t backend
"""

import itertools
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.allocation import ORDER_KEYS, allocate  # noqa: E402


def item(study_id, code, old, new):
    return {'study_id': study_id, 'code': code, 'run_id': study_id * 10,
            'old_first': set(old), 'new_first': set(new)}


def brute_force(items):
    """Largest common quota by exhaustive search."""
    pool = sorted({code for it in items for order in ORDER_KEYS for code in it[order]})
    for quota in range(len(pool) // (2 * len(items)), -1, -1):
        slots = [(position, order) for position in range(len(items)) for order in ORDER_KEYS]
        choices = [list(itertools.combinations(sorted(items[position][order]), quota))
                   for position, order in slots]
        for bundle in itertools.product(*choices):
            seen = set()
            overlap = False
            for group in bundle:
                if seen & set(group):
                    overlap = True
                    break
                seen |= set(group)
            if not overlap:
                return quota
    return 0


def minimal_violations(items, quota):
    """All minimum-size slot sets whose neighbourhood is smaller than quota."""
    slots = [(position, order) for position in range(len(items)) for order in ORDER_KEYS]
    found = []
    for size in range(1, len(slots) + 1):
        for subset in itertools.combinations(slots, size):
            people = set()
            for position, order in subset:
                people |= items[position][order]
            if len(people) < quota * size:
                found.append(frozenset(subset))
        if found:
            return found
    return found


def build(seed, batches=None, people=None):
    rng = random.Random(seed)
    batches = batches or rng.randint(2, 4)
    people = people or rng.randint(4, 14)
    visitors = [f'V{index:02}' for index in range(people)]
    items = []
    for index in range(batches):
        split = rng.randint(0, people)
        old_pool, new_pool = visitors[:split], visitors[split:]
        old = {code for code in old_pool if rng.random() < 0.85}
        new = {code for code in new_pool if rng.random() < 0.85}
        items.append(item(index + 1, f'B{index + 1}', old, new))
    return items


class AllocationTest(unittest.TestCase):
    def check_shape(self, result, items):
        members = result['members']
        quota = result['k']
        seen = set()
        for row in members:
            self.assertNotIn(row['visitor_code'], seen)
            seen.add(row['visitor_code'])
            source = next(it for it in items if it['study_id'] == row['study_id'])
            self.assertIn(row['visitor_code'], source[row['reading_order']])
        for it in items:
            for order in ORDER_KEYS:
                self.assertEqual(sum(1 for row in members if row['study_id'] == it['study_id']
                                     and row['reading_order'] == order), quota)
        self.assertEqual(len(members), quota * 2 * len(items))

    def test_matches_brute_force_on_random_instances(self):
        for seed in range(240):
            items = build(seed)
            result = allocate(items)
            self.assertEqual(result['k'], brute_force(items), f'seed {seed}: {items}')
            self.check_shape(result, items)

    def test_visitor_only_joins_one_batch(self):
        items = [item(1, 'A', ['V1', 'V2'], ['V3', 'V4']),
                 item(2, 'B', ['V3', 'V4'], ['V5', 'V6'])]
        result = allocate(items)
        self.assertEqual(result['k'], 1)
        codes = [row['visitor_code'] for row in result['members']]
        self.assertEqual(len(codes), len(set(codes)))

    def test_outcomes_do_not_affect_selection(self):
        first = allocate([item(1, 'A', ['V1', 'V2'], ['V3', 'V4'])])
        second = allocate([item(1, 'A', ['V1', 'V2'], ['V3', 'V4'])])
        self.assertEqual(first['members'], second['members'])

    def test_previous_roster_is_kept_when_tied(self):
        items = [item(1, 'A', ['V1', 'V2'], ['V3', 'V4']),
                 item(2, 'B', ['V1', 'V2'], ['V3', 'V4'])]
        previous = {'V1': 2, 'V2': 2, 'V3': 2, 'V4': 2}
        result = allocate(items, previous)
        self.assertEqual(result['k'], 1)
        retained = result['diagnostics']['retention']
        self.assertEqual(retained['kept'], 2)
        self.assertEqual(retained['moved'], 2)
        kept = {row['visitor_code']: row['study_id'] for row in result['members']}
        self.assertEqual(sum(1 for sid in kept.values() if sid == 2), 2)

    def test_moves_only_when_quota_requires_it(self):
        items = [item(1, 'A', ['V1', 'V2'], ['V3', 'V4']),
                 item(2, 'B', ['V1', 'V2'], ['V3', 'V4'])]
        previous = {'V1': 1, 'V2': 1, 'V3': 1, 'V4': 1}
        result = allocate(items, previous)
        self.assertEqual(result['k'], 1)
        retained = result['diagnostics']['retention']
        self.assertEqual((retained['kept'], retained['moved']), (2, 2))

    def test_dropped_previous_members_are_counted(self):
        items = [item(1, 'A', ['V1'], ['V2'])]
        result = allocate(items, {'V1': 1, 'V2': 1, 'V9': 1})
        self.assertEqual(result['k'], 1)
        retained = result['diagnostics']['retention']
        self.assertEqual((retained['kept'], retained['dropped'], retained['added']), (2, 1, 0))

    def test_shortage_names_the_thin_batch(self):
        items = [item(1, 'A', ['V1', 'V2', 'V3'], ['V4', 'V5', 'V6']),
                 item(2, 'B', ['V7', 'V8'], ['V9', 'V10'])]
        result = allocate(items)
        self.assertEqual(result['k'], 2)
        self.assertEqual(result['diagnostics']['reason'], 'shortage')
        self.assertEqual([row['code'] for row in result['diagnostics']['shortage']], ['B'])

    def test_shared_pool_names_the_competing_batches(self):
        # A and B each have four people per order, but their old readers overlap
        # on three people, so together they can seat only five across two batches.
        items = [item(1, 'A', ['V1', 'V2', 'V3', 'V4'], ['V5', 'V6', 'V7', 'V8']),
                 item(2, 'B', ['V2', 'V3', 'V4', 'V9'], ['V6', 'V7', 'V8', 'V10']),
                 item(3, 'C', ['V11', 'V12', 'V13', 'V14'], ['V15', 'V16', 'V17', 'V18'])]
        result = allocate(items)
        self.assertEqual(result['k'], 2)
        diagnostics = result['diagnostics']
        self.assertEqual(diagnostics['reason'], 'shared_pool')
        self.assertEqual(diagnostics['contention']['codes'], ['A', 'B'])
        self.assertEqual(diagnostics['contention']['needed'], 6)
        self.assertEqual(diagnostics['contention']['pool'], 5)
        self.assertEqual(diagnostics['contention']['shortfall'], 1)
        self.assertEqual(diagnostics['contention']['groups'][0]['orders'], ['old_first'])

    def test_shared_pool_can_span_one_order_only(self):
        # Both orders are plentiful per batch, but A and B overlap on old_first.
        items = [item(1, 'A', ['V1', 'V2', 'V3'], ['V11', 'V12', 'V13']),
                 item(2, 'B', ['V2', 'V3', 'V4'], ['V11', 'V12', 'V14']),
                 item(3, 'C', ['V5', 'V6', 'V7'], ['V15', 'V16', 'V17'])]
        result = allocate(items)
        self.assertEqual(result['k'], 2)
        diagnostics = result['diagnostics']
        self.assertEqual(diagnostics['reason'], 'shared_pool')
        self.assertEqual(diagnostics['contention']['codes'], ['A', 'B'])
        self.assertEqual(diagnostics['contention']['slots'], 2)
        self.assertEqual(diagnostics['contention']['groups'][0]['orders'], ['old_first'])

    def test_order_crossover_exhausts_the_pool(self):
        # A's new readers and B's old readers are the same two visitors; at k=2 the
        # two batches together need eight distinct people but only six exist.
        items = [item(1, 'A', ['V1', 'V2'], ['V3', 'V4']),
                 item(2, 'B', ['V3', 'V4'], ['V5', 'V6'])]
        diagnostics = allocate(items)['diagnostics']
        self.assertEqual(diagnostics['k'], 1)
        self.assertEqual(diagnostics['reason'], 'pool_bound')
        self.assertEqual(diagnostics['contention']['codes'], ['A', 'B'])
        self.assertEqual(sorted(order for group in diagnostics['contention']['groups']
                                for order in group['orders']),
                         ['new_first', 'old_first'])

    def test_no_constraint_reports_none(self):
        items = [item(1, 'A', ['V1', 'V2', 'V3', 'V4'], ['V5', 'V6', 'V7', 'V8']),
                 item(2, 'B', ['V9', 'V10', 'V11', 'V12'], ['V13', 'V14', 'V15', 'V16'])]
        diagnostics = allocate(items)['diagnostics']
        self.assertEqual(diagnostics['k'], 4)
        self.assertEqual(diagnostics['reason'], 'shortage')  # each batch is at its own limit
        self.assertIsNone(diagnostics['contention'])
        self.assertEqual([row['short_by'] for row in diagnostics['shortage']], [1, 1])

    def test_contention_is_the_minimum_violating_group(self):
        for seed in range(90):
            items = build(seed, batches=4, people=12)
            result = allocate(items)
            target = result['k'] + 1
            report = result['diagnostics']['contention']
            sufficient = [it for it in items
                          if min(len(it['old_first']), len(it['new_first'])) >= target]
            expected = minimal_violations(sufficient, target) if len(sufficient) >= 2 else []
            expected = [group for group in expected if len(group) >= 2]
            if not expected:
                self.assertIsNone(report, f'seed {seed}: unexpected contention {items}')
                continue
            size = min(len(candidate) for candidate in expected)
            self.assertEqual(report['slots'], size, f'seed {seed}: {items}')
            slot_set = frozenset(
                (next(index for index, it in enumerate(sufficient)
                      if it['study_id'] == group['study_id']), order)
                for group in report['groups'] for order in group['orders'])
            self.assertIn(slot_set, [frozenset(candidate) for candidate in expected],
                          f'seed {seed}: {items}')


if __name__ == '__main__':
    unittest.main()
