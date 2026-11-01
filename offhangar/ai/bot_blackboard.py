# -*- coding: utf-8 -*-
''' (bot_ai_v2): the bot's state as a SCHEMA instead of a flat dict.

Problem this replaces
---------------------
`BattleDirector` keeps one flat dict per bot, written from many places with no
declared fields: `register_profile` creates 19 keys (`bot_ai.py:1663-1684`),
`order_for` adds `position`/`health_fraction`/`hp_abs` (`:3441-3445`), the SPG
branch adds `artillery_home` (`:3476-3479`), the bait evaluator adds
`cyber_bait_*` (`:3141`), the radio layer adds `artillery_no_los_since` /
`artillery_help_at`, and callers outside the module poke it too
(`offline_battle.py:22163-22166` writes `agent['skill_tier']`).  Consequences
measured, not guessed:

* a key that nobody created yet reads back as a default, and the mod's other
  big trap - `_MockVeh.__getattr__` returning None - is the same failure mode on
  the vehicle side (HANDOFF_antigravity.md §0 п.6);
* `or 0.0` is needed everywhere to survive it, and `or` then eats a legitimate
  zero (the d97 defect 3, `_pi/outgoing/РЕЦЕНЗИЯ_d97.md`);
* a mode cannot remember anything, because there is nowhere to put it (the
  reason `punish_peek` and `bait` never fire - 10 modes never ran in the
  20:52 session);
* adding a mode risks colliding with somebody else's key.

This module declares every field once, with a type and a default, in five
groups (spec §3.2): `me`, `memory`, `squad`, `mission`, `skills`.  Unknown
attributes raise AttributeError instead of returning None, so a typo is a crash
in a headless test rather than a silent wrong decision in battle.

Nothing here decides anything and nothing here touches the engine: the
blackboard is data, `bot_ai_v2` makes the decisions, `bot_ai_driver` moves the
tank.

Field origins (v1 key -> blackboard field) are listed in `V1_AGENT_MAP` so the
migration can be checked mechanically instead of by eye.
'''

import math

# How long a contact that is no longer visible stays usable as "last known
# position".  v1 keeps the position forever and only ages it in the scorer
# (`bot_ai.py:2263-2266`: `max(4.0, 42.0 - age * 6.0)`), i.e. memory fades over
# ~7 s; 15 s here is the outer bound after which a contact is dropped, and the
# scorer keeps its own fade.
CONTACT_MEMORY_SECONDS = 15.0

# Reason counters are the measurable side of the fire gate: `V2_REFUSE why=...`
# in the log, counted by _pi/tools/analyze.py, so "the bot stayed silent" has a
# cause instead of a guess.
REFUSAL_REASONS = ('elevation', 'depression', 'traverse', 'traverse_time',
                   'no_target', 'no_solution', 'unknown')


def _num(value, default=0.0):
	'''float() that never raises; `default=None` comes back as None so a caller
	can tell "absent" from "zero".'''
	try:
		result = float(value)
	except (TypeError, ValueError):
		return None if default is None else float(default)
	if result != result or result in (float('inf'), float('-inf')):
		return None if default is None else float(default)
	return result


class SelfState(object):
	'''What the bot knows about ITSELF this tick.

	`pitch` and `roll` are None until somebody feeds them: `order_for` only
	receives `(position, hull_yaw)` (`bot_ai.py:3440`), so the suspension pose is
	genuinely unknown to the planner today.  Declaring that as None (and not as
	0.0) is the whole point - the fire gate widens its margin when the pose is
	unknown instead of pretending the hull is level.'''

	__slots__ = ('bot_id', 'team', 'position', 'yaw', 'pitch', 'roll',
	             'health', 'max_health', 'health_fraction', 'alive',
	             'capabilities', 'caps_key', 'skill_tier', 'personality',
	             'profile', 'seed', 'reload_time', 'reload_ready_at',
	             'shoot_timer', 'last_shot_at', 'shot_count', 'speed',
	             'now', 'tick', 'order_count')

	def __init__(self, bot_id=0, team=1):
		self.bot_id = int(bot_id)
		self.team = int(team)
		self.position = (0.0, 0.0, 0.0)
		self.yaw = 0.0
		self.pitch = None
		self.roll = None
		self.health = 1.0
		self.max_health = 1.0
		self.health_fraction = 1.0
		self.alive = True
		self.capabilities = None
		self.caps_key = ''
		self.skill_tier = 'medium'
		self.personality = None
		self.profile = None
		self.seed = 0
		self.reload_time = 4.0
		self.reload_ready_at = -1e18
		self.shoot_timer = 999.0
		self.last_shot_at = -999.0
		self.shot_count = 0
		self.speed = 0.0
		self.now = 0.0
		self.tick = 0
		self.order_count = 0

	def gun_position(self):
		'''Muzzle estimate from the hull position: `order_for` never receives the
		gun node, so the capabilities' muzzle height is the only honest source
		(the client chain hull.turretPositions + chassis.hullPosition +
		turret.gunPosition is folded into `capabilities.gun_height`).'''
		height = 0.0
		caps = self.capabilities
		if caps is not None:
			height = _num(getattr(caps, 'gun_height', 0.0), 0.0)
		return (self.position[0], self.position[1] + height, self.position[2])

	def reload_remaining(self, now=None):
		'''Seconds until the gun is ready, from the interface's own numbers.

		`shoot_timer` counts UP since the last shot (`offline_battle.py:24591`
		initialises it to 0, `:24668` does `+= dt`) and the gun is ready once it
		passes the reload time (`:24781`, `:24827`) - the same reading v1 makes
		in `reload_remaining = max(0.0, reload_time - shoot_timer)`
		(`bot_ai.py:3607`).  After v2 has watched a shot itself,
		`reload_ready_at` (absolute) is the more accurate source.'''
		moment = self.now if now is None else _num(now, self.now)
		if self.reload_ready_at > -1e17:
			return max(0.0, self.reload_ready_at - moment)
		return max(0.0, _num(self.reload_time, 4.0) - _num(self.shoot_timer, 0.0))

	def loaded(self, now=None):
		'''True when the gun is ready to fire again.'''
		return self.reload_remaining(now) <= 0.0

	def note_shot(self, now, reload_time=None):
		self.last_shot_at = _num(now, self.last_shot_at)
		self.shot_count += 1
		if reload_time is not None:
			self.reload_time = max(0.1, _num(reload_time, self.reload_time))
		self.reload_ready_at = self.last_shot_at + self.reload_time


class ContactMemory(object):
	'''One remembered enemy/ally.

	Field names mirror the v1 contact dict (`bot_ai.update_contact`,
	`bot_ai.py:2100-2126`) so nothing downstream has to be relearned; `age`,
	`distance` and `shots_at_me` are the additions a tactics layer needs.'''

	__slots__ = ('id', 'team', 'position', 'health', 'max_health', 'class_tag',
	             'armor', 'speed', 'visible', 'last_seen', 'shootable_by_ids',
	             'age', 'distance', 'shots_at_me', 'last_shot_at', 'first_seen')

	def __init__(self, contact_id=0, team=2):
		self.id = int(contact_id)
		self.team = int(team)
		self.position = (0.0, 0.0, 0.0)
		self.health = 1.0
		self.max_health = 1.0
		self.class_tag = 'mediumTank'
		self.armor = 0.0
		self.speed = 0.0
		self.visible = False
		self.last_seen = -1e18
		self.first_seen = -1e18
		self.shootable_by_ids = ()
		self.age = 1e18
		self.distance = 1e18
		self.shots_at_me = 0
		self.last_shot_at = -1e18

	def health_fraction(self):
		total = max(1.0, _num(self.max_health, 1.0))
		return max(0.0, _num(self.health, 0.0)) / total

	def adopt(self, contact, now, own_position=None):
		'''Copy a v1 contact dict.  When the contact went dark v1 keeps the last
		position and only flips `visible` (`bot_ai.py:2123-2126`); this does the
		same and refreshes `age`/`distance` from `now`.'''
		if not isinstance(contact, dict):
			return self
		self.id = int(contact.get('id', self.id))
		self.team = int(contact.get('team', self.team))
		position = contact.get('position')
		if position is not None:
			self.position = (_num(position[0], 0.0), _num(position[1], 0.0),
			                 _num(position[2], 0.0))
		self.health = max(0.0, _num(contact.get('health'), 1.0))
		self.max_health = max(1.0, _num(contact.get('max_health'), 1.0))
		self.class_tag = str(contact.get('class_tag') or 'mediumTank')
		self.armor = max(0.0, _num(contact.get('armor'), 0.0))
		self.speed = max(0.0, _num(contact.get('speed'), 0.0))
		self.visible = bool(contact.get('visible'))
		ids = contact.get('shootable_by_ids')
		self.shootable_by_ids = tuple(ids) if ids else ()
		moment = _num(now, 0.0)
		if self.first_seen <= -1e17:
			self.first_seen = moment
		if self.visible:
			self.last_seen = _num(contact.get('last_seen'), moment)
		self.age = max(0.0, moment - self.last_seen)
		if own_position is not None:
			delta_x = self.position[0] - _num(own_position[0], 0.0)
			delta_z = self.position[2] - _num(own_position[2], 0.0)
			self.distance = math.sqrt(delta_x * delta_x + delta_z * delta_z)
		return self


class SquadState(object):
	'''Platoon facts.  v1 stores `squad_id`/`squad_role` on the agent and keeps
	the rest on the director (`bot_ai._assign_squad` :1850, `_squad_for` :1853,
	`_squad_force_balance` :1874); the blackboard caches the per-bot view.'''

	__slots__ = ('squad_id', 'role', 'leader_id', 'member_ids', 'centre',
	             'force_balance', 'help_requested_at', 'cohesion_on')

	def __init__(self):
		self.squad_id = None
		self.role = None
		self.leader_id = None
		self.member_ids = ()
		self.centre = None
		self.force_balance = 0.0
		self.help_requested_at = -1e18
		self.cohesion_on = False


class MissionState(object):
	'''What the bot is trying to do, with the deadlines that make a mode able to
	remember anything at all (the missing piece behind the 10 dead modes).'''

	__slots__ = ('intent', 'intent_since', 'intent_until', 'target_id',
	             'target_since', 'aim_point', 'move_point', 'corridor',
	             'route_id', 'route_index', 'route_anchor', 'no_los_since',
	             'quiet_since', 'relocate_after', 'deadline', 'last_mode',
	             'mode_since', 'artillery_home', 'artillery_no_los_since',
	             'artillery_help_at', 'route_started', 'route_completed',
	             'route_switch_at', 'route_join_anchor', 'route_join_index')

	def __init__(self):
		self.intent = 'advance'
		self.intent_since = 0.0
		self.intent_until = 0.0
		self.target_id = None
		self.target_since = 0.0
		self.aim_point = None
		self.move_point = None
		self.corridor = None
		self.route_id = 'direct'
		self.route_index = 0
		self.route_anchor = None
		self.no_los_since = -1e18
		self.quiet_since = -1e18
		self.relocate_after = -1e18
		self.deadline = 0.0
		self.last_mode = 'route'
		self.mode_since = 0.0
		self.artillery_home = None
		self.artillery_no_los_since = None
		self.artillery_help_at = None
		# Route lifecycle.  v1 keeps these on the agent dict from three different
		# methods (bot_ai.py:2423-2425, :2519, :3315-3316), which is how a route
		# switch could silently reset somebody else's field.
		self.route_started = False
		self.route_completed = False
		self.route_switch_at = 0.0
		self.route_join_anchor = None
		self.route_join_index = 0

	def set_intent(self, intent, now, hold=0.0):
		'''Change intent with hysteresis: the same intent keeps its start time, a
		new one gets `hold` seconds of grace before another switch is allowed.
		This is what replaces v1's +22 stickiness bonus (`bot_ai.py:2318`) with a
		timer that can be measured.'''
		intent = str(intent or 'advance')
		if intent == self.intent:
			self.intent_until = _num(now, 0.0) + max(0.0, _num(hold, 0.0))
			return False
		self.intent = intent
		self.intent_since = _num(now, 0.0)
		self.intent_until = self.intent_since + max(0.0, _num(hold, 0.0))
		return True

	def locked(self, now):
		'''True while an intent switch is still inside its hold window.'''
		return _num(now, 0.0) < self.intent_until

	def set_target(self, target_id, now):
		if target_id is None:
			self.target_id = None
			self.target_since = 0.0
			return False
		target_id = int(target_id)
		if target_id == self.target_id:
			return False
		self.target_id = target_id
		self.target_since = _num(now, 0.0)
		return True

	def set_mode(self, mode, now):
		mode = str(mode or 'route')
		if mode != self.last_mode:
			self.last_mode = mode
			self.mode_since = _num(now, 0.0)
		return mode


class SkillsState(object):
	'''Per-bot execution memory: gun angles, bait timers, hazard recovery.

	Mirrors the v1 keys `cyber_bait_state`/`cyber_bait_timer`/
	`cyber_bait_target_id`/`cyber_bait_start_pos`/`cyber_bait_cooldown_until`
	(`bot_ai.py:1670-1674`, evaluator `:3141`) so the behaviours that already
	work keep working, but with declared fields.'''

	__slots__ = ('turret_yaw', 'gun_pitch', 'slewing', 'bait_state',
	             'bait_until', 'bait_cooldown_until', 'bait_target_id',
	             'bait_start_pos', 'hazard_mode', 'hazard_until', 'stuck_since',
	             'refusals', 'refusal_total', 'last_refusal_at', 'last_refusal',
	             'callout_attack_at', 'support_call_at')

	def __init__(self):
		self.turret_yaw = 0.0
		self.gun_pitch = 0.0
		self.slewing = False
		self.bait_state = 'idle'
		self.bait_until = 0.0
		self.bait_cooldown_until = 0.0
		self.bait_target_id = None
		self.bait_start_pos = None
		self.hazard_mode = ''
		self.hazard_until = 0.0
		self.stuck_since = -1e18
		self.refusals = dict([(reason, 0) for reason in REFUSAL_REASONS])
		self.refusal_total = 0
		self.last_refusal_at = -1e18
		self.last_refusal = ''
		# Communication cooldowns: v1 stores them as `co_attack_at`
		# (bot_ai.py:2378, the 'Attacking X!' callout) and `last_support_call`
		# (:3562, 's soft-radio support call).
		self.callout_attack_at = -1e18
		self.support_call_at = -1e18

	def note_refusal(self, reason, now):
		'''Count a refused shot.  Counters only - the gate itself lives in
		`bot_capabilities.fire_gate`, and the instruments that verify it
		(GUNPROBE / FBSOL / `clamp=max`) stay independent.'''
		reason = str(reason or 'unknown')
		if reason not in self.refusals:
			reason = 'unknown'
		self.refusals[reason] = self.refusals.get(reason, 0) + 1
		self.refusal_total += 1
		self.last_refusal = reason
		self.last_refusal_at = _num(now, 0.0)
		return reason

	def refusal_summary(self):
		parts = []
		for reason in REFUSAL_REASONS:
			count = int(self.refusals.get(reason, 0))
			if count:
				parts.append('%s=%d' % (reason, count))
		return ' '.join(parts) if parts else 'none'


class BotBlackboard(object):
	'''The whole per-bot world model: five declared groups, no string keys.

	`meta` carries bookkeeping that must not be confused with tactics
	(generation, source of the order, whether the v1 fallback was used).'''

	__slots__ = ('me', 'memory', 'squad', 'mission', 'skills', 'meta')

	def __init__(self, bot_id=0, team=1):
		self.me = SelfState(bot_id, team)
		self.memory = {}
		self.squad = SquadState()
		self.mission = MissionState()
		self.skills = SkillsState()
		self.meta = {'gen': 0, 'order_source': 'local', 'v1_fallback': 0,
		             'v2_orders': 0, 'created_at': 0.0, 'caps_built_at': 0.0}

	# -- construction -------------------------------------------------------
	@staticmethod
	def for_bot(bot_id, team, capabilities=None, caps_key=''):
		board = BotBlackboard(bot_id, team)
		board.me.capabilities = capabilities
		board.me.caps_key = str(caps_key or '')
		return board

	# -- per-tick input -----------------------------------------------------
	def update_self(self, position, hull_yaw, health, max_health, now,
			reload_time=None, shoot_timer=None):
		'''Fill `me` from the ONLY self-data `order_for` receives
		(`bot_ai.py:3440`).  Everything the interface does not carry stays at its
		declared default - notably `pitch`/`roll` = None.'''
		me = self.me
		if position is not None:
			me.position = (_num(position[0], 0.0), _num(position[1], 0.0),
			               _num(position[2], 0.0))
		me.yaw = _num(hull_yaw, 0.0)
		me.health = _num(health, 1.0)
		me.max_health = max(1.0, _num(max_health, 1.0))
		me.health_fraction = me.health / me.max_health
		me.alive = me.health > 0.0
		me.now = _num(now, 0.0)
		me.tick += 1
		me.order_count += 1
		if reload_time is not None:
			me.reload_time = max(0.1, _num(reload_time, me.reload_time))
		if shoot_timer is not None:
			me.shoot_timer = _num(shoot_timer, me.shoot_timer)
		return me

	def sync_from_agent(self, agent, now=0.0):
		'''Pull the v1 flat agent dict into the schema (migration adapter).

		Only keys that exist are copied; a missing key leaves the declared
		default, which is exactly the difference from `getattr(agent, 'x', None)`
		scattered over the code.  Returns the number of fields copied, so a test
		can assert the map did not silently rot.'''
		if not isinstance(agent, dict):
			return 0
		copied = 0
		moment = _num(now, 0.0)
		for v1_key, group, field in V1_AGENT_MAP:
			if v1_key not in agent:
				continue
			value = agent[v1_key]
			target = getattr(self, group, None)
			if target is None:
				continue
			try:
				setattr(target, field, value)
				copied += 1
			except AttributeError:
				# A field that left the schema: fail loudly in tests, stay alive
				# in battle.
				continue
		if 'skill_tier' in agent:
			self.me.skill_tier = str(agent.get('skill_tier') or 'medium').lower()
			copied += 1
		if 'profile' in agent:
			self.me.profile = agent.get('profile')
			copied += 1
		if 'personality' in agent:
			self.me.personality = agent.get('personality')
			copied += 1
		if 'seed' in agent:
			self.me.seed = int(_num(agent.get('seed'), 0))
			copied += 1
		self.me.now = moment
		return copied

	# -- contacts -----------------------------------------------------------
	def remember(self, contact, now=None):
		'''Adopt one v1 contact dict into `memory`, keyed by target id.'''
		if not isinstance(contact, dict):
			return None
		contact_id = int(contact.get('id', 0))
		if _num(contact.get('health'), 1.0) <= 0.0:
			self.memory.pop(contact_id, None)
			return None
		moment = self.me.now if now is None else _num(now, self.me.now)
		entry = self.memory.get(contact_id)
		if entry is None:
			entry = ContactMemory(contact_id, int(contact.get('team', 2)))
			self.memory[contact_id] = entry
		entry.adopt(contact, moment, self.me.position)
		return entry

	def remember_all(self, contacts, now=None):
		count = 0
		for contact in (contacts or ()):
			if self.remember(contact, now) is not None:
				count += 1
		return count

	def contact(self, target_id):
		try:
			return self.memory.get(int(target_id))
		except (TypeError, ValueError):
			return None

	def visible(self, now=None, team=None):
		'''Contacts currently visible, nearest first.'''
		moment = self.me.now if now is None else _num(now, self.me.now)
		result = []
		for entry in self.memory.values():
			if not entry.visible:
				continue
			if team is not None and int(entry.team) != int(team):
				continue
			entry.age = max(0.0, moment - entry.last_seen)
			result.append(entry)
		result.sort(key=lambda item: item.distance)
		return result

	def remembered(self, now=None, max_age=None, team=None):
		'''Everything still in memory, visible or not, oldest last-known first.'''
		moment = self.me.now if now is None else _num(now, self.me.now)
		limit = (CONTACT_MEMORY_SECONDS if max_age is None
		         else max(0.0, _num(max_age, CONTACT_MEMORY_SECONDS)))
		result = []
		for entry in self.memory.values():
			if team is not None and int(entry.team) != int(team):
				continue
			entry.age = max(0.0, moment - entry.last_seen)
			if entry.age > limit:
				continue
			result.append(entry)
		result.sort(key=lambda item: item.age)
		return result

	def forget_stale(self, now=None, max_age=None):
		'''Drop contacts older than the memory horizon.  Returns how many went.'''
		moment = self.me.now if now is None else _num(now, self.me.now)
		limit = (CONTACT_MEMORY_SECONDS if max_age is None
		         else max(0.0, _num(max_age, CONTACT_MEMORY_SECONDS)))
		stale = []
		for contact_id, entry in self.memory.items():
			if not entry.visible and (moment - entry.last_seen) > limit:
				stale.append(contact_id)
		for contact_id in stale:
			del self.memory[contact_id]
		return len(stale)

	def note_incoming(self, shooter_id, now=None):
		'''Somebody shot at me - the memory a `punish_peek`/`bait` candidate
		needs and v1 has nowhere to put.'''
		moment = self.me.now if now is None else _num(now, self.me.now)
		try:
			entry = self.memory.get(int(shooter_id))
		except (TypeError, ValueError):
			entry = None
		if entry is None:
			entry = ContactMemory(int(shooter_id), 2)
			self.memory[int(shooter_id)] = entry
		entry.shots_at_me += 1
		entry.last_shot_at = moment
		return entry

	# -- export -------------------------------------------------------------
	def snapshot(self):
		'''Plain-types copy for a log line or a network publish.  Deliberately
		shallow and value-only: no formula, so an instrument reading it cannot
		end up recomputing what the code computes.'''
		me = self.me
		return {
			'id': int(me.bot_id), 'team': int(me.team),
			'pos': (round(me.position[0], 3), round(me.position[1], 3),
			        round(me.position[2], 3)),
			'yaw': round(me.yaw, 4),
			'pitch': None if me.pitch is None else round(_num(me.pitch), 4),
			'roll': None if me.roll is None else round(_num(me.roll), 4),
			'hp': round(me.health_fraction, 3), 'alive': bool(me.alive),
			'tier': str(me.skill_tier), 'caps': str(me.caps_key),
			'contacts': len(self.memory),
			'visible': len([c for c in self.memory.values() if c.visible]),
			'intent': str(self.mission.intent),
			'mode': str(self.mission.last_mode),
			'target': self.mission.target_id,
			'refusals': self.skills.refusal_summary(),
			'squad': self.squad.squad_id, 'role': self.squad.role,
			'gen': int(self.meta.get('gen', 0)),
			'src': str(self.meta.get('order_source', 'local')),
			'v1_fallback': int(self.meta.get('v1_fallback', 0)),
			'v2_orders': int(self.meta.get('v2_orders', 0)),
		}


# v1 flat agent key -> (blackboard group, field).  Checked by
# _pi/v2/test_d100_blackboard.py against the real `register_profile` body, so a
# renamed v1 key fails a test instead of silently reading a default.
V1_AGENT_MAP = (
	('id', 'me', 'bot_id'),
	('team', 'me', 'team'),
	('position', 'me', 'position'),
	('health_fraction', 'me', 'health_fraction'),
	('hp_abs', 'me', 'health'),
	('skill_tier', 'me', 'skill_tier'),
	('last_shot_at', 'me', 'last_shot_at'),
	('shot_count', 'me', 'shot_count'),
	('squad_id', 'squad', 'squad_id'),
	('squad_role', 'squad', 'role'),
	('target_id', 'mission', 'target_id'),
	('waypoint_index', 'mission', 'route_index'),
	('artillery_home', 'mission', 'artillery_home'),
	('artillery_no_los_since', 'mission', 'artillery_no_los_since'),
	('artillery_help_at', 'mission', 'artillery_help_at'),
	('cyber_bait_state', 'skills', 'bait_state'),
	('cyber_bait_timer', 'skills', 'bait_until'),
	('cyber_bait_target_id', 'skills', 'bait_target_id'),
	('cyber_bait_start_pos', 'skills', 'bait_start_pos'),
	('cyber_bait_cooldown_until', 'skills', 'bait_cooldown_until'),
	('route_started', 'mission', 'route_started'),
	('route_completed', 'mission', 'route_completed'),
	('route_switch_at', 'mission', 'route_switch_at'),
	('route_join_anchor', 'mission', 'route_join_anchor'),
	('route_join_index', 'mission', 'route_join_index'),
	('co_attack_at', 'skills', 'callout_attack_at'),
	('last_support_call', 'skills', 'support_call_at'),
)

# v1 agent keys that are memo caches, not tactics state, and are therefore NOT
# migrated - they die with the v1 director and rebuilding them is cheap:
#   '_rc_cache'    reload-cover memo, 2 s (bot_ai.py:2828)
#   '_arty_pb_log' artillery point-blank log throttle (bot_ai.py:2686)
# test_d100_v2.py reads this tuple, so adding a v1 cache key without declaring
# it here fails the test instead of hiding.
CACHE_KEYS = ('_rc_cache', '_arty_pb_log')

# Groups and their slot tuples, exported so a test can walk the whole schema.
GROUPS = (('me', SelfState), ('memory', None), ('squad', SquadState),
          ('mission', MissionState), ('skills', SkillsState))


def schema_fields():
	'''[(group, field)] for every declared slot - the "no magic defaults" check.'''
	fields = []
	for group, klass in GROUPS:
		if klass is None:
			fields.append((group, '<ContactMemory>'))
			for field in ContactMemory.__slots__:
				fields.append((group, field))
			continue
		for field in klass.__slots__:
			fields.append((group, field))
	return fields
