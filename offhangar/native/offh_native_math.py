# -*- coding: utf-8 -*-
"""Optional native math helper (offh_math.dll) for the offline battle mod.

Why this module exists
----------------------
The 0.8.2 client embeds a statically linked CPython 2.6.4 which exports no
``Py_*`` symbols, so no off-the-shelf accelerator (PyPy, Cython, numpy,
greenlet) can ever be loaded into it.  Two levers remain: separate processes,
and our own C code reached through ``ctypes``.  A ctypes call releases the GIL
for its whole duration, which means the DLL may run its own threads and those
threads really do execute in parallel with the client.

This module is that lever, and it is strictly optional:

* it never patches ``WorldOfTanks.exe`` and never injects into a process;
* it loads a DLL that the mod itself ships, from the mod's own folder;
* every entry point is wrapped so that any failure - missing file, wrong
  version, 64-bit/32-bit mismatch, load error - degrades to the pure Python
  path with a single logged line;
* it touches no BigWorld object.  The DLL receives flat numeric buffers and
  returns flat numeric buffers, so no engine object is ever seen from a
  native thread (engine objects are single-threaded by design).

Config flags (``offhangar_user/config.json``, defaults in
``config_defaults.json``):

``native_math_selftest``
	Run the one-shot core/GIL self-test at the first battle frame and log
	``NATIVE_MATH selftest ...``.  Measures, does not change behaviour.
``native_math``
	Use the DLL for the spotting candidate pre-filter, with a per-frame
	parity check against the Python result.  Any mismatch disables the
	native path for the rest of the battle and logs it.

Both default to ``false``: the mod behaves exactly as before unless asked.
"""

import os

try:
	import ctypes
	_CTYPES_ERROR = None
except ImportError as _ctypes_error:
	# The 0.8.2 client embeds a stripped CPython 2.6.4: ctypes/__init__.py is
	# there, the _ctypes C extension is not, and a stock _ctypes.pyd cannot be
	# dropped in either because it would import python26.dll while the exe
	# exports no Python symbols at all.  Verified in game on 01.10:
	# 'NATIVE_MATH init failed: No module named _ctypes'.
	ctypes = None
	_CTYPES_ERROR = str(_ctypes_error)

from gui.mods.offhangar.logging import LOG_ERROR, LOG_NOTE

EXPECTED_VERSION = 1
_MAX_ENTITIES_FALLBACK = 256
_MAX_CANDIDATES_FALLBACK = 64
PROXIMITY_SPOT_DISTANCE = 50.0
MAX_FOLIAGE_BONUS = 0.60

FLAG_SKIP = 0
FLAG_CANDIDATE = 1
FLAG_NEEDS_QUERY = 2

# None = not attempted yet, False = attempted and failed, else the ctypes lib.
_STATE = [None]
_INFO = [None]
_SELFTEST_DONE = [False]
_DISABLED = [False]


def _candidate_paths():
	"""Search order for the DLL: env override, tools/native, module folder."""
	paths = []
	env_path = os.environ.get('OFFHANGAR_NATIVE_MATH_DLL')
	if env_path:
		paths.append(env_path)
	here = os.path.dirname(os.path.abspath(__file__))
	offh_root = os.path.dirname(here) if os.path.basename(here) == 'native' else here
	# .../res_mods/0.8.2/scripts/client/gui/mods/offhangar -> .../0.8.2
	pack_root = os.path.normpath(os.path.join(offh_root, os.pardir, os.pardir,
		os.pardir, os.pardir, os.pardir))
	paths.append(os.path.join(pack_root, 'tools', 'native', 'offh_math.dll'))
	paths.append(os.path.join(offh_root, 'offh_math.dll'))
	paths.append(os.path.join(here, 'offh_math.dll'))
	return paths


def _bind(lib):
	"""Declare prototypes; raises when a required entry point is missing."""
	for name in ('offh_math_version', 'offh_max_entities', 'offh_max_candidates',
			'offh_cpu_count'):
		if not hasattr(lib, name):
			raise RuntimeError('offh_math has no %s' % name)
	lib.offh_math_version.restype = ctypes.c_int
	lib.offh_math_version.argtypes = []
	lib.offh_max_entities.restype = ctypes.c_int
	lib.offh_max_entities.argtypes = []
	lib.offh_max_candidates.restype = ctypes.c_int
	lib.offh_max_candidates.argtypes = []
	lib.offh_cpu_count.restype = ctypes.c_int
	lib.offh_cpu_count.argtypes = []
	if hasattr(lib, 'offh_affinity_mask'):
		lib.offh_affinity_mask.restype = ctypes.c_ulonglong
		lib.offh_affinity_mask.argtypes = []
	if hasattr(lib, 'offh_selftest_serial'):
		lib.offh_selftest_serial.restype = ctypes.c_double
		lib.offh_selftest_serial.argtypes = [ctypes.c_int]
	if hasattr(lib, 'offh_selftest_parallel'):
		lib.offh_selftest_parallel.restype = ctypes.c_double
		lib.offh_selftest_parallel.argtypes = [ctypes.c_int, ctypes.c_int]
	for name in ('offh_contact_candidates', 'offh_contact_candidates_mt'):
		if not hasattr(lib, name):
			raise RuntimeError('offh_math has no %s' % name)
	lib.offh_contact_candidates.restype = ctypes.c_int
	lib.offh_contact_candidates.argtypes = [
		ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_sbyte),
		ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_float),
		ctypes.c_int, ctypes.c_int, ctypes.c_float, ctypes.c_float,
		ctypes.c_float, ctypes.POINTER(ctypes.c_int),
		ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_ubyte)]
	lib.offh_contact_candidates_mt.restype = ctypes.c_int
	lib.offh_contact_candidates_mt.argtypes = [
		ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_sbyte),
		ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_float),
		ctypes.c_int, ctypes.POINTER(ctypes.c_int),
		ctypes.POINTER(ctypes.c_float), ctypes.c_int, ctypes.c_int,
		ctypes.c_float, ctypes.c_float, ctypes.POINTER(ctypes.c_int),
		ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_ubyte),
		ctypes.POINTER(ctypes.c_int)]
	return lib


def load(quiet=False):
	"""Return the bound library, or None after one logged failure."""
	if ctypes is None:
		if not _DISABLED[0]:
			_DISABLED[0] = True
			if not quiet:
				LOG_NOTE('NATIVE_MATH unavailable: this client has no _ctypes '
					'(%s); python fallback, nothing to do' % _CTYPES_ERROR)
		return None
	if _DISABLED[0]:
		return None
	if _STATE[0] is not None:
		return _STATE[0] or None
	_STATE[0] = False
	tried = []
	for path in _candidate_paths():
		if not path or not os.path.isfile(path):
			tried.append('%s (absent)' % path)
			continue
		try:
			lib = _bind(ctypes.CDLL(path))
		except Exception as error:
			tried.append('%s (load failed: %s)' % (path, error))
			continue
		try:
			version = int(lib.offh_math_version())
		except Exception as error:
			tried.append('%s (version call failed: %s)' % (path, error))
			continue
		if version != EXPECTED_VERSION:
			tried.append('%s (version %d, expected %d)' % (
				path, version, EXPECTED_VERSION))
			continue
		_STATE[0] = lib
		_INFO[0] = {
			'path': path,
			'version': version,
			'max_entities': int(lib.offh_max_entities()),
			'max_candidates': int(lib.offh_max_candidates()),
		}
		LOG_NOTE('NATIVE_MATH loaded version=%d entities<=%d candidates<=%d '
			'from %s' % (version, _INFO[0]['max_entities'],
				_INFO[0]['max_candidates'], path))
		return lib
	if not quiet:
		LOG_ERROR('NATIVE_MATH unavailable, python fallback: %s' % (
			'; '.join(tried) or 'no candidate path'))
	return None


def disable(reason):
	"""Permanently fall back to Python for this session (logged once)."""
	if _DISABLED[0]:
		return
	_DISABLED[0] = True
	_STATE[0] = False
	LOG_ERROR('NATIVE_MATH disabled (%s), python fallback for the rest of the '
		'session' % reason)


def _popcount(value):
	count = 0
	while value:
		count += int(value & 1)
		value >>= 1
	return count


def selftest(iterations=200000, threads=None):
	"""Measure native scaling inside the live client and log one block.

	Read-only: it computes nothing the game uses.  Returns a dict for tests.
	"""
	lib = load()
	if lib is None:
		return None
	if not hasattr(lib, 'offh_selftest_serial'):
		LOG_NOTE('NATIVE_MATH selftest skipped: dll has no selftest entry '
			'(older build)')
		return None
	if threads is None:
		threads = []
		try:
			cores = int(lib.offh_cpu_count())
		except Exception:
			cores = 1
		for count in (1, 2, 4, 6, 8):
			if count <= max(1, cores):
				threads.append(count)
	results = {}
	try:
		cores = int(lib.offh_cpu_count())
		mask = 0
		if hasattr(lib, 'offh_affinity_mask'):
			try:
				mask = int(lib.offh_affinity_mask())
			except Exception:
				mask = 0
		results['cores'] = cores
		results['affinity_mask'] = mask
		results['affinity_cores'] = _popcount(mask) if mask else -1
		serial = float(lib.offh_selftest_serial(int(iterations)))
		results['serial_ms'] = serial
		LOG_NOTE('NATIVE_MATH selftest cores=%d affinity=0x%x (cores allowed=%d) '
			'serial=%0.2fms iters=%d' % (cores, mask,
				results['affinity_cores'], serial, int(iterations)))
		for count in threads:
			wall = float(lib.offh_selftest_parallel(int(count),
				int(iterations)))
			speedup = (serial * count / wall) if wall > 0 else 0.0
			results[count] = (wall, speedup)
			LOG_NOTE('NATIVE_MATH selftest threads=%d wall=%0.2fms '
				'speedup=%0.2fx' % (count, wall, speedup))
		# GIL check: a Python thread must keep running during a native call.
		gil = _gil_probe(lib, max(1, int(iterations) // 4))
		results['gil_probe_python_iters'] = gil
		LOG_NOTE('NATIVE_MATH selftest gil_probe python_iters=%d (%s)' % (
			gil, 'GIL released, python threads run alongside' if gil > 0 else
				'NO progress - GIL held or process pinned'))
	except Exception as error:
		LOG_ERROR('NATIVE_MATH selftest failed: %s' % str(error))
		disable('selftest error')
		return None
	return results


def _gil_probe(lib, iterations):
	"""Count Python-level iterations in a thread while native code runs."""
	counter = [0]
	stop = [False]

	def _spin():
		count = 0
		while not stop[0]:
			count += 1
		counter[0] = count

	try:
		import threading
		thread = threading.Thread(target=_spin)
		thread.setDaemon(True)
		thread.start()
		for _ in range(4):
			lib.offh_selftest_parallel(1, int(iterations))
		stop[0] = True
		thread.join(2.0)
	except Exception as error:
		LOG_ERROR('NATIVE_MATH gil probe failed: %s' % str(error))
		stop[0] = True
	return int(counter[0])


def selftest_once():
	"""Idempotent wrapper used from the battle callback."""
	if _SELFTEST_DONE[0]:
		return
	_SELFTEST_DONE[0] = True
	try:
		from gui.mods.offhangar._constants import CONFIG_OPTIONS
		if not CONFIG_OPTIONS.get('native_math_selftest', False):
			return
	except Exception:
		return
	selftest()


class ContactBuffers(object):
	"""Reusable structure-of-arrays buffers for the candidate pre-filter.

	One instance per battle; nothing is allocated per frame.  ``fill`` is the
	marshalling step and is the price the integration pays every frame, so it
	is measured separately in the host benchmark.
	"""

	def __init__(self, entity_count):
		lib = load()
		if ctypes is None or lib is None:
			raise RuntimeError('offh_math unavailable: %s' %
				(reason_unavailable() or 'load failed'))
		info = _INFO[0] or {}
		self.lib = lib
		self.max_candidates = int(info.get('max_candidates') or
			_MAX_CANDIDATES_FALLBACK)
		limit = int(info.get('max_entities') or _MAX_ENTITIES_FALLBACK)
		self.n = min(int(entity_count), limit)
		self.pos = (ctypes.c_float * (self.n * 3))()
		self.team = (ctypes.c_sbyte * self.n)()
		self.alive = (ctypes.c_ubyte * self.n)()
		self.view = (ctypes.c_float * self.n)()
		self.camo = (ctypes.c_float * self.n)()

	def set_entity(self, slot, x, y, z, team, alive, view_range, camo):
		base = slot * 3
		self.pos[base] = x
		self.pos[base + 1] = y
		self.pos[base + 2] = z
		self.team[slot] = team
		self.alive[slot] = 1 if alive else 0
		self.view[slot] = view_range
		self.camo[slot] = camo

	def candidates(self, targets, thread_count=1):
		"""Return [(slot, [(entity_index, distance_sq, flag), ...]), ...]."""
		if self.lib is None:
			disable('no library')
			return None
		count = len(targets)
		if count <= 0:
			return []
		tbuf = (ctypes.c_int * count)()
		tcamo = (ctypes.c_float * count)()
		for slot in range(count):
			index = targets[slot]
			tbuf[slot] = index
			tcamo[slot] = self.camo[index]
		out_idx = (ctypes.c_int * (count * self.max_candidates))()
		out_d2 = (ctypes.c_float * (count * self.max_candidates))()
		out_flag = (ctypes.c_ubyte * (count * self.max_candidates))()
		out_count = (ctypes.c_int * count)()
		try:
			total = self.lib.offh_contact_candidates_mt(
				self.pos, self.team, self.alive, self.view, self.n,
				tbuf, tcamo, count, int(thread_count),
				ctypes.c_float(PROXIMITY_SPOT_DISTANCE),
				ctypes.c_float(MAX_FOLIAGE_BONUS),
				out_idx, out_d2, out_flag, out_count)
		except Exception as error:
			disable('candidates call failed: %s' % str(error))
			return None
		if total < 0:
			disable('candidates returned %d' % total)
			return None
		result = []
		for slot in range(count):
			base = slot * self.max_candidates
			limit = int(out_count[slot])
			result.append([(int(out_idx[base + k]), float(out_d2[base + k]),
				int(out_flag[base + k])) for k in range(limit)])
		return result


def reason_unavailable():
	"""Human-readable reason when the native path cannot work here."""
	if ctypes is None:
		return 'no _ctypes in the embedded interpreter (%s)' % _CTYPES_ERROR
	if _DISABLED[0]:
		return 'disabled after a failure (see NATIVE_MATH lines above)'
	return ''


def enabled():
	"""True only when the config asks for it AND the library loaded."""
	if ctypes is None:
		return False
	try:
		from gui.mods.offhangar._constants import CONFIG_OPTIONS
		if not CONFIG_OPTIONS.get('native_math', False):
			return False
	except Exception:
		return False
	return load(quiet=True) is not None
