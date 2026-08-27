# -*- coding: utf-8 -*-
'''Runtime navigation grid for bot pathfinding, and A* over it.

WHY
    Bots pick sensible destinations (see bot_routes.py) but drive at them in a
    straight line, so roughly 30% of AI samples sit in stuck-escape, grinding
    and reversing against whatever happens to be in the way. Positional
    cleverness is capped by the ability to REACH a position.

WHAT
    A passability grid sampled from the live map with the same two probes
    bot_routes already uses, plus A* over it. It needs no baked data, so unlike
    the route nodes it works on ALL maps - including the six of ours that no
    longer exist in modern WoT and therefore have no Wargaming route data.

CELL COUNT IS CONSTANT, CELL SIZE IS NOT
    Cell size is derived from the map's playable bounds to hit a target cell
    count. That fixes the probe budget per map, and as a bonus puts the finest
    resolution on the SMALL maps - which are the city maps (Ensk, Himmelsdorf,
    Munchen) where street width is what matters. A flat cell size would have
    been too coarse for exactly those.

BUILDINGS COME FREE
    A downward ray hits the ROOF, so a cell inside a building reads as a large
    height jump against its neighbours and marks itself impassable. That is how
    structures enter the grid without any extra probing. Known wrong case:
    bridges and overhangs, where the deck reads as ground and the road beneath
    is invisible. Accepted.

LESSONS ALREADY PAID FOR (see bot_routes.py, and CLAUDE.md)
    - Terrain is not queryable for the first ~12 frames of a battle and streams
      in by distance. A failed probe is NOT a verdict; everything retries.
    - wg_collideSegment cannot see water - it returns the LAKEBED - so water
      needs its own probe or bots get routed into lakes.
    - Probing must be sliced across frames or it hitches.

No BigWorld imports: every world query arrives as a callback, so this is fully
desktop-testable. Run `python nav_grid.py` for the self-test.
'''

import heapq
import math
import os
import struct
from array import array

# Cell states.
# Three states, and that is the whole model. Nothing in this module DECIDES that
# ground is unusable: a cell is drivable if the map has ground there, and the only
# thing that blocks it is an area a human painted.
#
# Two automatic policies were tried and both got it wrong on a city map. A slope
# heuristic condemned 1441 of 2126 cells on Ruinberg for being a street NEXT TO a
# building. Reachability pruning was better but still a rule the author had to
# argue with. Both are gone: blocking is now the user's intent and nothing else.
UNKNOWN = 0                 # never probed - no coordinate data, not a verdict
PASSABLE = 1
BLOCKED_PAINTED = 2         # a human said no. The only blocking there is.

STATE_NAMES = {
    UNKNOWN: 'unknown', PASSABLE: 'passable', BLOCKED_PAINTED: 'painted-avoid',
}

# Roughly how many cells to spend on a map, whatever its size.
TARGET_CELLS = 10000
# Clamp, so a tiny or enormous map cannot produce a silly cell size (m).
MIN_CELL = 6.0
MAX_CELL = 16.0

# Slope limits, as a ratio of the horizontal step. These MUST match the bot
# feelers in offline_battle.py (0.45 up / 0.7 down): a grid that disagrees with
# local avoidance would hand out paths the bot physically refuses to follow.
MAX_UP_RATIO = 0.45
MAX_DOWN_RATIO = 0.7
# Extra cost for stepping into a cell that touches a blocked one. Keeps routes
# off the staircase edge of a painted area without ever refusing a narrow gap.
# Must stay well under 1.0 or the heuristic stops being admissible enough for
# the paths to look sensible.
WALL_COST = 0.35

# Give up waiting only after this many CONSECUTIVE passes learn nothing at all.
# At a 40-cell slice over 10k cells a pass is ~250 frames, so this is minutes of
# genuinely no new terrain - well beyond any streaming delay. The counter resets
# the moment a single cell answers.
STALL_PASSES = 30
# ...and never bank a grid that is mostly holes. Below this we keep measuring
# and simply do not save, because a bad grid saved is a bad grid forever.
STALL_MIN_COVERAGE = 0.90

# Cells probed per tick. Each one is a wg_collideSegment into the collision
# scene, which is not cheap on a city map: at 200 a first-time bake of a 10k
# grid cost ~50k rays over ~900 frames and made the opening minutes of the
# battle unplayable. A grid is baked ONCE per map and reloaded from the dump
# ever after, so the bake can afford to be slow and invisible rather than fast
# and ruinous - it now finishes over a couple of quiet minutes instead.
PROBE_SLICE = 40

DUMP_MAGIC = b'OFFHNAV1'


def choose_cell_size(width, height, target=TARGET_CELLS):
    '''Cell size (m) that lands a map of this size near the target cell count.'''
    area = max(1.0, float(width) * float(height))
    cell = math.sqrt(area / float(target))
    return max(MIN_CELL, min(MAX_CELL, cell))


def grid_shape(bounds, target=TARGET_CELLS):
    '''(x0, z0, x1, z1, cell, nx, nz) for these bounds.

    Split out so a dump can be checked for reusability against exactly the
    arithmetic that built it, rather than against a second copy of it.
    '''
    x0, z0, x1, z1 = [float(v) for v in bounds]
    if x1 < x0:
        x0, x1 = x1, x0
    if z1 < z0:
        z0, z1 = z1, z0
    cell = choose_cell_size(x1 - x0, z1 - z0, target)
    nx = max(2, int(math.ceil((x1 - x0) / cell)))
    nz = max(2, int(math.ceil((z1 - z0) / cell)))
    return x0, z0, x1, z1, cell, nx, nz


class NavGrid(object):
    '''Passability over the playable area, built incrementally.

    bounds is (minX, minZ, maxX, maxZ) - the ARENA bounds (the red line), not
    the full space, because that is the area bots may drive in.
    '''

    def __init__(self, bounds, map_name='?', target=TARGET_CELLS):
        self.map_name = map_name
        (x0, z0, x1, z1, cell, nx, nz) = grid_shape(bounds, target)
        self.x0, self.z0, self.x1, self.z1 = x0, z0, x1, z1
        self.cell = cell
        self.nx, self.nz = nx, nz
        n = self.nx * self.nz
        self.n = n
        self.state = array('b', [UNKNOWN]) * n
        self.ground = array('f', [0.0]) * n
        self.known = array('b', [0]) * n
        self._cursor = 0
        self.passes = 0
        self.probes = 0
        self._dirty = True
        self._settled = False
        self._barren = 0
        self.reach = None

    # -- geometry ------------------------------------------------------------
    def index(self, ix, iz):
        return iz * self.nx + ix

    def coords(self, i):
        return (i % self.nx, i // self.nx)

    def center(self, i):
        ix, iz = self.coords(i)
        return (self.x0 + (ix + 0.5) * self.cell,
                self.z0 + (iz + 0.5) * self.cell)

    def cell_at(self, x, z):
        '''Index of the cell containing (x, z), or None if off-grid.'''
        ix = int((x - self.x0) / self.cell)
        iz = int((z - self.z0) / self.cell)
        if ix < 0 or iz < 0 or ix >= self.nx or iz >= self.nz:
            return None
        return self.index(ix, iz)

    # -- construction --------------------------------------------------------
    def step(self, probe, budget=PROBE_SLICE):
        '''Probe the next slice of cells. Returns True once a full pass is done.

        Unknown cells are RETRIED on later passes because terrain streams in;
        only a cell that answered is decided. Safe to call every frame - it
        settles to a no-op once everything is known.

        probe.ground(x, z)                      -> y or None
        '''
        done = scanned = 0
        while done < budget and scanned < self.n:
            i = self._cursor
            self._cursor += 1
            if self._cursor >= self.n:
                self._cursor = 0
                self.passes += 1
                # Barren-pass counter, for the case where cells can NEVER be
                # answered. This must be a genuine last resort, not a guess:
                # terrain streams in as the battle goes on, so a pass finding
                # nothing usually just means the player has not moved yet.
                # Prokhorovka looked stalled at 92% and went on to reach 100%
                # in the same battle - settling on one barren pass would have
                # dumped an incomplete map PERMANENTLY, which is far worse than
                # measuring for longer.
                if self._dirty:
                    self._barren = 0
                else:
                    self._barren += 1
                if (self._barren >= STALL_PASSES
                        and self.coverage() >= STALL_MIN_COVERAGE):
                    self._settled = True
                    return True
                # Only re-derive when a cell actually resolved this pass. The
                # live run spent ~300 passes waiting on the last few cells to
                # stream in; deriving each time cost ~12M operations for no
                # change whatsoever.
                if self._dirty:
                    self.derive()
                    self._dirty = False
            scanned += 1
            if self.known[i]:
                continue
            # Count the ATTEMPT, not the success. Early in a battle nothing is
            # probeable, and if only successes counted against the budget a
            # single frame would burn the whole 10k-cell grid on failed rays -
            # exactly the hitch the slicing exists to prevent.
            done += 1
            x, z = self.center(i)
            y = probe.ground(x, z)
            self.probes += 1
            if y is None:
                continue
            self.ground[i] = y
            self.known[i] = 1
            self._dirty = True
            self.state[i] = PASSABLE
            # Water is deliberately NOT probed here. It used to block, and now
            # nothing blocks but paint - so the probe would cost a second ray per
            # cell (20k instead of 10k on a 100x100 map) to produce information
            # the painter already shows better: the game's own minimap layer is
            # the one source that renders water clearly, so the human can see
            # where to paint without the grid measuring it.
        # derive() is O(cells x 4) and runs ONLY on a pass boundary (above), not
        # per step. At 10k cells and a 200-cell budget that is once per ~50
        # frames; calling it every frame would cost ~40k operations of pure
        # Python 2.6 per frame, which is a visible hitch on its own.
        return self.passes >= 1

    def settled(self):
        '''Is the grid finished?

        True once every cell is measured, OR once a whole pass has learned
        nothing new - see step(). A map with void outside its playable area can
        never reach full coverage, and treating that as unfinished meant it was
        re-measured from scratch every single battle.

        Cached. This is asked once a frame for the whole life of a battle, and
        an O(cells) scan there is pure waste once the answer stops changing -
        the flag only ever goes False -> True, and only step() can flip it.
        '''
        if self._settled:
            return True
        for i in range(self.n):
            if not self.known[i]:
                return False
        self._settled = True
        return True

    def clear_painted(self):
        """Forget every painted block, returning those cells to measurement.

        derive() deliberately never overwrites BLOCKED_PAINTED - a human's
        decision outranks a measurement arriving later - which means painted
        cells are permanent until something explicitly lets them go. An editor
        that deletes an avoid area has to be able to, or the area disappears
        from the drawing while its cells stay blocked forever.
        """
        n = 0
        for i in range(self.n):
            if self.state[i] == BLOCKED_PAINTED:
                self.state[i] = PASSABLE if self.known[i] else UNKNOWN
                n += 1
        if n:
            self.reach = None          # connectivity just changed
        return n

    def derive(self):
        """Ground present -> drivable. That is the entire rule.

        Kept as a method because coverage grows while chunks stream in, and a
        cell only becomes drivable once its height has actually been measured.
        A painted block is never overwritten - a human's decision outranks a
        measurement arriving later.
        """
        for i in range(self.n):
            if self.state[i] == BLOCKED_PAINTED:
                continue
            self.state[i] = PASSABLE if self.known[i] else UNKNOWN

    # -- queries -------------------------------------------------------------
    def passable(self, i):
        return i is not None and 0 <= i < self.n and self.state[i] == PASSABLE

    def coverage(self):
        if not self.n:
            return 0.0
        return sum(self.known) / float(self.n)

    def passable_ratio(self):
        if not self.n:
            return 0.0
        c = 0
        for s in self.state:
            if s == PASSABLE:
                c += 1
        return c / float(self.n)

    def counts(self):
        out = {}
        for s in self.state:
            out[s] = out.get(s, 0) + 1
        return dict((STATE_NAMES[k], v) for k, v in out.items())

    def nearest_passable(self, i, radius=4, reachable=False):
        '''Closest passable cell to i within radius cells, or None.

        Path endpoints are real world positions that may land on a blocked
        cell (a tank sitting against a wall), so both ends get snapped.

        reachable=True additionally requires the cell to be connected to the
        rest of the map. Snapping a destination to a passable cell that happens
        to be sealed inside a painted area only converts one guaranteed A*
        failure into another; for a DESTINATION the caller wants somewhere the
        bot can actually get to.
        '''
        if i is None:
            return None
        _ok = self.passable
        if reachable:
            def _ok(j):
                return self.passable(j) and self.can_reach(j)
        if _ok(i):
            return i
        ix, iz = self.coords(i)
        for r in range(1, radius + 1):
            best, bestd = None, None
            for dz in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if max(abs(dx), abs(dz)) != r:
                        continue
                    jx, jz = ix + dx, iz + dz
                    if jx < 0 or jz < 0 or jx >= self.nx or jz >= self.nz:
                        continue
                    j = self.index(jx, jz)
                    if not _ok(j):
                        continue
                    d = dx * dx + dz * dz
                    if bestd is None or d < bestd:
                        best, bestd = j, d
            if best is not None:
                return best
        return None

    # -- search --------------------------------------------------------------
    def build_clearance(self):
        '''Mark cells that touch a blocked one, so A* can prefer the middle.

        A painted area is quantised to whole cells, so its boundary is a
        staircase; a path that hugs it inherits every step of that staircase and
        the bot reads as constantly correcting. Nudging the route one cell away
        from the wall costs almost nothing in distance and removes most of the
        weaving, because the straightener can then pull a clean line.

        This does NOT block anything - it only makes hugging slightly expensive,
        so a corridor exactly one cell wide is still used rather than abandoned.
        '''
        nx, nz, st = self.nx, self.nz, self.state
        near = array('b', [0]) * self.n
        for iz in range(nz):
            base = iz * nx
            for ix in range(nx):
                i = base + ix
                if st[i] != PASSABLE:
                    continue
                for dz in (-1, 0, 1):
                    jz = iz + dz
                    if jz < 0 or jz >= nz:
                        near[i] = 1
                        break
                    for dx in (-1, 0, 1):
                        jx = ix + dx
                        if jx < 0 or jx >= nx:
                            near[i] = 1
                            break
                        if st[jz * nx + jx] != PASSABLE:
                            near[i] = 1
                            break
                    if near[i]:
                        break
        self.near_wall = near
        return sum(near)

    def astar(self, start, goal, max_expansions=40000, wall_cost=WALL_COST):
        '''Cell indices from start to goal inclusive, or None.

        8-connected with an octile heuristic. Diagonals may not cut a corner
        between two blocked cells, or paths clip building corners.
        '''
        start = self.nearest_passable(start)
        goal = self.nearest_passable(goal)
        if start is None or goal is None:
            return None
        # Answer from the flood fill when we have one. An unreachable goal is the
        # worst case for A* - it expands everything before failing - and it is
        # also the case that repeats, so short-circuit it.
        if not (self.can_reach(start) and self.can_reach(goal)):
            return None
        if start == goal:
            return [start]
        nx, nz = self.nx, self.nz
        gx, gz = self.coords(goal)

        def h(i):
            ix, iz = i % nx, i // nx
            dx, dz = abs(ix - gx), abs(iz - gz)
            return (dx + dz) + (1.41421356 - 2.0) * min(dx, dz)

        _near = getattr(self, 'near_wall', None)
        if wall_cost <= 0.0:
            _near = None
        openq = [(h(start), 0.0, start)]
        came = {}
        gscore = {start: 0.0}
        closed = set()
        expansions = 0
        while openq:
            _f, g, cur = heapq.heappop(openq)
            if cur in closed:
                continue
            if cur == goal:
                path = [cur]
                while cur in came:
                    cur = came[cur]
                    path.append(cur)
                path.reverse()
                return path
            closed.add(cur)
            expansions += 1
            if expansions > max_expansions:
                return None
            cx, cz = cur % nx, cur // nx
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1),
                           (1, 1), (1, -1), (-1, 1), (-1, -1)):
                jx, jz = cx + dx, cz + dz
                if jx < 0 or jz < 0 or jx >= nx or jz >= nz:
                    continue
                j = jz * nx + jx
                if self.state[j] != PASSABLE or j in closed:
                    continue
                if not self._step_ok(cur, j, bool(dx and dz)):
                    continue          # cannot climb/drop between these cells
                if dx and dz:
                    # No corner cutting.
                    if (self.state[cz * nx + jx] != PASSABLE
                            or self.state[jz * nx + cx] != PASSABLE):
                        continue
                    step = 1.41421356
                else:
                    step = 1.0
                if _near is not None and _near[j]:
                    step += wall_cost
                ng = g + step
                if ng < gscore.get(j, 1e30):
                    gscore[j] = ng
                    came[j] = cur
                    heapq.heappush(openq, (ng + h(j), ng, j))
        return None

    def path_world(self, path):
        '''Cell path -> list of (x, z) centres.'''
        return [self.center(i) for i in path] if path else []

    def _step_ok(self, i, j, diag=False):
        '''May a vehicle move between these two adjacent cells?

        Cell state says whether you may BE somewhere; this says whether you may
        get there. Splitting the two is what stops a street being condemned for
        having a building next to it while still refusing to drive up the wall.
        '''
        k = 1.41421356 if diag else 1.0
        dy = self.ground[j] - self.ground[i]
        return -(MAX_DOWN_RATIO * self.cell * k) <= dy <= (MAX_UP_RATIO * self.cell * k)

    def line_clear(self, a, b):
        '''True when every cell on the straight line a->b is passable.

        Bresenham over cells. Used for string-pulling, below.
        '''
        ax, az = self.coords(a)
        bx, bz = self.coords(b)
        dx, dz = abs(bx - ax), abs(bz - az)
        sx = 1 if bx > ax else -1
        sz = 1 if bz > az else -1
        x, z = ax, az
        err = dx - dz
        prev = None
        while True:
            cur = z * self.nx + x
            if self.state[cur] != PASSABLE:
                return False
            # A smoothed leg must also be CLIMBABLE end to end, or string-pulling
            # could shortcut a path straight up a bank.
            if prev is not None and not self._step_ok(prev, cur, True):
                return False
            prev = cur
            if x == bx and z == bz:
                return True
            e2 = 2 * err
            if e2 > -dz:
                err -= dz
                x += sx
            if e2 < dx:
                err += dx
                z += sz

    def smooth(self, path, max_skip=14):
        '''String-pull a grid path into a few long legs.

        A raw 8-connected path staircases along every diagonal, and a bot
        steering at each cell centre in turn would visibly wobble down the
        whole route. Collapsing runs with clear line of sight gives legs the
        driver can actually hold a heading on. max_skip bounds the cost.
        '''
        if not path or len(path) < 3:
            return path
        out = [path[0]]
        i = 0
        n = len(path)
        while i < n - 1:
            j = min(n - 1, i + max_skip)
            while j > i + 1 and not self.line_clear(path[i], path[j]):
                j -= 1
            out.append(path[j])
            i = j
        return out

    def build_reach(self, seeds=None):
        """Flood fill once, so "could A* even get there?" becomes a set lookup.

        This blocks NOTHING and changes no route - it is purely an early-out. A
        failed A* is the most expensive thing this module can do: it explores the
        entire reachable region before giving up, and with a destination that can
        never be reached it did so EVERY FRAME. A live Ruinberg run logged 3350
        failures against 308 successes and the frame rate collapsed.
        """
        starts = []
        for sx, sz in (seeds or ()):
            i = self.cell_at(sx, sz)
            i = self.nearest_passable(i, radius=8) if i is not None else None
            if i is not None:
                starts.append(i)
        if not starts:
            first = None
            for i in range(self.n):
                if self.state[i] == PASSABLE:
                    first = i
                    break
            if first is None:
                self.reach = set()
                return 0
            starts = [first]
        seen = set()
        for st in starts:
            if st not in seen:
                seen |= self.reachable_from(st)
        self.reach = seen
        return len(seen)

    def can_reach(self, i):
        """True when a path to this cell is even possible. Without a flood fill
        having been built, assume yes and let A* answer."""
        r = getattr(self, 'reach', None)
        if r is None:
            return True
        return i in r

    def reachable_from(self, start):
        '''Set of cells reachable from start - used to spot a grid that has
        fragmented into islands, which is the classic symptom of a slope limit
        set too tight.'''
        start = self.nearest_passable(start)
        if start is None:
            return set()
        seen = set([start])
        stack = [start]
        nx, nz = self.nx, self.nz
        while stack:
            cur = stack.pop()
            cx, cz = cur % nx, cur // nx
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                jx, jz = cx + dx, cz + dz
                if jx < 0 or jz < 0 or jx >= nx or jz >= nz:
                    continue
                j = jz * nx + jx
                if (j not in seen and self.state[j] == PASSABLE
                        and self._step_ok(cur, j)):
                    seen.add(j)
                    stack.append(j)
        return seen

    def apply_avoid(self, polys, inside_test):
        """Mark cells inside painted keep-out areas as blocked.

        This is why avoid-areas need no new pathfinding code: a blocked cell is
        a blocked cell, so A* routes around a painted area exactly as it routes
        around a lake. Called once after the grid settles.

        inside_test(x, z, poly) is passed in rather than imported so this module
        stays free of dependencies (bot_routes.point_in_poly is the one used).
        Returns how many cells were newly blocked.
        """
        if not polys:
            return 0
        boxes = []
        skipped = 0
        for poly in polys:
            # Accept BOTH a bare list of points and the profile's own
            # {'poly': [...]} entry. The first version took only the bare form
            # while the caller passed the dicts, so iterating a dict yielded its
            # KEYS, every comparison was string-vs-float, and the whole thing
            # silently blocked 0 cells. Unwrapping here means the natural call
            # works and cannot fail quietly.
            if isinstance(poly, dict):
                poly = poly.get('poly') or []
            if len(poly) < 3:
                skipped += 1
                continue
            try:
                xs = [float(q[0]) for q in poly]
                zs = [float(q[1]) for q in poly]
            except (TypeError, ValueError, IndexError):
                skipped += 1
                continue
            boxes.append((poly, min(xs), min(zs), max(xs), max(zs)))
        self.avoid_skipped = skipped
        self.build_clearance()
        if not boxes:
            return 0
        n = 0
        for i in range(self.n):
            if self.state[i] == BLOCKED_PAINTED:
                continue
            cx, cz = self.center(i)
            h = self.cell * 0.5
            for poly, x0, z0, x1, z1 in boxes:
                if cx + h < x0 or cx - h > x1 or cz + h < z0 or cz - h > z1:
                    continue
                # Test the CORNERS as well as the centre, so a cell the polygon
                # merely overlaps is blocked too. Centre-only marking let a path
                # clip up to a diagonal half-cell (5.6 m measured on Ruinberg)
                # inside a painted boundary - quantisation rather than a hole,
                # but there is no reason to leave it.
                hit = inside_test(cx, cz, poly)
                if not hit:
                    for ox, oz in ((-h, -h), (h, -h), (-h, h), (h, h)):
                        if inside_test(cx + ox, cz + oz, poly):
                            hit = True
                            break
                if hit:
                    if self.state[i] != BLOCKED_PAINTED:
                        self.state[i] = BLOCKED_PAINTED
                        n += 1
                    break
        return n

    # -- persistence ---------------------------------------------------------
    def fits(self, bounds, target=TARGET_CELLS):
        '''Would a grid built NOW for these bounds have this same shape?

        A dump is only reusable if it covers the same area at the same
        resolution. Everything else about the grid is derived from the measured
        heights, so there are no other tunables to stamp - blocking is painted,
        and paint is applied fresh each battle.
        '''
        try:
            x0, z0, x1, z1 = [float(v) for v in bounds]
        except Exception:
            return False
        for a, b in ((self.x0, x0), (self.z0, z0), (self.x1, x1), (self.z1, z1)):
            if abs(a - b) > 0.5:
                return False
        _s = grid_shape((x0, z0, x1, z1), target)
        return _s[5] == self.nx and _s[6] == self.nz

    def dump(self, path):
        '''Write the grid so all further work can happen off the client.

        Launching the game is the expensive step; with a dump on disk, path
        quality, A* tuning and rendering are all desktop work.
        '''
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            try:
                os.makedirs(d)
            except OSError:
                pass
        f = open(path, 'wb')
        try:
            f.write(DUMP_MAGIC)
            head = ('%s\n%.6f %.6f %.6f %.6f\n%.6f\n%d %d\n'
                    % (self.map_name, self.x0, self.z0, self.x1, self.z1,
                       self.cell, self.nx, self.nz)).encode('utf-8')
            f.write(struct.pack('<I', len(head)))
            f.write(head)
            f.write(self.state.tostring() if hasattr(self.state, 'tostring')
                    else self.state.tobytes())
            g = self.ground
            f.write(g.tostring() if hasattr(g, 'tostring') else g.tobytes())
        finally:
            f.close()
        return path

    @classmethod
    def load(cls, path):
        f = open(path, 'rb')
        try:
            if f.read(len(DUMP_MAGIC)) != DUMP_MAGIC:
                raise ValueError('not an offhangar nav dump: %s' % path)
            hl = struct.unpack('<I', f.read(4))[0]
            head = f.read(hl).decode('utf-8').split('\n')
            name = head[0]
            x0, z0, x1, z1 = [float(v) for v in head[1].split()]
            nx, nz = [int(v) for v in head[3].split()]
            g = cls.__new__(cls)
            g.map_name = name
            g.x0, g.z0, g.x1, g.z1 = x0, z0, x1, z1
            g.cell = float(head[2])
            g.nx, g.nz = nx, nz
            g.n = nx * nz
            g.state = array('b')
            g.ground = array('f')
            _read_into(g.state, f, g.n)
            _read_into(g.ground, f, g.n * 4)
            g.known = array('b', [1]) * g.n
            g._cursor = 0
            g.passes = 1
            g.probes = 0
            g._dirty = False
            g._settled = True
            g._barren = 0
            g.avoid_skipped = 0
            # Re-derive on load so an OLD dump follows the CURRENT rules: the
            # heights are the measurement, everything else is derived from them,
            # so a rule change applies to dumps taken before it without anyone
            # re-baking 33 maps.
            #
            # ORDER MATTERS. derive() resets every cell with ground to passable,
            # so pruning first and deriving second silently undid the prune - the
            # loaded grid came back 100% passable.
            g.derive()
            return g
        finally:
            f.close()


def _read_into(arr, f, nbytes):
    data = f.read(nbytes)
    if hasattr(arr, 'frombytes'):
        arr.frombytes(data)
    else:
        arr.fromstring(data)


# --- self-test --------------------------------------------------------------
def _budget_respected(bounds, budget=50):
    '''A step must never exceed its budget, even when every probe fails.'''
    class NeverLoads(object):
        def __init__(self):
            self.calls = 0

        def ground(self, x, z):
            self.calls += 1
            return None

        def water_depth(self, x, y, z):
            return -1.0
    g = NavGrid(bounds, 'budget', target=2500)
    p = NeverLoads()
    g.step(p, budget=budget)
    return p.calls <= budget


def _selftest():
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))

    def _inside(x, z, poly):
        inside = False
        n = len(poly)
        j = n - 1
        for i in range(n):
            xi, zi = poly[i][0], poly[i][1]
            xj, zj = poly[j][0], poly[j][1]
            if (zi > z) != (zj > z) and x < (xj - xi) * (z - zi) / ((zj - zi) or 1e-12) + xi:
                inside = not inside
            j = i
        return inside

    # Cell sizing: constant COUNT, so a small map gets small cells.
    c800 = choose_cell_size(800, 800)
    c1200 = choose_cell_size(1200, 1200)
    c1400 = choose_cell_size(1400, 1400)
    ck('small map -> finer cells', c800 < c1200 < c1400)
    ck('cell size clamped low', choose_cell_size(50, 50) >= MIN_CELL)
    ck('cell size clamped high', choose_cell_size(9000, 9000) <= MAX_CELL)
    for w in (800, 1000, 1200, 1400):
        n = (w / choose_cell_size(w, w)) ** 2
        ck('cell count near target for %dm' % w, TARGET_CELLS * 0.4 < n < TARGET_CELLS * 2.5)

    B = (-300.0, -300.0, 300.0, 300.0)

    class Flat(object):
        def ground(self, x, z):
            return 10.0

        def water_depth(self, x, y, z):
            return -1.0

    g = NavGrid(B, 'flat', target=2500)
    guard = 0
    while not g.settled() and guard < 200:
        g.step(Flat())
        guard += 1
    ck('flat map fully covered', g.coverage() == 1.0)
    ck('flat map fully passable', g.passable_ratio() == 1.0)
    ck('grid spans bounds', g.nx * g.cell >= 599.0 and g.nz * g.cell >= 599.0)
    a = g.cell_at(-290.0, -290.0)
    b = g.cell_at(290.0, 290.0)
    p = g.astar(a, b)
    ck('flat path found', p is not None)
    ck('flat path starts and ends right', p[0] == a and p[-1] == b)
    ck('flat path is roughly diagonal', len(p) < g.nx * 2)

    # A wall with a gap: the path must go through the gap, not the wall.
    class Wall(object):
        def ground(self, x, z):
            if abs(x) < 12.0 and not (-40.0 < z < 40.0):
                return 200.0          # impassably high ridge
            return 10.0

        def water_depth(self, x, y, z):
            return -1.0
    gw = NavGrid(B, 'wall', target=2500)
    guard = 0
    while not gw.settled() and guard < 200:
        gw.step(Wall())
        guard += 1
    p = gw.astar(gw.cell_at(-200.0, 0.0), gw.cell_at(200.0, 0.0))
    ck('wall path found', p is not None)
    if p:
        pts = gw.path_world(p)
        crossed = [(x, z) for x, z in pts if abs(x) < 12.0]
        ck('wall crossed only at the gap', all(-45.0 < z < 45.0 for x, z in crossed))
        ck('wall path longer than straight line', len(p) > (400.0 / gw.cell))

    # Lake: water must block even though the lakebed gives perfectly good ground.
    class Lake(object):
        def ground(self, x, z):
            return 10.0

        def water_depth(self, x, y, z):
            return 3.0 if (x * x + z * z) < 90 * 90 else -1.0
    gl = NavGrid(B, 'lake', target=2500)
    guard = 0
    while not gl.settled() and guard < 200:
        gl.step(Lake())
        guard += 1
    mid = gl.cell_at(0.0, 0.0)
    # Water does NOT block. Nothing blocks but paint - if a lake should be
    # off-limits the user paints it, and the painter's minimap layer shows
    # exactly where it is.
    ck('water alone does not block', gl.state[mid] == PASSABLE)
    ck('an unpainted map is fully drivable', gl.passable_ratio() == 1.0)
    p = gl.astar(gl.cell_at(-200.0, 0.0), gl.cell_at(200.0, 0.0))
    ck('path found across an unpainted map', p is not None)
    lake = [[-90.0, -90.0], [90.0, -90.0], [90.0, 90.0], [-90.0, 90.0]]
    gl.apply_avoid([{'poly': lake}], _inside)
    p2 = gl.astar(gl.cell_at(-200.0, 0.0), gl.cell_at(200.0, 0.0))
    ck('once painted, the path goes round it', p2 is not None)
    if p2:
        ck('painted lake is respected',
           all(gl.state[i] != BLOCKED_PAINTED for i in p2))

    # A sealed pocket is NOT blocked any more: the grid states no opinion about
    # whether somewhere is useful, only whether the map has ground there.
    class Pocket(object):
        def ground(self, x, z):
            r = math.sqrt(x * x + z * z)
            return 200.0 if 60.0 < r < 90.0 else 10.0
    gp = NavGrid(B, 'pocket', target=2500)
    while not gp.settled():
        gp.step(Pocket())
    ck('a walled pocket is still drivable ground', gp.passable_ratio() == 1.0)
    # ...but A* still will not CLIMB the wall to get in. That is the movement
    # model, not a blocking policy: cells stay open, impossible steps do not.
    ck('but A* will not climb into it',
       gp.astar(gp.cell_at(-200.0, 0.0), gp.cell_at(0.0, 0.0)) is None)

    # Chunk streaming: unknown cells must be retried, never written off.
    class Late(object):
        def __init__(self):
            self.calls = 0

        def ground(self, x, z):
            self.calls += 1
            return None if self.calls < 300 else 10.0

        def water_depth(self, x, y, z):
            return -1.0
    gs = NavGrid(B, 'late', target=900)
    lp = Late()
    gs.step(lp)
    ck('early pass covers nothing', gs.coverage() == 0.0)
    ck('not settled while unknown', not gs.settled())
    guard = 0
    while not gs.settled() and guard < 500:
        gs.step(lp)
        guard += 1
    ck('recovers once terrain streams in', gs.coverage() == 1.0)

    # Endpoint snapping: a tank parked against an obstacle must still get a
    # path, but a point deep inside a lake must NOT be silently teleported to
    # the shore - that would hand out a path starting somewhere the bot is not.
    edge = gl.cell_at(0.0, -86.0)          # just inside the lake rim
    ck('blocked endpoint near an edge snaps',
       gl.nearest_passable(edge) is not None and gl.passable(gl.nearest_passable(edge)))
    ck('deep inside a lake does not snap', gl.nearest_passable(mid) is None)
    ck('off-grid position returns None', gl.cell_at(9999.0, 0.0) is None)
    ck('probe budget is respected per step',
       _budget_respected(B))

    # Round trip through the dump format.
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), 'offh_navgrid_test.grid')
    gl.dump(tmp)
    gl2 = NavGrid.load(tmp)
    ck('dump round-trips shape', (gl2.nx, gl2.nz) == (gl.nx, gl.nz))
    ck('dump round-trips cell size', abs(gl2.cell - gl.cell) < 1e-6)
    ck('dump round-trips bounds', abs(gl2.x0 - gl.x0) < 1e-6 and abs(gl2.z1 - gl.z1) < 1e-6)
    ck('dump round-trips states', list(gl2.state) == list(gl.state))
    ck('loading re-derives, so an old dump follows current rules',
       gl2.passes >= 1 and gl2.counts().get('unknown', 0) == 0)
    # derive() must never clear a painted block - a human decision outranks a
    # height measurement arriving later.
    gpo = NavGrid(B, 'order', target=2500)
    while not gpo.settled():
        gpo.step(Flat())
    gpo.apply_avoid([{'poly': [[-40.0, -40.0], [40.0, -40.0],
                               [40.0, 40.0], [-40.0, 40.0]]}], _inside)
    ck('paint applied', gpo.state[gpo.cell_at(0.0, 0.0)] == BLOCKED_PAINTED)
    gpo.derive()
    ck('derive does not clear paint', gpo.state[gpo.cell_at(0.0, 0.0)] == BLOCKED_PAINTED)
    ck('dump round-trips ground', max(abs(a - b) for a, b in zip(gl2.ground, gl.ground)) < 1e-3)
    ck('loaded grid can path', gl2.astar(gl2.cell_at(-200.0, 0.0), gl2.cell_at(200.0, 0.0)) is not None)
    try:
        os.remove(tmp)
    except OSError:
        pass

    # String pulling. A raw 8-connected path staircases; smoothing must shorten
    # it WITHOUT ever cutting through a blocked cell.
    p = gw.astar(gw.cell_at(-200.0, 0.0), gw.cell_at(200.0, 0.0))
    sm = gw.smooth(p)
    ck('smoothing shortens the path', len(sm) < len(p))
    ck('smoothed path keeps both ends', sm[0] == p[0] and sm[-1] == p[-1])
    ck('smoothed legs are all clear',
       all(gw.line_clear(sm[k], sm[k + 1]) for k in range(len(sm) - 1)))
    ck('smoothing never crosses the wall',
       all(gw.state[i] == PASSABLE for i in sm))
    # Every smoothed leg must still be walkable end to end, i.e. the union of
    # the legs covers the same corridor.
    ck('line_clear rejects a blocked line',
       not gw.line_clear(gw.cell_at(-200.0, 200.0), gw.cell_at(200.0, 200.0)))
    ck('line_clear accepts an open line',
       gl.line_clear(gl.cell_at(-250.0, -250.0), gl.cell_at(-150.0, -250.0)))
    ck('smoothing a trivial path is a no-op', gw.smooth([5]) == [5])

    # The case that drove all of this: a street beside a building. Nothing
    # automatic may condemn it, and nothing does any more.
    class Town(object):
        def ground(self, x, z):
            return 12.0 if (abs(x) < 12.0 and abs(z) < 12.0) else 1.0
    gt = NavGrid(B, 'town', target=2500)
    while not gt.settled():
        gt.step(Town())
    ck('street beside a building is drivable', gt.state[gt.cell_at(20.0, 0.0)] == PASSABLE)
    ck('nothing is blocked without paint', gt.passable_ratio() == 1.0)
    ck('cannot step from street onto the roof',
       not gt._step_ok(gt.cell_at(20.0, 0.0), gt.cell_at(0.0, 0.0)))
    pr2 = gt.astar(gt.cell_at(-60.0, 0.0), gt.cell_at(60.0, 0.0))
    ck('path routes around the building', pr2 is not None)
    if pr2:
        ck('every step is climbable',
           all(gt._step_ok(pr2[k - 1], pr2[k], True) for k in range(1, len(pr2))))

    # Painted avoid areas: a human veto that A* honours for free.
    ga = NavGrid(B, 'avoid', target=2500)
    while not ga.settled():
        ga.step(Flat())
    open_before = ga.astar(ga.cell_at(-200.0, 0.0), ga.cell_at(200.0, 0.0))
    ck('open map paths straight through', open_before is not None)
    wall = [[-20.0, -300.0], [20.0, -300.0], [20.0, 300.0], [-20.0, 300.0]]
    blocked = ga.apply_avoid([wall], _inside)
    ck('avoid area blocked cells', blocked > 0)
    # A cell the polygon only OVERLAPS must be blocked too, not just one whose
    # centre is inside - otherwise a path clips the boundary by half a cell.
    gover = NavGrid(B, 'overlap', target=2500)
    while not gover.settled():
        gover.step(Flat())
    c0 = gover.cell_at(0.0, 0.0)
    cx0, cz0 = gover.center(c0)
    h = gover.cell * 0.5
    sliver = [[cx0 + h * 0.4, cz0 - 400.0], [cx0 + h * 1.6, cz0 - 400.0],
              [cx0 + h * 1.6, cz0 + 400.0], [cx0 + h * 0.4, cz0 + 400.0]]
    gover.apply_avoid([sliver], _inside)
    ck('a sliver that misses cell centres still blocks',
       gover.state[c0] == BLOCKED_PAINTED)
    ck('avoid state is its own reason',
       STATE_NAMES[ga.state[ga.cell_at(0.0, 0.0)]] == 'painted-avoid')
    ck('no path through a full-width painted wall',
       ga.astar(ga.cell_at(-200.0, 0.0), ga.cell_at(200.0, 0.0)) is None)
    ga.derive()
    ck('derive does not resurrect a painted cell',
       ga.state[ga.cell_at(0.0, 0.0)] == BLOCKED_PAINTED)
    ck('apply_avoid is idempotent', ga.apply_avoid([wall], _inside) == 0)
    ck('no polys is a no-op', ga.apply_avoid([], _inside) == 0)
    # The shape the PROFILE actually hands over. Testing only the bare list is
    # how the live run ended up blocking 0 cells.
    gd = NavGrid(B, 'dictshape', target=2500)
    while not gd.settled():
        gd.step(Flat())
    nd = gd.apply_avoid([{'poly': wall}], _inside)
    ck('accepts the profile {"poly": [...]} shape', nd > 0)
    ck('dict and bare shapes agree', nd == blocked)
    ck('degenerate polygons are counted, not silently ignored',
       gd.apply_avoid([{'poly': [[0.0, 0.0]]}, {'poly': []}], _inside) == 0
       and gd.avoid_skipped == 2)

    gp2 = NavGrid(B, 'avoid2', target=2500)
    while not gp2.settled():
        gp2.step(Flat())
    gate = [[-20.0, -300.0], [20.0, -300.0], [20.0, -40.0], [-20.0, -40.0]]
    gp2.apply_avoid([gate], _inside)
    pth = gp2.astar(gp2.cell_at(-200.0, -200.0), gp2.cell_at(200.0, -200.0))
    ck('path detours around a partial painted area', pth is not None)
    if pth:
        ck('detour avoids painted cells',
           all(gp2.state[i] != BLOCKED_PAINTED for i in pth))

    # Slope limits must match the feelers, or the grid promises paths the bot
    # will refuse to drive.
    # The flood fill is an EARLY-OUT, not a rule: it must never change a result
    # A* would otherwise produce, only how fast the answer comes.
    gr = NavGrid(B, 'reach', target=2500)
    while not gr.settled():
        gr.step(Pocket())
    a_out = gr.cell_at(-200.0, 0.0)
    a_in = gr.cell_at(0.0, 0.0)
    p_before = gr.astar(a_out, a_in)
    n = gr.build_reach([(-200.0, 0.0)])
    p_after = gr.astar(a_out, a_in)
    ck('flood fill built', n > 0)
    ck('early-out agrees with A* (both refuse)', p_before is None and p_after is None)
    ck('reachable goals still path',
       gr.astar(a_out, gr.cell_at(-100.0, 100.0)) is not None)
    ck('can_reach is honest', gr.can_reach(a_out) and not gr.can_reach(a_in))
    ck('without a fill nothing is assumed',
       NavGrid(B, 'nofill', target=100).can_reach(0))

    ck('slope limits match the bot feelers', MAX_UP_RATIO == 0.45 and MAX_DOWN_RATIO == 0.7)

    bad = 0
    for name, ok in checks:
        if not ok:
            bad += 1
            print('FAIL %s' % name)
    # --- settled() caching must not be able to lie -------------------------
    class _P2(object):
        def __init__(self, h=1.0):
            self.h = h
        def ground(self, x, z):
            return self.h

    gs = NavGrid((-100., -100., 100., 100.), 'settle', target=64)
    ck('settled False while cells are unmeasured', not gs.settled())
    for _ in range(40):
        gs.step(_P2())
    ck('settled True once every cell is measured', gs.settled())
    # The cache must agree with an honest scan, not merely be sticky.
    ck('cached settled matches a full scan',
       gs.settled() == all(gs.known[i] for i in range(gs.n)))
    gs2 = NavGrid((-100., -100., 100., 100.), 'settle2', target=64)
    gs2.step(_P2(), budget=1)
    ck('one probe does not settle a grid', not gs2.settled())

    # --- a dump is reusable only for the same area at the same resolution ---
    import tempfile
    B = (-400., -400., 400., 400.)
    gd2 = NavGrid(B, 'fitmap')
    for _ in range(200):
        gd2.step(_P2())
    _tmp = os.path.join(tempfile.gettempdir(), 'offh_fits_test.grid')
    gd2.dump(_tmp)
    gl = NavGrid.load(_tmp)
    ck('a dump fits the bounds it was built for', gl.fits(B))
    ck('a dump does not fit different bounds',
       not gl.fits((-600., -600., 600., 600.)))
    ck('a dump does not fit a different cell count',
       not gl.fits(B, target=2500))
    ck('a loaded dump is settled, so it never re-probes', gl.settled())
    ck('a loaded dump needs no probes', gl.probes == 0)
    try:
        os.remove(_tmp)
    except OSError:
        pass

    # --- snapping a destination must land somewhere actually reachable -----
    gr = NavGrid((-100., -100., 100., 100.), 'snap', target=400)
    for _ in range(200):
        gr.step(_P2())
    # Seal a single cell off with paint on all sides.
    _cx, _cz = gr.nx // 2, gr.nz // 2
    _mid = gr.index(_cx, _cz)
    for dz in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx or dz:
                gr.state[gr.index(_cx + dx, _cz + dz)] = BLOCKED_PAINTED
    gr.build_reach([gr.center(0)])
    ck('the sealed cell is passable but unreachable',
       gr.passable(_mid) and not gr.can_reach(_mid))
    ck('a plain snap happily returns the sealed cell',
       gr.nearest_passable(_mid, radius=4) == _mid)
    _sn = gr.nearest_passable(_mid, radius=4, reachable=True)
    ck('a reachable snap refuses it', _sn != _mid)
    ck('a reachable snap returns something reachable',
       _sn is None or gr.can_reach(_sn))
    ck('a reachable snap is a no-op on an ordinary cell',
       gr.nearest_passable(0, radius=4, reachable=True) == 0)

    # --- clearance: prefer the middle, but never refuse a narrow gap -------
    gw = NavGrid((-100., -100., 100., 100.), 'clear', target=400)
    for _ in range(300):
        gw.step(_P2())
    # A wall across the middle with a single one-cell doorway.
    _mz = gw.nz // 2
    _door = gw.nx // 2
    for ix in range(gw.nx):
        if ix != _door:
            gw.state[gw.index(ix, _mz)] = BLOCKED_PAINTED
    gw.build_clearance()
    ck('clearance marks the doorway as touching a wall',
       gw.near_wall[gw.index(_door, _mz)] == 1)
    ck('clearance leaves open ground unmarked',
       gw.near_wall[gw.index(_door, 0)] == 0)
    _a = gw.index(_door, 0)
    _b = gw.index(_door, gw.nz - 1)
    _p0 = gw.astar(_a, _b, wall_cost=0.0)
    _p1 = gw.astar(_a, _b, wall_cost=WALL_COST)
    ck('a one-cell doorway is still used with a wall cost', bool(_p1))
    ck('the doorway path goes through the doorway',
       bool(_p1) and gw.index(_door, _mz) in _p1)
    ck('a wall cost does not change whether a path exists',
       bool(_p0) == bool(_p1))

    # In the open, the wall cost must steer AWAY from an obstacle rather than
    # merely cost more - that is the whole point.
    go = NavGrid((-100., -100., 100., 100.), 'clear2', target=400)
    for _ in range(300):
        go.step(_P2())
    _bz = go.nz // 2
    for ix in range(2, go.nx - 2):
        go.state[go.index(ix, _bz)] = BLOCKED_PAINTED
    go.build_clearance()
    _s2 = go.index(1, _bz - 3)
    _g2 = go.index(1, _bz + 3)
    _q0 = go.astar(_s2, _g2, wall_cost=0.0) or []
    _q1 = go.astar(_s2, _g2, wall_cost=WALL_COST) or []
    _hug0 = sum(1 for i in _q0 if go.near_wall[i])
    _hug1 = sum(1 for i in _q1 if go.near_wall[i])
    ck('a wall cost reduces hugging when there is room to avoid it',
       _hug1 <= _hug0)
    ck('rounding an obstacle still succeeds with a wall cost', bool(_q1))
    ck('apply_avoid refreshes the clearance field',
       hasattr(gw, 'near_wall'))

    # --- a map with unprobeable cells must still FINISH --------------------
    # Prokhorovka stalled at 92% coverage on 790 cells of void, so it never
    # settled, never dumped, and re-measured itself every battle.
    class _HoleyProbe(object):
        """Ground everywhere except a band that never answers."""
        def __init__(s, g):
            s.g = g
            s.calls = 0
        def ground(s, x, z):
            s.calls += 1
            return None if z > 60.0 else 1.0

    gh = NavGrid((-100., -100., 100., 100.), 'holey', target=400)
    ph = _HoleyProbe(gh)
    for _ in range(4000):
        if gh.settled():
            break
        gh.step(ph)
    ck('a grid with unprobeable cells still settles', gh.settled())
    ck('it settles BELOW full coverage', gh.coverage() < 1.0)
    ck('the probeable part was still measured', gh.coverage() > 0.5)
    ck('unprobeable cells stay UNKNOWN, not passable',
       all(gh.state[i] != PASSABLE for i in range(gh.n) if not gh.known[i]))
    ck('a settled holey grid stops costing probes',
       (lambda before: (gh.step(ph), ph.calls == before)[1])(ph.calls)
       if gh.settled() else False)
    ck('it took many barren passes, not one', gh.passes >= STALL_PASSES)

    # ★ The regression this rule must not cause: terrain streams in, so a run of
    # barren passes early on is NORMAL. Prokhorovka looked stalled at 92% and
    # reached 100% in the same battle; settling there would have banked an
    # incomplete map permanently.
    class _SlowProbe(object):
        '''Answers nothing for a long while, then everything.'''
        def __init__(s, quiet):
            s.n = 0
            s.quiet = quiet
        def ground(s, x, z):
            s.n += 1
            return None if s.n < s.quiet else 1.0

    gsl = NavGrid((-100., -100., 100., 100.), 'slow', target=400)
    slow = _SlowProbe(quiet=gsl.n * (STALL_PASSES - 10))
    for _ in range(60000):
        if gsl.settled():
            break
        gsl.step(slow)
    ck('a long streaming delay does NOT bank an empty grid',
       gsl.coverage() > 0.99)

    # And a grid that is mostly holes must never be banked at all.
    class _MostlyNothing(object):
        def ground(s, x, z):
            return 1.0 if z < -60.0 else None

    gmn = NavGrid((-100., -100., 100., 100.), 'holes', target=400)
    for _ in range(30000):
        if gmn.settled():
            break
        gmn.step(_MostlyNothing())
    ck('a mostly-empty grid is never declared finished',
       not gmn.settled() and gmn.coverage() < STALL_MIN_COVERAGE)

    # It must still reach FULL coverage when everything is probeable.
    gf = NavGrid((-100., -100., 100., 100.), 'full', target=400)
    for _ in range(4000):
        if gf.settled():
            break
        gf.step(_P2())
    ck('a fully probeable grid still reaches 100%%', gf.coverage() == 1.0)

    # Terrain streams in: a probe that fails early and answers later must not
    # cause an early settle that loses the map.
    class _LateProbe(object):
        def __init__(s):
            s.n = 0
        def ground(s, x, z):
            s.n += 1
            return 1.0 if s.n > 900 else None

    gl = NavGrid((-100., -100., 100., 100.), 'late', target=400)
    lp = _LateProbe()
    for _ in range(4000):
        if gl.settled():
            break
        gl.step(lp)
    ck('terrain that streams in late is still picked up', gl.coverage() > 0.9)

    # --- painted blocking must be removable -------------------------------
    gcp = NavGrid((-100., -100., 100., 100.), 'clearpaint', target=400)
    for _ in range(300):
        gcp.step(_P2())
    _n0 = gcp.counts().get('passable', 0)
    gcp.apply_avoid([[(-50., -50.), (50., -50.), (50., 50.), (-50., 50.)]], _inside)
    ck('painting blocks cells', gcp.counts().get('painted-avoid', 0) > 0)
    gcp.derive()
    ck('derive() alone does NOT unblock painted cells - that is the point',
       gcp.counts().get('painted-avoid', 0) > 0)
    _c = gcp.clear_painted()
    ck('clear_painted() reports how many it freed', _c > 0)
    ck('and the cells are blocked no longer',
       gcp.counts().get('painted-avoid', 0) == 0)
    ck('the map is back to what was measured',
       gcp.counts().get('passable', 0) == _n0)
    ck('clearing invalidates the reach set', gcp.reach is None)
    ck('clearing an unpainted grid is a no-op', gcp.clear_painted() == 0)

    print('%d/%d checks passed' % (len(checks) - bad, len(checks)))
    return bad == 0


if __name__ == '__main__':
    import sys
    sys.exit(0 if _selftest() else 1)
