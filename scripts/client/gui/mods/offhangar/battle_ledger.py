# -*- coding: utf-8 -*-
"""Who did what to whom, for one battle.

The post-battle screen is built from a per-PAIR record: for every
(shooter, victim) the client wants the nine numbers in
VEH_INTERACTION_DETAILS, and every column of the team table is a sum over
those pairs. The offline build used to keep three loose counters instead
(damage_from_player / hits_from_player / damage_from_bots, all stored on
the VICTIM), which cannot answer "what did bot 12 deal" at all - so the
results screen showed each bot the damage it had RECEIVED.

This module only WRITES the record. It never applies damage, never
touches health, and nothing in the battle reads it back: the callers are
the existing damage sites, one line each, and the only consumer runs
after the battle is over. Cost is per hit event, not per frame.

Names and semantics follow scripts/common/battle_results_shared.py, so
the output drops straight into the results dicts the client parses.
"""

# scripts/common/battle_results_shared.py, VEH_INTERACTION_DETAILS
DETAIL_FIELDS = ('spotted', 'killed', 'hits', 'he_hits', 'pierced',
                 'damageDealt', 'damageAssisted', 'crits', 'fire')

# arena.onVehicleKilled reason codes, as the mod already passes them.
KILL_SHOT = 0
KILL_FIRE = 2
KILL_RAM = 3
KILL_DROWN = 5
# Deaths nobody gets credit for. A drowned tank is not a frag, and the
# real game does not hand one out either.
_UNCREDITED = (KILL_DROWN,)


def _new_detail():
	return dict((name, 0) for name in DETAIL_FIELDS)


class BattleLedger(object):
	"""One battle's record. Created per battle, or reset()."""

	def __init__(self):
		self.reset()

	def reset(self):
		self._pairs = {}        # (shooterID, victimID) -> detail dict
		self._shots = {}        # shooterID -> rounds actually fired
		self._mileage = {}      # vehicleID -> metres driven
		self._capture = {}      # vehicleID -> [gained, dropped]
		self._killed_by = {}    # victimID -> (killerID, reason)
		self._spotted_by = {}   # victimID -> first observer that revealed it
		self._rounds = {}       # shooterID -> {shellIndex: rounds fired}

	# -- writing ---------------------------------------------------------

	def _pair(self, shooter, victim):
		key = (shooter, victim)
		detail = self._pairs.get(key)
		if detail is None:
			detail = _new_detail()
			self._pairs[key] = detail
		return detail

	def note_shot(self, shooter, shellIndex=None):
		"""One round left the barrel. Counted even when it misses:
		'shots' is the denominator the sniper medal divides by.

		shellIndex, when given, is the slot on the gun that was fired, so
		the resupply bill can charge each shell type at its own price -
		a magazine of gold rounds does not cost what an AP load costs.
		"""
		if shooter is None or shooter < 0:
			return
		self._shots[shooter] = self._shots.get(shooter, 0) + 1
		if shellIndex is not None:
			perType = self._rounds.get(shooter)
			if perType is None:
				perType = {}
				self._rounds[shooter] = perType
			perType[shellIndex] = perType.get(shellIndex, 0) + 1

	def note_hit(self, shooter, victim, damage=0, pierced=False, he=False,
	             crits=0, fire=False, assisted=0):
		"""One shell (or blast) landed on victim.

		damage is the HP that actually came off, so a hit on a tank with
		40 HP left counts 40 and not the shell's nominal roll - that is
		what the results screen shows and what medals are judged on.
		"""
		if shooter is None or victim is None or shooter < 0 or victim < 0:
			return
		if shooter == victim:
			return
		detail = self._pair(shooter, victim)
		detail['hits'] += 1
		if pierced:
			detail['pierced'] += 1
		if he:
			detail['he_hits'] += 1
		if crits:
			detail['crits'] += int(crits)
		if fire:
			detail['fire'] += 1
		if damage > 0:
			detail['damageDealt'] += int(damage)
		if assisted > 0:
			detail['damageAssisted'] += int(assisted)

	def note_damage(self, shooter, victim, damage, fire=False, crits=0):
		"""Damage with no shell behind it - a fire tick, a ram, a fall.
		Adds to damageDealt WITHOUT counting a hit, so accuracy stays
		honest; the real game reports kills with hits=0 for exactly this.
		"""
		if shooter is None or victim is None or shooter < 0 or victim < 0:
			return
		if shooter == victim or damage <= 0:
			return
		detail = self._pair(shooter, victim)
		detail['damageDealt'] += int(damage)
		if fire:
			detail['fire'] += 1
		if crits:
			detail['crits'] += int(crits)

	def note_kill(self, victim, killer, reason=KILL_SHOT):
		"""The frag goes to whoever landed the killing blow, NOT to
		whoever dealt the most damage over the battle. First call wins: a
		tank dies once, and the mod's cleanup passes re-fire the event.
		"""
		if victim is None or victim < 0 or victim in self._killed_by:
			return
		if reason in _UNCREDITED or killer is None or killer < 0 or killer == victim:
			self._killed_by[victim] = (0, reason)
			return
		self._killed_by[victim] = (killer, reason)
		self._pair(killer, victim)['killed'] = 1

	def note_spot(self, observer, victim):
		"""observer was the FIRST to reveal victim. Only the first counts:
		that is the one the scout medal and assist credit use."""
		if observer is None or victim is None or observer < 0 or victim < 0:
			return
		if observer == victim or victim in self._spotted_by:
			return
		self._spotted_by[victim] = observer
		self._pair(observer, victim)['spotted'] = 1

	def note_mileage(self, vehicleID, metres):
		if vehicleID is None or vehicleID < 0 or metres <= 0:
			return
		self._mileage[vehicleID] = self._mileage.get(vehicleID, 0.0) + float(metres)

	def note_capture(self, vehicleID, gained=0, dropped=0):
		if vehicleID is None or vehicleID < 0:
			return
		slot = self._capture.get(vehicleID)
		if slot is None:
			slot = [0, 0]
			self._capture[vehicleID] = slot
		slot[0] += int(gained)
		slot[1] += int(dropped)

	# -- reading ---------------------------------------------------------

	def killer_of(self, victimID):
		"""vehicleID that gets the frag, or 0 for alive / uncredited."""
		return self._killed_by.get(victimID, (0, None))[0]

	def is_dead(self, victimID):
		return victimID in self._killed_by

	def rounds_for(self, shooterID):
		"""{shellIndex: rounds fired}, for the resupply bill."""
		return dict(self._rounds.get(shooterID, {}))

	def spotter_of(self, victimID):
		return self._spotted_by.get(victimID, 0)

	def details_for(self, shooterID):
		"""{victimID: detail dict} - the 'details' field of a results row,
		and the source of the personal efficiency list."""
		out = {}
		for key, detail in self._pairs.iteritems():
			if key[0] == shooterID:
				out[key[1]] = dict(detail)
		return out

	def totals_for(self, vehicleID, allies=()):
		"""Every table column this ledger can answer for one vehicle.

		allies are the vehicleIDs on the same team; hits against them are
		reported separately as tdamageDealt / tkills, which the client
		prints in red, instead of being folded into the normal totals.
		"""
		allies = set(allies)
		out = {'shots': self._shots.get(vehicleID, 0),
		       'hits': 0, 'he_hits': 0, 'pierced': 0,
		       'damageDealt': 0, 'damageAssisted': 0,
		       'spotted': 0, 'damaged': 0, 'kills': 0,
		       'tdamageDealt': 0, 'tkills': 0,
		       'damageReceived': 0, 'shotsReceived': 0}
		for key, detail in self._pairs.iteritems():
			shooter, victim = key
			if shooter == vehicleID:
				out['spotted'] += detail['spotted']
				if victim in allies:
					out['tdamageDealt'] += detail['damageDealt']
					out['tkills'] += detail['killed']
				else:
					out['hits'] += detail['hits']
					out['he_hits'] += detail['he_hits']
					out['pierced'] += detail['pierced']
					out['damageDealt'] += detail['damageDealt']
					out['damageAssisted'] += detail['damageAssisted']
					out['kills'] += detail['killed']
					if detail['damageDealt'] > 0:
						out['damaged'] += 1
			elif victim == vehicleID:
				out['damageReceived'] += detail['damageDealt']
				out['shotsReceived'] += detail['hits']
		out['isTeamKiller'] = out['tkills'] > 0
		cap = self._capture.get(vehicleID) or (0, 0)
		out['capturePoints'] = cap[0]
		out['droppedCapturePoints'] = cap[1]
		# The client renders mileage as km with one decimal, from metres.
		out['mileage'] = int(self._mileage.get(vehicleID, 0.0))
		# A firing path that forgets to report must not make the table
		# claim fewer shots than there were landed hits.
		landed = out['hits'] + out['tkills']
		if out['shots'] < landed:
			out['shots'] = landed
		return out

	def summary(self):
		"""One-line census for the log."""
		return 'pairs=%d shooters=%d kills=%d spotted=%d' % (
			len(self._pairs), len(self._shots), len(self._killed_by),
			len(self._spotted_by))


_ledger = [None]


def get():
	"""The current battle's ledger, created on first use."""
	if _ledger[0] is None:
		_ledger[0] = BattleLedger()
	return _ledger[0]


def reset():
	"""Start a fresh battle. Safe to call when none exists yet."""
	if _ledger[0] is None:
		_ledger[0] = BattleLedger()
	else:
		_ledger[0].reset()
	return _ledger[0]
