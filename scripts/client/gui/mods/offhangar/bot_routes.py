# -*- coding: utf-8 -*-
'''Baked per-map bot destinations, and the class-aware choice of one.

WHY THIS EXISTS
    The bot AI's only notion of "where do I go" is the nearest living enemy.
    Both teams start on opposite sides, so every bot beelines at the enemy
    line-up and all 30 converge on the midpoint of the map - the "they rush
    into the middle and die" report. Nothing about the terrain or the tank's
    class enters into it.

WHERE THE DATA COMES FROM
    Wargaming's own bot navigation graph. Modern clients ship it per map in
    spaces/<map>/space.bin (section 'UDOS', a BigWorld packed DataSection) as
    AiZoneNode user-data-objects. The 0.8.2 maps predate bots entirely and
    carry none of it, but the modern maps grew OUTWARD FROM THE SAME ORIGIN
    (red-line expansion), so the coordinates transfer: every extracted node on
    every shared map lands inside the corresponding 0.8.2 bounds.

    scratchpad/bake_routes.py does the extraction. It is a BUILD-TIME tool and
    needs a modern install; only its JSON output ships, in offhangar/routes/.

WHAT THE DATA ACTUALLY IS
    Not a connected navmesh. On Malinovka it is 134 nodes in 67 DISJOINT PAIRS
    (every node has degree exactly 1), median 296 m apart - authored movement
    legs, a start and an end, with a WG 'desirability' 0..100. So this module
    treats the nodes as a DESTINATION POOL and the pair link as "having got
    here, that is the sensible next place to be".

    WG's rich per-class scoring (preferenceHeavyTank and friends) lives on
    AiZoneCenter, which Malinovka has none of - so class preference is derived
    here instead, from cheap terrain probes at the ~134 node positions. That is
    two orders of magnitude cheaper than sampling the whole map.

VALIDATION, AND WHY IT MUST NOT BE STRICT
    Map layouts were reworked between 2012 and now. In-bounds does not mean
    still-drivable: a node can sit where a building now stands, or on a hill
    that was reshaped. So every node is re-probed against the LIVE 0.8.2 terrain
    and the baked Y is discarded - only X/Z carry over.

    Two lessons from the first live run, which reported 0/134 usable on a
    perfectly good Malinovka:

    1. A failed probe is not a verdict. Validation began within ~12 frames of
       battle start, before the terrain was queryable at all, and the result was
       latched forever. Failures are now RETRIED; only an out-of-bounds node, or
       a ledge confirmed by probes that all answered, is permanently rejected.
    2. Being unable to probe a node is not a reason to discard it. Steering only
       ever reads a destination's X/Z - the height is never used - so a node we
       cannot measure is still a perfectly good place to send a tank. After a
       few passes those are accepted provisionally, carrying baked elevation and
       neutral openness, and are upgraded in the background if a probe lands.

    The upshot: probing improves the CLASS SCORING, it does not gate the
    feature. The per-map pass rate is logged either way, so "which maps still
    work" stays a number the game prints rather than a guess.

No BigWorld imports: every world query arrives as a callback, which keeps this
desktop-testable. Run `python bot_routes.py` for the self-test.
'''

import json
import math
import os
import random

# --- tank classes -----------------------------------------------------------
HEAVY = 'heavy'
MEDIUM = 'medium'
LIGHT = 'light'
TD = 'td'
SPG = 'spg'
CLASSES = (HEAVY, MEDIUM, LIGHT, TD, SPG)


def class_of(tags):
    '''Map a vehicle descriptor's type tags onto our five buckets.'''
    if not tags:
        return MEDIUM
    if 'SPG' in tags:
        return SPG
    if 'AT-SPG' in tags:
        return TD
    if 'heavyTank' in tags:
        return HEAVY
    if 'lightTank' in tags:
        return LIGHT
    if 'mediumTank' in tags:
        return MEDIUM
    return MEDIUM


# --- how each class weighs a candidate destination --------------------------
# openness    how far you can see from the node, 0 (enclosed) .. 1 (wide open)
# elev        height rank among surviving nodes, 0 (low) .. 1 (high)
# fwd_target  where on the own-base -> enemy-base axis this class wants to BE,
#             0 = own base, 1 = enemy base
# fwd_hold    how hard it insists on that depth
# spread      penalty for picking what a team-mate already took
#
# fwd_target is a BAND, not a direction, and that matters: an earlier version
# simply rewarded "forward", so heavies, mediums and lights all saturated at
# 1.0 and drove into the enemy spawn together - the original converge-and-die
# behaviour reached by a longer route. Scoring distance from a per-class depth
# instead lays the team out with SPGs at the back, TDs behind the midline, the
# brawlers on the contested line and lights pushing furthest.
CLASS_WEIGHTS = {
    HEAVY:  {'openness': -0.55, 'elev': 0.10, 'fwd_target': 0.62, 'fwd_hold': 1.10, 'spread': 0.45},
    MEDIUM: {'openness': 0.05, 'elev': 0.25, 'fwd_target': 0.55, 'fwd_hold': 0.80, 'spread': 0.55},
    LIGHT:  {'openness': 0.20, 'elev': 0.35, 'fwd_target': 0.72, 'fwd_hold': 0.70, 'spread': 0.70},
    TD:     {'openness': 0.85, 'elev': 0.45, 'fwd_target': 0.33, 'fwd_hold': 0.90, 'spread': 0.50},
    SPG:    {'openness': 0.55, 'elev': 0.30, 'fwd_target': 0.10, 'fwd_hold': 1.30, 'spread': 0.40},
}

# How far a bot must get before its destination counts as reached (m).
ARRIVE_RADIUS = 28.0
# An enemy THIS close is in our face - worth leaving the route to finish (m).
# Deliberately small. The first version diverted to the enemy at 260 m, which
# meant that the moment two advancing teams came within a third of a map of
# each other EVERY bot dropped its route and drove head-on at the nearest
# target: measured at 48% of all samples, and exactly the converge-on-the-
# middle behaviour this module exists to stop. Driving at a target is almost
# never right anyway - the gun is aimed by completely separate code, so a bot
# fights perfectly well while standing still.
BRAWL_RANGE = 60.0
# Having REACHED its position, a bot holds and fights while anything is within
# this (m) instead of walking on to the next node.
HOLD_RANGE = 400.0
# Ray length used to measure openness (m).
OPENNESS_RAY = 150.0
# Nodes validated per tick, so the probe cost never lands in one frame.
VALIDATE_SLICE = 12
# Passes to spend waiting for terrain before accepting a node we could not
# probe. Steering only ever reads a destination's X/Z - the height is never
# used - so an unprobeable node is still a perfectly good place to send a tank;
# all we lose is its elevation and openness scores. Dropping it instead would
# make the whole feature hostage to how BigWorld happens to stream chunks.
PROVISIONAL_AFTER_PASSES = 3
# Openness assumed for a node we never managed to probe.
NEUTRAL_OPENNESS = 0.5
# Standing water deeper than this makes a node unusable (m). A ground probe
# alone CANNOT catch this: wg_collideSegment happily returns the lakebed, so a
# node in the middle of a pond validates perfectly. The baked height is no help
# either - it comes from the modern map, where Malinovka's swamp was reworked,
# so a node that is dry land there can be under water in 0.8.2. This is the one
# check that has to come from the live map.
MAX_WADE_DEPTH = 0.8


def routes_dir(mod_dir):
    return os.path.join(mod_dir, 'routes')


def load_graph(map_name, mod_dir):
    '''Baked graph for a map, or None when we have not baked that map.

    map_name arrives as anything from 'spaces/02_malinovka' to '02_malinovka';
    only the leaf matters.
    '''
    if not map_name:
        return None
    leaf = str(map_name).replace('\\', '/').rstrip('/').split('/')[-1]
    path = os.path.join(routes_dir(mod_dir), '%s.routes.json' % leaf)
    if not os.path.exists(path):
        return None
    f = open(path, 'r')
    try:
        data = json.load(f)
    except ValueError:
        return None
    finally:
        f.close()
    nodes = data.get('nodes') or {}
    if not nodes.get('pos'):
        return None
    return data


class RouteMap(object):
    '''Validated, scored destinations for the battle currently loading.

    Built incrementally: call step() once per tick until ready is True. Until
    then callers should behave exactly as they did before this module existed.
    '''

    def __init__(self, graph, bounds=None):
        n = graph['nodes']
        self.pos = [list(p) for p in n['pos']]
        self.adj = [list(a) for a in n.get('adj') or []]
        self.desirability = list(n.get('desirability') or [100] * len(self.pos))
        self.bounds = bounds
        self.map_name = graph.get('map', '?')

        self.alive = [False] * len(self.pos)
        self.probed = [False] * len(self.pos)
        self.perm_dead = [False] * len(self.pos)
        self.openness = [0.0] * len(self.pos)
        self.ground_y = [0.0] * len(self.pos)
        self.elev = [0.0] * len(self.pos)
        self.live = []
        self._cursor = 0
        self.passes = 0
        self.ready = False
        self.taken = {}
        self.reasons = {'bounds': 0, 'noground': 0, 'ledge': 0, 'water': 0}

    # -- construction --------------------------------------------------------
    def step(self, probe, budget=VALIDATE_SLICE):
        '''Validate/score nodes, a slice per call. True once a full pass is done.

        Failures are RETRIED on later passes, deliberately. BigWorld streams
        terrain chunks by distance, so a probe into the far half of the map can
        legitimately find nothing during the opening seconds and succeed once
        the chunk arrives. The first version latched the result of one early
        pass and reported 0/134 usable on a map that was entirely fine.

        Only an out-of-bounds node is rejected permanently - that verdict cannot
        change. Once every node is either alive or permanently dead this settles
        down to a no-op, so it is safe to keep calling every frame.

        probe.ground(x, z)                      -> y or None
        probe.clear_dist(x, y, z, dx, dz, maxd) -> metres before something blocks
        '''
        n = len(self.pos)
        if not n:
            self.ready = True
            return True
        done = scanned = 0
        while done < budget and scanned < n:
            i = self._cursor
            self._cursor += 1
            if self._cursor >= n:
                self._cursor = 0
                self.passes += 1
                self._recompute()
            scanned += 1
            if self.probed[i] or self.perm_dead[i]:
                continue
            self._validate_one(i, probe)
            done += 1
        if self.passes >= 1 and not self.ready:
            self.ready = True
            self._recompute()
        if self.passes >= PROVISIONAL_AFTER_PASSES:
            self._promote_provisional()
        return self.ready

    def settled(self):
        '''True once no node can still change verdict - nothing left to retry.

        A provisionally-accepted node is usable but NOT settled: it is alive
        without real terrain behind its scores, so probing continues in the
        background to upgrade it.'''
        for i in range(len(self.pos)):
            if not self.probed[i] and not self.perm_dead[i]:
                return False
        return True

    def _promote_provisional(self):
        '''Accept whatever we still could not probe.

        Steering reads only X/Z, so these are real destinations; they just
        carry the baked elevation and a neutral openness until a probe lands.'''
        n = 0
        for i in range(len(self.pos)):
            if self.probed[i] or self.perm_dead[i] or self.alive[i]:
                continue
            if not self._in_bounds(self.pos[i][0], self.pos[i][2]):
                self.perm_dead[i] = True
                continue
            self.ground_y[i] = self.pos[i][1]
            self.openness[i] = NEUTRAL_OPENNESS
            self.alive[i] = True
            n += 1
        if n:
            self._recompute()
        return n

    def _in_bounds(self, x, z):
        b = self.bounds
        if not b:
            return True
        # A little slack: arena_defs sometimes place valid points just outside
        # their own declared box (Himmelsdorf's team 1 flag does exactly this).
        return (b[0] - 30.0) <= x <= (b[2] + 30.0) and (b[1] - 30.0) <= z <= (b[3] + 30.0)

    def _validate_one(self, i, probe):
        x, z = self.pos[i][0], self.pos[i][2]
        if not self._in_bounds(x, z):
            self.perm_dead[i] = True      # the only verdict that cannot change
            self.reasons['bounds'] += 1
            return
        y = probe.ground(x, z)
        if y is None:
            self.reasons['noground'] += 1
            return
        # Standing water. Optional on the probe so the desktop tests can leave
        # it out, but in the game it is what stops bots being sent into a lake.
        _wd = getattr(probe, 'water_depth', None)
        if _wd is not None:
            d = _wd(x, y, z)
            if d is not None and d > MAX_WADE_DEPTH:
                self.perm_dead[i] = True
                self.alive[i] = False
                self.reasons['water'] += 1
                return
        # Cliff / roof / inside-a-building reject: the ground around the node
        # has to exist and be at a comparable height. Same shape of test the
        # mod already trusts for validating spawn points. 4.5 m over a 6 m step
        # is about 37 degrees - steeper than anything drivable, so this rejects
        # ledges without rejecting honest hillside.
        for dx, dz in ((6.0, 0.0), (-6.0, 0.0), (0.0, 6.0), (0.0, -6.0)):
            ny = probe.ground(x + dx, z + dz)
            if ny is None:
                # Neighbour chunk not in yet - say nothing and come back to it.
                self.reasons['noground'] += 1
                return
            if abs(ny - y) > 4.5:
                # Every probe answered and the ground really does fall away:
                # a stable verdict, so stop re-testing it every pass. Revoke a
                # provisional acceptance if this node had already been let in.
                self.perm_dead[i] = True
                self.alive[i] = False
                self.reasons['ledge'] += 1
                return
        # The baked Y came off a modern map; trust only X/Z and re-seat here.
        self.pos[i][1] = y
        self.ground_y[i] = y
        total = 0.0
        rays = 8
        for k in range(rays):
            a = (2.0 * math.pi * k) / rays
            total += probe.clear_dist(x, y + 2.0, z, math.sin(a), math.cos(a), OPENNESS_RAY)
        self.openness[i] = (total / rays) / OPENNESS_RAY
        self.alive[i] = True
        self.probed[i] = True

    def _recompute(self):
        '''Refresh the live set and the elevation ranking.

        Called on every pass, not once: nodes keep arriving as chunks stream in,
        and elevation is a RANK among survivors, so it has to be restated each
        time the survivor set grows.'''
        live = [i for i in range(len(self.pos)) if self.alive[i]]
        if live:
            ys = [self.ground_y[i] for i in live]
            lo, hi = min(ys), max(ys)
            span = (hi - lo) or 1.0
            self.elev = [((self.ground_y[i] - lo) / span) if self.alive[i] else 0.0
                         for i in range(len(self.pos))]
        else:
            self.elev = [0.0] * len(self.pos)
        self.live = live

    # -- queries -------------------------------------------------------------
    def pass_rate(self):
        if not self.pos:
            return 0.0
        return len(self.live) / float(len(self.pos))

    def usable(self):
        return self.ready and len(self.live) >= 4

    def _forward(self, i, own_base, enemy_base):
        '''0 at your own base, 1 at the enemy base, projected onto the axis.'''
        if not own_base or not enemy_base:
            return 0.5
        ax, az = own_base[0], own_base[1]
        bx, bz = enemy_base[0], enemy_base[1]
        vx, vz = bx - ax, bz - az
        L2 = vx * vx + vz * vz
        if L2 <= 1.0:
            return 0.5
        px, pz = self.pos[i][0] - ax, self.pos[i][2] - az
        t = (px * vx + pz * vz) / L2
        return max(0.0, min(1.0, t))

    def score(self, i, cls, own_base, enemy_base):
        w = CLASS_WEIGHTS.get(cls) or CLASS_WEIGHTS[MEDIUM]
        fwd = self._forward(i, own_base, enemy_base)
        s = (w['openness'] * self.openness[i]
             + w['elev'] * self.elev[i]
             - w['fwd_hold'] * abs(fwd - w['fwd_target']))
        # WG's own authored value for the spot, gently applied: it is a real
        # signal but it was authored against the modern layout.
        s += 0.20 * (self.desirability[i] / 100.0)
        n = self.taken.get(i, 0)
        if n:
            s -= w['spread'] * n
        return s

    def pick(self, cls, own_base, enemy_base, rng=None, jitter=0.10):
        '''Best-scoring destination for one bot. Returns an index or None.

        jitter keeps 30 bots from stacking on one optimum and makes successive
        battles look different; the ranking still dominates.
        '''
        if not self.usable():
            return None
        best, best_s = None, None
        for i in self.live:
            s = self.score(i, cls, own_base, enemy_base)
            if rng is not None and jitter:
                s += (rng.random() - 0.5) * 2.0 * jitter
            if best_s is None or s > best_s:
                best, best_s = i, s
        if best is not None:
            self.taken[best] = self.taken.get(best, 0) + 1
        return best

    def claim(self, i):
        '''Mark a node as spoken for. pick() does this itself; callers that
        move a bot along an authored leg must do it explicitly, or the spread
        penalty stops seeing where everyone actually is.'''
        if i is not None:
            self.taken[i] = self.taken.get(i, 0) + 1

    def release(self, i):
        if i in self.taken:
            n = self.taken[i] - 1
            if n > 0:
                self.taken[i] = n
            else:
                del self.taken[i]

    def next_from(self, i, rng=None):
        '''Where to go having reached node i: follow the authored leg if it
        survived validation, else None so the caller re-picks.'''
        if i is None or i >= len(self.adj):
            return None
        opts = [j for j in self.adj[i] if j < len(self.alive) and self.alive[j]]
        if not opts:
            return None
        if rng is not None and len(opts) > 1:
            return opts[int(rng.random() * len(opts)) % len(opts)]
        return opts[0]

    def position(self, i):
        return tuple(self.pos[i])


# --- painted profiles -------------------------------------------------------
# The save format the desktop painter writes and the mod reads. Versioned from
# the start: the mod ships alongside profile files that a user may have painted
# with an older or newer tool, and silently misreading them would be worse than
# refusing them.
#
#   {"format": "offhangar-bot-profile",
#    "version": 1,
#    "map": "02_malinovka",
#    "bounds": [minX, minZ, maxX, maxZ],      # sanity check against the arena
#    "destinations": [{"pos": [x, z], "team": 1|2|0,
#                      "classes": ["heavy", ...], "role": ""}],
#    "routes":       [{"points": [[x, z], ...], "team": 1|2|0,
#                      "classes": [...], "name": ""}],
#    "avoid":        [{"poly": [[x, z], ...]}]}
#
# `avoid` is the ONLY human-defined blocking, and deliberately so. Two automatic
# slope heuristics were tried and both condemned drivable ground on a city map;
# the grid now blocks only what it MEASURED (water, no ground) or what it proved
# unreachable by flood fill. An `allow` key from an older build parses and is
# ignored - it existed to correct a heuristic that no longer exists.
#
# team 0 means "either side". Coordinates are world X/Z in metres - the same
# frame everything else in the mod uses, and NOT grid cells, so a profile stays
# valid if the grid resolution ever changes.
PROFILE_FORMAT = 'offhangar-bot-profile'
PROFILE_VERSION = 1
PROFILE_DIR = 'painted'


def profile_path(map_name, mod_dir):
    leaf = str(map_name or '').replace('\\', '/').rstrip('/').split('/')[-1]
    return os.path.join(mod_dir, PROFILE_DIR, '%s.paint.json' % leaf)


def _xz(v):
    """A coordinate pair, or None. Rejects anything that is not two numbers."""
    if not isinstance(v, (list, tuple)) or len(v) < 2:
        return None
    try:
        return (float(v[0]), float(v[1]))
    except (TypeError, ValueError):
        return None


def _clean_classes(v):
    out = []
    if isinstance(v, (list, tuple)):
        for c in v:
            if c in CLASSES and c not in out:
                out.append(c)
    return out


def _clean_team(v):
    try:
        t = int(v)
    except (TypeError, ValueError):
        return 0
    return t if t in (0, 1, 2) else 0


class Profile(object):
    """A validated painted profile, with the queries the bot AI needs.

    Malformed ENTRIES are dropped individually rather than failing the whole
    file - a profile is hand-authored, and losing a whole map's work to one bad
    row would be the wrong trade. Counts of what was dropped are kept in
    `.dropped` so it can be logged rather than hidden.
    """

    def __init__(self, map_name):
        self.map_name = map_name
        self.destinations = []
        self.routes = []
        self.avoid = []
        self.allow = []
        self.dropped = {'destinations': 0, 'routes': 0, 'avoid': 0, 'allow': 0}

    # -- queries -----------------------------------------------------------
    def _match(self, item, team, cls):
        if item['team'] not in (0, team):
            return False
        return (not item['classes']) or (cls in item['classes'])

    def destinations_for(self, team, cls):
        """World positions this team+class may be sent to."""
        return [d['pos'] for d in self.destinations if self._match(d, team, cls)]

    def routes_for(self, team, cls):
        """Ordered point lists this team+class may follow."""
        return [r['points'] for r in self.routes if self._match(r, team, cls)]

    def blocks(self, x, z):
        """True when (x, z) is inside a painted keep-out area."""
        for a in self.avoid:
            if point_in_poly(x, z, a['poly']):
                return True
        return False

    def allows(self, x, z):
        """True when (x, z) is inside a painted ALLOW area - a human override of
        the slope heuristic. Avoid still wins over allow."""
        for a in self.allow:
            if point_in_poly(x, z, a['poly']):
                return True
        return False

    def is_empty(self):
        return not (self.destinations or self.routes or self.avoid or self.allow)

    def summary(self):
        return ('%d destinations, %d routes, %d avoid areas, %d allow areas'
                % (len(self.destinations), len(self.routes), len(self.avoid),
                   len(self.allow)))


def point_in_poly(x, z, poly):
    """Even-odd test. Shared with the painter so both agree on what is inside."""
    inside = False
    n = len(poly)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, zi = poly[i][0], poly[i][1]
        xj, zj = poly[j][0], poly[j][1]
        if (zi > z) != (zj > z):
            if x < (xj - xi) * (z - zi) / ((zj - zi) or 1e-12) + xi:
                inside = not inside
        j = i
    return inside


def flip_profile_teams(prof):
    '''Swap team 1 and team 2 throughout, in place. Team 0 ("both") is left.'''
    n = 0
    for coll in (getattr(prof, 'destinations', None) or [],
                 getattr(prof, 'routes', None) or []):
        for e in coll:
            t = e.get('team')
            if t == 1:
                e['team'] = 2
                n += 1
            elif t == 2:
                e['team'] = 1
                n += 1
    if hasattr(prof, '_cache'):
        try:
            prof._cache.clear()
        except Exception:
            pass
    return n


def parse_profile(data, map_name=None, bounds=None):
    """dict -> Profile, or None when the document is not one we understand."""
    if not isinstance(data, dict):
        return None
    if data.get('format') != PROFILE_FORMAT:
        return None
    try:
        ver = int(data.get('version', 0))
    except (TypeError, ValueError):
        return None
    if ver > PROFILE_VERSION:
        return None            # newer than we know how to read - refuse, do not guess
    name = data.get('map') or map_name or '?'
    prof = Profile(name)

    for d in (data.get('destinations') or []):
        if not isinstance(d, dict):
            prof.dropped['destinations'] += 1
            continue
        pos = _xz(d.get('pos'))
        if pos is None or (bounds and not _in_box(pos, bounds)):
            prof.dropped['destinations'] += 1
            continue
        prof.destinations.append({'pos': pos, 'team': _clean_team(d.get('team')),
                                  'classes': _clean_classes(d.get('classes')),
                                  'role': str(d.get('role') or '')})

    for r in (data.get('routes') or []):
        if not isinstance(r, dict):
            prof.dropped['routes'] += 1
            continue
        pts = []
        for q in (r.get('points') or []):
            xz = _xz(q)
            if xz is not None and (not bounds or _in_box(xz, bounds)):
                pts.append(xz)
        if len(pts) < 2:
            prof.dropped['routes'] += 1
            continue
        prof.routes.append({'points': pts, 'team': _clean_team(r.get('team')),
                            'classes': _clean_classes(r.get('classes')),
                            'name': str(r.get('name') or '')})

    for key, dest in (('avoid', prof.avoid),):
        for a in (data.get(key) or []):
            if not isinstance(a, dict):
                prof.dropped[key] += 1
                continue
            poly = []
            for q in (a.get('poly') or []):
                xz = _xz(q)
                if xz is not None:
                    poly.append(xz)
            if len(poly) < 3:
                prof.dropped[key] += 1
                continue
            dest.append({'poly': poly})
    return prof


def _in_box(pos, bounds, slack=60.0):
    x0, z0, x1, z1 = bounds
    return (min(x0, x1) - slack <= pos[0] <= max(x0, x1) + slack
            and min(z0, z1) - slack <= pos[1] <= max(z0, z1) + slack)


def load_profile(map_name, mod_dir, bounds=None):
    """Painted profile for a map, or None when there is not one.

    Never raises: an unreadable profile must degrade to "no profile", not break
    the battle.
    """
    path = profile_path(map_name, mod_dir)
    if not os.path.exists(path):
        return None
    f = None
    try:
        f = open(path, 'r')
        data = json.load(f)
    except (ValueError, IOError, OSError):
        return None
    finally:
        if f is not None:
            f.close()
    return parse_profile(data, map_name, bounds)


def orientation_is_flipped(prof, base1, base2):
    """Is this profile's team 1 painted at the team-2 end of the map?

    The editor derives team numbers from the map's arena_def; the offline mod
    assigns them its own way, and on some maps the two disagree. When they do,
    every bot drives the full length of the map to the ENEMY spawn - both teams
    cross, meet in the middle, and it looks exactly like the "everyone rushes
    the centre" behaviour this whole feature exists to remove.

    Decided by measurement, not by trusting either side: compare where each
    team's routes START against where that team's base actually is. Returns None
    when there is not enough to compare, so an ambiguous case changes nothing.
    """
    if not base1 or not base2:
        return None
    starts = {}
    for team in (1, 2):
        pts = []
        for cls in (HEAVY, MEDIUM, LIGHT, TD, SPG):
            for r in prof.routes_for(team, cls):
                pts.append(r[0])
            pts.extend(prof.destinations_for(team, cls))
        if pts:
            starts[team] = (sum(p[0] for p in pts) / float(len(pts)),
                            sum(p[1] for p in pts) / float(len(pts)))
    if 1 not in starts and 2 not in starts:
        return None

    def d2(a, b):
        return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2

    same = flipped = 0.0
    for team, home, away in ((1, base1, base2), (2, base2, base1)):
        if team not in starts:
            continue
        same += d2(starts[team], home)
        flipped += d2(starts[team], away)
    if same == flipped:
        return None
    return flipped < same


# How close a class wants to be before it stops closing and fights. Reaching a
# painted position does not mean freezing there: a heavy that can see a target
# 400 m away should be closing on it, while a TD at 400 m is already exactly
# where it wants to be. This is what separates "held position" from "stopped".
ENGAGE_RANGE = {
    HEAVY:  70.0,       # brawler - wants to be in its face
    MEDIUM: 180.0,
    LIGHT:  240.0,      # fast, fragile, fights at arm's length
    TD:     400.0,      # sniping is the job; closing throws away its advantage.
                        # Deliberately equal to HOLD_RANGE: a TD that can see a
                        # target at all is already at a distance it likes.
    SPG:    1e9,        # never closes, ever
}


def engage_range(cls):
    """Distance at which this class stops advancing and fights."""
    return ENGAGE_RANGE.get(cls, ENGAGE_RANGE[MEDIUM])


def similar_classes(cls):
    """Classes whose routes this one can borrow, nearest first.

    A profile rarely covers all five classes, and a class with nothing painted
    would otherwise fall all the way back to chasing the nearest enemy - the
    behaviour this feature exists to replace. Borrowing is far better than that:
    a TD sent down an SPG's route is at roughly the right depth, which is most
    of what the route was saying.

    Ordered by how close the two classes want to be on the own-base ->
    enemy-base axis, DERIVED from CLASS_WEIGHTS rather than hardcoded, so the
    order cannot drift if those bands are retuned.
    """
    me = (CLASS_WEIGHTS.get(cls) or CLASS_WEIGHTS[MEDIUM])['fwd_target']
    others = [c for c in (HEAVY, MEDIUM, LIGHT, TD, SPG) if c != cls]
    return sorted(others,
                  key=lambda c: (abs(CLASS_WEIGHTS[c]['fwd_target'] - me), c))


def spread_radius(sharing, base_min=8.0, base_max=22.0):
    """How far to fan a bot out from a step several bots are heading for.

    One offset size cannot serve both cases: with two bots on a route, 8-22 m
    keeps them together as intended; with ten it is a pile-up. Area grows with
    the number sharing, so the radius grows as its square root - ten bots get
    about twice the spread of two, not ten times.
    """
    n = max(1, int(sharing))
    k = math.sqrt(n)
    return base_min * k, base_max * k


def profile_document(map_name, bounds, destinations, routes, avoid):
    """Build the document the painter writes - kept HERE so the writer and the
    reader can never drift apart."""
    return {'format': PROFILE_FORMAT, 'version': PROFILE_VERSION,
            'map': map_name,
            'bounds': [round(float(v), 1) for v in bounds] if bounds else None,
            'destinations': destinations, 'routes': routes, 'avoid': avoid}


# --- automatic route generation ---------------------------------------------
# Every map gets class-appropriate routes without anyone painting anything.
# The output is a PROFILE in exactly the format the painter writes, so it can be
# opened, edited and saved as a hand-made one - generated data is a starting
# point, never a separate mechanism.

# How many distinct routes to lay down per team+class. Several are needed for
# the same reason a real team does not drive in single file: bots of one class
# share a route, and one lane for fifteen heavies is a traffic jam.
ROUTES_PER_CLASS = 3

# Lateral lanes as a fraction of the map half-width, measured across the
# own-base -> enemy-base axis. Spread wide enough to be genuinely different
# approaches rather than three parallel lines a few metres apart.
LANES = (-0.62, 0.0, 0.62)

# Waypoints along a route. Two is a straight line and says nothing a pathfinder
# could not work out; four gives the route an actual shape to commit to.
ROUTE_STEPS = 4

# How far a generated point may be nudged, in metres, so successive generations
# and successive battles are not identical.
JITTER = 34.0


def _perp(ax, az):
    return -az, ax


def _norm(x, z):
    d = math.sqrt(x * x + z * z)
    return (x / d, z / d) if d > 1e-6 else (0.0, 1.0)


def generate_profile(grid, bases, bounds, map_name='?', seed=0,
                     classes=None, routes_per_class=ROUTES_PER_CLASS):
    """Lay out routes for EVERY class from the grid alone.

    bases is {team: [(x, z), ...]} - the anchors the runtime already knows.
    Each class is sent to its own depth band on the own-base -> enemy-base axis
    (a TD wants to be a third of the way over, a light most of the way), down
    one of several lateral lanes, with the intermediate steps A*-ed so a route
    follows ground a tank can actually drive rather than a straight line through
    a building.

    Classes NOT in the current battle are generated too, deliberately: the file
    is written once per map and reused, and which tanks turn up varies per
    battle. Generating only what is present would leave a map permanently
    missing light and SPG routes because the first battle happened to have none.

    Pure: no BigWorld, no I/O. Returns a profile document.
    """
    rnd = random.Random(seed)
    classes = classes or (HEAVY, MEDIUM, LIGHT, TD, SPG)
    anchors = {}
    for t in (1, 2):
        pts = list(bases.get(t) or [])
        if pts:
            anchors[t] = (sum(p[0] for p in pts) / float(len(pts)),
                          sum(p[1] for p in pts) / float(len(pts)))
    routes = []
    dests = []
    if len(anchors) < 2:
        # With only one anchor there is no axis and therefore no notion of
        # forward; say so by returning an empty profile rather than inventing
        # a direction.
        return profile_document(map_name, bounds, dests, routes, [])

    x0, z0, x1, z1 = [float(v) for v in bounds]
    half = max(x1 - x0, z1 - z0) * 0.5

    def snap(x, z):
        """Nearest cell a bot can actually be at AND get to."""
        i = grid.cell_at(x, z)
        if i is None:
            return None
        if not (grid.passable(i) and grid.can_reach(i)):
            i = grid.nearest_passable(i, radius=12, reachable=True)
        return i

    for team in (1, 2):
        home = anchors[team]
        away = anchors[2 if team == 1 else 1]
        ax, az = away[0] - home[0], away[1] - home[1]
        span = math.sqrt(ax * ax + az * az) or 1.0
        ux, uz = _norm(ax, az)
        px, pz = _perp(ux, uz)
        start = snap(home[0], home[1])
        for cls in classes:
            w = CLASS_WEIGHTS.get(cls) or CLASS_WEIGHTS[MEDIUM]
            depth = w['fwd_target']
            for n in range(routes_per_class):
                lane = LANES[n % len(LANES)]
                lane += (rnd.random() - 0.5) * 0.18
                pts = []
                for k in range(1, ROUTE_STEPS + 1):
                    # Ease out along the axis so the early steps leave the base
                    # promptly and the later ones creep, which is what a real
                    # advance looks like.
                    f = depth * (float(k) / ROUTE_STEPS) ** 0.75
                    # Fan out from the spawn rather than starting wide, or every
                    # route begins by driving sideways across your own base.
                    spread = lane * half * min(1.0, 0.35 + f * 1.3)
                    tx = home[0] + ux * span * f + px * spread
                    tz = home[1] + uz * span * f + pz * spread
                    tx += (rnd.random() - 0.5) * JITTER
                    tz += (rnd.random() - 0.5) * JITTER
                    i = snap(tx, tz)
                    if i is None:
                        continue
                    cx, cz = grid.center(i)
                    pts.append([round(cx, 1), round(cz, 1)])
                pts = _dedupe_steps(pts)
                if len(pts) < 2:
                    continue
                if start is not None:
                    pts = _follow_ground(grid, start, pts)
                # Last word: a step the runtime would only have to snap away
                # again is not worth writing. _follow_ground falls back to the
                # raw point when A* fails, which is the one way an undrivable
                # step can survive this far.
                pts = [q for q in pts if _drivable(grid, q[0], q[1])]
                pts = _dedupe_steps(pts)
                if len(pts) < 2:
                    continue
                routes.append({'points': pts, 'team': team,
                               'classes': [cls], 'generated': True})
    return profile_document(map_name, bounds, dests, routes, [])


def _drivable(grid, x, z):
    i = grid.cell_at(x, z)
    return i is not None and grid.passable(i) and grid.can_reach(i)


def _dedupe_steps(pts, min_gap=25.0):
    """Drop steps that land on top of the previous one.

    Snapping several targets into the same pocket collapses them onto one cell,
    and a route with two identical steps makes a bot arrive and immediately
    arrive again.
    """
    out = []
    for p in pts:
        if out:
            dx = p[0] - out[-1][0]
            dz = p[1] - out[-1][1]
            if dx * dx + dz * dz < min_gap * min_gap:
                continue
        out.append(p)
    return out


def _follow_ground(grid, start, pts):
    """Replace each straight hop with ground a tank can actually drive.

    A generated step is a point in space; the line to it may cross a building.
    A* between consecutive steps and keep the corners, so the route carries the
    same intent but along real ground. Falls back to the raw step whenever no
    path exists, so generation never fails outright.
    """
    out = []
    cur = start
    for p in pts:
        goal = grid.cell_at(p[0], p[1])
        if goal is None:
            out.append(p)
            continue
        path = grid.astar(cur, goal)
        if not path:
            out.append(p)
            cur = goal
            continue
        way = grid.smooth(path)
        # smooth() returns cells; keep a couple of interior corners so the route
        # bends around what it must, without turning into a cell-by-cell dump.
        for c in way[1:-1][:2]:
            cx, cz = grid.center(c)
            out.append([round(cx, 1), round(cz, 1)])
        out.append(p)
        cur = goal
    return _dedupe_steps(out)


# --- self-test --------------------------------------------------------------
def _selftest():
    import random

    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))

    ck('class heavy', class_of(('heavyTank',)) == HEAVY)
    ck('class td', class_of(('AT-SPG',)) == TD)
    ck('class spg beats at-spg', class_of(('SPG',)) == SPG)
    ck('class default', class_of(()) == MEDIUM)
    ck('class unknown tag', class_of(('observer',)) == MEDIUM)

    # A synthetic map: a low open field in the north, a high open ridge in the
    # south, and an enclosed corridor in the middle.
    pos = []
    kind = []
    for i in range(12):
        pos.append([-200.0 + i * 10.0, 0.0, -300.0]); kind.append('open_low')
    for i in range(12):
        pos.append([-200.0 + i * 10.0, 0.0, 300.0]); kind.append('open_high')
    for i in range(12):
        pos.append([-200.0 + i * 10.0, 0.0, 0.0]); kind.append('closed')
    graph = {'map': 'test', 'nodes': {
        'pos': pos,
        'adj': [[] for _ in pos],
        'desirability': [100] * len(pos),
    }}

    class P(object):
        def ground(self, x, z):
            if abs(x) > 400 or abs(z) > 400:
                return None
            return 40.0 if z > 200 else 2.0

        def clear_dist(self, x, y, z, dx, dz, maxd):
            return maxd if abs(z) > 200 else 12.0

    rm = RouteMap(graph, bounds=(-500.0, -500.0, 500.0, 500.0))
    guard = 0
    while not rm.step(P()) and guard < 100:
        guard += 1
    ck('validation completes', rm.ready)
    ck('all nodes survived', rm.pass_rate() == 1.0)
    ck('usable', rm.usable())
    ck('ground reseated', all(abs(rm.pos[i][1] - (40.0 if rm.pos[i][2] > 200 else 2.0)) < 1e-6
                              for i in rm.live))
    ck('openness split', rm.openness[0] > 0.9 and rm.openness[24] < 0.2)
    ck('elev split', rm.elev[12] > 0.9 and rm.elev[0] < 0.1)

    own, enemy = (0.0, -400.0), (0.0, 400.0)
    rng = random.Random(7)

    def best_kind(cls):
        r = RouteMap(graph, bounds=(-500.0, -500.0, 500.0, 500.0))
        while not r.step(P()):
            pass
        return kind[r.pick(cls, own, enemy, rng=rng, jitter=0.0)]

    ck('heavy takes cover', best_kind(HEAVY) == 'closed')
    ck('td takes the open ridge', best_kind(TD) == 'open_high')
    ck('spg stays back', best_kind(SPG) == 'open_low')

    # Depth layering. A plain "reward forward" model let heavy/medium/light all
    # saturate at the enemy base, which is the pile-into-the-middle failure this
    # whole module exists to remove - so assert the ordering explicitly, on a
    # map whose nodes are spread evenly along the base-to-base axis.
    ladder = [[0.0, 0.0, -400.0 + k * 40.0] for k in range(21)]
    graph3 = {'map': 'ladder', 'nodes': {
        'pos': ladder, 'adj': [[] for _ in ladder],
        'desirability': [100] * len(ladder)}}

    class Flat(object):
        def ground(self, x, z):
            return 1.0

        def clear_dist(self, x, y, z, dx, dz, maxd):
            return maxd * 0.5
    depth = {}
    for cls in CLASSES:
        r = RouteMap(graph3, bounds=None)
        while not r.step(Flat()):
            pass
        depth[cls] = r._forward(r.pick(cls, own, enemy, jitter=0.0), own, enemy)
    ck('spg sits furthest back', depth[SPG] < depth[TD])
    ck('td sits behind the brawlers', depth[TD] < depth[HEAVY])
    ck('light pushes furthest', depth[LIGHT] > depth[HEAVY])
    ck('nobody parks in the enemy base', max(depth.values()) < 0.95)
    ck('nobody hides in their own base', max(depth.values()) > 0.5)

    # Out-of-bounds and cliff rejection.
    graph2 = {'map': 't2', 'nodes': {
        'pos': [[0.0, 0.0, 0.0], [9999.0, 0.0, 0.0], [50.0, 0.0, 50.0]],
        'adj': [[2], [], [0]], 'desirability': [100, 100, 100]}}

    class P2(object):
        def ground(self, x, z):
            if abs(x) > 400 or abs(z) > 400:
                return None
            return 90.0 if abs(x - 56.0) < 1.0 else 1.0   # ledge beside node 2

        def clear_dist(self, x, y, z, dx, dz, maxd):
            return maxd
    rm2 = RouteMap(graph2, bounds=(-500.0, -500.0, 500.0, 500.0))
    while not rm2.step(P2()):
        pass
    ck('out-of-bounds dropped', not rm2.alive[1])
    ck('cliff-edge dropped', not rm2.alive[2])
    ck('flat node kept', rm2.alive[0])
    ck('too few nodes -> unusable', not rm2.usable())
    ck('next_from skips dead', rm2.next_from(0) is None)

    # Spread: repeated picks should not all land on one node.
    rm3 = RouteMap(graph, bounds=(-500.0, -500.0, 500.0, 500.0))
    while not rm3.step(P()):
        pass
    picks = [rm3.pick(HEAVY, own, enemy, rng=rng, jitter=0.0) for _ in range(6)]
    ck('spread penalty fans out', len(set(picks)) > 1)
    rm3.release(picks[0])
    ck('release decrements', rm3.taken.get(picks[0], 0) == picks.count(picks[0]) - 1)

    # Graph traversal.
    graph4 = {'map': 't4', 'nodes': {
        'pos': [[0.0, 0.0, 0.0], [60.0, 0.0, 0.0]],
        'adj': [[1], [0]], 'desirability': [100, 100]}}
    rm4 = RouteMap(graph4, bounds=None)
    while not rm4.step(P()):
        pass
    ck('leg followed', rm4.next_from(0) == 1 and rm4.next_from(1) == 0)

    # Chunk streaming: terrain that is not probeable yet must be RETRIED, not
    # written off. This is the bug that made the first live run report 0/134
    # usable on a perfectly good Malinovka.
    class Streaming(object):
        '''Finds no ground until it has been asked a few times.'''

        def __init__(self):
            self.calls = 0

        def ground(self, x, z):
            self.calls += 1
            if self.calls < 60:
                return None
            return 1.0

        def clear_dist(self, x, y, z, dx, dz, maxd):
            return maxd
    rm5 = RouteMap(graph, bounds=(-500.0, -500.0, 500.0, 500.0))
    sp = Streaming()
    rm5.step(sp)
    ck('early pass finds nothing', len(rm5.live) == 0)
    ck('early failure is not permanent', not any(rm5.perm_dead))
    ck('not settled while retryable', not rm5.settled())
    guard = 0
    while not rm5.settled() and guard < 500:
        rm5.step(sp)
        guard += 1
    ck('recovers once terrain arrives', rm5.pass_rate() == 1.0)
    ck('settles after recovery', rm5.settled())
    ck('usable after recovery', rm5.usable())
    ck('elev restated on later passes', max(rm5.elev) >= 0.0 and len(rm5.elev) == len(rm5.pos))

    # Out-of-bounds is the one permanent verdict, so it must not be retried.
    rm6 = RouteMap(graph2, bounds=(-500.0, -500.0, 500.0, 500.0))
    while not rm6.settled() and rm6.passes < 50:
        rm6.step(P2())
    ck('out-of-bounds is permanent', rm6.perm_dead[1])
    ck('bounds reject counted once', rm6.reasons['bounds'] == 1)
    ck('settled with a permanent reject', rm6.settled())

    # Terrain that never becomes probeable at all: the feature must still work,
    # because steering only needs X/Z. This is the safety net that stops chunk
    # streaming being able to silently disable the whole thing.
    class NeverLoads(object):
        def ground(self, x, z):
            return None

        def clear_dist(self, x, y, z, dx, dz, maxd):
            return maxd
    rm7 = RouteMap(graph, bounds=(-500.0, -500.0, 500.0, 500.0))
    nl = NeverLoads()
    for _ in range(PROVISIONAL_AFTER_PASSES - 1):
        for _ in range(len(rm7.pos) // VALIDATE_SLICE + 2):
            rm7.step(nl)
    ck('nothing probed', not any(rm7.probed))
    guard = 0
    while rm7.passes < PROVISIONAL_AFTER_PASSES and guard < 500:
        rm7.step(nl)
        guard += 1
    rm7.step(nl)
    ck('provisionally accepted', rm7.usable())
    ck('provisional keeps baked height', rm7.ground_y[0] == graph['nodes']['pos'][0][1])
    ck('provisional openness neutral', rm7.openness[0] == NEUTRAL_OPENNESS)
    ck('provisional is not "probed"', not rm7.probed[0])
    ck('still not settled - keeps trying to upgrade', not rm7.settled())
    ck('provisional bots get a destination',
       rm7.pick(HEAVY, own, enemy, jitter=0.0) is not None)

    # The ranges that decide hold-vs-chase. These are the numbers that made the
    # first live build still pile into the middle: diverting to any enemy within
    # 260 m meant both advancing teams dropped their routes and drove head-on.
    ck('brawl range is close quarters only', BRAWL_RANGE <= 80.0)
    ck('brawl range well inside hold range', BRAWL_RANGE < HOLD_RANGE / 4.0)
    ck('hold range covers normal engagements', HOLD_RANGE >= 300.0)
    ck('arrive radius smaller than brawl range', ARRIVE_RADIUS < BRAWL_RANGE)
    ck('jitter cannot outrank a class preference',
       all(abs(w['fwd_target'] - v['fwd_target']) * min(w['fwd_hold'], v['fwd_hold']) > 0.10
           for w, v in ((CLASS_WEIGHTS[SPG], CLASS_WEIGHTS[LIGHT]),
                        (CLASS_WEIGHTS[TD], CLASS_WEIGHTS[HEAVY]))))

    # Water. The ground probe returns the LAKEBED, so a node in a pond passes
    # every other test - this is the only thing standing between the bots and
    # a lake, and Malinovka is where it showed up.
    class Lake(object):
        '''Dry land except a pond around the origin.'''

        def ground(self, x, z):
            return 1.0

        def clear_dist(self, x, y, z, dx, dz, maxd):
            return maxd

        def water_depth(self, x, y, z):
            return 3.0 if (x * x + z * z) < 10000.0 else -1.0
    lake_graph = {'map': 'lake', 'nodes': {
        'pos': [[0.0, 0.0, 0.0], [40.0, 0.0, 30.0], [300.0, 0.0, 300.0], [-260.0, 0.0, 10.0]],
        'adj': [[], [], [], []], 'desirability': [100] * 4}}
    rm8 = RouteMap(lake_graph, bounds=None)
    while not rm8.settled() and rm8.passes < 40:
        rm8.step(Lake())
    ck('node in the lake rejected', not rm8.alive[0])
    ck('node in the shallows rejected', not rm8.alive[1])
    ck('dry nodes kept', rm8.alive[2] and rm8.alive[3])
    ck('water reject is permanent', rm8.perm_dead[0] and rm8.perm_dead[1])
    ck('water counted', rm8.reasons['water'] == 2)
    # Promotion must not hand a water node back: perm_dead has to outrank it.
    rm8._promote_provisional()
    ck('water node never provisionally readmitted',
       not rm8.alive[0] and not rm8.alive[1])

    # A probe with no water_depth at all must still work (desktop tests, and any
    # future caller that cannot answer the question).
    rm9 = RouteMap(lake_graph, bounds=None)
    while not rm9.settled() and rm9.passes < 40:
        rm9.step(Flat())
    ck('water check is optional', len(rm9.live) == 4)

    # --- painted profile format -------------------------------------------
    doc = profile_document('02_malinovka', (-500.0, -500.0, 500.0, 500.0),
                           [{'pos': [100.0, -50.0], 'team': 1,
                             'classes': ['td', 'spg'], 'role': 'ridge'},
                            {'pos': [-200.0, 120.0], 'team': 2,
                             'classes': ['heavy'], 'role': ''}],
                           [{'points': [[0.0, -400.0], [50.0, -200.0], [120.0, -40.0]],
                             'team': 1, 'classes': ['heavy'], 'name': 'hill'}],
                           [{'poly': [[40.0, -140.0], [180.0, -140.0],
                                      [180.0, -20.0], [40.0, -20.0]]}])
    ck('document is versioned', doc['version'] == PROFILE_VERSION
       and doc['format'] == PROFILE_FORMAT)
    pr = parse_profile(doc, bounds=(-500.0, -500.0, 500.0, 500.0))
    ck('profile parses', pr is not None and not pr.is_empty())
    ck('nothing dropped from a clean doc', sum(pr.dropped.values()) == 0)
    ck('team+class query', pr.destinations_for(1, 'td') == [(100.0, -50.0)])
    ck('class filter excludes', pr.destinations_for(1, 'heavy') == [])
    ck('team filter excludes', pr.destinations_for(1, 'heavy') == []
       and pr.destinations_for(2, 'heavy') == [(-200.0, 120.0)])
    ck('routes query', len(pr.routes_for(1, 'heavy')) == 1
       and len(pr.routes_for(1, 'heavy')[0]) == 3)
    ck('avoid area blocks inside', pr.blocks(100.0, -80.0))
    ck('avoid area passes outside', not pr.blocks(-300.0, 300.0))

    # An `allow` key from an older build must parse and be ignored: it existed
    # to correct a slope heuristic that no longer exists.
    legacy = profile_document('m', None, [], [], [])
    legacy['allow'] = [{'poly': [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]]}]
    pl = parse_profile(legacy)
    ck('a legacy allow key is ignored, not fatal', pl is not None and pl.allow == [])

    # team 0 applies to both sides; empty classes apply to all classes
    both = profile_document('m', None,
                            [{'pos': [1.0, 2.0], 'team': 0, 'classes': [], 'role': ''}],
                            [], [])
    pb = parse_profile(both)
    ck('team 0 matches both', pb.destinations_for(1, 'spg') == [(1.0, 2.0)]
       and pb.destinations_for(2, 'light') == [(1.0, 2.0)])

    # a newer version must be REFUSED, not guessed at
    future = dict(doc)
    future['version'] = PROFILE_VERSION + 1
    ck('future version refused', parse_profile(future) is None)
    ck('foreign document refused', parse_profile({'hello': 1}) is None)
    ck('non-dict refused', parse_profile([1, 2, 3]) is None)

    # malformed ENTRIES are dropped individually, not fatally
    messy = profile_document('m', (-100.0, -100.0, 100.0, 100.0),
                             [{'pos': 'nope', 'team': 1, 'classes': ['td']},
                              {'pos': [5.0, 5.0], 'team': 9, 'classes': ['bogus', 'td']},
                              {'pos': [9000.0, 0.0], 'team': 1, 'classes': ['td']}],
                             [{'points': [[0.0, 0.0]]}],
                             [{'poly': [[0.0, 0.0], [1.0, 1.0]]}])
    pm_ = parse_profile(messy, bounds=(-100.0, -100.0, 100.0, 100.0))
    ck('bad coordinate dropped', pm_.dropped['destinations'] == 2)
    ck('out-of-bounds dropped', len(pm_.destinations) == 1)
    ck('bad team normalised to both', pm_.destinations[0]['team'] == 0)
    ck('unknown class filtered out', pm_.destinations[0]['classes'] == ['td'])
    ck('short route dropped', pm_.dropped['routes'] == 1 and not pm_.routes)
    ck('short polygon dropped', pm_.dropped['avoid'] == 1 and not pm_.avoid)

    # round trip through real JSON, which is how it will actually travel
    import json as _json
    import tempfile as _tf
    _p = os.path.join(_tf.gettempdir(), 'offh_profile_test', 'painted')
    if not os.path.isdir(_p):
        os.makedirs(_p)
    _f = open(os.path.join(_p, '02_malinovka.paint.json'), 'w')
    _json.dump(doc, _f)
    _f.close()
    rt = load_profile('02_malinovka', os.path.dirname(_p),
                      (-500.0, -500.0, 500.0, 500.0))
    ck('loads from disk', rt is not None and len(rt.destinations) == 2)
    ck('loads via spaces/ prefixed name',
       load_profile('spaces/02_malinovka', os.path.dirname(_p)) is not None)
    ck('missing profile returns None',
       load_profile('99_nope', os.path.dirname(_p)) is None)
    _bad = open(os.path.join(_p, '03_bad.paint.json'), 'w')
    _bad.write('{ not json')
    _bad.close()
    ck('corrupt file returns None, does not raise',
       load_profile('03_bad', os.path.dirname(_p)) is None)
    ck('summary reads sensibly', 'destinations' in rt.summary())

    ck('missing map returns None', load_graph('99_nope', '.') is None)
    ck('empty name returns None', load_graph('', '.') is None)

    bad = 0
    for name, ok in checks:
        if not ok:
            bad += 1
            print('FAIL %s' % name)
    # --- automatic generation: every class, every map, no painting ----------
    class _FlatGrid(object):
        """A featureless but fully drivable 800x800 map."""
        def __init__(s, blocked=()):
            s.cell = 8.0
            s.x0 = s.z0 = -400.0
            s.x1 = s.z1 = 400.0
            s.nx = s.nz = 100
            s.n = s.nx * s.nz
            s.blocked = set(blocked)
        def cell_at(s, x, z):
            if x < s.x0 or z < s.z0 or x >= s.x1 or z >= s.z1:
                return None
            return int((z - s.z0) / s.cell) * s.nx + int((x - s.x0) / s.cell)
        def center(s, i):
            return (s.x0 + (i % s.nx + 0.5) * s.cell,
                    s.z0 + (i // s.nx + 0.5) * s.cell)
        def passable(s, i):
            return i is not None and 0 <= i < s.n and i not in s.blocked
        def can_reach(s, i):
            return s.passable(i)
        def nearest_passable(s, i, radius=4, reachable=False):
            return i if s.passable(i) else None
        def astar(s, a, b):
            return [a, b]
        def smooth(s, path):
            return path

    B = (-400., -400., 400., 400.)
    bs = {1: [(0., 300.)], 2: [(0., -300.)]}
    fg = _FlatGrid()
    doc = generate_profile(fg, bs, B, 'gen', seed=3)
    rts = doc['routes']
    ck('generated profile parses as a profile',
       parse_profile(doc).is_empty() is False)
    ck('every class gets routes on both teams',
       all(any(r['team'] == t and r['classes'] == [c] for r in rts)
           for t in (1, 2) for c in (HEAVY, MEDIUM, LIGHT, TD, SPG)))
    ck('classes absent from any battle are still generated',
       sum(1 for r in rts if r['classes'] == [SPG]) == 2 * ROUTES_PER_CLASS)
    ck('several routes per class, so one lane is not shared by everyone',
       all(sum(1 for r in rts if r['team'] == t and r['classes'] == [c])
           == ROUTES_PER_CLASS
           for t in (1, 2) for c in (HEAVY, MEDIUM, LIGHT, TD, SPG)))
    ck('a route has more than two steps, so it states a way to go',
       all(len(r['points']) >= 2 for r in rts)
       and max(len(r['points']) for r in rts) > 2)

    # Depth: the whole point of per-class routes is that they end up in
    # different places. An SPG must not finish where a light does.
    def _depth(r):
        home = bs[r['team']][0]
        away = bs[2 if r['team'] == 1 else 1][0]
        ax, az = away[0] - home[0], away[1] - home[1]
        sp = ax * ax + az * az
        ex, ez = r['points'][-1]
        return ((ex - home[0]) * ax + (ez - home[1]) * az) / sp
    for c in (HEAVY, MEDIUM, LIGHT, TD, SPG):
        ds = [_depth(r) for r in rts if r['classes'] == [c]]
        tgt = CLASS_WEIGHTS[c]['fwd_target']
        ck('%s routes end near its own depth band' % c,
           all(abs(d - tgt) < 0.22 for d in ds))
    ck('an SPG stays well behind a light',
       max(_depth(r) for r in rts if r['classes'] == [SPG])
       < min(_depth(r) for r in rts if r['classes'] == [LIGHT]))

    # Randomness has to be reproducible, or two runs disagree about the map.
    ck('the same seed generates the same routes',
       generate_profile(fg, bs, B, 'gen', seed=3)['routes'] == rts)
    ck('a different seed generates different routes',
       generate_profile(fg, bs, B, 'gen', seed=4)['routes'] != rts)

    # Never emit a step the runtime would only have to snap away again.
    ck('every generated step is drivable',
       all(_drivable(fg, x, z) for r in rts for (x, z) in r['points']))
    ck('no route doubles back onto its own previous step',
       all(math.hypot(r['points'][i][0] - r['points'][i - 1][0],
                      r['points'][i][1] - r['points'][i - 1][1]) > 1.0
           for r in rts for i in range(1, len(r['points']))))

    # One anchor means no axis; inventing a direction would be worse than none.
    ck('one base only -> no routes rather than a guessed direction',
       generate_profile(fg, {1: [(0., 0.)]}, B, 'gen')['routes'] == [])
    ck('no bases at all -> empty, not an exception',
       generate_profile(fg, {}, B, 'gen')['routes'] == [])

    # A map that is mostly blocked must degrade, not emit junk.
    fgb = _FlatGrid(blocked=range(0, 100 * 100, 2))
    db = generate_profile(fgb, bs, B, 'gen', seed=5)
    ck('a heavily blocked map still only emits drivable steps',
       all(_drivable(fgb, x, z) for r in db['routes'] for (x, z) in r['points']))

    # --- team numbering must match the MAP, not the editor -----------------
    # Measured on Prokhorovka: the game spawns team 1 at z=+372 while the editor
    # painted team 1 starting at z=-445. Every bot then drove the length of the
    # map to the enemy spawn, both teams crossed, and they met in the middle -
    # indistinguishable from the "everyone rushes the centre" bug this feature
    # exists to remove.
    inv = profile_document('m', B, [], [
        {'points': [[0., -400.], [0., -200.]], 'team': 1, 'classes': [HEAVY]},
        {'points': [[0., 400.], [0., 200.]], 'team': 2, 'classes': [HEAVY]}], [])
    pinv = parse_profile(inv)
    north, south = (0., 400.), (0., -400.)
    ck('an inverted profile is detected',
       orientation_is_flipped(pinv, north, south) is True)
    ck('a correct profile is left alone',
       orientation_is_flipped(pinv, south, north) is False)
    ck('flipping fixes it',
       (flip_profile_teams(pinv),
        orientation_is_flipped(pinv, north, south))[1] is False)
    ck('flipping reports how many entries moved',
       flip_profile_teams(parse_profile(inv)) == 2)
    ck('flipping twice is the identity',
       (lambda p: (flip_profile_teams(p), flip_profile_teams(p),
                   [r['team'] for r in p.routes])[2])(parse_profile(inv))
       == [1, 2])
    ck('routes_for follows the flip',
       (lambda p: (flip_profile_teams(p),
                   p.routes_for(1, HEAVY)[0][0][1])[1])(parse_profile(inv)) > 0)
    ck('missing bases -> no opinion, nothing changed',
       orientation_is_flipped(pinv, None, south) is None)
    ck('an empty profile -> no opinion',
       orientation_is_flipped(parse_profile(profile_document('m', B, [], [], [])),
                              north, south) is None)

    # --- borrowing and fan-out ---------------------------------------------
    for c in (HEAVY, MEDIUM, LIGHT, TD, SPG):
        sim = similar_classes(c)
        ck('%s can borrow from all four other classes' % c,
           sorted(sim) == sorted([x for x in (HEAVY, MEDIUM, LIGHT, TD, SPG) if x != c]))
        ck('%s never lists itself' % c, c not in sim)
    ck('an SPG borrows from a TD before a light',
       similar_classes(SPG).index(TD) < similar_classes(SPG).index(LIGHT))
    ck('a light borrows from a heavy before an SPG',
       similar_classes(LIGHT).index(HEAVY) < similar_classes(LIGHT).index(SPG))
    ck('a heavy borrows from a medium first',
       similar_classes(HEAVY)[0] == MEDIUM)
    ck('the order is by depth, nearest first',
       all(abs(CLASS_WEIGHTS[similar_classes(TD)[i]]['fwd_target']
               - CLASS_WEIGHTS[TD]['fwd_target'])
           <= abs(CLASS_WEIGHTS[similar_classes(TD)[i + 1]]['fwd_target']
                  - CLASS_WEIGHTS[TD]['fwd_target'])
           for i in range(len(similar_classes(TD)) - 1)))

    ck('one bot gets the plain fan-out', spread_radius(1) == (8.0, 22.0))
    ck('more bots sharing means a wider fan',
       spread_radius(10)[1] > spread_radius(2)[1] > spread_radius(1)[1])
    ck('the fan grows sub-linearly, not 10x for 10 bots',
       spread_radius(10)[1] < 4.0 * spread_radius(1)[1])
    ck('zero or nonsense sharing is treated as one',
       spread_radius(0) == spread_radius(1) and spread_radius(-3) == spread_radius(1))
    ck('the fan never inverts', all(spread_radius(n)[0] < spread_radius(n)[1]
                                    for n in (1, 2, 5, 10, 30)))

    # --- engagement distance -----------------------------------------------
    ck('every class has an engagement range',
       all(engage_range(c) > 0 for c in (HEAVY, MEDIUM, LIGHT, TD, SPG)))
    ck('a heavy closes much further than a TD',
       engage_range(HEAVY) < engage_range(TD))
    ck('a TD is already in position anywhere it can see',
       engage_range(TD) >= HOLD_RANGE)
    ck('an SPG never closes', engage_range(SPG) > 1000.0)
    ck('the order runs brawler -> sniper',
       engage_range(HEAVY) < engage_range(MEDIUM) < engage_range(LIGHT)
       < engage_range(TD) < engage_range(SPG))
    ck('an unknown class gets the medium default',
       engage_range('nonsense') == engage_range(MEDIUM))
    ck('a heavy seeing an enemy at 400 m wants to close',
       400.0 > engage_range(HEAVY))
    ck('a TD seeing an enemy at 400 m stays put',
       not (400.0 > engage_range(TD)))

    print('%d/%d checks passed' % (len(checks) - bad, len(checks)))
    return bad == 0


if __name__ == '__main__':
    import sys
    sys.exit(0 if _selftest() else 1)
