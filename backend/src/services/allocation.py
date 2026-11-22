"""Shared-visitor allocation for a combined proof roster.

Several label batches are reviewed together.  Every batch contributes one
completed verification run; only visitors whose old and new readings were both
observed in that run are eligible.  A visitor may join at most one batch, and
every batch must receive the same number of visitors in each reading order.

The largest common quota is found by binary search over a bipartite max-flow
(Dinic).  Ties are broken with a min-cost flow that keeps as many people as
possible in the batch they belonged to on the previous roster.  A few batches
are explained exactly: the smallest group of batches and reading orders whose
eligible union is smaller than the seats they need names the competition, while
a per-batch census names a batch that is simply too thin.  Whether a visitor
answered correctly never enters eligibility or the tie-break.

The module is standard-library only so the allocation can be tested without the
service stack.
"""

from collections import deque
from heapq import heappop, heappush

INF = float('inf')
ORDER_KEYS = ('old_first', 'new_first')


class _Dinic:
    def __init__(self, size):
        self.size = size
        self.graph = [[] for _ in range(size)]

    def add(self, source, target, capacity):
        self.graph[source].append([target, capacity, len(self.graph[target])])
        self.graph[target].append([source, 0, len(self.graph[source]) - 1])

    def reachable(self, source):
        seen = [False] * self.size
        seen[source] = True
        queue = deque([source])
        while queue:
            node = queue.popleft()
            for target, capacity, _ in self.graph[node]:
                if capacity > 0 and not seen[target]:
                    seen[target] = True
                    queue.append(target)
        return seen

    def max_flow(self, source, sink):
        flow = 0
        while True:
            level = [-1] * self.size
            level[source] = 0
            queue = deque([source])
            while queue:
                node = queue.popleft()
                for target, capacity, _ in self.graph[node]:
                    if capacity > 0 and level[target] < 0:
                        level[target] = level[node] + 1
                        queue.append(target)
            if level[sink] < 0:
                return flow
            cursor = [0] * self.size

            def push(node, limit):
                if node == sink:
                    return limit
                while cursor[node] < len(self.graph[node]):
                    edge = self.graph[node][cursor[node]]
                    target, capacity, reverse = edge
                    if capacity > 0 and level[target] == level[node] + 1:
                        sent = push(target, min(limit, capacity))
                        if sent:
                            edge[1] -= sent
                            self.graph[target][reverse][1] += sent
                            return sent
                    cursor[node] += 1
                return 0

            while True:
                sent = push(source, INF)
                if not sent:
                    break
                flow += sent


def _all_visitors(items):
    return {code for item in items for order in ORDER_KEYS for code in item[order]}


def _layout(items):
    """Return sorted visitors, their node numbers, slot nodes and the sink index."""
    visitors = sorted(_all_visitors(items))
    visitor_at = {code: index + 1 for index, code in enumerate(visitors)}
    base = 1 + len(visitors)
    nodes = {}
    for position in range(len(items)):
        for offset, order in enumerate(ORDER_KEYS):
            nodes[(position, order)] = base + 2 * position + offset
    return visitors, visitor_at, nodes, base + 2 * len(items)


def _network(items, capacity):
    """Source -> visitor -> (batch, order) -> sink.  Order slots carry the quota."""
    visitors, visitor_at, nodes, sink = _layout(items)
    dinic = _Dinic(sink + 1)
    for code in visitors:
        dinic.add(0, visitor_at[code], 1)
    for position, item in enumerate(items):
        for order in ORDER_KEYS:
            for code in item[order]:
                dinic.add(visitor_at[code], nodes[(position, order)], 1)
            dinic.add(nodes[(position, order)], sink, capacity)
    return dinic, sink


def _feasible(items, capacity):
    if capacity <= 0:
        return True
    dinic, sink = _network(items, capacity)
    return dinic.max_flow(0, sink) == capacity * 2 * len(items)


def _min_cost_flow(size, edges, source, sink, need):
    """Primal-dual min-cost flow: Dijkstra potentials then a blocking flow.

    All costs are non-negative, so zero potentials are feasible.  Each phase
    shifts potentials to the shortest reduced-cost distances, then pushes a
    blocking flow along the zero reduced-cost edges.  Cost is optimal for the
    requested amount.
    """
    graph = [[] for _ in range(size)]

    def add(start, end, capacity, cost):
        edge = [end, capacity, cost, len(graph[end])]
        graph[start].append(edge)
        graph[end].append([start, 0, -cost, len(graph[start]) - 1])
        return edge

    added = [add(*edge) for edge in edges]
    potential = [0] * size
    flow = 0

    def push(node, limit, level, cursor):
        if node == sink:
            return limit
        while cursor[node] < len(graph[node]):
            edge = graph[node][cursor[node]]
            target, capacity, cost, reverse = edge
            if capacity > 0 and level[target] == level[node] + 1 \
                    and cost + potential[node] - potential[target] == 0:
                sent = push(target, min(limit, capacity), level, cursor)
                if sent:
                    edge[1] -= sent
                    graph[target][reverse][1] += sent
                    return sent
            cursor[node] += 1
        return 0

    while flow < need:
        distance = [INF] * size
        distance[source] = 0
        heap = [(0, source)]
        while heap:
            current, node = heappop(heap)
            if current != distance[node]:
                continue
            for target, capacity, cost, _ in graph[node]:
                if capacity <= 0:
                    continue
                reduced = current + cost + potential[node] - potential[target]
                if reduced < distance[target]:
                    distance[target] = reduced
                    heappush(heap, (reduced, target))
        if distance[sink] == INF:
            break
        for node in range(size):
            if distance[node] < INF:
                potential[node] += distance[node]
        while flow < need:
            level = [-1] * size
            level[source] = 0
            queue = deque([source])
            while queue:
                node = queue.popleft()
                for target, capacity, cost, _ in graph[node]:
                    if capacity > 0 and level[target] < 0 \
                            and cost + potential[node] - potential[target] == 0:
                        level[target] = level[node] + 1
                        queue.append(target)
            if level[sink] < 0:
                break
            cursor = [0] * size
            while flow < need:
                sent = push(source, need - flow, level, cursor)
                if not sent:
                    break
                flow += sent
    return flow, added


def _assign(items, capacity, previous):
    """Pick members at the fixed quota, preferring the previous roster's batches.

    Costs are lexicographic over the whole allocation: a seat kept in the same
    batch costs 0, a returning visitor seated in another batch costs ``weight``,
    and a visitor new to the roster costs ``weight + 1``, with ``weight`` larger
    than the total seat count.  So the sum first maximises people kept in their
    original batch, then people carried over at all.  Correctness never appears.
    """
    if capacity <= 0:
        return []
    visitors, visitor_at, nodes, sink = _layout(items)
    seats = capacity * 2 * len(items)
    weight = seats + 1
    edges = [(0, visitor_at[code], 1, 0) for code in visitors]
    tracked = []
    for position, item in enumerate(items):
        for order in ORDER_KEYS:
            for code in item[order]:
                if code not in previous:
                    cost = weight + 1
                elif previous[code] == item['study_id']:
                    cost = 0
                else:
                    cost = weight
                tracked.append((len(edges), code, position, order))
                edges.append((visitor_at[code], nodes[(position, order)], 1, cost))
    for position in range(len(items)):
        for order in ORDER_KEYS:
            edges.append((nodes[(position, order)], sink, capacity, 0))
    need = capacity * 2 * len(items)
    flow, added = _min_cost_flow(sink + 1, edges, 0, sink, need)
    if flow != need:
        raise RuntimeError('allocation flow fell short of a feasible quota')
    members = []
    for edge_index, code, position, order in tracked:
        if added[edge_index][1] == 0:  # the unit edge was saturated
            members.append({'visitor_code': code, 'study_id': items[position]['study_id'],
                            'run_id': items[position]['run_id'], 'reading_order': order})
    members.sort(key=lambda row: (row['study_id'], row['reading_order'], row['visitor_code']))
    return members


def _max_common_capacity(items):
    if not items:
        return 0
    per_batch = min(min(len(item[order]) for order in ORDER_KEYS) for item in items)
    pool = len(_all_visitors(items))
    upper = min(per_batch, pool // (2 * len(items)))
    low, high = 0, upper
    while low < high:
        middle = (low + high + 1) // 2
        if _feasible(items, middle):
            low = middle
        else:
            high = middle - 1
    return low


def _hall_group(items, quota, exhaustive_limit=8):
    """Smallest group of slots whose eligible pool cannot fill ``quota`` people.

    Hall's condition fails for some slot set whenever the quota is infeasible.
    The minimum-cardinality violating set is automatically inclusion-minimal, so
    it is the tightest explanation of what blocks the quota.  Up to eight
    batches are enumerated exactly (four choices per batch: none, old-only,
    new-only, both); beyond that a residual-graph witness is trimmed.
    """
    if len(items) <= exhaustive_limit:
        return _enumerated_violator(items, quota)
    return _residual_violator(items, quota)


def _bit_pools(items):
    codes = sorted(_all_visitors(items))
    bit = {code: 1 << index for index, code in enumerate(codes)}
    pools = []
    for item in items:
        old_mask = 0
        for code in item['old_first']:
            old_mask |= bit[code]
        new_mask = 0
        for code in item['new_first']:
            new_mask |= bit[code]
        pools.append((old_mask, new_mask))
    return pools


def _enumerated_violator(items, quota):
    pools = _bit_pools(items)
    best = None
    for selection in range(1, 4 ** len(items)):
        digits = selection
        chosen = {}
        for position in range(len(items)):
            digits, choice = divmod(digits, 4)
            if not choice:
                continue
            chosen[position] = ORDER_KEYS if choice == 3 else (ORDER_KEYS[choice - 1],)
        if len(chosen) < 2:
            continue
        slots = sum(len(orders) for orders in chosen.values())
        people_mask = 0
        for position, orders in chosen.items():
            for order in orders:
                people_mask |= pools[position][ORDER_KEYS.index(order)]
        people = people_mask.bit_count()
        if people >= quota * slots:
            continue
        rank = (slots, -len(chosen), -(quota * slots - people))
        if best is None or rank < best[0]:
            best = (rank, chosen, people)
    if best is None:
        return None
    _, chosen, people = best
    return _describe(items, chosen, people, quota)


def _residual_violator(items, quota):
    dinic, sink = _network(items, quota)
    if dinic.max_flow(0, sink) == quota * 2 * len(items):
        return None
    _, _, nodes, _ = _layout(items)
    reachable = dinic.reachable(0)
    chosen = {}
    for (position, order), node in nodes.items():
        if not reachable[node]:
            chosen.setdefault(position, []).append(order)
    if len(chosen) < 2:
        return None

    def pool(selection):
        return {code for position, orders in selection.items()
                for order in orders for code in items[position][order]}

    changed = True
    while changed and len(chosen) > 1:
        changed = False
        for position in list(chosen):
            trial = {index: orders for index, orders in chosen.items() if index != position}
            slots = sum(len(orders) for orders in trial.values())
            if trial and len(pool(trial)) < quota * slots:
                chosen, changed = trial, True
                break
    if len(chosen) < 2:
        return None
    return _describe(items, chosen, pool(chosen), quota)


def _describe(items, chosen, people, quota):
    """``people`` is either a visitor count or the visitor-code set itself."""
    groups = []
    for position in sorted(chosen):
        item = items[position]
        groups.append({'study_id': item['study_id'], 'code': item['code'],
                       'run_id': item['run_id'], 'orders': list(chosen[position]),
                       'pool': len({code for order in chosen[position] for code in item[order]})})
    slots = sum(len(orders) for orders in chosen.values())
    total = people if isinstance(people, int) else len(people)
    return {
        'quota': quota,
        'study_ids': [group['study_id'] for group in groups],
        'codes': [group['code'] for group in groups],
        'batches': len(groups),
        'slots': slots,
        'needed': quota * slots,
        'pool': total,
        'shortfall': quota * slots - total,
        'groups': groups,
    }


def diagnose(items, capacity, members, previous):
    target = capacity + 1
    per_study = []
    for item in items:
        per_study.append({
            'study_id': item['study_id'], 'code': item['code'], 'run_id': item['run_id'],
            'eligible_old': len(item['old_first']), 'eligible_new': len(item['new_first']),
            'eligible_total': len(item['old_first'] | item['new_first']),
            'selected_old': sum(1 for row in members if row['study_id'] == item['study_id']
                                and row['reading_order'] == 'old_first'),
            'selected_new': sum(1 for row in members if row['study_id'] == item['study_id']
                                and row['reading_order'] == 'new_first'),
        })
    shortage = [{
        'study_id': row['study_id'], 'code': row['code'],
        'needed_per_order': target,
        'eligible_old': row['eligible_old'], 'eligible_new': row['eligible_new'],
        'short_by': target - min(row['eligible_old'], row['eligible_new']),
    } for row in per_study if min(row['eligible_old'], row['eligible_new']) < target]
    sufficient = [item for item, row in zip(items, per_study)
                  if min(row['eligible_old'], row['eligible_new']) >= target]
    contention = _hall_group(sufficient, target) if len(sufficient) >= 2 else None
    pool = len(_all_visitors(items))
    pool_cap = pool // (2 * len(items)) if items else 0
    if shortage:
        reason = 'shortage'
    elif contention and contention['batches'] == len(items):
        # Every batch has room on its own, yet all of them together run out of people.
        reason = 'pool_bound'
    elif contention:
        reason = 'shared_pool'
    else:
        reason = 'none'
    selected = {(row['visitor_code'], row['study_id']) for row in members}
    selected_codes = {code for code, _ in selected}
    kept = sum(1 for code, study_id in selected if previous.get(code) == study_id)
    moved = sum(1 for code, study_id in selected if code in previous and previous[code] != study_id)
    return {
        'k': capacity, 'next_k': target, 'batches': len(items), 'people': len(members),
        'individual_cap': min((min(row['eligible_old'], row['eligible_new']) for row in per_study),
                              default=0),
        'pool_cap': pool_cap,
        'visitor_pool': pool,
        'reason': reason,
        'per_study': per_study,
        'shortage': shortage,
        'contention': contention,
        'retention': {
            'previous': len(previous), 'kept': kept, 'moved': moved,
            'dropped': len([code for code in previous if code not in selected_codes]),
            'added': len(members) - kept - moved,
        },
    }


def allocate(items, previous=None):
    """Return the largest common quota, its members, and limitation diagnostics.

    ``items`` is a list of ``{'study_id', 'code', 'run_id', 'old_first',
    'new_first'}`` mappings whose order sets hold only fully measured visitors
    from that batch's frozen run snapshot.
    """
    previous = previous or {}
    capacity = _max_common_capacity(items)
    members = _assign(items, capacity, previous)
    return {'k': capacity, 'members': members,
            'diagnostics': diagnose(items, capacity, members, previous)}
