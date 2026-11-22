"""Tie-break optimality and scale checks for the allocation engine."""

import itertools
import random
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.allocation import ORDER_KEYS, allocate  # noqa: E402


def item(study_id, code, old, new):
    return {'study_id': study_id, 'code': code, 'run_id': study_id * 10,
            'old_first': set(old), 'new_first': set(new)}


def brute_quota(items):
    """Largest common quota by exhaustive search."""
    pool = {code for it in items for order in ORDER_KEYS for code in it[order]}
    for quota in range(len(pool) // (2 * len(items)), -1, -1):
        slots = [(p, order) for p in range(len(items)) for order in ORDER_KEYS]
        for bundle in itertools.product(*[list(itertools.combinations(sorted(items[p][order]), quota))
                                          for p, order in slots]):
            seen = set()
            if all(not (seen & set(group)) and not seen.update(group) for group in bundle):
                return quota
    return 0


def brute_tiebreak(items, quota, previous):
    """Best (in_place, kept, selected) at a fixed quota, by exhaustive search."""
    slots = [(p, order) for p in range(len(items)) for order in ORDER_KEYS]
    best = None
    for bundle in itertools.product(*[list(itertools.combinations(sorted(items[p][order]), quota))
                                      for p, order in slots]):
        seen = set()
        overlap = False
        for group in bundle:
            if seen & set(group):
                overlap = True
                break
            seen |= set(group)
        if overlap:
            continue
        in_place = sum(1 for (p, _), group in zip(slots, bundle) for code in group
                       if previous.get(code) == items[p]['study_id'])
        kept = sum(1 for code in seen if code in previous)
        score = (in_place, kept)
        if best is None or score > best[0]:
            best = (score, seen)
    return best


class TieBreakTest(unittest.TestCase):
    def test_retention_is_optimal(self):
        rng = random.Random(7)
        checked = 0
        for case in range(60):
            batches = rng.randint(2, 3)
            people = [f'P{index:02}' for index in range(rng.randint(4, 7))]
            items = []
            for index in range(batches):
                old = set(rng.sample(people, rng.randint(1, max(1, len(people) // 2))))
                new = set(rng.sample(people, rng.randint(1, max(1, len(people) // 2))))
                items.append(item(index + 1, f'B{index + 1}', old, new))
            quota = brute_quota(items)
            self.assertEqual(allocate(items)['k'], quota, f'case {case}: quota {items}')
            if not quota:
                continue
            previous = {code: rng.randint(1, batches) for code in people if rng.random() < 0.7}
            expected = brute_tiebreak(items, quota, previous)
            result = allocate(items, previous)
            retention = result['diagnostics']['retention']
            in_place = retention['kept']
            returning = retention['kept'] + retention['moved']
            self.assertEqual((in_place, returning), expected[0],
                             f'case {case}: {items} prev={previous}')
            checked += 1
        self.assertGreater(checked, 20)

    def test_large_roster_is_quick(self):
        rng = random.Random(11)
        people = [f'PD-{index:04}' for index in range(1600)]
        items = []
        for index in range(6):
            items.append(item(index + 1, f'PD-{index + 1}',
                              set(rng.sample(people, 900)), set(rng.sample(people, 900))))
        previous = {code: rng.randint(1, 6) for code in people if rng.random() < 0.5}
        started = time.perf_counter()
        result = allocate(items, previous)
        elapsed = time.perf_counter() - started
        # Upper bound from the total pool: 1600 visitors / 12 seats per person.
        self.assertEqual(result['k'], 133)
        self.assertEqual(len(result['members']), 133 * 2 * 6)
        for entry in result['diagnostics']['per_study']:
            self.assertEqual((entry['selected_old'], entry['selected_new']), (133, 133))
        self.assertLess(elapsed, 20, f'allocation took {elapsed:.1f}s')


if __name__ == '__main__':
    unittest.main()
