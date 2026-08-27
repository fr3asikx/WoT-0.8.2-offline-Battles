# -*- coding: utf-8 -*-
"""Credits, XP and service costs for one battle.

WHAT IS REAL AND WHAT IS RECONSTRUCTED
--------------------------------------
Real, straight out of this client's own files:

  * the per-vehicle economy multipliers - xpFactor, creditsFactor and
    crewXpFactor are read off the vehicle type by items/vehicles.py, so a
    premium tank earns what it is supposed to earn.
  * the repair bill - VehicleDescr.getMaxRepairCost() is a real method,
    and scripts/common/items/vehicles.py computes it as
    `maxHealth * type.repairCost` plus a term per module. Charging the
    fraction of it that matches the hit points actually lost is the same
    shape retail used.
  * the shell prices, for the resupply line.
  * the factor chain the results screen applies on top, which
    gui/scaleform/battleresults.py spells out by reversing it in
    __calculateBaseXp / __calculateBaseCredits: the numbers handed over
    here are the BASE, and premium / daily-double / aogas multiply it.

Reconstructed, because it never shipped with the client - the earning
formula ran on the server:

  * how much a point of damage is worth. WG has never published the
    coefficients. What IS documented (wargaming.net support, "How do I
    earn Experience?") is the STRUCTURE, and that is what is modelled
    here: damage weighted by the tier difference to the victim, plus
    kills, plus damage an ally dealt to something you spotted, plus base
    capture and defence points, plus a survival bonus, and +50% for the
    winning team.

The coefficients below are calibrated against the era's well-known
figures rather than invented freehand: a strong tier-8 game (2500
damage, 3 kills, a win) lands near 1400 XP and about 60k gross credits,
with credits-per-damage rising with tier the way retail's did. All of
them are overridable from config.json under "economy_tuning", so nothing
here has to be recompiled to be re-tuned.
"""

# --- reconstructed coefficients ---------------------------------------------
# XP per point of damage at equal tier. 2500 damage -> 750 XP before the
# kill and win terms, which is the right order for the era.
XP_PER_DAMAGE = 0.30
# XP per point of damage an ally dealt to a tank you had spotted. Retail
# paid assists well, but below a hit of your own.
XP_PER_ASSIST = 0.22
# Flat XP per kill, scaled by the victim's tier.
XP_PER_KILL = 60.0
# XP for revealing a tank nobody had seen.
XP_PER_SPOT = 20.0
XP_PER_CAPTURE_POINT = 4.0
XP_PER_DEFENCE_POINT = 4.0
# Still alive when it ended.
XP_SURVIVAL_BONUS = 50.0
# "+50% to each tank of the team" - the one multiplier WG states outright.
XP_WIN_FACTOR = 1.5
# A draw pays neither the win bonus nor a penalty.
XP_DRAW_FACTOR = 1.0

# Credits per point of damage. This used to be a straight multiple of the
# tier, which was wrong at both ends: it starved the low tiers (a tier-5
# game paid about half what it should) and it made the tier scaling far
# steeper than retail's.
#
# What actually happens in WoT is that INCOME per point of damage is
# fairly flat across the tiers - it rises, but gently. What explodes with
# tier is the COST side, and that is why high tiers are hard to run at a
# profit. The cost side here is not guessed at all: repairCost per hit
# point is read out of this client's own vehicle files, where it runs
# 3.54 at tier 2, 5.34 at tier 5, 8.26 at tier 8 and 11.17 at tier 10,
# against hull hit points that grow far faster still. Letting a gentle
# income curve meet that steep cost curve reproduces the era's economy:
# mid tiers profit comfortably, a tier-8 loss roughly breaks even, and a
# mediocre tier-10 game loses money.
CREDITS_PER_DAMAGE = 14.5
CREDITS_PER_ASSIST = 10.0
CREDITS_PER_KILL = 350.0
CREDITS_PER_SPOT = 120.0
CREDITS_PER_CAPTURE_POINT = 25.0
# Gentle rise per tier above 5: tier 2 pays 15.0 per point, tier 8 19.0,
# tier 10 20.4 - before the win bonus.
CREDITS_TIER_SLOPE = 0.04
CREDITS_TIER_PIVOT = 5.0
CREDITS_WIN_FACTOR = 1.5
CREDITS_DRAW_FACTOR = 1.0

# Free XP is 5% of what the vehicle earned - a retail rule, not a guess.
FREE_XP_FRACTION = 0.05

# Tier weighting. Killing or damaging something above you pays more, below
# you pays less, and the curve is flattened so a tier-3 kill in a tier-8
# tank is worth little but never zero.
TIER_RATIO_MIN = 0.4
TIER_RATIO_MAX = 2.0

_TUNABLE = ('XP_PER_DAMAGE', 'XP_PER_ASSIST', 'XP_PER_KILL', 'XP_PER_SPOT',
            'XP_PER_CAPTURE_POINT', 'XP_PER_DEFENCE_POINT',
            'XP_SURVIVAL_BONUS', 'XP_WIN_FACTOR', 'XP_DRAW_FACTOR',
            'CREDITS_PER_DAMAGE', 'CREDITS_PER_ASSIST',
            'CREDITS_PER_KILL', 'CREDITS_PER_SPOT',
            'CREDITS_PER_CAPTURE_POINT', 'CREDITS_TIER_SLOPE',
            'CREDITS_TIER_PIVOT', 'CREDITS_WIN_FACTOR',
            'CREDITS_DRAW_FACTOR', 'FREE_XP_FRACTION',
            'TIER_RATIO_MIN', 'TIER_RATIO_MAX')


def apply_tuning(overrides):
    """Overlay config.json "economy_tuning" onto the constants above.
    Returns the names actually changed, for the log."""
    if not overrides:
        return []
    applied = []
    g = globals()
    for name in _TUNABLE:
        key = name.lower()
        if key in overrides:
            try:
                g[name] = float(overrides[key])
                applied.append(key)
            except (TypeError, ValueError):
                pass
    return applied


def _tier_ratio(victimTier, ownTier):
    """How much a tier-`victimTier` target is worth to a tier-`ownTier`
    tank. Clamped at both ends so the reward stays sane across the whole
    spread a battle can contain."""
    try:
        own = float(ownTier or 0)
        victim = float(victimTier or 0)
    except (TypeError, ValueError):
        return 1.0
    if own <= 0 or victim <= 0:
        return 1.0
    ratio = victim / own
    if ratio < TIER_RATIO_MIN:
        return TIER_RATIO_MIN
    if ratio > TIER_RATIO_MAX:
        return TIER_RATIO_MAX
    return ratio


def params_from_descriptor(td):
    """The economy numbers this client really carries per vehicle.

    Everything has a fallback: a mock descriptor in an offline battle can
    be missing any of it, and a battle result that fails to build is far
    worse than one with a neutral multiplier.
    """
    out = {'tier': 1, 'xpFactor': 1.0, 'creditsFactor': 1.0,
           'crewXpFactor': 1.0, 'maxRepairCost': 0.0, 'maxHealth': 0}
    if td is None:
        return out
    try:
        vtype = getattr(td, 'type', None)
        out['tier'] = int(getattr(vtype, 'level', 1) or 1)
        out['xpFactor'] = float(getattr(vtype, 'xpFactor', 1.0) or 1.0)
        out['creditsFactor'] = float(getattr(vtype, 'creditsFactor', 1.0) or 1.0)
        out['crewXpFactor'] = float(getattr(vtype, 'crewXpFactor', 1.0) or 1.0)
    except Exception:
        pass
    try:
        out['maxHealth'] = int(getattr(td, 'maxHealth', 0) or 0)
    except Exception:
        pass
    try:
        # The real method, from scripts/common/items/vehicles.py. It sums
        # the hull term (maxHealth * type.repairCost) and one term per
        # module, which is exactly the full-wreck bill.
        out['maxRepairCost'] = float(td.getMaxRepairCost())
    except Exception:
        # Fall back to the hull term alone rather than charging nothing.
        try:
            out['maxRepairCost'] = float(out['maxHealth']) * float(
                getattr(getattr(td, 'type', None), 'repairCost', 0.0) or 0.0)
        except Exception:
            out['maxRepairCost'] = 0.0
    return out


def shell_cost(td, roundsUsed):
    """Resupply bill for the rounds fired, as (credits, gold).

    roundsUsed is {shellIndex: count}.

    A shell price is ALREADY a (credits, gold) pair - items/_xml.py
    readPrice() returns (0, gold) for a gold item and (credits, 0)
    otherwise, so exactly one of the two is ever non-zero. Reading the
    second element as an "is this a gold shell" flag and then billing
    price[0] regardless, as this used to, charged a gold round NOTHING:
    its credit slot is 0 and the gold amount was thrown away. Both
    elements are amounts; add each to its own column.
    """
    credits = 0.0
    gold = 0.0
    if td is None or not roundsUsed:
        return (0, 0)
    try:
        shots = td.gun.get('shots', []) or []
    except Exception:
        return (0, 0)
    for index, count in roundsUsed.items():
        try:
            price = shots[int(index)]['shell'].get('price', (0, 0))
            rounds = int(count)
            credits += float(price[0]) * rounds
            if len(price) > 1:
                gold += float(price[1]) * rounds
        except Exception:
            continue
    return (int(round(credits)), int(round(gold)))


# Consumable prices, read out of this client's own
# res/scripts/item_defs/vehicles/common/equipments.xml. Each entry is the
# (credits, gold) pair items/_xml.py readPrice() produces: a gold item has
# 0 credits and vice versa. Anything consumed in the battle has to be bought
# again, which is what the results screen calls autoEquip.
CONSUMABLE_PRICES = {
    'smallrepairkit': (3000, 0),
    'smallmedkit': (3000, 0),
    'handextinguishers': (3000, 0),
    'largerepairkit': (0, 50),
    'largemedkit': (0, 50),
    'autoextinguishers': (0, 50),
    'lendleaseoil': (5000, 0),
    'gasoline100': (5000, 0),
    'qualityoil': (5000, 0),
    'removedrpmlimiter': (3000, 0),
    'gasoline105': (0, 50),
    'chocolate': (0, 50),
    'cocacola': (0, 50),
    'ration': (0, 50),
    'hotcoffee': (0, 50),
    'ration_china': (0, 50),
    'ration_uk': (0, 50),
}


def equipment_cost(consumables):
    """Bill for the consumables spent, as (credits, gold).

    consumables is the battle's own slot list - dicts carrying at least
    a lower-case `name` and the `used` flag the activation path sets. An
    unused kit costs nothing; a kit that was fired costs a fresh one.
    """
    credits = 0
    gold = 0
    for item in (consumables or ()):
        try:
            if not item.get('used'):
                continue
            price = CONSUMABLE_PRICES.get(str(item.get('name', '')).lower())
        except Exception:
            continue
        if not price:
            continue
        credits += price[0]
        gold += price[1]
    return (credits, gold)


def compute(stats, vehicle, context=None):
    """Base credits and XP for one battle, plus the service costs.

    stats:   the player's results row (the ledger's own keys).
    vehicle: params_from_descriptor() output.
    context: won / draw / survived / health / victimTiers / roundsUsed /
             equipmentCost.

    Returns a dict of exactly the fields the results screen reads. These
    are BASE values: premium, the daily double and aogas are applied on
    top by battleresults.py, so applying them here too would double them.
    """
    stats = stats or {}
    vehicle = vehicle or {}
    context = context or {}

    tier = int(vehicle.get('tier', 1) or 1)
    damage = float(stats.get('damageDealt', 0) or 0)
    assisted = float(stats.get('damageAssisted', 0) or 0)
    spotted = int(stats.get('spotted', 0) or 0)
    capture = int(stats.get('capturePoints', 0) or 0)
    dropped = int(stats.get('droppedCapturePoints', 0) or 0)

    # Damage is weighted by the average tier of what was actually hit, so
    # farming the bottom of the list pays less than trading with the top.
    victimTiers = context.get('victimTiers') or []
    if victimTiers:
        weights = [_tier_ratio(t, tier) for t in victimTiers]
        damageWeight = sum(weights) / float(len(weights))
    else:
        damageWeight = 1.0

    killTiers = context.get('killTiers') or []
    killWeight = 0.0
    for t in killTiers:
        killWeight += _tier_ratio(t, tier)
    if not killTiers:
        killWeight = float(stats.get('kills', 0) or 0)

    # --- XP -----------------------------------------------------------
    xp = 0.0
    xp += damage * damageWeight * XP_PER_DAMAGE
    xp += assisted * XP_PER_ASSIST
    xp += killWeight * XP_PER_KILL
    xp += spotted * XP_PER_SPOT
    xp += capture * XP_PER_CAPTURE_POINT
    xp += dropped * XP_PER_DEFENCE_POINT
    if context.get('survived'):
        xp += XP_SURVIVAL_BONUS
    if context.get('won'):
        xp *= XP_WIN_FACTOR
    elif context.get('draw'):
        xp *= XP_DRAW_FACTOR
    xp *= float(vehicle.get('xpFactor', 1.0) or 1.0)

    # --- credits ------------------------------------------------------
    # Income rises gently with tier; the steep part of the economy is the
    # cost side below, which comes from the vehicle files.
    tierScale = 1.0 + CREDITS_TIER_SLOPE * (tier - CREDITS_TIER_PIVOT)
    if tierScale < 0.5:
        tierScale = 0.5
    credits = 0.0
    credits += damage * damageWeight * CREDITS_PER_DAMAGE * tierScale
    credits += assisted * CREDITS_PER_ASSIST * tierScale
    credits += killWeight * CREDITS_PER_KILL * tierScale
    credits += spotted * CREDITS_PER_SPOT * tierScale
    credits += (capture + dropped) * CREDITS_PER_CAPTURE_POINT * tierScale
    if context.get('won'):
        credits *= CREDITS_WIN_FACTOR
    elif context.get('draw'):
        credits *= CREDITS_DRAW_FACTOR
    credits *= float(vehicle.get('creditsFactor', 1.0) or 1.0)

    # --- service costs ------------------------------------------------
    # The repair bill is the share of the full-wreck cost that matches the
    # hit points actually lost, which is how retail scaled it.
    maxHealth = float(vehicle.get('maxHealth', 0) or 0)
    health = float(context.get('health', maxHealth) or 0)
    if maxHealth > 0:
        lostFraction = max(0.0, min(1.0, (maxHealth - health) / maxHealth))
    else:
        lostFraction = 0.0
    repair = int(round(float(vehicle.get('maxRepairCost', 0.0) or 0.0) * lostFraction))

    loadCredits, loadGold = shell_cost(context.get('descriptor'),
                                       context.get('roundsUsed'))
    equipCredits, equipGold = equipment_cost(context.get('consumables'))

    xp = int(round(xp))
    crewXp = int(round(xp * float(vehicle.get('crewXpFactor', 1.0) or 1.0)))
    freeXp = int(round(xp * FREE_XP_FRACTION))

    return {
        'xp': xp,
        'freeXP': freeXp,
        'tmenXP': crewXp,
        'credits': int(round(credits)),
        'autoRepairCost': repair,
        'autoLoadCost': (loadCredits, loadGold),
        'autoEquipCost': (equipCredits, equipGold),
        'xpPenalty': 0,
        'creditsPenalty': 0,
        'creditsContributionIn': 0,
        'creditsContributionOut': 0,
    }


def summary(result):
    """One line for the log."""
    if not result:
        return 'economy: nothing'
    return ('economy: xp=%d free=%d crew=%d credits=%d repair=%d ammo=%s'
            % (result.get('xp', 0), result.get('freeXP', 0),
               result.get('tmenXP', 0), result.get('credits', 0),
               result.get('autoRepairCost', 0),
               result.get('autoLoadCost', (0, 0))))
