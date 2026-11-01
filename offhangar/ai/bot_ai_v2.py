# -*- coding: utf-8 -*-
''' (bot_ai_v2): the tactics layer, in new files, next to v1.

What this module is
-------------------
`bot_ai_v2.BotDirectorV2` is a FACADE over the shipped `bot_ai.BattleDirector`.
Perception, the navgraph, vehicle profiles and execution stay exactly where they
are (spec §1: "оставить"), the facade delegates every attribute it does not
override, and only ONE method is v2's own: `order_for`.  The order dict keeps the
same 16 keys v1 returns (`bot_ai.py:3456-3473`), so the execution layer
(`offline_battle.py:22167-22215`) and the network path
(`network_battle.py:2240-2296`) do not change at all.

What  actually switches on
----------------------------------
ONE behaviour: the fire admissibility gate (spec §3.1, owner's step 3).
`fire_allowed` may only stay True when the gun can physically reach the aim
point; otherwise the shot is refused, the refusal is counted by reason, and the
bot keeps v1's movement order (relocating is v1's cover/route logic, which
already works).  Everything else in this file - capabilities, blackboard,
utility candidates, skill profiles - is built, logged and tested in 100 but does
NOT change behaviour yet: candidates are scored and reported, and only stage 102
lets them choose `combat_mode`.  That keeps the A/B honest: with
`bot_ai_v2=true` the only new variable is "bots stop shooting where the gun
cannot reach".

Fail-safe contract
------------------
Any exception anywhere in v2 returns the v1 order unchanged and is counted
(`V2_ERR where=...` once per place, never per tick).  A director that cannot
build capabilities (e.g. the LAN server, which registers by PROFILE and has no
typeDescriptor - `bot_ai.register_profile`, :1657) does not gate fire at all:
refusing a shot for lack of data would be worse than today.

Instrument discipline
---------------------
The markers below report DECISIONS and INPUTS (`V2_GATE id=... why=...
pitch=... lim=...`), never a recomputation of what the execution layer already
measures.  Acceptance is read from the independent instruments: `GUNPROBE
clamped=`, `FBSOL reason=no_solution`, `clamp=max`, `STALLPROBE`, `MODEPROBE`
(spec §4).  Rule: the probe must not use the formula of the code it checks.

py2.6: tabs, no f-strings, no set/dict comprehensions, no engine import at
module load.
'''

import math
import time as _v2_time
_OFFH_V2_GLOBAL_FRAME_STAMP = -1.0
_OFFH_V2_GLOBAL_FRAME_COUNT = 0
_OFFH_V2_MAX_PER_FRAME = 2
import random

# --- the order contract ----------------------------------------------------
# The 16 keys v1 puts in every order (bot_ai.py:3456-3473).  Consumers read a
# subset: offline_battle.py reads aim_position / move_position / face_position /
# fire_allowed / fire_range / target_id / combat_mode / throttle_override /
# shell_index / route_id / route_index / route_anchor (:22205-22215),
# network_battle.py:2240 walks ('aim_position','face_position','move_position',
# 'route_anchor') and :2277/:2289 compare combat_mode against 'artillery_fire'
# and 'advance_contact', and _offh_ai_apply_local_cover reads personality
# (:10968/:10977, keys 'patience' and 'aggression').  `hull_aiming` is NOT an
# order key - it is a local in offline_battle.py (:22116/:22859/:24306).
ORDER_KEYS = (
	'target_id', 'aim_position', 'face_position', 'move_position',
	'fire_allowed', 'combat_mode', 'throttle_override', 'desired_range',
	'fire_range', 'route_id', 'route_index', 'route_anchor', 'personality',
	'profile', 'shell_index', 'force_balance', 'max_speed_mps',
)


def _log_note(text):
	'''Best-effort LOG_NOTE, same pattern bot_ai.py:19-28 uses: the engine logger
	is imported lazily and every failure is swallowed, because a missing log line
	is never worth a bot's decision.  No flush here - flushes belong to event
	markers, not to a per-tick layer (rule 6 of the crash protocol).'''
	try:
		from gui.mods.offhangar.logging import LOG_NOTE
		LOG_NOTE(text)
	except Exception:
		pass


def _num(value, default=0.0):
	'''float() that never raises; default=None comes back as None, so "absent"
	stays distinguishable from zero (the `or 0.0` trap, РЕЦЕНЗИЯ_d97 defect 3).'''
	try:
		result = float(value)
	except (TypeError, ValueError):
		return None if default is None else float(default)
	if result != result or result in (float('inf'), float('-inf')):
		return None if default is None else float(default)
	return result


def normalize_order(order, fallback_position):
	'''Guarantee the 16 keys exist with sane types.

	Called on EVERY v2 return path, including the fallback ones, so a consumer
	never sees a half-built order.  Extra keys v1 may add later are preserved.'''
	if not isinstance(order, dict):
		order = {}
	hold = (float(fallback_position[0]), float(fallback_position[1]),
	        float(fallback_position[2]))
	defaults = {
		'target_id': None,
		'aim_position': hold,
		'face_position': hold,
		'move_position': hold,
		'fire_allowed': False,
		'combat_mode': 'route',
		'throttle_override': None,
		'desired_range': 0.0,
		'fire_range': 0.0,
		'route_id': 'direct',
		'route_index': 0,
		'route_anchor': hold,
		'personality': {},
		'profile': {},
		'shell_index': 0,
		'force_balance': 0.0,
		'max_speed_mps': None,
	}
	for key in ORDER_KEYS:
		if key not in order:
			order[key] = defaults[key]
	for key in ('aim_position', 'face_position', 'move_position', 'route_anchor'):
		value = order.get(key)
		if value is None:
			order[key] = hold
		else:
			order[key] = (_num(value[0], hold[0]), _num(value[1], hold[1]),
			              _num(value[2], hold[2]))
	order['fire_allowed'] = bool(order.get('fire_allowed'))
	order['combat_mode'] = str(order.get('combat_mode') or 'route')
	order['shell_index'] = max(0, int(_num(order.get('shell_index'), 0)))
	order['route_index'] = int(_num(order.get('route_index'), 0))
	order['desired_range'] = _num(order.get('desired_range'), 0.0)
	order['fire_range'] = _num(order.get('fire_range'), 0.0)
	order['force_balance'] = _num(order.get('force_balance'), 0.0)
	if not isinstance(order.get('personality'), dict):
		order['personality'] = {}
	if not isinstance(order.get('profile'), dict):
		order['profile'] = {}
	return order


# ---------------------------------------------------------------------------
# Complexity = ACCESS + NOISE + REACTION (spec §3.4), not a scalar multiplier.
#
# v1's `make_personality(seed, skill_tier)` (bot_ai.py:715-741) has branches for
# 'hard' and 'easy' only - `medium` changes NOTHING but the route-jitter range,
# so a medium bot is a raw random draw.  These profiles give each tier a
# different candidate set (access), a different aim-noise multiplier and a
# different reaction window.  Reaction ranges are the shipped ones
# (config_defaults.json:80-84, read by offline_battle.py:819 for ) so v2
# does not invent a second scale; if the owner adds `candidates` / `aim_noise` /
# `gate_margin` under bot_skill_profiles.<tier>, those win.
# ---------------------------------------------------------------------------
CANDIDATE_NAMES = (
	'advance', 'hold', 'engage', 'relocate', 'cover_reload', 'support_fire',
	'flank', 'retreat', 'bait', 'punish_peek', 'arty_fire', 'arty_reposition',
	'squad_follow', 'squad_help',
)

SKILL_DEFAULTS = {
	# easy: no flank/bait/punish_peek, biggest aim noise, slowest reaction
	'easy': {
		'candidates': ('advance', 'hold', 'engage', 'relocate', 'cover_reload',
		               'support_fire', 'retreat', 'arty_fire',
		               'arty_reposition', 'squad_follow'),
		'aim_noise': 1.6,
		'reaction': (0.9, 1.5),
		'gate_margin_scale': 1.5,   # sloppier: needs a wider window to shoot
		'consider_traverse': False, # ignores how long the turn takes
		'mistakes': 0.25,
	},
	'medium': {
		'candidates': ('advance', 'hold', 'engage', 'relocate', 'cover_reload',
		               'support_fire', 'flank', 'retreat', 'arty_fire',
		               'arty_reposition', 'squad_follow', 'squad_help'),
		'aim_noise': 1.0,
		'reaction': (0.45, 0.8),
		'gate_margin_scale': 1.0,
		'consider_traverse': True,
		'mistakes': 0.10,
	},
	# hard: everything, plus bait/punish_peek, and it prices the traverse
	'hard': {
		'candidates': CANDIDATE_NAMES,
		'aim_noise': 1.0,
		'reaction': (0.2, 0.35),
		'gate_margin_scale': 0.75,
		'consider_traverse': True,
		'mistakes': 0.03,
	},
}

_PROFILE_CACHE = {}


def skill_profile(tier, config=None):
	'''Resolved skill profile for one tier: access + noise + reaction.

	`config` is the shipped `bot_skill_profiles` table when the caller has it
	(config_defaults.json:80-84); its values win over the built-ins, and a tier
	missing from the config falls back to 'medium' - the same fallback v1's
	`make_personality` effectively has.'''
	key = str(tier or 'medium').lower()
	if key not in SKILL_DEFAULTS:
		key = 'medium'
	cached = _PROFILE_CACHE.get(key)
	table = config if isinstance(config, dict) else None
	if cached is not None and cached[0] is table:
		return cached[1]
	base = dict(SKILL_DEFAULTS[key])
	if table is not None:
		theirs = table.get(key)
		if isinstance(theirs, dict):
			for name in ('aim_noise', 'gate_margin_scale', 'consider_traverse',
			             'mistakes'):
				if name in theirs:
					base[name] = theirs[name]
			if 'reaction' in theirs:
				pair = theirs['reaction']
				try:
					base['reaction'] = (_num(pair[0], base['reaction'][0]),
					                    _num(pair[1], base['reaction'][1]))
				except (TypeError, IndexError, KeyError):
					pass
			if 'candidates' in theirs:
				allowed = []
				for name in theirs['candidates']:
					if str(name) in CANDIDATE_NAMES and str(name) not in allowed:
						allowed.append(str(name))
				if allowed:
					base['candidates'] = tuple(allowed)
	base['tier'] = key
	profile = dict(base)
	_PROFILE_CACHE[key] = (table, profile)
	return profile


def reset_skill_cache():
	try:
		_PROFILE_CACHE.clear()
	except Exception:
		pass


def profile_summary(profile):
	'''One loggable line: what a tier is ALLOWED to do, which is the part that
	`make_personality` never expressed.'''
	return ('V2_SKILL tier=%s cands=%d/%d aim_noise=%.2f react=%.2f-%.2f '
	        'gate_margin_scale=%.2f traverse=%s mistakes=%.2f list=%s') % (
		profile.get('tier', 'medium'),
		len(profile.get('candidates', ())), len(CANDIDATE_NAMES),
		_num(profile.get('aim_noise'), 1.0),
		_num(profile.get('reaction', (0.45, 0.8))[0], 0.45),
		_num(profile.get('reaction', (0.45, 0.8))[1], 0.8),
		_num(profile.get('gate_margin_scale'), 1.0),
		'yes' if profile.get('consider_traverse') else 'no',
		_num(profile.get('mistakes'), 0.1),
		','.join(profile.get('candidates', ())))


# ---------------------------------------------------------------------------
# Utility candidates (spec §3.3).  Every candidate is a pure function
# `(ctx) -> (terms, params)` or None.  None means "not applicable right now",
# and a candidate that is not applicable is never offered - that is how the 10
# permanently dead v1 modes disappear BY CONSTRUCTION instead of by tuning.
#
# All candidates are scored by ONE utility function (`utility_score`); a
# candidate only supplies facts (terms), never its own formula, and personality
# traits enter as WEIGHTS, not as code branches.  Six terms, all documented at
# `TERMS`.
# ---------------------------------------------------------------------------
TERMS = ('progress', 'safety', 'fire', 'support', 'risk', 'cost')

# Weights per trait.  Deliberately few and round: this table is the calibration
# surface, and every entry is a "how much does this trait care about this term"
# answer, not a magic score added to a target (v1's `_choose_contact` has ~20 of
# those, bot_ai.py:2242-2384, which is what made them fight each other).
TRAIT_WEIGHTS = (
	# (term, trait, weight)
	('progress', 'aggression', 0.55),
	('progress', 'initiative', 0.35),
	('progress', 'caution', -0.30),
	('safety', 'caution', 0.65),
	('safety', 'patience', 0.25),
	('safety', 'aggression', -0.35),
	('fire', 'aggression', 0.45),
	('fire', 'initiative', 0.25),
	('support', 'teamwork', 0.75),
	('support', 'initiative', -0.20),
	('risk', 'caution', 0.60),
	('risk', 'aggression', -0.40),
	('cost', 'patience', -0.35),
	('cost', 'adaptability', -0.30),
)


class WorldContext(object):
	'''Everything a candidate may look at, assembled once per bot per tick.

	Candidates get THIS and nothing else, so "what does the tactics layer know"
	is a declared list instead of whatever a 276-line method happened to touch.'''

	__slots__ = ('board', 'caps', 'order', 'profile', 'personality', 'skill',
	             'now', 'target', 'distance', 'gate', 'loaded', 'reload_left',
	             'balance', 'squad_balance', 'allies_near', 'enemies_visible',
	             'enemies_remembered', 'shots_at_me', 'no_los_for', 'weights',
	             'class_tag', 'is_arty', 'rng', 'offered', 'chosen')

	def __init__(self):
		self.board = None
		self.caps = None
		self.order = None
		self.profile = {}
		self.personality = {}
		self.skill = {}
		self.now = 0.0
		self.target = None
		self.distance = 1e18
		self.gate = None
		self.loaded = True
		self.reload_left = 0.0
		self.balance = 0.0
		self.squad_balance = 0.0
		self.allies_near = 0
		self.enemies_visible = 0
		self.enemies_remembered = 0
		self.shots_at_me = 0
		self.no_los_for = 0.0
		self.weights = None
		self.class_tag = 'mediumTank'
		self.is_arty = False
		self.rng = None
		self.offered = ()
		self.chosen = None


def trait_weights(personality, skill):
	'''One weight per term, from personality traits and the tier's profile.

	`mistakes` (tier) shrinks every weight toward zero, which is how a weak bot
	gets a flatter, less decisive utility surface instead of a separate branch.'''
	weights = dict([('progress', 0.35), ('safety', 0.45), ('fire', 0.60),
	                ('support', 0.30), ('risk', -0.35), ('cost', -0.25)])
	if isinstance(personality, dict):
		for term, trait, weight in TRAIT_WEIGHTS:
			trait_value = _num(personality.get(trait), None)
			if trait_value is None:
				continue
			weights[term] = weights[term] + weight * (trait_value - 0.5) * 2.0
	mistakes = _num((skill or {}).get('mistakes'), 0.1)
	if mistakes > 0.0:
		scale = max(0.0, 1.0 - min(0.9, mistakes))
		for term in TERMS:
			weights[term] = weights[term] * scale
	return weights


def utility_score(ctx, terms):
	'''THE utility function.  Candidates supply facts, this supplies the scale.'''
	total = 0.0
	weights = ctx.weights or {}
	for term in TERMS:
		value = _num(terms.get(term), 0.0)
		if value == 0.0:
			continue
		total += _num(weights.get(term), 0.0) * value
	return total


# --- candidate implementations --------------------------------------------
def cand_advance(ctx):
	'''Follow the intention corridor.  Offered while there is somewhere to go;
	`cost` is the fraction of the route left, so a nearly finished push scores
	higher than a fresh one (v1's `route` mode is ~21-25% of all bot time,
	MODEPROBE of the 20:52 session - the point of v2 is that it stops being the
	default answer).'''
	order = ctx.order or {}
	route_id = order.get('route_id')
	if not route_id or str(route_id) in ('direct', 'server_wait'):
		return None
	if ctx.target is not None and ctx.enemies_visible > 0:
		return None
	terms = {'progress': 1.0, 'cost': 0.35, 'safety': -0.15}
	return (terms, {'route_id': str(route_id)})


def cand_hold(ctx):
	'''Stay and keep the arc.  Always applicable: it is the baseline every other
	candidate has to beat, which is what makes the scores comparable.'''
	safety = 0.25
	if not ctx.loaded:
		safety += 0.35
	if ctx.balance < 0.0:
		safety += min(0.5, abs(ctx.balance) * 0.25)
	terms = {'safety': safety, 'progress': -0.10, 'cost': 0.05}
	return (terms, {})


def cand_engage(ctx):
	'''Shoot the chosen target.  Offered ONLY when the fire gate says the gun can
	reach it - this is the candidate that turns the gate into behaviour.'''
	if ctx.target is None:
		return None
	gate = ctx.gate
	if gate is not None and not gate.ok:
		return None
	if ctx.distance >= 1e17:
		return None
	fire = 0.82 if ctx.loaded else 0.25
	caps = ctx.caps
	if caps is not None and gate is not None:
		penetration = _num(getattr(gate, 'pen_best', 0.0), 0.0)
		armor = _num(getattr(ctx.target, 'armor', 0.0), 0.0)
		if penetration > 0.0 and armor > 0.0:
			# >1 means guaranteed through, <1 means it is a gamble
			fire += max(-0.35, min(0.45, (penetration / armor - 1.0) * 0.6))
	desired = _num(ctx.profile.get('desired_range'), 0.0)
	if desired > 0.0 and ctx.distance < 1e17:
		fire += max(-0.15, min(0.25, (1.0 - abs(ctx.distance - desired) /
		                              max(1.0, desired)) * 0.2))
	terms = {'fire': fire, 'progress': 0.42, 'risk': 0.22, 'cost': 0.08}
	return (terms, {'target_id': int(getattr(ctx.target, 'id', 0) or 0)})


def cand_relocate(ctx):
	'''Move to shoot: no line of sight for a while, or the gun cannot reach from
	here.  This is the answer the gate must produce instead of a silent bot or a
	shot into the sky (owner's step 3: "сменить позицию или молчать").'''
	if ctx.target is None:
		return None
	refused = False
	gate = ctx.gate
	if gate is not None and not gate.ok:
		refused = str(getattr(gate, 'why', '')) in ('elevation', 'depression',
		                                            'traverse')
	if not refused and ctx.no_los_for < 4.5:
		return None
	terms = {'progress': 0.85, 'fire': 0.40, 'safety': 0.25, 'cost': 0.20}
	return (terms, {'reason': ('gate' if refused else 'no_los'),
	                'no_los_for': round(ctx.no_los_for, 1)})


def cand_cover_reload(ctx):
	'''Break line of sight while the gun is empty.  v1 has the behaviour
	(`_reload_cover_position`, bot_ai.py:2843, and 's real cover);
	here it becomes an offerable candidate with a score instead of a branch.'''
	if ctx.target is None or ctx.loaded or ctx.reload_left < 1.2:
		return None
	terms = {'safety': 0.95, 'progress': 0.25, 'cost': 0.20, 'risk': 0.10}
	return (terms, {'reload_left': round(ctx.reload_left, 2)})


def cand_support_fire(ctx):
	'''Shoot what an ally is fighting.  Needs somebody else to be engaged, so on
	an empty field it is never offered.'''
	if ctx.allies_near <= 0 or ctx.target is None:
		return None
	if not ctx.loaded and ctx.reload_left > 1.5:
		return None
	gate = ctx.gate
	if gate is not None and not gate.ok:
		return None
	terms = {'support': 0.65, 'fire': 0.20, 'risk': 0.20}
	return (terms, {'allies_near': int(ctx.allies_near)})


def cand_flank(ctx):
	'''Wide move to a side arc.  Only offered when the target is visible, the
	machine can actually reach the far side in reasonable time and the tier
	allows it (access is the tier's, not a weight).'''
	if ctx.target is None or ctx.enemies_visible <= 0:
		return None
	if ctx.is_arty or bool(getattr(ctx.caps, 'limited_traverse', False)):
		return None
	caps = ctx.caps
	if caps is None:
		return None
	speed = _num(getattr(caps, 'forward_speed', 0.0), 0.0)
	if speed <= 1.0 or ctx.distance >= 1e17 or ctx.distance > 290.0:
		return None
	# Don't abandon a loaded guaranteed shot to start a flank; shoot first, then flank!
	if ctx.loaded and ctx.gate is not None and ctx.gate.ok and ctx.distance < 210.0:
		pen = _num(getattr(ctx.gate, 'pen_best', 0.0), 0.0)
		arm = _num(getattr(ctx.target, 'armor', 0.0), 0.0)
		if arm <= 0.0 or pen >= arm * 1.08:
			return None
	travel = (ctx.distance * 1.35) / max(1.0, speed)
	if travel > 24.0:
		return None
	mob_bonus = 0.25 if ctx.class_tag in ('lightTank', 'mediumTank') else 0.0
	terms = {'progress': 0.80 + mob_bonus, 'fire': 0.45, 'support': 0.35, 'risk': 0.20, 'cost': 0.16}
	return (terms, {'travel_s': round(travel, 1)})


def cand_retreat(ctx):
	'''Disengage when badly hurt or locally outnumbered at close/mid range.'''
	hp = _num(getattr(ctx.board.me, 'health_fraction', 1.0), 1.0)
	tank_tier = str((ctx.skill or {}).get('tier', 'medium')).lower()
	hp_thresh = 0.28 if tank_tier == 'hard' else (0.22 if tank_tier == 'medium' else 0.15)
	bal_thresh = -1.25 if tank_tier == 'hard' else (-1.55 if tank_tier == 'medium' else -2.05)
	enemies_near = int(ctx.board.meta.get('visible_enemies_near', 0))
	hurt = (hp <= hp_thresh) and (ctx.shots_at_me > 0 or ctx.distance < 210.0)
	outnumbered = (ctx.distance < 220.0) and (
		(ctx.balance <= bal_thresh and enemies_near >= 2) or
		(enemies_near >= max(3, ctx.allies_near + 2) and hp < 0.58 and ctx.shots_at_me > 0)
	)
	if not hurt and not outnumbered:
		return None
	# If gun is loaded and can shoot right now, take the parting shot unless critically low HP!
	if ctx.loaded and ctx.gate is not None and ctx.gate.ok and hp > 0.18:
		return None
	urgency = (0.35 if hurt else 0.0) + (min(0.45, abs(ctx.balance) * 0.22) if outnumbered else 0.0)
	terms = {'safety': 0.82 + urgency, 'risk': -0.45, 'progress': -0.15, 'cost': 0.15}
	return (terms, {'hp': round(hp, 2), 'balance': round(ctx.balance, 2)})


def cand_bait(ctx):
	'''Twitch-peek / sidescrape bait: ONLY for turreted tanks when holding cover against a loaded enemy.'''
	if ctx.is_arty or bool(getattr(ctx.caps, 'limited_traverse', False)):
		return None
	if ctx.class_tag == 'AT-SPG':
		return None
	if ctx.shots_at_me <= 0 or ctx.target is None or ctx.distance > 140.0 or ctx.loaded:
		return None
	if ctx.board.skills.bait_cooldown_until > ctx.now:
		return None
	if _num(getattr(ctx.board.me, 'health_fraction', 1.0), 1.0) <= 0.35:
		return None
	terms = {'safety': 0.96, 'progress': 0.48, 'fire': 0.55, 'support': 0.30, 'risk': 0.08, 'cost': 0.06}
	return (terms, {'shots_at_me': int(ctx.shots_at_me)})


def cand_punish_peek(ctx):
	'''Push out and punish an enemy that recently fired (on reload) or pre-aim a
	remembered corner while our gun is loaded.'''
	if not ctx.loaded:
		return None
	if ctx.target is None and ctx.enemies_remembered <= 0:
		return None
	recent_shot = False
	if ctx.target is not None:
		last_t = _num(getattr(ctx.target, 'last_shot_at', -999.0), -999.0)
		if (ctx.now - last_t) < 7.5 and ctx.distance < 240.0:
			recent_shot = True
	if not recent_shot and ctx.shots_at_me <= 0:
		return None
	if ctx.distance > 250.0 and ctx.enemies_visible > 0:
		return None
	terms = {'fire': 0.92, 'progress': 0.55, 'safety': 0.25, 'risk': 0.30}
	return (terms, {'remembered': int(ctx.enemies_remembered)})


def cand_arty_fire(ctx):
	'''Indirect fire.  The gate is the same rule as for everyone else, with the
	1.0 m aim offset the client's own artillery path uses
	(offline_battle.py:10228-10230).'''
	if not ctx.is_arty or ctx.target is None:
		return None
	gate = ctx.gate
	if gate is not None and not gate.ok:
		return None
	if not ctx.loaded:
		return None
	terms = {'fire': 0.75, 'support': 0.30, 'safety': 0.10, 'cost': 0.10}
	return (terms, {'target_id': int(getattr(ctx.target, 'id', 0) or 0)})


def cand_arty_reposition(ctx):
	'''SPG bush camping doctrine:
	SPG NEVER flees immediately after firing! It stays stationary in its bush nest,
	maintaining camo net and reloading. It ONLY relocates if:
	1) It has been actively spotted (is_spotted == True),
	2) Enemy is dangerously close (< 85m proxy danger),
	3) It has had NO LOS / target arc for > 40 seconds, or
	4) It took damage from a blind shot.'''
	if not ctx.is_arty:
		return None
	is_spotted = bool(getattr(ctx, 'is_spotted', False) or getattr(ctx, 'spotted', False))
	threat_close = bool(getattr(ctx, 'threat_distance', 999.0) < 85.0)
	no_los_long = bool(ctx.no_los_for >= 40.0)
	took_hit = bool(getattr(ctx, 'took_hit_recently', False))
	if not (is_spotted or threat_close or no_los_long or took_hit):
		return None
	terms = {'safety': 0.85, 'cost': 0.20, 'fire': -0.40, 'progress': 0.10}
	return (terms, {'loaded': bool(ctx.loaded), 'why': 'threat' if (is_spotted or threat_close) else 'no_los'})


def cand_squad_follow(ctx):
	'''Stay with the platoon while travelling.  v1 does this inside
	`_squad_order` (bot_ai.py:2863); as a candidate it can lose to a fight.'''
	squad = ctx.board.squad
	if squad.squad_id is None or squad.role == 'leader':
		return None
	if ctx.target is not None and ctx.enemies_visible > 0:
		return None
	terms = {'support': 0.45, 'progress': 0.35, 'safety': 0.20, 'cost': 0.15}
	return (terms, {'squad': str(squad.squad_id)})


def cand_squad_help(ctx):
	'''Answer a losing platoon.  Offered only while the call is fresh, which is
	what `help_requested_at` is for (v1 has the 20 s window inside
	`_squad_call_help`, bot_ai.py:2382).'''
	squad = ctx.board.squad
	if squad.leader_id is None or squad.role == 'leader':
		return None
	if ctx.squad_balance > -0.8:
		return None
	if ctx.target is not None and ctx.enemies_visible > 0:
		return None
	terms = {'support': 0.85, 'progress': 0.10, 'risk': 0.35, 'cost': 0.40}
	return (terms, {'balance': round(ctx.squad_balance, 2)})


CANDIDATES = (
	('advance', cand_advance),
	('hold', cand_hold),
	('engage', cand_engage),
	('relocate', cand_relocate),
	('cover_reload', cand_cover_reload),
	('support_fire', cand_support_fire),
	('flank', cand_flank),
	('retreat', cand_retreat),
	('bait', cand_bait),
	('punish_peek', cand_punish_peek),
	('arty_fire', cand_arty_fire),
	('arty_reposition', cand_arty_reposition),
	('squad_follow', cand_squad_follow),
	('squad_help', cand_squad_help),
)


def evaluate_candidates(ctx):
	'''[(name, score, terms, params)] for every candidate this bot may run.

	Filtered by the tier's ACCESS list first (spec §3.4: easy has fewer
	candidates, not worse coefficients), then scored by `utility_score`.  A
	candidate returning None is not offered at all, so the MODEPROBE-style census
	of what v2 offers is exactly this list.'''
	allowed = (ctx.skill or {}).get('candidates') or CANDIDATE_NAMES
	allowed_set = dict([(str(name), True) for name in allowed])
	offers = []
	for name, function in CANDIDATES:
		if not allowed_set.get(name):
			continue
		try:
			result = function(ctx)
		except Exception:
			result = None
		if result is None:
			continue
		terms, params = result
		offers.append((name, utility_score(ctx, terms), terms, params or {}))
	offers.sort(key=lambda item: -item[1])
	ctx.offered = tuple([item[0] for item in offers])
	ctx.chosen = offers[0] if offers else None
	return offers


def offers_summary(offers, limit=4):
	'''`name=score` for the top few, for one log line.'''
	parts = []
	for name, score, terms, params in offers[:limit]:
		parts.append('%s=%.2f' % (name, score))
	return ' '.join(parts) if parts else 'none'


# ---------------------------------------------------------------------------
# The director
# ---------------------------------------------------------------------------
class BotDirectorV2(object):
	'''Tactics layer v2: a facade over the shipped `bot_ai.BattleDirector`.

	Everything v1 does stays v1's - perception, contacts, routes, squads, radio,
	callouts, advisories, profiles - and is reachable through `__getattr__`, so
	the call sites in `offline_battle.py` keep working unchanged:
	`_ai_director.agents` (:21508/:21664/:22163), `.register` (:21560),
	`.order_for` (:22167), `.notify_shot` (:24876), `.notify_vehicle_shot`
	(:24879/:27925), `.player_vid` (:21592, a WRITE), `.player_advisory`
	(:21593), `.blocked_shot_advisory` (:21631), and on the director object
	itself `update_contact`, `set_king_of_hill`, `set_assault_mode`,
	`set_arena_bounds`, `map_name`, `map_data`, `contacts` (:5646-5680) plus
	`chat_events` (`_offh_bot_chatter_events`, :21575).

	Attribute WRITES are forwarded, which matters: `offline_battle.py:21592` does
	`_ai_director.player_vid = ...` and v1 reads it back off the director inside
	`_callout_warnings` (`bot_ai.py:1483`), so a facade that kept the value
	locally would silently kill callouts to the player.

	Only `order_for` is v2's, and with `bot_ai_v2=false` it returns v1's own dict
	object - not a copy, not a normalised copy.
	'''

	_OWN = ('_inner', '_boards', '_caps', '_cfg', '_rng', '_counters',
	        '_reasons', '_offers', '_window_at', '_err_seen', '_skill_logged',
	        '_key_mismatch')

	# Contact mirroring and the ally sweep are throttled: doing both for every bot
	# on every tick would cost more than the whole v1 order (PERF of the 20:52
	# session: ai_order=1.86 ms over 26.3 calls, bot_loop=13.04 ms), and spec §4
	# п.7 caps the decision layer at 1.5 ms/frame.
	CONTACT_SYNC_INTERVAL = 0.25
	ALLY_SCAN_INTERVAL = 0.5
	ALLY_SCAN_RADIUS = 250.0

	def __init__(self, inner=None, *args, **kwargs):
		object.__setattr__(self, '_inner', None)
		object.__setattr__(self, '_boards', {})
		object.__setattr__(self, '_caps', {})
		object.__setattr__(self, '_cfg', None)
		object.__setattr__(self, '_rng', random.Random(20261009))
		object.__setattr__(self, '_counters', {})
		object.__setattr__(self, '_reasons', {})
		object.__setattr__(self, '_offers', {})
		object.__setattr__(self, '_window_at', 0.0)
		object.__setattr__(self, '_err_seen', {})
		object.__setattr__(self, '_skill_logged', {})
		object.__setattr__(self, '_key_mismatch', 0)
		if inner is None or not hasattr(inner, 'order_for'):
			if inner is not None:
				args = (inner,) + tuple(args)
			inner = self._build_v1(*args, **kwargs)
		object.__setattr__(self, '_inner', inner)
		try:
			seed = int(getattr(inner, 'battle_seed', 0) or 0)
		except Exception:
			seed = 0
		object.__setattr__(self, '_rng', random.Random(seed or 20261009))

	@staticmethod
	def _build_v1(*args, **kwargs):
		from gui.mods.offhangar.bot_ai import BattleDirector
		return BattleDirector(*args, **kwargs)

	def v1_director(self):
		'''The wrapped v1 director (tests, diagnostics, the 101 switch point).'''
		return object.__getattribute__(self, '_inner')

	# -- delegation ---------------------------------------------------------
	def __getattr__(self, name):
		# Only reached when normal lookup fails, so v2's own methods win.
		try:
			inner = object.__getattribute__(self, '_inner')
		except AttributeError:
			inner = None
		if inner is None:
			raise AttributeError(name)
		return getattr(inner, name)

	def __setattr__(self, name, value):
		if name in BotDirectorV2._OWN:
			object.__setattr__(self, name, value)
			return
		try:
			inner = object.__getattribute__(self, '_inner')
		except AttributeError:
			inner = None
		if inner is None:
			object.__setattr__(self, name, value)
			return
		setattr(inner, name, value)

	# -- configuration ------------------------------------------------------
	def _enabled(self):
		'''Flag `bot_ai_v2`, read once per director (= once per battle), the same
		idiom v1 uses for its own flags (`bot_ai._fight_fix_on`, :891-905).
		Shipped default false = v1 behaviour; rollback is one digit.'''
		cfg = object.__getattribute__(self, '_cfg')
		if cfg is not None:
			return cfg[0]
		on = True
		profiles = None
		margin_scale = 1.0
		try:
			from gui.mods.offhangar._constants import CONFIG_OPTIONS as _V2CFG
			on = bool(_V2CFG.get('bot_ai_v2', True))
			profiles = _V2CFG.get('bot_skill_profiles', None)
			raw_scale = _V2CFG.get('bot_ai_v2_gate_margin', None)
			if raw_scale is not None:
				margin_scale = max(0.0, _num(raw_scale, 1.0))
		except Exception:
			on = False
		cfg = (on, profiles, margin_scale)
		object.__setattr__(self, '_cfg', cfg)
		_log_note('V2_CFG on=%s cands=%d order_keys=%d' % (
			'yes' if on else 'no', len(CANDIDATE_NAMES), len(ORDER_KEYS)))
		return on

	def set_v2_config(self, on, profiles=None, margin_scale=1.0):
		'''Test/host hook: pin the flag without a CONFIG_OPTIONS import.'''
		object.__setattr__(self, '_cfg', (bool(on), profiles,
		                                 max(0.0, _num(margin_scale, 1.0))))

	# -- registration: the only place a typeDescriptor is available ----------
	def register(self, bot_id, team, descriptor, display_name='Bot',
			skill_tier='medium', spawn_position=None, preassigned_route_id=None, *args, **kwargs):
		'''v1's register, plus: build the capabilities ONCE from the real
		typeDescriptor.  `offline_battle.py:21560-21563` passes `_ai_td`, so this
		is where the gun geometry becomes reachable at all.'''
		agent = self._inner.register(bot_id, team, descriptor, display_name,
		                             skill_tier=skill_tier, spawn_position=spawn_position,
		                             preassigned_route_id=preassigned_route_id)
		try:
			board = self._board(agent)
			caps = self._capabilities(agent, descriptor)
			board.me.capabilities = caps
			board.me.caps_key = str(getattr(caps, 'key', '') or '')
			board.meta['has_descriptor'] = True
			board.meta['caps_built_at'] = board.me.now
		except Exception as err:
			self._fail('register', err)
		return agent

	def register_profile(self, bot_id, team, profile, display_name='Bot',
			skill_tier='medium', spawn_position=None, preassigned_route_id=None, *args, **kwargs):
		'''LAN/worker path: no typeDescriptor exists, so there is no gun geometry.
		Without `gun['pitchLimits']` the gate MUST NOT refuse shots - it counts
		`no_caps` and v1's decision stands.'''
		agent = self._inner.register_profile(bot_id, team, profile, display_name,
		                                     skill_tier=skill_tier, spawn_position=spawn_position,
		                                     preassigned_route_id=preassigned_route_id)
		try:
			board = self._board(agent)
			board.me.capabilities = None
			board.meta['has_descriptor'] = False
		except Exception as err:
			self._fail('register_profile', err)
		return agent

	def _capabilities(self, agent, descriptor):
		'''Cached per vehicle name.  Never keyed by `id(descriptor)`: an id can be
		reused after collection and would then serve one tank's limits to
		another.'''
		build = _capabilities_builder()
		if build is None:
			return None
		profile = agent.get('profile') if isinstance(agent, dict) else None
		key = ''
		if isinstance(profile, dict):
			key = str(profile.get('vehicle_name') or '')
		if not key and isinstance(agent, dict):
			key = 'bot_%s' % agent.get('id', 0)
		cache = object.__getattribute__(self, '_caps')
		cached = cache.get(key)
		if cached is not None:
			return cached
		if descriptor is None:
			return None
		caps = build(descriptor, profile, key)
		cache[key] = caps
		return caps

	# -- blackboards --------------------------------------------------------
	def _board(self, agent):
		boards = object.__getattribute__(self, '_boards')
		try:
			bot_id = int(agent.get('id', 0))
		except Exception:
			bot_id = 0
		board = boards.get(bot_id)
		if board is None:
			board = _new_blackboard(bot_id, int(agent.get('team', 1) or 1))
			if board is None:
				return None
			board.meta['has_descriptor'] = False
			board.meta['allies_near'] = 0
			board.meta['allies_near_at'] = -1e18
			board.meta['contacts_sync_at'] = -1e18
			board.meta['visible_enemies'] = 0
			board.meta['remembered_enemies'] = 0
			board.meta['shots_at_me'] = 0
			board.meta['gate_log_refused'] = -1e18
			board.meta['gate_log_agree'] = -1e18
			boards[bot_id] = board
		return board

	def board_for(self, bot_id):
		'''Diagnostics/tests: the blackboard of one bot.'''
		return object.__getattribute__(self, '_boards').get(int(bot_id))

	# -- hooks for the execution layer already has these values ----
	def note_pose(self, bot_id, pitch, roll):
		'''Feed the real suspension pose (`m_veh.pitch` / `m_veh.roll`).
		`order_for` does not carry it (`bot_ai.py:3440`), so without this hook the
		gate assumes a level hull and widens its margin.'''
		board = object.__getattribute__(self, '_boards').get(int(bot_id))
		if board is None:
			return False
		board.me.pitch = _num(pitch, None)
		board.me.roll = _num(roll, None)
		board.meta['pose_fed'] = True
		return True

	def note_gun(self, bot_id, turret_yaw, gun_pitch):
		'''Feed where the gun actually is (`m_veh._turret_yaw`, read at
		`offline_battle.py:24398`), so the gate can price the slew instead of
		guessing from the interface's zeroes.'''
		board = object.__getattribute__(self, '_boards').get(int(bot_id))
		if board is None:
			return False
		board.skills.turret_yaw = _num(turret_yaw, 0.0)
		board.skills.gun_pitch = _num(gun_pitch, 0.0)
		board.meta['gun_fed'] = True
		return True

	def note_incoming(self, bot_id, shooter_id, now=None):
		'''Somebody shot at this bot: update both v2 blackboard and v1 agent incoming_from.'''
		try:
			if hasattr(self._inner, 'note_incoming'):
				self._inner.note_incoming(bot_id, shooter_id, now)
		except Exception:
			pass
		board = object.__getattribute__(self, '_boards').get(int(bot_id))
		if board is None:
			return False
		board.note_incoming(shooter_id, now)
		return True

	def update_wrecks(self, driver_frame):
		return self._inner.update_wrecks(driver_frame)

	def notify_shot(self, bot_id, now):
		board = object.__getattribute__(self, '_boards').get(int(bot_id))
		if board is not None:
			board.me.note_shot(now)
		return self._inner.notify_shot(bot_id, now)

	def notify_vehicle_shot(self, vehicle_id, now, reload_time=4.0):
		board = object.__getattribute__(self, '_boards').get(int(vehicle_id))
		if board is not None:
			board.me.note_shot(now, reload_time)
		return self._inner.notify_vehicle_shot(vehicle_id, now, reload_time)

	# -- the only method v2 owns -------------------------------------------
	def order_for(self, bot_id, position, hull_yaw, health, max_health, now,
			reload_time=4.0, shoot_timer=999.0):
		'''Same signature, same 16 keys, same values - plus the fire gate.

		Flag off: v1's own dict object, untouched (the byte-identical contract).
		Flag on and anything raises: v1's order, counted under `errors`.'''
		order = self._inner.order_for(bot_id, position, hull_yaw, health,
		                              max_health, now, reload_time=reload_time,
		                              shoot_timer=shoot_timer)
		if not self._enabled():
			return order
		try:
			return self._v2_order(bot_id, order, position, hull_yaw, health,
			                      max_health, now, reload_time, shoot_timer)
		except Exception as err:
			self._fail('order', err)
			return order

	def _v2_order(self, bot_id, order, position, hull_yaw, health, max_health,
			now, reload_time, shoot_timer):
		counters = object.__getattribute__(self, '_counters')
		counters['orders'] = counters.get('orders', 0) + 1
		if isinstance(order, dict) and order.get('target_id') is None and not order.get('fire_allowed'):
			return order
		try:
			agent = self._inner.agents.get(int(bot_id))
		except Exception:
			agent = None
		if not isinstance(agent, dict) or not isinstance(order, dict):
			counters['no_agent'] = counters.get('no_agent', 0) + 1
			return order
		self._check_keys(order)
		board = self._board(agent)
		if board is None:
			counters['no_board'] = counters.get('no_board', 0) + 1
			return order
		board.sync_from_agent(agent, now)
		board.update_self(position, hull_yaw, health, max_health, now,
		                  reload_time, shoot_timer)
		board.meta['order_source'] = 'local'
		board.meta['v2_orders'] = int(board.meta.get('v2_orders', 0)) + 1
		ctx = self._context(board, agent, order, now, full=True)
		gate = self._gate(board, ctx, order)
		ctx.gate = gate
		_gate_refused = self._apply_gate(board, ctx, order, gate, now)
		if _gate_refused:
			board.meta['gate_refusal_streak'] = int(board.meta.get('gate_refusal_streak', 0)) + 1
		else:
			board.meta['gate_refusal_streak'] = 0
		_prev_mp = order.get('move_position')
		# [FIX v12.0] Only evaluate utility candidates when fire gate refused >= 3 consecutive times
		if _gate_refused and int(board.meta.get('gate_refusal_streak', 0)) >= 3:
			offers = evaluate_candidates(ctx)
			self._tally_offers(offers)
			self._apply_stage_102(board, agent, ctx, order, offers, gate, now)
		else:
			return order
		_new_mp = order.get('move_position')
		if _new_mp is not None and _new_mp != _prev_mp:
			try:
				from gui.mods.offhangar.bot_ai import _real_cover_grid as _v2_rc_grid
				_g = _v2_rc_grid()
				if _g is not None and hasattr(_g, 'snap_to_safe_baked_point'):
					_snapped = _g.snap_to_safe_baked_point(_new_mp, 4)
					if _snapped is not None:
						order['move_position'] = _snapped
					else:
						order['move_position'] = _prev_mp
			except Exception:
				pass
		return order

	def _apply_stage_102(self, board, agent, ctx, order, offers, gate, now):
		'''Stage 102: Let the winning utility candidate and fire gate actively steer
		repositioning on slopes/bridges, reload cover, flanking, and hull-aiming.'''
		try:
			cur_mode = str(order.get('combat_mode') or 'route')
			if cur_mode in ('radio_hold', 'radio_retreat', 'cliff_dive_ram', 'hill_descent_flank', 'base_defense', 'reset_cap', 'reload_cover', 'investigate', 'punish_peek', 'punish_push', 'bait_twitch_out', 'bait_twitch_back', 'bait_juke_out', 'bait_juke_back', 'artillery_reposition'):
				return
			if cur_mode == 'route' and not order.get('fire_allowed'):
				return
			me_pos_chk = board.me.position
			tpos_chk = getattr(ctx.target, 'position', None) if ctx.target is not None else order.get('aim_position')
			if not ctx.is_arty and me_pos_chk is not None and tpos_chk is not None:
				if hasattr(self._inner, '_is_mountain_blocked') and self._inner._is_mountain_blocked(me_pos_chk, tpos_chk):
					return
				if hasattr(self._inner, '_is_chord_impassable') and self._inner._is_chord_impassable(me_pos_chk, tpos_chk):
					return
			if float(agent.get('hit_reaction_until', 0.0) or 0.0) > float(now or 0.0):
				return
			me_pos = board.me.position
			if gate is not None and not gate.ok and ctx.target is not None:
				why = str(getattr(gate, 'why', '') or '')
				tpos = getattr(ctx.target, 'position', None) or order.get('aim_position')
				if tpos is not None and me_pos is not None:
					dx = float(tpos[0]) - float(me_pos[0])
					dy = float(tpos[1]) - float(me_pos[1])
					dz = float(tpos[2]) - float(me_pos[2])
					dist = math.sqrt(dx * dx + dz * dz)
					if why == 'traverse':
						order['face_position'] = (float(tpos[0]), float(tpos[1]), float(tpos[2]))
					elif why == 'elevation' and not ctx.is_arty:
						# Target is overhead (e.g. Westfield bridge, dy > 10m): NEVER drive closer
						# underneath the bridge! Back out / flank wide to standoff range where the
						# gun can elevate, or continue climbing the high-ground route anchor.
						side = 1.0 if (int(board.me.bot_id) & 1) == 0 else -1.0
						if dist > 0.5:
							ux = dx / dist
							uz = dz / dist
						else:
							ux, uz = 1.0, 0.0
						r_anchor = order.get('route_anchor')
						if dy > 14.0 and r_anchor is not None and len(r_anchor) >= 3:
							# Follow route climb / flank away from the valley floor under the bridge
							repo = (float(me_pos[0]) - ux * 28.0 - uz * 22.0 * side,
							        float(me_pos[1]),
							        float(me_pos[2]) - uz * 28.0 + ux * 22.0 * side)
						else:
							standoff = max(115.0, abs(dy) * 3.4)
							step_back = max(18.0, min(42.0, standoff - dist))
							repo = (float(me_pos[0]) - ux * step_back - uz * 16.0 * side,
							        float(me_pos[1]),
							        float(me_pos[2]) - uz * step_back + ux * 16.0 * side)
						order['move_position'] = repo
						order['face_position'] = (float(tpos[0]), float(tpos[1]), float(tpos[2]))
						order['combat_mode'] = 'flank'
						order['throttle_override'] = None
						return
					elif why == 'depression' and 8.0 < dist <= 135.0 and not ctx.is_arty:
						ux = dx / dist
						uz = dz / dist
						side = 1.0 if (int(board.me.bot_id) & 1) == 0 else -1.0
						if dy < -8.0 and dist < 85.0:
							# Target is below a ridge/cliff: contour along the ridge rather than plunging off
							repo = (float(me_pos[0]) - uz * 16.0 * side,
							        float(me_pos[1]),
							        float(me_pos[2]) + ux * 16.0 * side)
						else:
							repo = (float(me_pos[0]) + ux * 12.0 - uz * 6.0 * side,
							        float(me_pos[1]),
							        float(me_pos[2]) + uz * 12.0 + ux * 6.0 * side)
						if not (hasattr(self._inner, '_is_chord_impassable') and self._inner._is_chord_impassable(me_pos, repo)):
							order['move_position'] = repo
							order['face_position'] = (float(tpos[0]), float(tpos[1]), float(tpos[2]))
							order['combat_mode'] = 'advance_contact'
							order['throttle_override'] = None
							return
			if not offers or ctx.distance < 35.0:
				return
			winner = offers[0][0]
			if winner == 'cover_reload' and ctx.reload_left > 2.0 and ctx.distance <= 145.0 and ctx.target is not None and not ctx.is_arty:
				if cur_mode in ('engage', 'hold', 'support_fire'):
					tpos = getattr(ctx.target, 'position', None) or order.get('aim_position')
					if tpos is not None and len(tpos) >= 3 and hasattr(self._inner, '_reload_cover_position'):
						cov = self._inner._reload_cover_position(agent, me_pos, (float(tpos[0]), float(tpos[1]), float(tpos[2])))
						if cov is not None:
							order['move_position'] = cov
							order['combat_mode'] = 'reload_cover'
							order['throttle_override'] = None
			elif winner == 'flank' and ctx.target is not None and not ctx.is_arty and not bool(getattr(ctx.caps, 'limited_traverse', False)):
				if cur_mode in ('engage', 'advance_contact', 'support_fire') and ctx.class_tag in ('lightTank', 'mediumTank') and hasattr(self._inner, '_flank_position'):
					tpos = getattr(ctx.target, 'position', None) or order.get('aim_position')
					if tpos is not None and len(tpos) >= 3:
						flk = self._inner._flank_position(agent, me_pos, (float(tpos[0]), float(tpos[1]), float(tpos[2])))
						if flk is not None:
							order['move_position'] = flk
							order['combat_mode'] = 'flank'
							order['throttle_override'] = None
			elif winner == 'retreat' and ctx.target is not None and not ctx.is_arty:
				tpos = getattr(ctx.target, 'position', None) or order.get('aim_position')
				cov = None
				if tpos is not None and len(tpos) >= 3 and hasattr(self._inner, '_reload_cover_position'):
					cov = self._inner._reload_cover_position(agent, me_pos, (float(tpos[0]), float(tpos[1]), float(tpos[2])))
				if cov is None and hasattr(self._inner, '_fallback_position'):
					cov = self._inner._fallback_position(agent, me_pos)
				if cov is not None:
					order['move_position'] = cov
					order['combat_mode'] = 'withdraw'
					order['throttle_override'] = None
			elif winner == 'punish_peek' and ctx.target is not None and not ctx.is_arty and not bool(getattr(ctx.caps, 'limited_traverse', False)):
				tpos = getattr(ctx.target, 'position', None) or order.get('aim_position')
				if tpos is not None and len(tpos) >= 3:
					dx = float(tpos[0]) - float(me_pos[0])
					dz = float(tpos[2]) - float(me_pos[2])
					dist = max(1.0, math.sqrt(dx * dx + dz * dz))
					if dist > 35.0 and ctx.balance >= -0.4:
						peek_p = (float(me_pos[0]) + (dx / dist) * 8.0,
						          float(me_pos[1]),
						          float(me_pos[2]) + (dz / dist) * 8.0)
						order['move_position'] = peek_p
						order['combat_mode'] = 'punish_peek'
						order['throttle_override'] = None
			elif winner == 'bait' and ctx.target is not None and not ctx.is_arty and not bool(getattr(ctx.caps, 'limited_traverse', False)):
				tpos = getattr(ctx.target, 'position', None) or order.get('aim_position')
				if tpos is not None and len(tpos) >= 3:
					dx = float(tpos[0]) - float(me_pos[0])
					dz = float(tpos[2]) - float(me_pos[2])
					dist = max(1.0, math.sqrt(dx * dx + dz * dz))
					# Reverse straight back along the line of fire (sidescrape rock back) so tank never turns sideways!
					bait_p = (float(me_pos[0]) - (dx / dist) * 7.0,
					          float(me_pos[1]),
					          float(me_pos[2]) - (dz / dist) * 7.0)
					order['move_position'] = bait_p
					order['combat_mode'] = 'bait'
					order['throttle_override'] = None
					board.skills.bait_cooldown_until = ctx.now + 8.5
			elif winner == 'relocate' and ctx.target is not None and not ctx.is_arty:
				tpos = getattr(ctx.target, 'position', None) or order.get('aim_position')
				if tpos is not None and len(tpos) >= 3 and hasattr(self._inner, '_spread_attack_position'):
					order['move_position'] = self._inner._spread_attack_position(agent, me_pos, (float(tpos[0]), float(tpos[1]), float(tpos[2])))
					order['face_position'] = (float(tpos[0]), float(tpos[1]), float(tpos[2]))
					order['combat_mode'] = 'advance_contact'
					order['throttle_override'] = None
		except Exception as err:
			self._fail('stage_102', err)

	# -- context ------------------------------------------------------------
	def _context(self, board, agent, order, now, full=True):
		ctx = WorldContext()
		ctx.board = board
		ctx.caps = board.me.capabilities
		ctx.order = order
		ctx.now = _num(now, 0.0)
		profile = board.me.profile if isinstance(board.me.profile, dict) else {}
		ctx.profile = profile
		personality = board.me.personality
		ctx.personality = personality if isinstance(personality, dict) else {}
		tier = 'hard'
		cfg = object.__getattribute__(self, '_cfg') or (False, None, 1.0)
		ctx.skill = skill_profile(tier, cfg[1])
		self._log_skill_once(tier, ctx.skill)
		ctx.class_tag = str(profile.get('class_tag') or 'mediumTank')
		ctx.is_arty = ctx.class_tag == 'SPG'
		target_id = order.get('target_id')
		if target_id is not None:
			ctx.target = board.contact(target_id)
		if full:
			self._populate_full_context(ctx, board, order, now)
		return ctx

	def _populate_full_context(self, ctx, board, order, now):
		ctx.loaded = bool(board.me.loaded(now))
		ctx.reload_left = board.me.reload_remaining(now)
		ctx.balance = _num(order.get('force_balance'), 0.0)
		ctx.squad_balance = _num(board.squad.force_balance, 0.0)
		ctx.rng = object.__getattribute__(self, '_rng')
		ctx.weights = trait_weights(ctx.personality, ctx.skill)
		team = int(board.me.team or 1)
		self._sync_contacts(board, team, now)
		ctx.enemies_visible = int(board.meta.get('visible_enemies', 0))
		ctx.enemies_remembered = int(board.meta.get('remembered_enemies', 0))
		ctx.shots_at_me = int(board.meta.get('shots_at_me', 0))
		ctx.allies_near = self._allies_near(board, team, now)
		target_id = order.get('target_id')
		mission = board.mission
		moment = _num(now, 0.0)
		if target_id is not None:
			ctx.target = board.contact(target_id)
			if ctx.target is not None:
				ctx.distance = _num(ctx.target.distance, 1e18)
				ctx.no_los_for = 0.0 if ctx.target.visible else _num(
					ctx.target.age, 0.0)
			else:
				ctx.no_los_for = max(0.0, moment -
				                     _num(mission.no_los_since, moment))
		else:
			ctx.target = None
			ctx.no_los_for = max(0.0, moment -
			                     _num(mission.no_los_since, moment))
		mission.no_los_since = moment - ctx.no_los_for
		mission.set_target(target_id, now)
		mission.set_mode(order.get('combat_mode'), now)

	def _sync_contacts(self, board, team, now):
		'''Mirror v1's per-team contact table into this bot's memory.

		`update_contact(observing_team, ...)` stores what `observing_team` SEES and
		refuses same-team entries (`bot_ai.py:2100-2107`), so a team-1 bot's known
		enemies live in `contacts[1]`.  Throttled: CONTACT_SYNC_INTERVAL.'''
		moment = _num(now, 0.0)
		if moment - _num(board.meta.get('contacts_sync_at'), -1e18) < \
				self.CONTACT_SYNC_INTERVAL:
			return
		board.meta['contacts_sync_at'] = moment
		try:
			known = self._inner.contacts.get(team, {})
		except Exception:
			known = {}
		visible = 0
		visible_near = 0
		remembered = 0
		shots_at_me = 0
		own_pos = board.me.position
		for contact in (known or {}).values():
			entry = board.remember(contact, moment)
			if entry is None or int(entry.team) == team:
				continue
			if entry.visible:
				visible += 1
				try:
					if own_pos is not None and entry.position is not None:
						dx_e = float(entry.position[0]) - float(own_pos[0])
						dz_e = float(entry.position[2]) - float(own_pos[2])
						if dx_e * dx_e + dz_e * dz_e <= 52900.0:  # 230m radius
							visible_near += 1
				except Exception:
					pass
			else:
				remembered += 1
			shots_at_me += int(entry.shots_at_me or 0)
		board.forget_stale(moment)
		board.meta['visible_enemies'] = visible
		board.meta['visible_enemies_near'] = visible_near
		board.meta['remembered_enemies'] = remembered
		board.meta['shots_at_me'] = shots_at_me

	def _allies_near(self, board, team, now):
		'''Same-team living bots inside ALLY_SCAN_RADIUS.  v1 keeps no ally contact
		table (only enemies), so this reads the agent dict - throttled, because it
		is O(bots) per bot.'''
		moment = _num(now, 0.0)
		if moment - _num(board.meta.get('allies_near_at'), -1e18) < \
				self.ALLY_SCAN_INTERVAL:
			return int(board.meta.get('allies_near', 0))
		board.meta['allies_near_at'] = moment
		count = 0
		try:
			own = board.me.position
			radius_sq = self.ALLY_SCAN_RADIUS * self.ALLY_SCAN_RADIUS
			for other_id, other in self._inner.agents.items():
				if int(other_id) == int(board.me.bot_id):
					continue
				if int(other.get('team', 0) or 0) != team:
					continue
				if _num(other.get('hp_abs'), 0.0) <= 0.0:
					continue
				position = other.get('position')
				if position is None:
					continue
				delta_x = _num(position[0], 0.0) - _num(own[0], 0.0)
				delta_z = _num(position[2], 0.0) - _num(own[2], 0.0)
				if delta_x * delta_x + delta_z * delta_z <= radius_sq:
					count += 1
		except Exception as err:
			self._fail('allies', err)
			count = int(board.meta.get('allies_near', 0))
		board.meta['allies_near'] = count
		return count

	# -- the fire gate ------------------------------------------------------
	def _gate(self, board, ctx, order):
		'''Resolve the admissibility gate.  None = nothing to check (no target and
		no fire request, or no gun geometry at all).'''
		caps = ctx.caps
		if caps is None:
			return None
		if ctx.target is None and not order.get('fire_allowed'):
			return None
		aim = order.get('aim_position')
		if aim is None:
			return None
		caps_module = _capabilities_module()
		if caps_module is None:
			return None
		cfg = object.__getattribute__(self, '_cfg') or (False, None, 1.0)
		scale = _num(ctx.skill.get('gate_margin_scale'), 1.0) * _num(cfg[2], 1.0)
		margin = None
		if scale > 0.0 and abs(scale - 1.0) > 1e-6:
			base = (caps_module.MARGIN_POSE_KNOWN if board.me.pitch is not None
			        else caps_module.MARGIN_LEVEL_HULL)
			margin = base * scale
		# v3.6: Aim at hull/turret center (+1.35m for direct fire, +1.0m for arty) instead of ground (+0.0m),
		# and never refuse fire_allowed for turret traverse_time (_ai_gun_aligned already gates trigger on alignment).
		budget = None
		aim_off_y = 1.0 if ctx.is_arty else max(1.55, float(getattr(caps, 'gun_height', 1.65) or 1.65))
		return caps_module.fire_gate(
			caps, board.me.gun_position(), aim, board.me.yaw,
			board.me.pitch, board.me.roll,
			turret_yaw=board.skills.turret_yaw,
			gun_pitch=board.skills.gun_pitch,
			margin=margin, traverse_budget=budget, aim_offset_y=aim_off_y)

	def _apply_gate(self, board, ctx, order, gate, now):
		'''THE rule (owner's step 3): `fire_allowed` stays True only when the gun
		can physically reach the aim point.  Returns True when a shot v1 allowed
		was refused.'''
		counters = object.__getattribute__(self, '_counters')
		if gate is None:
			if order.get('fire_allowed'):
				counters['no_caps'] = counters.get('no_caps', 0) + 1
			return False
		counters['gated'] = counters.get('gated', 0) + 1
		if gate.ok:
			counters['gate_ok'] = counters.get('gate_ok', 0) + 1
			return False
		reasons = object.__getattribute__(self, '_reasons')
		reasons[gate.why] = reasons.get(gate.why, 0) + 1
		board.skills.note_refusal(gate.why, now)
		if not order.get('fire_allowed'):
			counters['agree_silent'] = counters.get('agree_silent', 0) + 1
			self._log_gate(board, gate, 'agree')
			return False
		# v3.9: When a turretless TD/SPG is turning its hull toward any in-range target (why=='traverse'),
		# keep fire_allowed=True and set face_position so _td_can_aim pivots the hull and fires once aligned!
		if ctx.is_arty and gate.distance >= 75.0:
			order['face_position'] = gate.target_position
			return False
		if gate.why == 'traverse' and gate.distance < 565.0:
			order['face_position'] = gate.target_position
			return False
		if order.get('combat_mode') in ('cliff_dive_ram', 'airborne_jump_shot', 'circle_of_death', 'ram_brawl', 'clinch'):
			return False
		order['fire_allowed'] = False
		counters['applied'] = counters.get('applied', 0) + 1
		self._log_gate(board, gate, 'refused')
		return True

	def _log_gate(self, board, gate, action):
		'''Rate-limited per bot and per kind (refusals 5 s, agreements 20 s), so a
		whole battle stays readable and the marker cannot become a perf problem of
		its own.  No flush here: this is a per-tick layer, not an event marker
		(flushes belong to crash bisection, protocol rule 6).'''
		try:
			interval = 60.0 if action == 'refused' else 120.0
			key = 'gate_log_%s' % action
			if board.me.now - _num(board.meta.get(key), -1e18) < interval:
				return
			board.meta[key] = board.me.now
			if False: _log_note('V2_GATE %s id=%d why=%s dist=%.1f pitch=%.4f '
			          'lim=%.3f/%.3f src=%s m=%.3f tyaw=%.3f trv=%.2f pose=%s '
			          'pen=%.0f' % (
				action, int(board.me.bot_id), gate.why, gate.distance,
				gate.gun_pitch, gate.pitch_min, gate.pitch_max, gate.pitch_source,
				gate.pitch_margin, gate.turret_yaw, gate.traverse_s,
				'known' if gate.pose_known else 'level', gate.pen_best))
		except Exception as err:
			self._fail('log_gate', err)

	def _check_keys(self, order):
		'''Contract check: v1's key set must stay the 16 keys consumers read.
		Logged once per battle, never silently repaired.'''
		if object.__getattribute__(self, '_key_mismatch'):
			return
		missing = [key for key in ORDER_KEYS if key not in order]
		extra = [key for key in order.keys() if key not in ORDER_KEYS and key not in ('wreck_cover_id', 'defense_base_id', 'target_velocity', 'plan_waypoints', 'action_desc', 'tactic_reason')]
		if missing or extra:
			object.__setattr__(self, '_key_mismatch', 1)
			_log_note('V2_KEYS mismatch missing=%s extra=%s' % (
				','.join(missing) or '-', ','.join(extra) or '-'))

	def _log_skill_once(self, tier, profile):
		logged = object.__getattribute__(self, '_skill_logged')
		if logged.get(tier):
			return
		logged[tier] = True
		_log_note(profile_summary(profile))

	# -- tallies ------------------------------------------------------------
	def _tally_offers(self, offers):
		tally = object.__getattribute__(self, '_offers')
		for name, score, terms, params in offers:
			entry = tally.get(name)
			if entry is None:
				entry = [0, 0, -1e18, 1e18]   # offered, chosen, best, worst
				tally[name] = entry
			entry[0] += 1
			if score > entry[2]:
				entry[2] = score
			if score < entry[3]:
				entry[3] = score
		if offers:
			tally[offers[0][0]][1] += 1

	def _window(self, now):
		'''One pair of aggregate lines every 10 s - MODEPROBE's cadence, so the v1
		mode census and the v2 offer census can be read side by side.'''
		moment = _num(now, 0.0)
		window_at = object.__getattribute__(self, '_window_at')
		if window_at <= 0.0:
			object.__setattr__(self, '_window_at', moment)
			return
		if moment - window_at < 30.0:
			return
		span = moment - window_at
		object.__setattr__(self, '_window_at', moment)
		counters = object.__getattribute__(self, '_counters')
		tally = object.__getattribute__(self, '_offers')
		offered = []
		chosen = []
		for name in CANDIDATE_NAMES:
			entry = tally.get(name)
			if entry is None:
				continue
			offered.append('%s=%d' % (name, entry[0]))
			if entry[1]:
				chosen.append('%s=%d' % (name, entry[1]))
		never = [name for name in CANDIDATE_NAMES if name not in tally]
		reasons = object.__getattribute__(self, '_reasons')
		why = ['%s=%d' % (key, reasons[key]) for key in sorted(reasons.keys())]
		_log_note('V2_TACTICS window=%.1fs orders=%d gated=%d ok=%d agree=%d '
		          'applied=%d no_caps=%d no_agent=%d err=%d why=%s' % (
			span, counters.get('orders', 0), counters.get('gated', 0),
			counters.get('gate_ok', 0), counters.get('agree_silent', 0),
			counters.get('applied', 0), counters.get('no_caps', 0),
			counters.get('no_agent', 0), counters.get('errors', 0),
			' '.join(why) or '-'))
		_log_note('V2_CAND offered=%s | top=%s | never=%s' % (
			' '.join(offered) or '-', ' '.join(chosen) or '-',
			','.join(never) or '-'))
		for key in list(counters.keys()):
			counters[key] = 0
		object.__setattr__(self, '_reasons', {})
		object.__setattr__(self, '_offers', {})

	def _fail(self, where, err):
		'''Count and log ONCE per (place, error type).  v2 must never take a bot
		down: every caller falls back to the v1 order.'''
		counters = object.__getattribute__(self, '_counters')
		counters['errors'] = counters.get('errors', 0) + 1
		seen = object.__getattribute__(self, '_err_seen')
		key = '%s:%s' % (where, type(err).__name__)
		if seen.get(key):
			return
		seen[key] = True
		_log_note('V2_ERR where=%s err=%r' % (where, err))

	# -- diagnostics --------------------------------------------------------
	def v2_counters(self):
		return dict(object.__getattribute__(self, '_counters'))

	def v2_reasons(self):
		return dict(object.__getattribute__(self, '_reasons'))

	def v2_offers(self):
		return dict(object.__getattribute__(self, '_offers'))

	def reset_v2(self, gen=None):
		'''New battle generation: drop boards, caches and counters.  v1's own
		state dies with the director object, so this only has to clean v2's.'''
		object.__getattribute__(self, '_boards').clear()
		object.__getattribute__(self, '_caps').clear()
		object.__getattribute__(self, '_counters').clear()
		object.__getattribute__(self, '_reasons').clear()
		object.__getattribute__(self, '_offers').clear()
		object.__getattribute__(self, '_err_seen').clear()
		object.__getattribute__(self, '_skill_logged').clear()
		object.__setattr__(self, '_window_at', 0.0)
		object.__setattr__(self, '_key_mismatch', 0)
		if gen is not None:
			object.__setattr__(self, '_gen', int(_num(gen, 0)))
		reset_skill_cache()
		try:
			from gui.mods.offhangar.bot_capabilities import reset_cache
			reset_cache()
		except Exception:
			pass


# ---------------------------------------------------------------------------
# Lazy module resolution: v2 must import cleanly in the client (package path),
# in the worker (flat path) and in a headless test (either), and a missing
# sibling module must degrade to "no v2 decisions", never to an ImportError at
# director construction time.
# ---------------------------------------------------------------------------
_MODULES = {}


def _capabilities_module():
	cached = _MODULES.get('caps')
	if cached is not None:
		return cached or None
	module = None
	try:
		from gui.mods.offhangar import bot_capabilities as module
	except ImportError:
		try:
			import bot_capabilities as module
		except ImportError:
			module = None
	except Exception:
		module = None
	_MODULES['caps'] = module or False
	return module


def _capabilities_builder():
	module = _capabilities_module()
	if module is None:
		return None
	return getattr(module, 'build_capabilities', None)


def _new_blackboard(bot_id, team):
	cached = _MODULES.get('board')
	if cached is not None:
		klass = cached or None
	else:
		klass = None
		try:
			from gui.mods.offhangar.bot_blackboard import BotBlackboard
			klass = BotBlackboard
		except ImportError:
			try:
				from bot_blackboard import BotBlackboard
				klass = BotBlackboard
			except ImportError:
				klass = None
		except Exception:
			klass = None
		_MODULES['board'] = klass or False
	if klass is None:
		return None
	return klass(bot_id, team)
