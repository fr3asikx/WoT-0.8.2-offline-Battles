# -*- coding: utf-8 -*-
# Crosshair penetration indicator (native gun marker).
#
# The stock client already has a penetration-colouring crosshair: the gun
# marker (_FlashGunMarker in AvatarInputHandler/control_modes) tints itself
# green / orange / red by whether the loaded shell pierces the plate under the
# reticle. It does the whole calculation itself (distance fall-off included) in
# _changeColor(hitPoint, armor); it only runs when updateGunMarker() is handed
# a non-None `collData` describing the vehicle under the aim.
#
# The offline mod drives the gun marker manually and always passed
# collData=None, so the feature was dead. This module supplies the missing
# piece: a raycast over the offline mock vehicles that reproduces the collData
# the live client would get from the engine.
#
# collData contract consumed by _FlashGunMarker.update():
#   collData[0].publicInfo['team']  -> team, compared to BigWorld.player().team
#                                      (same team => marker stays 'normal')
#   collData[2]                     -> nominal armor (mm) at the hit plate
# (collData[1] is unused by _changeColor; we still pass the hit point for
#  parity with the engine's tuple shape.)
#
# No BigWorld import at load time (Math is imported lazily), so this stays cheap
# to import and safe anywhere.

# Aim raycast reach (m). Beyond this the marker stays neutral; also caps cost.
DEFAULT_MAX_DIST = 720.0

# Broad-phase cull radius (m): a bot whose centre lies farther than this from
# the aim ray can't be under the reticle, so it skips the expensive per-plate
# hitbox raycast. Comfortably larger than any hull+turret.
BROAD_RADIUS = 12.0

# cos(85 deg): floor on the impact-angle cosine when converting nominal plate
# armor to effective (line-of-sight) thickness, mirroring the shot resolver.
MIN_ANGLE_COS = 0.087


class _TeamShim(object):
    """Minimal stand-in for collData[0]: the native marker only reads
    .publicInfo['team'] off it."""
    __slots__ = ('publicInfo',)

    def __init__(self, team):
        self.publicInfo = {'team': team}


def _resolve_team(veh, player_team):
    team = getattr(veh, '_bot_team', None)
    if team is None:
        pi = getattr(veh, 'publicInfo', None)
        if pi is not None:
            try:
                team = pi.get('team', None)
            except Exception:
                team = None
    if team is None:
        # Unknown -> treat as the opposing team so it still colours.
        team = 2 if player_team == 1 else 1
    return team


def build_coll_data(mocks, player_id, player_team, start_pos, dir_vec,
                    world_dist, max_dist=DEFAULT_MAX_DIST):
    """Raycast the aim ray against the mock vehicles and return the collData
    tuple (teamShim, hitPoint, armor) for the nearest tank under the reticle,
    or None when the reticle is on empty space / world geometry.
    """
    import Math
    d = Math.Vector3(dir_vec)
    try:
        d.normalise()
    except Exception:
        pass
    end_pos = start_pos + d.scale(max_dist)

    best = None
    best_veh = None
    best_d = max_dist
    r2 = BROAD_RADIUS * BROAD_RADIUS
    for eid, m in mocks.iteritems():
        if eid == player_id:
            continue
        if not getattr(m, 'isAlive', False):
            continue
        # Unspotted (hidden) tanks must not colour the marker: the pen
        # indicator worked as a wallhack, lighting up when the reticle
        # swept over an invisible enemy (user report + video).
        if not getattr(m, '_spot_visible', True):
            continue
        # Keep the stored position in step with the model before testing.
        try:
            m.position = m.model.position
        except Exception:
            pass
        # Broad-phase: perpendicular distance of the bot centre to the aim ray.
        try:
            p = m.position
            vx = p.x - start_pos.x
            vy = p.y - start_pos.y
            vz = p.z - start_pos.z
            t = vx * d.x + vy * d.y + vz * d.z
            if t < 0.0 or t > best_d:
                continue
            cx = p.x - (start_pos.x + d.x * t)
            cy = p.y - (start_pos.y + d.y * t)
            cz = p.z - (start_pos.z + d.z * t)
            if (cx * cx + cy * cy + cz * cz) > r2:
                continue
        except Exception:
            pass  # broad-phase unusable -> fall through to the precise test
        try:
            col = m.collideSegment(start_pos, end_pos)
        except Exception:
            col = None
        if col is not None and col[0] < best_d:
            best_d = col[0]
            best = col
            best_veh = m

    if best is None:
        return None
    # A wall/hill in front of the tank blocks the shot (same rule the shot uses).
    if world_dist is not None and best_d > world_dist + 0.5:
        return None

    # The stock marker (_changeColor) compares this against nominal piercing
    # power with NO angle term, so the engine feeds it EFFECTIVE (line-of-sight)
    # armor. collideSegment returns nominal plate armor (best[2]) plus the
    # impact-angle cosine (best[1]); convert the same way the shot resolver does
    # (armor / cos, capped at 85 deg) so the colour matches what a shot would do.
    nominal_armor = best[2]
    angle_cos = abs(best[1])
    if angle_cos < MIN_ANGLE_COS:
        angle_cos = MIN_ANGLE_COS
    eff_armor = nominal_armor / angle_cos if angle_cos > 0.0 else nominal_armor
    team = _resolve_team(best_veh, player_team)
    hit_point = start_pos + d.scale(best_d)
    return (_TeamShim(team), hit_point, eff_armor)
