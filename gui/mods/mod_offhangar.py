# -*- coding: utf-8 -*-
# CameraNode executes this source under the dotted name gui.mods.mod_offhangar
# without importing its parent package first. Mark the bootstrap imports as
# absolute, then construct that missing package before importing game modules.
__package__ = ''

import os
import signal
import sys
import types


def _ensure_gui_mods_package():
	package_name = 'gui.mods'
	package = sys.modules.get(package_name)
	package_dir = os.path.dirname(__file__)
	if package is None:
		package = types.ModuleType(package_name)
		package.__file__ = os.path.join(package_dir, '__init__.py')
		package.__package__ = package_name
		package.__path__ = [package_dir]
		sys.modules[package_name] = package
	elif not hasattr(package, '__path__'):
		package.__path__ = [package_dir]
	gui_package = sys.modules.get('gui')
	if gui_package is not None:
		setattr(gui_package, 'mods', package)
	current = sys.modules.get(__name__)
	if current is not None:
		setattr(package, 'mod_offhangar', current)
	return package


_ensure_gui_mods_package()

def _offh_ensure_engine_config_optimized():
	"""Automatically decrypt/unpack (if binary packed 0x62A14E45) and optimize
	res_mods/<ver>/engine_config.xml on startup so clean clients work out-of-the-box."""
	try:
		from gui.mods.offhangar.core.paths import detect_wot_client_version
		ver = detect_wot_client_version()
	except Exception:
		ver = '0.8.2'
	target_dir = os.path.join('res_mods', ver)
	target_path = os.path.join(target_dir, 'engine_config.xml')
	try:
		if os.path.isfile(target_path):
			fh = open(target_path, 'rb')
			try:
				raw = fh.read()
			finally:
				fh.close()
			# If plain text XML already exists, patch optimization tags in-place preserving user edits
			raw_txt = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else str(raw)
			if not raw[:4] == b'\x45\x4e\xa1\x62' and '<engine_config.xml>' in raw_txt:
				if '<streamCacheSizeKB>16384</streamCacheSizeKB>' in raw_txt and '<maxFrameRate>200</maxFrameRate>' in raw_txt:
					return True
				import re as _re_ec
				raw_mod = raw_txt
				for _tag, _val in (('softwareChannels', '64'), ('virtualChannels', '512'), ('ADPCMCodecs', '64'), ('streamCacheSizeKB', '16384'), ('maxFrameRate', '200'), ('cacheEffects', 'true'), ('useLatentOcclusionQueries', 'true')):
					raw_mod = _re_ec.sub(r'<' + _tag + r'>\s*[^<]*\s*</' + _tag + r'>', '<' + _tag + '>' + _val + '</' + _tag + '>', raw_mod)
				fw0 = open(target_path, 'wb')
				try:
					fw0.write(raw_mod.encode('utf-8'))
				finally:
					fw0.close()
				return True
	except Exception:
		pass
	try:
		if not os.path.isdir(target_dir):
			os.makedirs(target_dir)
		# Try ResMgr first: if engine_config.xml is packed in res/engine_config.xml,
		# ResMgr transparently unpacks binary XML sections in memory.
		_xml_out = """<engine_config.xml>
  <personality>game</personality>
  <preferences>preferences.xml</preferences>
  <renderer>
    <cacheEffects>true</cacheEffects>
    <gammaCorrectionOutside>1.0</gammaCorrectionOutside>
    <gammaCorrectionInside>1.0</gammaCorrectionInside>
    <gammaCorrectionSpeed>0.2</gammaCorrectionSpeed>
    <maxFrameRate>200</maxFrameRate>
    <settings>
      <effect>shaders/terrain/terrain_lit.fx</effect>
    </settings>
    <useLatentOcclusionQueries>true</useLatentOcclusionQueries>
  </renderer>
  <influx>
    <force_use_bw_collision>true</force_use_bw_collision>
    <control_point_hmap_offset>0.1f</control_point_hmap_offset>
    <control_point_grid_step>1.0f</control_point_grid_step>
  </influx>
  <animation>
    <streamCacheSizeKB>16384</streamCacheSizeKB>
  </animation>
  <screenShot>
    <extension>jpg</extension>
    <name>screenshots/shot</name>
  </screenShot>
  <superShot>
    <hRes>2048</hRes>
    <farPlaneDist>1500</farPlaneDist>
    <floraVBSize>16000000</floraVBSize>
    <disableGUI>true</disableGUI>
  </superShot>
  <accessMonitor>false</accessMonitor>
  <hasDevelopmentAssertions>false</hasDevelopmentAssertions>
  <debugKeys>
    <combination>
      <key>APPS</key>
    </combination>
    <combination>
      <key>GRAVE</key>
    </combination>
  </debugKeys>
  <entities>
    <disablePreloads>false</disablePreloads>
    <enforcePreloads>false</enforcePreloads>
    <entityCollision>false</entityCollision>
    <particlesActive>true</particlesActive>
    <movementThreshold>0.25</movementThreshold>
  </entities>
  <ui>
    <straferate>5.0</straferate>
    <loadingText>151 231 233</loadingText>
  </ui>
  <camera>
    <lookTable>0.00\t0.00</lookTable>
    <lookTable>0.10\t0.05</lookTable>
    <lookTable>0.30\t0.10</lookTable>
    <lookTable>0.50\t0.15</lookTable>
    <lookTable>0.70\t0.30</lookTable>
    <lookTable>0.90\t1.00</lookTable>
    <lookAxisMin>0.10</lookAxisMin>
    <lookAxisMax>0.91</lookAxisMax>
    <cameraYawSpeed>90</cameraYawSpeed>
    <cameraPitchSpeed>60</cameraPitchSpeed>
    <accelerateStart>0.00</accelerateStart>
    <accelerateRate>2.00</accelerateRate>
    <accelerateEnd>1.00</accelerateEnd>
  </camera>
  <directionCursor>
    <invertVerticalMovement>false</invertVerticalMovement>
    <mouseSensitivity>0.005</mouseSensitivity>
    <mouseHVBias>0.667</mouseHVBias>
    <maxPitch>50.0</maxPitch>
    <minPitch>-65.0</minPitch>
    <yaw>0.000</yaw>
    <pitch>0.000</pitch>
    <lookSpring>false</lookSpring>
    <lookSpringRate>0.75</lookSpringRate>
    <lookSpringIdleTime>3600.0</lookSpringIdleTime>
    <lookSpringOnMove>true</lookSpringOnMove>
  </directionCursor>
  <simpleGui>
    <breakOnLeak>false</breakOnLeak>
    <breakOnAllocId>0</breakOnAllocId>
  </simpleGui>
  <debug>
    <framesCount>0</framesCount>
  </debug>
  <soundMgr>
    <enabled>true</enabled>
    <errorLevel>warning</errorLevel>
    <softwareChannels>64</softwareChannels>
    <virtualChannels>512</virtualChannels>
    <ADPCMCodecs>64</ADPCMCodecs>
    <mediaPath>audio</mediaPath>
    <soundbanks>
      <project>
        <name>vehicles</name>
      </project>
      <project>
        <name>weapons</name>
      </project>
      <project>
        <name>hits</name>
      </project>
      <project>
        <name>GUI</name>
      </project>
      <project>
        <name>ingame_voice</name>
      </project>
      <project>
        <name>music</name>
      </project>
      <project>
        <name>ambient</name>
      </project>
      <project>
        <name>objects</name>
      </project>
      <project>
        <name>tanks</name>
      </project>
    </soundbanks>
    <networkUpdates>false</networkUpdates>
    <enableProfiler>false</enableProfiler>
    <allowUnload>false</allowUnload>
    <maxSpeed>100.0</maxSpeed>
  </soundMgr>
  <loggerID>111</loggerID>
</engine_config.xml>
"""
		fw = open(target_path, 'wb')
		try:
			fw.write(_xml_out.encode('utf-8'))
		finally:
			fw.close()
		return True
	except Exception:
		return False

_offh_ensure_engine_config_optimized()


import Account
import AccountCommands
import Avatar as AvatarModule
import AvatarInputHandler as AvatarInputHandlerModule
import BigWorld
import account_shared

from ConnectionManager import connectionManager
from GameSessionController import _GameSessionController
from account_helpers.Shop import Shop
from debug_utils import LOG_CURRENT_EXCEPTION, LOG_ERROR
try:
	from gui.Scaleform.Login import Login
except ImportError:
	try:
		from gui.Scaleform.daapi.view.login.LoginView import LoginView as Login
	except ImportError:
		Login = None
try:
	from gui.Scaleform.CommonPage import CommonPage
except ImportError:
	try:
		from gui.Scaleform.daapi.view.lobby.header.LobbyHeader import LobbyHeader as CommonPage
	except ImportError:
		CommonPage = None
try:
	from gui.Scaleform.gui_items.Vehicle import Vehicle
except ImportError:
	try:
		from gui.shared.gui_items.Vehicle import Vehicle
	except ImportError:
		Vehicle = None
from helpers.time_utils import _TimeCorrector, _g_instance
from nations import INDICES
from predefined_hosts import g_preDefinedHosts

_OFFH_SUBFOLDERS = ('core', 'native', 'ai', 'combat', 'physics', 'ui', 'network')

_OFFH_ALIASES = {
	'paths': ('core', 'paths'),
	'logging': ('core', 'logging'),
	'state': ('core', 'state'),
	'session_guards': ('core', 'session_guards'),
	'cbt_profiles_and_locale': ('core', 'cbt_profiles_and_locale'),
	'_constants': ('core', '_constants'),
	'offh_multithread': ('native', 'multithread_pool'),
	'native_filter_bridge': ('native', 'filter_bridge'),
	'offh_native_math': ('native', 'native_math'),
	'native_navmesh_probe': ('native', 'navmesh_probe'),
	'native_vehicle_physics_probe': ('native', 'physics_probe'),
	'bot_ai': ('ai', 'director_v1'),
	'bot_ai_v2': ('ai', 'director_v2'),
	'bot_ai_driver': ('ai', 'driver'),
	'bot_ai_navigation': ('ai', 'navigation'),
	'bot_ai_cover': ('ai', 'cover'),
	'bot_blackboard': ('ai', 'blackboard'),
	'bot_capabilities': ('ai', 'capabilities'),
	'artillery_arc_queue': ('ai', 'artillery_arc'),
	'bot_ai_maps': ('ai', 'maps'),
	'projectile_runtime': ('combat', 'ballistics'),
	'device_damage': ('combat', 'damage_modules'),
	'spotting': ('combat', 'spotting_camo'),
	'physics': ('physics', 'vehicle_physics'),
	'vehicle_collision': ('physics', 'collisions'),
	'destructibles_authority': ('physics', 'destructibles'),
	'spawn_streaming_bootstrap': ('physics', 'spawn_bootstrap'),
	'battle_feedback': ('ui', 'battle_hud'),
	'spectator_minimap': ('ui', 'minimap'),
	'data': ('ui', 'hangar_data'),
	'server': ('ui', 'hangar_server'),
	'network_battle': ('network', 'lan_battle'),
}

def _ensure_offhangar_package():
	"""Create the source package and its 7 logical subpackages (core, native, ai, combat, physics, ui, network)."""
	import sys, os, types
	mods_package = _ensure_gui_mods_package()
	package_name = 'gui.mods.offhangar'
	package = sys.modules.get(package_name)
	package_dir = os.path.join(os.path.dirname(__file__), 'offhangar')
	if package is None:
		package = types.ModuleType(package_name)
		package.__file__ = os.path.join(package_dir, '__init__.py')
		package.__package__ = package_name
		package.__path__ = [package_dir]
		sys.modules[package_name] = package
	elif not hasattr(package, '__path__'):
		package.__path__ = [package_dir]
	for _sub in _OFFH_SUBFOLDERS:
		_sub_dir = os.path.join(package_dir, _sub)
		if _sub_dir not in package.__path__:
			package.__path__.append(_sub_dir)
		_sub_pkg_name = package_name + '.' + _sub
		_sub_pkg = sys.modules.get(_sub_pkg_name)
		if _sub_pkg is None:
			_sub_pkg = types.ModuleType(_sub_pkg_name)
			_sub_pkg.__file__ = os.path.join(_sub_dir, '__init__.py')
			_sub_pkg.__package__ = _sub_pkg_name
			_sub_pkg.__path__ = [_sub_dir]
			sys.modules[_sub_pkg_name] = _sub_pkg
		setattr(package, _sub, _sub_pkg)
	setattr(mods_package, 'offhangar', package)
	return package

def _bind_module_aliases(parent, mod_name, mod, rel_path):
	import sys, os
	setattr(parent, mod_name, mod)
	try:
		sub_dir_name = os.path.basename(os.path.dirname(os.path.abspath(rel_path)))
		if sub_dir_name in _OFFH_SUBFOLDERS:
			sub_pkg_name = 'gui.mods.offhangar.' + sub_dir_name
			sub_pkg = sys.modules.get(sub_pkg_name)
			if sub_pkg is not None:
				sys.modules[sub_pkg_name + '.' + mod_name] = mod
				setattr(sub_pkg, mod_name, mod)
	except Exception:
		pass
	alias_info = _OFFH_ALIASES.get(mod_name)
	if alias_info is not None:
		alias_sub, alias_name = alias_info
		sub_pkg_name = 'gui.mods.offhangar.' + alias_sub
		sub_pkg = sys.modules.get(sub_pkg_name)
		if sub_pkg is not None:
			sys.modules[sub_pkg_name + '.' + alias_name] = mod
			setattr(sub_pkg, alias_name, mod)
			# The flat loader name and legacy core name must share one module object.
			if alias_name != 'constants':
				sys.modules['gui.mods.offhangar.' + alias_name] = mod
				setattr(parent, alias_name, mod)
			if mod_name != 'constants':
				sys.modules['gui.mods.offhangar.' + mod_name] = mod
				setattr(parent, mod_name, mod)

def _inject_submodule(mod_name, rel_path):
	"""Inject a .py file (from root or subfolder) as a submodule when normal import fails."""
	import sys, os, types
	parent = _ensure_offhangar_package()
	full_name = 'gui.mods.offhangar.' + mod_name
	existing = sys.modules.get(full_name)
	if existing is not None and getattr(existing, '_offh_exec_loaded', False):
		_bind_module_aliases(parent, mod_name, existing, rel_path)
		return existing
	mod = existing if existing is not None else types.ModuleType(full_name)
	mod.__file__ = rel_path
	mod.__package__ = 'gui.mods.offhangar'
	sys.modules[full_name] = mod
	try:
		execfile(rel_path, mod.__dict__)
		mod._offh_exec_loaded = True
	except Exception:
		if existing is None and full_name in sys.modules:
			del sys.modules[full_name]
		raise
	_bind_module_aliases(parent, mod_name, mod, rel_path)
	return mod

def _find_module_py_path(candidates, name):
	"""Locate <name>.py inside any subfolder (core, native, ai, combat, physics, ui, network) or root."""
	import os
	for _pkg_dir in candidates:
		for _sub in _OFFH_SUBFOLDERS:
			p_sub = os.path.join(_pkg_dir, _sub, name + '.py')
			if os.path.isfile(p_sub):
				return p_sub
		p_root = os.path.join(_pkg_dir, name + '.py')
		if os.path.isfile(p_root):
			return p_root
	return None

def _safe_import_offhangar():
	"""Load all offhangar modules across root and logical subfolders (core, native, ai, combat, physics, ui, network)."""
	import sys, os
	candidates = [r'res_mods\0.8.2\scripts\client\gui\mods\offhangar']
	try:
		_res_dir = os.path.abspath(r'res_mods\0.8.2\scripts\client\gui\mods\offhangar')
		_vfs_dir = os.path.abspath(r'scripts\client\gui\mods\offhangar')
		if os.path.isdir(_res_dir) and os.path.isdir(_vfs_dir) and os.path.normcase(_res_dir) != os.path.normcase(_vfs_dir):
			import shutil as _sh_sync
			for _sfn in os.listdir(_res_dir):
				_sp = os.path.join(_res_dir, _sfn)
				_dp = os.path.join(_vfs_dir, _sfn)
				if os.path.isfile(_sp) and (_sfn.endswith('.py') or _sfn.endswith('.pyd') or _sfn.endswith('.json')):
					try:
						_sh_sync.copy2(_sp, _dp)
					except Exception:
						pass
			for _sub in _OFFH_SUBFOLDERS:
				_s_sub = os.path.join(_res_dir, _sub)
				_d_sub = os.path.join(_vfs_dir, _sub)
				if os.path.isdir(_s_sub):
					try:
						if not os.path.isdir(_d_sub):
							os.makedirs(_d_sub)
						for _sfn in os.listdir(_s_sub):
							_sp = os.path.join(_s_sub, _sfn)
							if os.path.isfile(_sp) and _sfn.endswith('.py'):
								_sh_sync.copy2(_sp, os.path.join(_d_sub, _sfn))
					except Exception:
						pass
	except Exception:
		pass
	try:
		_this = __file__
		_c1 = os.path.join(os.path.dirname(_this), 'offhangar')
		if _c1 not in candidates:
			candidates.append(_c1)
		_abs = os.path.abspath(_this)
		_c2 = os.path.join(os.path.dirname(_abs), 'offhangar')
		if _c2 not in candidates:
			candidates.append(_c2)
	except Exception:
		pass
	# Clean up stale .pyc files (and any old flat root .py/.pyc duplicates when subfolder .py exists)
	for _cand_dir in candidates:
		try:
			if os.path.isdir(_cand_dir):
				_sub_py_names = set()
				for _sub in _OFFH_SUBFOLDERS:
					_sd = os.path.join(_cand_dir, _sub)
					if os.path.isdir(_sd):
						for _fn in os.listdir(_sd):
							if _fn.endswith('.py') and _fn != '__init__.py':
								_sub_py_names.add(_fn[:-3])
							elif _fn.endswith('.pyc'):
								try:
									os.remove(os.path.join(_sd, _fn))
								except Exception:
									pass
				for _fn in os.listdir(_cand_dir):
					if _fn in ('visual_suspension.pyc', 'offh_native_math.pyc', '__init__.py', 'offline_battle.py', 'mod_offhangar.py'):
						continue
					if _fn.endswith('.pyc'):
						_base = _fn[:-4]
						if _base in _sub_py_names or os.path.exists(os.path.join(_cand_dir, _base + '.py')):
							try:
								os.remove(os.path.join(_cand_dir, _fn))
							except Exception:
								pass
					elif _fn.endswith('.py'):
						_base = _fn[:-3]
						if _base in _sub_py_names:
							try:
								os.remove(os.path.join(_cand_dir, _fn))
							except Exception:
								pass
		except Exception:
			pass

	submodules = [
		'paths', 'utils', 'logging', '_constants', 'flags', 'diag_probe',
		'user_config', 'state', 'cbt_profiles_and_locale', 'runtime_state',
		'offh_multithread', 'offh_native_math', 'native_navmesh_probe',
		'replay_machinima', 'physics', 'vehicle_collision',
		'vehicle_pose', 'physics_monitor', 'native_filter_bridge',
		'physics_core', 'native_vehicle_physics_probe',
		'bot_loadout', 'projectile_runtime',
		'artillery_arc_queue', 'device_damage', 'internal_geometry',
		'internal_layout_profiles', 'internal_hit_layouts', 'internal_layout_store', 'internal_layout_debug',
		'spawn_streaming_bootstrap',
		'bot_ai_maps_group_a', 'bot_ai_maps_group_b', 'bot_ai_maps_group_c',
		'bot_ai_maps_extra', 'bot_ai_maps',
		'bot_capabilities', 'bot_blackboard', 'bot_ai', 'bot_ai_v2',
		'dynamic_map_baker', 'prebaked_navigation', 'battle_feedback', 'capture_rules',
		'vehicle_camouflage', 'spotting', 'foliage', 'prebaked_foliage',
		'bot_ai_navigation', 'bot_ai_cover', 'bot_ai_driver',
		'spectator_minimap', 'data', 'session_guards', 'offline_battle_stack', 'offline_battle',
		'command_router', 'command_handlers', 'server',
		'destructibles_authority', 'pen_indicator', 'lan_settings', 'lan_waiting_room',
		'network_battle', 'ingame_menu'
	]

	def _try_load(name):
		full = 'gui.mods.offhangar.' + name
		if full in sys.modules and getattr(sys.modules[full], '_offh_exec_loaded', False):
			return True
		py_path = _find_module_py_path(candidates, name)
		if py_path is not None:
			try:
				_inject_submodule(name, py_path)
				return True
			except Exception:
				if full in sys.modules:
					del sys.modules[full]
				return False
		try:
			__import__(full)
			return True
		except ImportError:
			pass
		return False

	pending = list(submodules)
	for _ in range(len(submodules) + 1):
		if not pending:
			break
		still = []
		progressed = False
		for name in pending:
			if _try_load(name):
				progressed = True
			else:
				still.append(name)
		pending = still
		if not progressed:
			break
	for name in pending:
		import traceback
		py_path = _find_module_py_path(candidates, name) or os.path.join(candidates[0], name + '.py')
		try:
			_inject_submodule(name, py_path)
		except Exception:
			_tb = traceback.format_exc()
			try:
				LOG_ERROR('[OFFHANGAR] Failed to import submodule %s (%s):\n%s' % (name, py_path, _tb))
			except Exception:
				print('[OFFHANGAR] Failed to import submodule', name, py_path, _tb)

_safe_import_offhangar()

try:
	# Clean up any legacy mods/offhangar_boot.py so game.py never logs ImportError
	for _b_cand in (
		os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mods", "offhangar_boot.py"),
		os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mods", "offhangar_boot.pyc"),
		r"res_mods\0.8.2\scripts\client\mods\offhangar_boot.py",
		r"res_mods\0.8.2\scripts\client\mods\offhangar_boot.pyc",
	):
		if os.path.exists(_b_cand):
			try:
				os.remove(_b_cand)
			except Exception:
				pass
except Exception:
	pass

try:
	from gui.mods.offhangar import replay_machinima as _rm_early
	_rm_early.sync_replay_setting_on_startup()
except Exception:
	pass

# Every submodule is now injected into sys.modules, so these star-imports
# resolve. They MUST come after the loader: this package cannot be imported
# normally in the embedded client ('Parent module gui.mods not found'), and
# server imports command_router - which the loader injects above.
from gui.mods.offhangar.logging import *
from gui.mods.offhangar.utils import *
from gui.mods.offhangar._constants import *
from gui.mods.offhangar.server import *

def _worker_diag(msg):
	try:
		with open('worker_diag.log', 'a') as _df:
			import time as _dtm
			_df.write('[%.3f] %s\n' % (_dtm.time(), msg))
	except Exception:
		pass

def _is_worker_mode():
	return os.environ.get('OFFHANGAR_WORKER_MODE') == 'authority_worker'

_worker_diag('mod_offhangar loaded, worker_mode=%s' % _is_worker_mode())


def _apply_worker_mode():
	if not _is_worker_mode():
		return
	try:
		import BigWorld
		# KEEP worldDrawEnabled(True) on worker!
		# Setting worldDrawEnabled(False) causes the BigWorld engine to enter
		# power-saving sleep (MsgWaitForMultipleObjects 300ms), dropping tick rate to 3.0 FPS!
		BigWorld.worldDrawEnabled(True)
		BigWorld.wg_setReducedFpsMode(False)
		BigWorld.wg_setReducedFpsMode = lambda *a, **kw: None
	except Exception:
		pass
	try:
		import ctypes
		from ctypes import wintypes
		kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
		NORMAL_PRIORITY_CLASS = 0x00000020
		kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), NORMAL_PRIORITY_CLASS)
		try:
			import multiprocessing
			cpus = multiprocessing.cpu_count()
			if cpus >= 8:
				kernel32.SetProcessAffinityMask(kernel32.GetCurrentProcess(), 0x00F0)
		except Exception:
			pass
		class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
			_fields_ = [
				('Version', wintypes.DWORD),
				('ControlMask', wintypes.DWORD),
				('StateMask', wintypes.DWORD),
			]
		throttle_state = PROCESS_POWER_THROTTLING_STATE()
		throttle_state.Version = 1
		throttle_state.ControlMask = 0x1 | 0x4
		throttle_state.StateMask = 0
		kernel32.SetProcessInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD]
		kernel32.SetProcessInformation.restype = wintypes.BOOL
		kernel32.SetProcessInformation(kernel32.GetCurrentProcess(), 4, ctypes.byref(throttle_state), ctypes.sizeof(throttle_state))
		try:
			winmm = ctypes.WinDLL('winmm')
			winmm.timeBeginPeriod(1)
		except Exception:
			pass
	except Exception:
		pass
	try:
		import FMOD
		if not getattr(FMOD, '_offhangar_worker_locked', False):
			_orig_fmod_setMasterVolume = FMOD.setMasterVolume
			def _worker_setMasterVolume(vol):
				return _orig_fmod_setMasterVolume(0.0)
			FMOD.setMasterVolume = _worker_setMasterVolume
			FMOD._offhangar_worker_locked = True
		FMOD.setMasterVolume(0.0)
	except Exception:
		pass
	try:
		import BigWorld
		BigWorld.wg_setMovieSoundMuted(True)
	except Exception:
		pass
	try:
		import SoundGroups
		if SoundGroups.g_instance is not None:
			SoundGroups.g_instance.enableLobbySounds(False)
			SoundGroups.g_instance.enableArenaSounds(False)
	except Exception:
		pass
	try:
		import BigWorld
		for w in ('Client Settings/maxFrameRate', 'maxFrameRate', 'Render/maxFrameRate'):
			try:
				BigWorld.setWatcher(w, 60)
			except Exception:
				pass
		try:
			BigWorld.setWatcher('Visibility/GUI', False)
		except Exception:
			pass
	except Exception:
		pass
	try:
		_apply_worker_graphics_low()
	except Exception:
		pass
	try:
		_apply_worker_render_size()
	except Exception:
		pass
	try:
		_log_worker_video_once()
	except Exception:
		pass


# --- worker graphics trim (config flag: worker_graphics_low) -----------------
# The worker is a second, WINDOWLESS copy of the client, and it still renders
# every frame: worldDrawEnabled(False) makes the engine fall into a power-saving
# wait (see the note in _apply_worker_mode above) and the tick drops to ~3 FPS.
# So it pays full price for graphics nobody ever sees. Measured 04.10 from the
# outside (PowerShell): both processes ~108-110% of one core, 811/874 MB, 53/58
# threads - while the worker's own simulation tick is only
# 'SERVER PERF cpu_core=11.8-14.4% tick=30.0Hz avg=3.63-4.33ms overruns=0'.
# The difference (~85-90% of a core) is render/GUI overhead.
#
# Two jobs here, coupled on purpose so the trim can never be enabled without
# its protection:
#   1. PROTECTION FIRST. The visible client and the worker run under the same
#      Windows user, so they share ONE preferences file
#      (%APPDATA%\Roaming\Wargaming.net\WorldOfTanks\preferences.xml). Stock
#      writes it via Settings.save() -> BigWorld.savePreferences()
#      (dev_ref/stock_082/client/settings.py:38-40), called on exit
#      (game.py:235) and from the login flow (gui/scaleform/login.py:216,228) -
#      and the worker DOES go through that login flow. Without the stub below the
#      worker would save its own minimal graphics over the player's settings.
#      If the stub cannot be installed we ABORT: a half-applied trim that gets
#      persisted is worse than no trim at all.
#   2. Then drop quality to the stock LOW preset with the same engine calls stock
#      uses: BigWorld.setGraphicsSetting(label, value) per setting
#      (dev_ref/stock_082/client/gui/graphicspresets.py:132), then
#      BigWorld.commitPendingGraphicsSettings()
#      (dev_ref/stock_082/client/gui/scaleform/settingsinterface.py:580,583).
#      Nothing is hardcoded: values come from the stock preset table
#      'system/data/graphics_settings_presets.xml' (inside res/packages/misc.pkg),
#      parsed exactly like gui/graphicspresets.py:26-40.
#      NOT touched here: resolution, video mode, monitor, maxFrameRate. The frame
#      cap is not the limiter - the authority already runs at fps med 31.10 with
#      the cap at 60, i.e. the worker is CPU-bound.
#
# _apply_worker_mode() runs three times (at module import, in the Login.populateUI
# override and in Account.onBecomePlayer) and the renderer may not exist yet at
# import time, so the done-mark is set only after a successful apply and every
# later call retries until then.
def _worker_prefs_readonly(tag):
	"""Make the shared preferences.xml read-only for THIS process. True on success.

	Both worker-side visual trims need this. The visible client and the worker run
	under the same Windows user, so they share ONE preferences file
	(%APPDATA%\\Roaming\\Wargaming.net\\WorldOfTanks\\preferences.xml), and stock writes
	it through Settings.save() -> BigWorld.savePreferences()
	(dev_ref/stock_082/client/settings.py:38-40), called on exit (game.py:235) and
	from the login flow (gui/scaleform/login.py:216,228) - and the worker goes
	through that very login flow. Without the stub, anything the worker changes
	about its own video mode or graphics would be saved over the player's settings.
	Callers must treat False as "do not touch anything".
	"""
	try:
		import BigWorld
	except Exception:
		return False
	try:
		if not getattr(BigWorld, '_offhangar_prefs_readonly', False):
			BigWorld._offhangar_prefs_readonly_orig = BigWorld.savePreferences
			BigWorld.savePreferences = lambda *a, **kw: None
			BigWorld._offhangar_prefs_readonly = True
			_worker_diag('%s savePreferences stubbed: shared preferences.xml is read-only for the worker' % tag)
	except Exception:
		_worker_diag('%s ABORTED: could not stub BigWorld.savePreferences' % tag)
		return False
	if not getattr(BigWorld, '_offhangar_prefs_readonly', False):
		_worker_diag('%s ABORTED: preferences protection not active' % tag)
		return False
	return True


# Worker render-window size (config: worker_render_size, "WxH" or "" = off).
# WHY: the worker's frame is ~38.7 ms of which only ~20.5 ms is the Python callback
# (measured 04.10 14:30 on 44_north_america: fps med 25.9, callback med 20.48 ms),
# so ~18 ms per frame is engine work - render/swap. The worker's frame rate is also
# how often bot states get published (`bot_updates` med 20.4/s against
# `snapshots` 30.0/s, `max_bot_gap` med 105 ms), which is exactly the "they all
# drive at 20-30 fps" look in the visible client. Shrinking the render target is
# the cheap lever on that ~18 ms. It is NOT a CPU lever - the worker's CPU is
# almost entirely the callback (20.48 ms x 25.9 fps = ~53% of a core, measured
# 51.4% from the outside), so do not expect the process to get cheaper.
# `worldDrawEnabled(False)` stays off limits: the engine then falls into a
# power-saving wait and the tick drops to ~3 FPS (note in _apply_worker_mode).
# The 0.9.22 reference does it as a measured experiment, not a blind switch:
# `authority_worker_probe.py:21 STAGE_NAMES = ('draw_on','draw_off','window_hidden')`,
# drawing is taken through a reversible lease with readback
# (`authority_worker.py:700-746 _WorldDrawLease`) and the window is hidden by an
# EXTERNAL supervisor that restores placement in a finally block
# (`authority_worker_probe_supervisor.py:2-8`). This flag is the same discipline in
# one step: change, then read the value back, and only then consider it applied.
# `BigWorld.resizeWindow` is the stock LIVE path (gui/GraphicsResolutions.py
# :244-281; only onSavePreferencesXml - gated on a MONITOR change - writes to disk),
# so no restart is needed and the player's own window is untouched (the worker
# lives on its own private desktop, see offline_worker_starter.py).
_worker_video_logged = [False]


def _log_worker_video_once():
	"""Worker only: report the video state the process ACTUALLY came up with.

	Needed because the shared-profile fix could not be confirmed any other way.
	The player's client and the worker used to read the same preferences.xml, so
	when the player switched to 1920x1009 with vsync on (04.10 15:24) the worker
	inherited it: frame med 36.02 -> 79.59 ms, fps med 25.9 -> 12.5, and because the
	worker publishes bot states at its own frame rate the client's `bot_updates`
	fell 20.4 -> 11.6/s with `max_bot_gap` med 105 -> 156 ms. The launcher now
	hands the worker a private %APPDATA% with 1024x768 and waitVSync=false, yet the
	next run still measured fps med 12.2 / frame med 81.05 ms - so either the
	engine does not take its preferences from %APPDATA%, or the slowdown has another
	cause. Guessing between those costs runs; this line settles it in one.
	Logged once per process from the main thread (never from the REPL thread), and
	only in the worker, so the visible client's log stays clean.
	"""
	if _worker_video_logged[0]:
		return
	_worker_video_logged[0] = True
	try:
		import BigWorld
	except Exception:
		return
	try:
		_res = BigWorld.wg_getCurrentResolution(True)
	except Exception:
		_res = None
	try:
		_idx = BigWorld.videoModeIndex()
	except Exception:
		_idx = None
	try:
		_win = BigWorld.isVideoWindowed()
	except Exception:
		_win = None
	try:
		_modes = len(BigWorld.listVideoModesAllMonitors() or [])
	except Exception:
		_modes = -1
	_worker_diag('WORKER_VIDEO resolution=%s videoModeIndex=%s windowed=%s monitors_with_modes=%d APPDATA=%s' % (
		str(_res), str(_idx), str(_win), _modes, os.environ.get('APPDATA', '')))


_worker_render_done = False
_worker_render_attempts = 0
_worker_render_before = None
_worker_render_list_logged = False


def _apply_worker_render_size():
	"""Worker only: shrink the render window to config `worker_render_size`.

	Idempotent; returns True once the readback confirms the requested size.
	"""
	global _worker_render_done, _worker_render_attempts, _worker_render_before
	global _worker_render_list_logged
	if _worker_render_done:
		return True
	try:
		from gui.mods.offhangar._constants import CONFIG_OPTIONS as _CFG
		_raw = str(_CFG.get('worker_render_size', '') or '').strip().lower()
	except Exception:
		return False
	if not _raw or _raw in ('off', '0', 'none'):
		return False
	_auto = (_raw == 'auto')
	_w = 0
	_h = 0
	if not _auto:
		try:
			_parts = _raw.replace('*', 'x').replace(' ', '').split('x', 1)
			_w = int(float(_parts[0]))
			_h = int(float(_parts[1]))
		except Exception:
			_worker_diag('WORKER_RES bad worker_render_size=%r, expected "WxH" or "auto"' % (_raw,))
			return False
		_w = max(160, min(1920, _w))
		_h = max(120, min(1200, _h))
	if not _worker_prefs_readonly('WORKER_RES'):
		return False
	try:
		import BigWorld
	except Exception:
		return False
	if _auto:
		# An ARBITRARY size is refused by the engine: 320x240 was requested six
		# times and every readback still said (1024, 768) - worker_diag 04.10 15:21,
		# 'WORKER_RES GAVE UP after 6 attempts, still (1024, 768) (want 320x240)'.
		# Stock never asks for a size that is not on the engine's own list:
		# gui/GraphicsResolutions.py:20-39 enumerates
		# BigWorld.listVideoModesAllMonitors() (mode[1]=width, mode[2]=height) and
		# :61-75 filters it with BigWorld.wg_getMaxWindowedResolution(monitor).
		# So 'auto' takes the SMALLEST supported size and logs the list once - that
		# both works around the refusal and tells us what is actually offered.
		_modes = []
		try:
			for _mon in (BigWorld.listVideoModesAllMonitors() or []):
				for _m in (_mon or []):
					try:
						_modes.append((int(_m[1]), int(_m[2])))
					except Exception:
						try:
							_modes.append((int(_m[0]), int(_m[1])))
						except Exception:
							pass
		except Exception:
			_modes = []
		_uniq = sorted(set(_modes))
		if not _worker_render_list_logged:
			_worker_render_list_logged = True
			try:
				_maxw = BigWorld.wg_getMaxWindowedResolution(0)
			except Exception:
				_maxw = None
			_worker_diag('WORKER_RES auto: %d distinct modes, max_windowed=%s, smallest=%s' % (
				len(_uniq), str(_maxw), str(_uniq[:5])))
		_pick = None
		for _mw, _mh in _uniq:
			if _mw < 160 or _mh < 120:
				continue
			if _pick is None or (_mw * _mh) < (_pick[0] * _pick[1]):
				_pick = (_mw, _mh)
		if _pick is None:
			_worker_diag('WORKER_RES auto: no usable mode in the engine list, giving up')
			return False
		_w = _pick[0]
		_h = _pick[1]
		_worker_diag('WORKER_RES auto picked %dx%d' % (_w, _h))
	# `BigWorld.resizeWindow` does NOT take effect inside the call. The first
	# version of this function read the resolution back immediately and got the
	# OLD value three times in a row (worker_diag 04.10 15:02:
	# 'WORKER_RES (1024, 768) -> (1024, 768) (want 320x240)' x3), so it kept
	# re-issuing the same request and never confirmed anything. The engine applies
	# the resize on its own frame, therefore: check first, request only if the
	# current size differs, then re-check from a DELAYED callback. Attempts are
	# bounded so a size the engine refuses cannot loop forever.
	try:
		_current = BigWorld.wg_getCurrentResolution(True)
	except Exception:
		_current = None
	try:
		if (_current is not None and int(_current[0]) == _w and
				int(_current[1]) == _h):
			_worker_render_done = True
			_worker_diag('WORKER_RES CONFIRMED %dx%d (was %s, attempts=%d)' % (
				_w, _h, str(_worker_render_before), _worker_render_attempts))
			return True
	except Exception:
		pass
	if _worker_render_attempts >= 6:
		_worker_diag('WORKER_RES GAVE UP after %d attempts, still %s (want %dx%d)'
			% (_worker_render_attempts, str(_current), _w, _h))
		return False
	_worker_render_attempts += 1
	if _worker_render_attempts == 1:
		_worker_render_before = _current
	try:
		BigWorld.resizeWindow(_w, _h)
	except Exception:
		_worker_diag('WORKER_RES resizeWindow(%d,%d) RAISED on attempt %d' % (
			_w, _h, _worker_render_attempts))
		return False
	_worker_diag('WORKER_RES attempt %d: resizeWindow(%d,%d) issued, current=%s' % (
		_worker_render_attempts, _w, _h, str(_current)))
	try:
		BigWorld.callback(1.5, _apply_worker_render_size)
	except Exception:
		pass
	return False


_worker_gfx_done = False


def _apply_worker_graphics_low():
	"""Worker only: make the shared preferences file read-only, then go LOW.

	Idempotent; returns True once the trim has been applied.
	"""
	global _worker_gfx_done
	if _worker_gfx_done:
		return True
	try:
		from gui.mods.offhangar._constants import CONFIG_OPTIONS as _CFG
		if not bool(_CFG.get('worker_graphics_low', False)):
			return False
	except Exception:
		return False
	try:
		import BigWorld
	except Exception:
		return False
	# 1. protection: the worker must never persist anything into the shared file.
	if not _worker_prefs_readonly('WORKER_GFX'):
		return False
	# 2. read the stock LOW preset table (same parsing as gui/graphicspresets.py).
	pairs = {}
	try:
		import ResMgr
		_section = ResMgr.openSection('system/data/graphics_settings_presets.xml')
		for _group in _section.values():
			if _group.asString != 'LOW':
				continue
			for _setting in _group.values():
				_label = _setting.readString('label')
				if _label:
					pairs[_label] = _setting.readInt('activeOption')
	except Exception:
		_worker_diag('WORKER_GFX preset table unreadable, graphics left alone')
		return False
	if not pairs:
		_worker_diag('WORKER_GFX preset LOW is empty, graphics left alone')
		return False
	# 3. apply engine-side only (no savePreferences anywhere below).
	_applied = 0
	_failed = []
	for _label in sorted(pairs.keys()):
		try:
			BigWorld.setGraphicsSetting(_label, pairs[_label])
			_applied += 1
		except Exception:
			_failed.append(_label)
	try:
		BigWorld.commitPendingGraphicsSettings()
	except Exception:
		pass
	_worker_diag('WORKER_GFX preset=LOW applied=%d/%d failed=%s' % (
		_applied, len(pairs), ','.join(_failed) if _failed else 'none'))
	if _applied == 0:
		return False
	_worker_gfx_done = True
	return True


def _apply_client_mode():
	if _is_worker_mode():
		return
	try:
		import ctypes
		from ctypes import wintypes
		kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
		HIGH_PRIORITY_CLASS = 0x00000080
		kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), HIGH_PRIORITY_CLASS)
		try:
			import multiprocessing
			cpus = multiprocessing.cpu_count()
			if cpus > 0:
				# Adaptive CPU Affinity: unclamp process access across all logical cores (sys_mask)
				# so D3D9 render, FMOD audio, BigWorld and offh_mt workers run without core starvation
				aff_mask = (1 << min(cpus, 32)) - 1
				kernel32.SetProcessAffinityMask(kernel32.GetCurrentProcess(), aff_mask)
		except Exception:
			pass
		class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
			_fields_ = [
				('Version', wintypes.DWORD),
				('ControlMask', wintypes.DWORD),
				('StateMask', wintypes.DWORD),
			]
		throttle_state = PROCESS_POWER_THROTTLING_STATE()
		throttle_state.Version = 1
		throttle_state.ControlMask = 0x1 | 0x4
		throttle_state.StateMask = 0
		kernel32.SetProcessInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD]
		kernel32.SetProcessInformation.restype = wintypes.BOOL
		kernel32.SetProcessInformation(kernel32.GetCurrentProcess(), 4, ctypes.byref(throttle_state), ctypes.sizeof(throttle_state))
	except Exception:
		pass
	_offh_autopatch_engine_and_prefs()
	_offh_optimize_native_subengines()


_OPTIMIZED_ENGINE_CONFIG_XML = '''<engine_config.xml>
  <personality>game</personality>
  <preferences>preferences.xml</preferences>
  <renderer>
    <cacheEffects>true</cacheEffects>
    <gammaCorrectionOutside>1.0</gammaCorrectionOutside>
    <gammaCorrectionInside>1.0</gammaCorrectionInside>
    <gammaCorrectionSpeed>0.2</gammaCorrectionSpeed>
    <maxFrameRate>200</maxFrameRate>
    <settings>
      <effect>shaders/terrain/terrain_lit.fx</effect>
    </settings>
    <useLatentOcclusionQueries>true</useLatentOcclusionQueries>
  </renderer>
  <influx>
    <force_use_bw_collision>true</force_use_bw_collision>
    <control_point_hmap_offset>0.1f</control_point_hmap_offset>
    <control_point_grid_step>1.0f</control_point_grid_step>
  </influx>
  <animation>
    <streamCacheSizeKB>16384</streamCacheSizeKB>
  </animation>
  <screenShot>
    <extension>jpg</extension>
    <name>screenshots/shot</name>
  </screenShot>
  <superShot>
    <hRes>2048</hRes>
    <farPlaneDist>1500</farPlaneDist>
    <floraVBSize>16000000</floraVBSize>
    <disableGUI>true</disableGUI>
  </superShot>
  <accessMonitor>false</accessMonitor>
  <hasDevelopmentAssertions>false</hasDevelopmentAssertions>
  <debugKeys>
    <combination>
      <key>APPS</key>
    </combination>
    <combination>
      <key>GRAVE</key>
    </combination>
  </debugKeys>
  <entities>
    <disablePreloads>false</disablePreloads>
    <enforcePreloads>false</enforcePreloads>
    <entityCollision>false</entityCollision>
    <particlesActive>true</particlesActive>
    <movementThreshold>0.25</movementThreshold>
  </entities>
  <ui>
    <straferate>5.0</straferate>
    <loadingText>151 231 233</loadingText>
  </ui>
  <camera>
    <lookTable>0.00	0.00</lookTable>
    <lookTable>0.10	0.05</lookTable>
    <lookTable>0.30	0.10</lookTable>
    <lookTable>0.50	0.15</lookTable>
    <lookTable>0.70	0.30</lookTable>
    <lookTable>0.90	1.00</lookTable>
    <lookAxisMin>0.10</lookAxisMin>
    <lookAxisMax>0.91</lookAxisMax>
    <cameraYawSpeed>90</cameraYawSpeed>
    <cameraPitchSpeed>60</cameraPitchSpeed>
    <accelerateStart>0.00</accelerateStart>
    <accelerateRate>2.00</accelerateRate>
    <accelerateEnd>1.00</accelerateEnd>
  </camera>
  <directionCursor>
    <invertVerticalMovement>false</invertVerticalMovement>
    <mouseSensitivity>0.005</mouseSensitivity>
    <mouseHVBias>0.667</mouseHVBias>
    <maxPitch>50.0</maxPitch>
    <minPitch>-65.0</minPitch>
    <yaw>0.000</yaw>
    <pitch>0.000</pitch>
    <lookSpring>false</lookSpring>
    <lookSpringRate>0.75</lookSpringRate>
    <lookSpringIdleTime>3600.0</lookSpringIdleTime>
    <lookSpringOnMove>true</lookSpringOnMove>
  </directionCursor>
  <simpleGui>
    <breakOnLeak>false</breakOnLeak>
    <breakOnAllocId>0</breakOnAllocId>
  </simpleGui>
  <debug>
    <framesCount>0</framesCount>
  </debug>
  <soundMgr>
    <enabled>true</enabled>
    <errorLevel>warning</errorLevel>
    <softwareChannels>64</softwareChannels>
    <virtualChannels>512</virtualChannels>
    <ADPCMCodecs>64</ADPCMCodecs>
    <mediaPath>audio</mediaPath>
    <soundbanks>
      <project>
        <name>vehicles</name>
      </project>
      <project>
        <name>weapons</name>
      </project>
      <project>
        <name>hits</name>
      </project>
      <project>
        <name>GUI</name>
      </project>
      <project>
        <name>ingame_voice</name>
      </project>
      <project>
        <name>music</name>
      </project>
      <project>
        <name>ambient</name>
      </project>
      <project>
        <name>objects</name>
      </project>
      <project>
        <name>tanks</name>
      </project>
    </soundbanks>
    <networkUpdates>false</networkUpdates>
    <enableProfiler>false</enableProfiler>
    <allowUnload>false</allowUnload>
    <maxSpeed>100.0</maxSpeed>
  </soundMgr>
  <loggerID>111</loggerID>
</engine_config.xml>
'''


def _offh_autopatch_engine_and_prefs():
	"""Auto-patch engine_config.xml (in res_mods/0.8.2 and res/, handling both binary
	PackedSection 0x62A14E45 and plain-text XML), patch preferences.xml, and apply
	the optimized values LIVE into WorldOfTanks.exe memory on the current run."""
	if getattr(_offh_autopatch_engine_and_prefs, '_done', False):
		return
	_offh_autopatch_engine_and_prefs._done = True
	import os, re
	patched_files = []
	checked_files = []
	for _ec_path in (r'res_mods\0.8.2\engine_config.xml', r'res\engine_config.xml'):
		try:
			_ec_dir = os.path.dirname(_ec_path)
			if not os.path.isfile(_ec_path):
				if _ec_dir and os.path.isdir(_ec_dir):
					with open(_ec_path, 'wb') as _wf:
						_wf.write(_OPTIMIZED_ENGINE_CONFIG_XML.encode('utf-8'))
					patched_files.append(_ec_path + ':created')
				continue
			checked_files.append(_ec_path)
			with open(_ec_path, 'rb') as _f:
				_raw = _f.read()
			# Check if file is binary BigWorld PackedSection (magic 0x62A14E45 -> 45 4e a1 62) or empty
			if len(_raw) < 32 or _raw[:4] == b'\x45\x4e\xa1\x62':
				_bak = _ec_path + '.bak_offh'
				if not os.path.exists(_bak) and len(_raw) > 0:
					try:
						with open(_bak, 'wb') as _bf:
							_bf.write(_raw)
					except Exception:
						pass
				with open(_ec_path, 'wb') as _wf:
					_wf.write(_OPTIMIZED_ENGINE_CONFIG_XML.encode('utf-8'))
				patched_files.append(_ec_path + ':unpacked_binary')
				continue
			_txt = _raw.decode('utf-8', errors='replace')
			_new = _txt
			# Ensure cacheEffects=true and useLatentOcclusionQueries=true inside <renderer>
			if '<cacheEffects>' in _new:
				_new = re.sub(r'<cacheEffects>\s*false\s*</cacheEffects>', '<cacheEffects>true</cacheEffects>', _new, flags=re.I)
			elif '<renderer>' in _new:
				_new = _new.replace('<renderer>', '<renderer>\n    <cacheEffects>true</cacheEffects>', 1)
			if '<useLatentOcclusionQueries>' in _new:
				_new = re.sub(r'<useLatentOcclusionQueries>\s*false\s*</useLatentOcclusionQueries>', '<useLatentOcclusionQueries>true</useLatentOcclusionQueries>', _new, flags=re.I)
			elif '</renderer>' in _new:
				_new = _new.replace('</renderer>', '    <useLatentOcclusionQueries>true</useLatentOcclusionQueries>\n  </renderer>', 1)
			# Replace any streamCacheSizeKB < 16384 (e.g. 1024, 2048, 4096, 8192) with 16384
			def _fix_stream_cache(m):
				try:
					if int(m.group(1)) < 16384:
						return '<streamCacheSizeKB>16384</streamCacheSizeKB>'
				except Exception:
					pass
				return m.group(0)
			_new = re.sub(r'<streamCacheSizeKB>\s*(\d+)\s*</streamCacheSizeKB>', _fix_stream_cache, _new)
			# Replace any maxFrameRate < 200 (e.g. 60, 100, 120, 125, 150) with 200
			def _fix_max_fps(m):
				try:
					if int(m.group(1)) < 200:
						return '<maxFrameRate>200</maxFrameRate>'
				except Exception:
					pass
				return m.group(0)
			_new = re.sub(r'<maxFrameRate>\s*(\d+)\s*</maxFrameRate>', _fix_max_fps, _new)
			# Replace softwareChannels > 64 and ADPCMCodecs > 64 with 64
			_new = re.sub(r'<softwareChannels>\s*(?:128|256)\s*</softwareChannels>', '<softwareChannels>64</softwareChannels>', _new)
			_new = re.sub(r'<ADPCMCodecs>\s*(?:128|256)\s*</ADPCMCodecs>', '<ADPCMCodecs>64</ADPCMCodecs>', _new)
			_new = re.sub(r'<hasDevelopmentAssertions>\s*true\s*</hasDevelopmentAssertions>', '<hasDevelopmentAssertions>false</hasDevelopmentAssertions>', _new, flags=re.I)
			_new = re.sub(r'<accessMonitor>\s*true\s*</accessMonitor>', '<accessMonitor>false</accessMonitor>', _new, flags=re.I)
			_new = re.sub(r'<enableProfiler>\s*true\s*</enableProfiler>', '<enableProfiler>false</enableProfiler>', _new, flags=re.I)
			if _new != _txt:
				_bak = _ec_path + '.bak_offh'
				if not os.path.exists(_bak):
					try:
						with open(_bak, 'wb') as _bf:
							_bf.write(_raw)
					except Exception:
						pass
				with open(_ec_path, 'wb') as _wf:
					_wf.write(_new.encode('utf-8'))
				patched_files.append(_ec_path + ':updated')
		except Exception:
			pass
	# Live in-memory application to WorldOfTanks.exe so even if the executable read an unpatched
	# engine_config.xml 100ms before Python initialized, the running process gets 16MB streamCacheSizeKB
	# and 200 maxFrameRate immediately without needing a restart!
	live_mem_applied = False
	try:
		import ctypes
		k32 = ctypes.WinDLL('kernel32')
		if not k32.IsBadReadPtr(ctypes.c_void_p(0x00524f62), 5) and not k32.IsBadReadPtr(ctypes.c_void_p(0x011682f8), 4):
			# Verify instruction at 0x00524f62 is 'mov ds:0x11682f8, eax' (a3 f8 82 16 01)
			op_bytes = (ctypes.c_ubyte * 5).from_address(0x00524f62)
			if tuple(op_bytes) == (0xa3, 0xf8, 0x82, 0x16, 0x01):
				cur_cache = ctypes.c_uint32.from_address(0x011682f8).value
				if cur_cache < (16384 << 10):
					ctypes.c_uint32.from_address(0x011682f8).value = (16384 << 10)
				live_mem_applied = True
	except Exception:
		pass
	try:
		import ResMgr
		ResMgr.purge('engine_config.xml', True)
	except Exception:
		pass
	try:
		import BigWorld
		if not _is_worker_mode():
			for _w_fps in ('Client Settings/maxFrameRate', 'maxFrameRate', 'Render/maxFrameRate'):
				try:
					BigWorld.setWatcher(_w_fps, 200)
				except Exception:
					pass
	except Exception:
		pass
	try:
		import BigWorld
		_pref_path = None
		if hasattr(BigWorld, 'wg_getPreferencesFilePath'):
			_pref_path = BigWorld.wg_getPreferencesFilePath()
		if not _pref_path:
			_appdata = os.environ.get('APPDATA', '')
			if _appdata:
				_pref_path = os.path.join(_appdata, 'Wargaming.net', 'WorldOfTanks', 'preferences.xml')
		if _pref_path and os.path.isfile(_pref_path):
			with open(_pref_path, 'rb') as _pf:
				_praw = _pf.read()
			_ptxt = _praw.decode('utf-8', errors='replace')
			_pnew = _ptxt
			if re.search(r'<waitVSync>\s*false\s*</waitVSync>', _pnew, flags=re.I) and re.search(r'<tripleBuffering>\s*true\s*</tripleBuffering>', _pnew, flags=re.I):
				_pnew = re.sub(r'<tripleBuffering>\s*true\s*</tripleBuffering>', '<tripleBuffering>\tfalse\t</tripleBuffering>', _pnew, flags=re.I)
			if _pnew != _ptxt:
				with open(_pref_path, 'wb') as _pwf:
					_pwf.write(_pnew.encode('utf-8'))
				patched_files.append('preferences.xml:tripleBuffering=false')
	except Exception:
		pass
	try:
		from gui.mods.offhangar.logging import LOG_NOTE
		LOG_NOTE('AUTOPATCH: engine_config checked=%s patched=%s live_streamCache16MB=%s' % (
			','.join(checked_files) if checked_files else 'none',
			','.join(patched_files) if patched_files else 'already_optimal',
			live_mem_applied))
	except Exception:
		pass


def _offh_optimize_native_subengines():
	"""Offload native C++ sub-engine worker threads (umbraob32.dll APIWorker occlusion
	culling, fmodex.dll / fmod_event_net.dll audio mixer & streamer, vivoxsdk.dll)
	off Physical Core 0 onto secondary CPU cores so WorldOfTanks.exe's main D3D9/Python
	thread has uncontested L1/L2 cache and execution units on Core 0."""
	if _is_worker_mode():
		return
	try:
		import ctypes
		from ctypes import wintypes
		kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
		hProc = kernel32.GetCurrentProcess()
		proc_mask = ctypes.c_size_t(0)
		sys_mask = ctypes.c_size_t(0)
		kernel32.GetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
		kernel32.GetProcessAffinityMask.restype = wintypes.BOOL
		if not kernel32.GetProcessAffinityMask(hProc, ctypes.byref(proc_mask), ctypes.byref(sys_mask)):
			return
		smask = int(sys_mask.value)
		if smask <= 1:
			return
		# Count logical CPUs in sys_mask
		n_cpus = bin(smask).count('1')
		if n_cpus < 4:
			return
		# Reserve Physical Core 0 (bits 0..1 on >=6 thread SMT CPUs, or bit 0 on 4-core CPUs)
		# for the main D3D9 + BigWorld + Python thread, and push all background sub-engine
		# threads (umbraob32.dll, fmodex.dll, vivoxsdk.dll) onto secondary cores!
		core0_reserve = 0x3 if (n_cpus >= 6 and (smask & ~0x3) != 0) else 0x1
		worker_mask = smask & ~core0_reserve
		if worker_mask == 0:
			worker_mask = smask & ~0x1
		kernel32.SetThreadAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
		kernel32.SetThreadAffinityMask.restype = ctypes.c_size_t
		kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
		kernel32.SetThreadPriority.restype = wintypes.BOOL
		kernel32.SetThreadPriorityBoost.argtypes = [wintypes.HANDLE, wintypes.BOOL]
		kernel32.SetThreadPriorityBoost.restype = wintypes.BOOL
		kernel32.GetModuleHandleA.argtypes = [ctypes.c_char_p]
		kernel32.GetModuleHandleA.restype = ctypes.c_void_p
		kernel32.IsBadReadPtr.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
		kernel32.IsBadReadPtr.restype = wintypes.BOOL

		# 1) Pin main thread to Logical CPU 0 with THREAD_PRIORITY_HIGHEST (2)
		hMain = kernel32.GetCurrentThread()
		kernel32.SetThreadAffinityMask(hMain, 0x1)
		kernel32.SetThreadPriority(hMain, 2)
		kernel32.SetThreadPriorityBoost(hMain, False)

		# 2) Direct Umbra 3.x Occlusion Booster (umbraob32.dll) APIWorker thread offload:
		# In Umbra::OB::Library::init (RVA 0x3a860), Umbra stores its APIWorker thread HANDLE
		# at hUmbra + 0x9955c and sets byte flag hUmbra + 0x99560 = 1.
		umbra_pinned = 0
		hUmbra = kernel32.GetModuleHandleA(b'umbraob32.dll')
		if hUmbra:
			umbra_base = int(hUmbra)
			if not kernel32.IsBadReadPtr(ctypes.c_void_p(umbra_base + 0x9955c), 8):
				active_flag = ctypes.c_ubyte.from_address(umbra_base + 0x99560).value
				hUmbraWorker = ctypes.c_void_p.from_address(umbra_base + 0x9955c).value
				if active_flag == 1 and hUmbraWorker and int(hUmbraWorker) not in (0, 0xFFFFFFFF):
					if kernel32.SetThreadAffinityMask(wintypes.HANDLE(hUmbraWorker), worker_mask):
						kernel32.SetThreadPriority(wintypes.HANDLE(hUmbraWorker), 1) # THREAD_PRIORITY_ABOVE_NORMAL
						kernel32.SetThreadPriorityBoost(wintypes.HANDLE(hUmbraWorker), False)
						umbra_pinned += 1

		# 3) Enumerate process threads via Toolhelp32 + NtQueryInformationThread(ThreadQuerySetWin32StartAddress=9)
		# to offload any threads owned by umbraob32.dll, fmodex.dll, fmod_event_net.dll, and vivoxsdk.dll.
		ntdll = ctypes.WinDLL('ntdll')
		ntdll.NtQueryInformationThread.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
		ntdll.NtQueryInformationThread.restype = ctypes.c_long

		mod_ranges = []
		for mname, msize, mprio in (
			(b'umbraob32.dll', 0xA1000, 1),
			(b'fmodex.dll', 0x1B0000, 1),
			(b'fmod_event_net.dll', 0x90000, 0),
			(b'vivoxsdk.dll', 0x450000, -2),
			(b'ortp.dll', 0x50000, -2),
		):
			hMod = kernel32.GetModuleHandleA(mname)
			if hMod:
				mod_ranges.append((mname, int(hMod), int(hMod) + msize, mprio))

		class THREADENTRY32(ctypes.Structure):
			_fields_ = [
				('dwSize', wintypes.DWORD),
				('cntUsage', wintypes.DWORD),
				('th32ThreadID', wintypes.DWORD),
				('th32OwnerProcessID', wintypes.DWORD),
				('tpBasePri', ctypes.c_long),
				('tpDeltaPri', ctypes.c_long),
				('dwFlags', wintypes.DWORD),
			]

		cur_pid = kernel32.GetCurrentProcessId()
		cur_tid = kernel32.GetCurrentThreadId()
		hSnap = kernel32.CreateToolhelp32Snapshot(0x00000004, 0) # TH32CS_SNAPTHREAD
		fmod_pinned = 0
		if hSnap and int(hSnap) != -1 and int(hSnap) != 0xFFFFFFFF:
			try:
				te = THREADENTRY32()
				te.dwSize = ctypes.sizeof(THREADENTRY32)
				if kernel32.Thread32First(hSnap, ctypes.byref(te)):
					while True:
						if te.th32OwnerProcessID == cur_pid and te.th32ThreadID != cur_tid:
							# THREAD_SET_INFORMATION (0x20) | THREAD_QUERY_INFORMATION (0x40)
							hTh = kernel32.OpenThread(0x0060, False, te.th32ThreadID)
							if hTh:
								try:
									start_addr = ctypes.c_void_p(0)
									if ntdll.NtQueryInformationThread(hTh, 9, ctypes.byref(start_addr), ctypes.sizeof(start_addr), None) == 0:
										sa = int(start_addr.value or 0)
										for mname, mlo, mhi, mprio in mod_ranges:
											if mlo <= sa < mhi:
												kernel32.SetThreadAffinityMask(hTh, worker_mask)
												kernel32.SetThreadPriority(hTh, mprio)
												if mname == b'umbraob32.dll':
													umbra_pinned += 1
												elif mname.startswith(b'fmod'):
													fmod_pinned += 1
												break
								finally:
									kernel32.CloseHandle(hTh)
						if not kernel32.Thread32Next(hSnap, ctypes.byref(te)):
							break
			finally:
				kernel32.CloseHandle(hSnap)
		from gui.mods.offhangar.logging import LOG_NOTE
		LOG_NOTE('SUBENGINE_OPT main_core=0x1 worker_mask=0x%x umbra_threads=%d fmod_threads=%d' % (
			worker_mask, umbra_pinned, fmod_pinned))
	except Exception:
		pass


try:
	from gui.Scaleform.StartGameVideo import StartGameVideo
	@override(StartGameVideo, 'populateUI')
	def StartGameVideo_populateUI(baseFunc, baseSelf, proxy):
		if _is_worker_mode():
			_worker_diag('StartGameVideo.populateUI skipped in worker mode')
			baseSelf.movieFiles = []
			try:
				import BigWorld
				BigWorld.wg_setMovieSoundMuted(True)
				BigWorld.worldDrawEnabled(False)
			except Exception:
				pass
			try:
				baseSelf.onMovieDone(0, 0)
				return
			except Exception:
				pass
		baseFunc(baseSelf, proxy)
except Exception:
	LOG_CURRENT_EXCEPTION()

try:
	import MusicController
	@override(MusicController.MusicController, 'play')
	def MusicController_play(baseFunc, baseSelf, eventId):
		if _is_worker_mode():
			return
		baseFunc(baseSelf, eventId)
except Exception:
	LOG_CURRENT_EXCEPTION()

try:
	import SoundGroups
	@override(SoundGroups.SoundGroups, 'loadSounds')
	def SoundGroups_loadSounds(baseFunc, baseSelf, groupName):
		if _is_worker_mode():
			return
		baseFunc(baseSelf, groupName)

	@override(SoundGroups.SoundGroups, 'applyPreferences')
	def SoundGroups_applyPreferences(baseFunc, baseSelf):
		if _is_worker_mode():
			try:
				import FMOD
				FMOD.setMasterVolume(0.0)
			except Exception:
				pass
			return
		baseFunc(baseSelf)
except Exception:
	LOG_CURRENT_EXCEPTION()


def _fix_fullscreen_cursor_focus():
	if _is_worker_mode():
		return
	try:
		import ctypes
		user32 = getattr(getattr(ctypes, 'windll', None), 'user32', None)
		if user32 is not None:
			hwnd = user32.FindWindowA('World of Tanks', None)
			if not hwnd:
				hwnd = user32.FindWindowA(None, 'World of Tanks')
			if not hwnd:
				hwnd = user32.FindWindowW(None, u'World of Tanks')
			if not hwnd:
				hwnd = user32.FindWindowA(None, 'W.o.T. Client')
			if not hwnd:
				hwnd = user32.FindWindowW(None, u'W.o.T. Client')
			if not hwnd:
				hwnd = user32.GetForegroundWindow()
			if hwnd:
				try:
					user32.BringWindowToTop(hwnd)
				except Exception:
					pass
				try:
					user32.SetForegroundWindow(hwnd)
				except Exception:
					pass
				try:
					user32.SetActiveWindow(hwnd)
				except Exception:
					pass
				try:
					user32.SetFocus(hwnd)
				except Exception:
					pass
				try:
					# Win32 cursor sync: micro relative move forces Windows & DirectInput cursor track
					user32.mouse_event(0x0001, 1, 0, 0, 0)
					user32.mouse_event(0x0001, -1, 0, 0, 0)
				except Exception:
					pass
	except Exception:
		pass
	try:
		import GUI
		mc = getattr(GUI, 'mcursor', None)
		if callable(mc):
			c = mc()
			if c:
				c.visible = False
				if hasattr(c, 'clipped'):
					c.clipped = False
	except Exception:
		pass


if _is_worker_mode():
	LOG_NOTE('OFFHANGAR_WORKER_MODE active (authority_worker): headless and mute enabled')
	_apply_worker_mode()
else:
	_apply_client_mode()
	try:
		if os.path.exists('offline-worker.ready'):
			os.remove('offline-worker.ready')
	except Exception:
		pass
	try:
		from gui.mods.offhangar import paths
		_mpath = os.path.join(getattr(paths, 'USER_DIR', 'offhangar_user'), 'offline-worker.ready')
		if os.path.exists(_mpath):
			os.remove(_mpath)
	except Exception:
		pass


try:
	from gui.mods.offhangar.data import getOfflineShopItems, getOfflineShopCacheData
except ImportError:
	import gui.mods.offhangar.data as _data_mod
	getOfflineShopItems = getattr(_data_mod, 'getOfflineShopItems', None)
	getOfflineShopCacheData = getattr(_data_mod, 'getOfflineShopCacheData', None)

try:
	from gui.mods.offhangar.session_guards import install_game_session_guards
except ImportError:
	import gui.mods.offhangar.session_guards as _sg_mod
	install_game_session_guards = getattr(_sg_mod, 'install_game_session_guards', lambda: None)

try:
	from gui.mods.offhangar.offline_battle import start_offline_random_from_hangar
except ImportError:
	import gui.mods.offhangar.offline_battle as _ob_mod
	start_offline_random_from_hangar = getattr(_ob_mod, 'start_offline_random_from_hangar', lambda *a, **k: None)


Account.LOG_DEBUG = LOG_DEBUG
Account.LOG_NOTE = LOG_NOTE
Account.LOG_ERROR = LOG_ERROR

def _offh_is_ru_locale():
	try:
		from gui.mods.offhangar import cbt_profiles_and_locale as _cbt_pal_ver
		_fn = getattr(_cbt_pal_ver, 'is_russian_locale', None) or getattr(_cbt_pal_ver, 'is_ru_locale', None)
		if callable(_fn):
			return bool(_fn())
	except Exception:
		pass
	return True

def _offh_full_version():
	try:
		if _offh_is_ru_locale():
			return 'v.0.8.2 — Offline Battles 2.0 (ЗБТ)'
		return 'v.0.8.2 — Offline Battles 2.0 (CBT)'
	except Exception:
		return 'v.0.8.2 — Offline Battles 2.0 (ЗБТ)'

def _offh_short_version():
	try:
		if _offh_is_ru_locale():
			return 'v.0.8.2 — Offline Battles 2.0 (ЗБТ)'
		return 'v.0.8.2 — Offline Battles 2.0 (CBT)'
	except Exception:
		return 'v.0.8.2 — Offline Battles 2.0 (ЗБТ)'

try:
	import helpers, sys
	helpers.getFullClientVersion = _offh_full_version
	helpers.getClientVersion = _offh_short_version
	for _m_name, _m_obj in list(sys.modules.items()):
		if _m_obj is not None:
			if hasattr(_m_obj, 'getFullClientVersion'):
				try:
					setattr(_m_obj, 'getFullClientVersion', _offh_full_version)
				except Exception:
					pass
			if hasattr(_m_obj, 'getClientVersion'):
				try:
					setattr(_m_obj, 'getClientVersion', _offh_short_version)
				except Exception:
					pass
except Exception:
	pass

try:
	from helpers import i18n
	import sys as _sys_i18n
	if not getattr(i18n, '_offh_login_i18n_patched', False):
		_orig_i18n_makeString = i18n.makeString
		i18n._orig_makeString = _orig_i18n_makeString
		def _offh_i18n_makeString(key, *args, **kwargs):
			try:
				k_s = key.encode('utf-8') if isinstance(key, unicode) else str(key)
				if k_s in ('#menu:login/login', '#menu:login/email', 'menu:login/login', 'menu:login/email'):
					return 'Псевдоним' if _offh_is_ru_locale() else 'Nickname'
				if k_s in ('#menu:login/registrationLink', 'menu:login/registrationLink', '#menu:login/createAccount', '#menu:login/register'):
					return ''
				if k_s in ('#menu:login/savePwd', '#menu:login/rememberPwd', '#menu:login/rememberPassword', 'menu:login/rememberPassword'):
					res = _orig_i18n_makeString(key, *args, **kwargs)
					res_s = res.encode('utf-8') if isinstance(res, unicode) else str(res or '')
					if not res_s or res_s == k_s or res_s.endswith('savePwd') or res_s.endswith('rememberPwd'):
						return 'Сохранить пароль' if _offh_is_ru_locale() else 'Save password'
					return res
			except Exception:
				pass
			return _orig_i18n_makeString(key, *args, **kwargs)
		i18n.makeString = _offh_i18n_makeString
		for _m_name, _m_obj in list(_sys_i18n.modules.items()):
			if _m_obj is not None:
				try:
					if getattr(_m_obj, 'makeString', None) is _orig_i18n_makeString:
						setattr(_m_obj, 'makeString', _offh_i18n_makeString)
				except Exception:
					pass
		i18n._offh_login_i18n_patched = True
except Exception:
	pass

try:
	from gui import GUI_SETTINGS
	if hasattr(GUI_SETTINGS, '_GuiSettings__settings') and isinstance(GUI_SETTINGS._GuiSettings__settings, dict):
		GUI_SETTINGS._GuiSettings__settings['rememberPassVisible'] = True
		GUI_SETTINGS._GuiSettings__settings['registrationUrl'] = ''
		GUI_SETTINGS._GuiSettings__settings['clearLoginValue'] = False
except Exception:
	pass

def _apply_cbt_hosts(*a, **kw):
	try:
		_cbt_host_item = g_preDefinedHosts._makeHostItem('W.o.T OB2.0 CBT', 'W.o.T OB2.0 CBT', 'W.o.T OB2.0 CBT')
		_legacy_host_item = g_preDefinedHosts._makeHostItem('offline.loc', 'W.o.T OB2.0 CBT', 'W.o.T OB2.0 CBT')
		g_preDefinedHosts._hosts[:] = [_cbt_host_item]
		if hasattr(g_preDefinedHosts, '_urlMap') and isinstance(g_preDefinedHosts._urlMap, dict):
			g_preDefinedHosts._urlMap.clear()
			g_preDefinedHosts._urlMap['W.o.T OB2.0 CBT'] = 0
			g_preDefinedHosts._urlMap['offline.loc'] = 0
		if hasattr(g_preDefinedHosts, '_byName') and isinstance(g_preDefinedHosts._byName, dict):
			g_preDefinedHosts._byName.clear()
			g_preDefinedHosts._byName['W.o.T OB2.0 CBT'] = _cbt_host_item
		if hasattr(g_preDefinedHosts, '_byUrl') and isinstance(g_preDefinedHosts._byUrl, dict):
			g_preDefinedHosts._byUrl.clear()
			g_preDefinedHosts._byUrl['W.o.T OB2.0 CBT'] = _cbt_host_item
		g_preDefinedHosts.urlIterator = lambda url: None
		g_preDefinedHosts.autodetect = lambda: False
		g_preDefinedHosts.resetPing = lambda: None
		g_preDefinedHosts.ping = lambda *args, **kwargs: None
	except Exception:
		try:
			g_preDefinedHosts._hosts[:] = [g_preDefinedHosts._makeHostItem(OFFLINE_SERVER_ADDRESS, 'W.o.T OB2.0 CBT', 'W.o.T OB2.0 CBT')]
		except Exception:
			pass

_apply_cbt_hosts()
try:
	g_preDefinedHosts.readScriptConfig = _apply_cbt_hosts
except Exception:
	pass


class _OfflineArenaStub(object):
	class _VehicleTypeStub(object):
		def __init__(self):
			self.type = self
			self.tags = set()
			self.turretRotatorSpeed = 0.0
			self.circularVisionRadius = 0

		def __getattr__(self, name):
			return 0

	class _ArenaTypeStub(object):
		def __init__(self):
			self.id = 1
			self.name = ''
			self.description = ''
			self.weatherPresets = []
			self.geometryName = ''
			self.gameplayName = 'ctf'
			self.teamBasePositions = ({}, {})
			self.teamSpawnPoints = ([], [])
			self.controlPoint = None
			self.roundLength = 900
			self.winnerIfTimeout = 0
			self.umbraEnabled = 0
			self.boundingBox = ( (0,0), (1000, 1000) )
			self.defaultReverbPreset = ''
			self.waterTexScale = 0.5
			self.waterFreqX = 1.0
			self.waterFreqZ = 1.0
			self.minimap = None

	class _EventStub(object):
		def __init__(self):
			self.delegates = []

		def __iadd__(self, other):
			if other not in self.delegates:
				self.delegates.append(other)
			return self

		def __isub__(self, other):
			if other in self.delegates:
				self.delegates.remove(other)
			return self

		def __call__(self, *args, **kwargs):
			for delegate in list(self.delegates):
				try:
					delegate(*args, **kwargs)
				except Exception:
					pass

	def __setattr__(self, name, value):
		# ``arena.onFoo += handler`` is a read-modify-write operation.  Python
		# therefore writes the EventStub returned by __getattr__ back onto the
		# arena after __iadd__.  If that becomes a normal instance attribute it
		# escapes _event_stubs, so the persistent offline arena retains one new
		# set of battle UI handlers (and all 30 vehicles they close over) every
		# round.  Keep dynamic events in the registry on both the read and write
		# sides so one cleanup path owns every delegate.
		if name.startswith('on') and isinstance(value, self._EventStub):
			events = self.__dict__.get('_event_stubs')
			if events is not None:
				events[name] = value
				return
		self.__dict__[name] = value

	def __init__(self):
		self.vehicles = {}
		self.statistics = {}
		self.arenaType = self._ArenaTypeStub()
		self.guiType = 1 # constants.ARENA_GUI_TYPE.RANDOM
		self.gameMode = 0 # constants.ARENA_GAME_MODE.CTF
		self.guiType = 0
		self.bonusType = 0
		self.extraData = {}
		self._event_stubs = {}
		self.period = 1
		self.periodLength = 600
		self.periodEndTime = 0.0

	def __getattr__(self, name):
		if name == 'periodEndTime':
			return self.__dict__.get('periodEndTime', 0.0)
		if name.startswith('on'):
			if name not in self._event_stubs:
				self._event_stubs[name] = self._EventStub()
			return self._event_stubs[name]
		return 0


class _OfflineVehicleStub(object):
	class _TypeDescriptorStub(object):
		def __init__(self):
			self.type = _OfflineArenaStub._VehicleTypeStub()

		def __getattr__(self, name):
			return 0

	def __init__(self):
		self.typeDescriptor = self._TypeDescriptorStub()
		self.id = 0


class _OfflineEvent(object):
	def __init__(self):
		self._listeners = []

	def __iadd__(self, other):
		if callable(other) and other not in self._listeners:
			self._listeners.append(other)
		return self

	def __isub__(self, other):
		if other in self._listeners:
			self._listeners.remove(other)
		return self

	def clear(self):
		del self._listeners[:]

	def __call__(self, *args, **kwargs):
		for fn in list(self._listeners):
			try:
				fn(*args, **kwargs)
			except Exception:
				pass


def _ensure_postmortem_event(obj):
	if obj is None:
		return
	try:
		cur = getattr(obj, 'onPostmortemVehicleChanged', None)
		if cur is None:
			obj.onPostmortemVehicleChanged = _OfflineEvent()
	except Exception:
		LOG_CURRENT_EXCEPTION()


def fini():
	os.kill(os.getpid(), signal.SIGTERM)

@override(Shop, '__onSyncComplete')
def Shop__onSyncComplete(baseFunc, baseSelf, syncID, data):
	_in_rev = data.get('rev', 1) if isinstance(data, dict) else 1
	_in_prev_rev = data.get('prevRev', _in_rev) if isinstance(data, dict) else _in_rev
	data = getOfflineShopCacheData(_in_rev, for_sync=True)
	data['prevRev'] = _in_prev_rev
	try:
		baseFunc(baseSelf, syncID, data)
	except Exception:
		try:
			baseSelf._Shop__cache = getOfflineShopCacheData(_in_rev, for_sync=False)
			baseSelf._Shop__isSynchronizing = False
		except Exception:
			pass
	try:
		if not getattr(baseSelf, '_Shop__cache', None) or not baseSelf._Shop__cache.get('items'):
			baseSelf._Shop__cache = getOfflineShopCacheData(_in_rev, for_sync=False)
		baseSelf._Shop__isSynchronizing = False
	except Exception:
		pass

@override(_TimeCorrector, 'serverRegionalTime')
def TimeCorrector_serverRegionalTime(baseFunc, baseSelf):
	regionalSecondsOffset = 0
	try:
		serverRegionalSettings = OFFLINE_SERVER_SETTINGS['regional_settings']
		regionalSecondsOffset = serverRegionalSettings['starting_time_of_a_new_day']
	except Exception:
		LOG_CURRENT_EXCEPTION()
	return _g_instance.serverUTCTime + regionalSecondsOffset

@override(_GameSessionController, 'isSessionStartedThisDay')
def GameSessionController_isSessionStartedThisDay(baseFunc, baseSelf):
	serverRegionalSettings = OFFLINE_SERVER_SETTINGS['regional_settings']
	return int(_g_instance.serverRegionalTime) / 86400 == int(baseSelf._GameSessionController__sessionStartedAt + serverRegionalSettings['starting_time_of_a_new_day']) / 86400

@override(_GameSessionController, '_getWeeklyPlayHours')
def GameSessionController_getWeeklyPlayHours(baseFunc, baseSelf):
	serverRegionalSettings = OFFLINE_SERVER_SETTINGS['regional_settings']
	weekDaysCount = account_shared.currentWeekPlayDaysCount(_g_instance.serverUTCTime, serverRegionalSettings['starting_time_of_a_new_day'], serverRegionalSettings['starting_day_of_a_new_weak'])
	return baseSelf._getDailyPlayHours() + sum(baseSelf._GameSessionController__stats.dailyPlayHours[1:weekDaysCount])

if Vehicle is not None and hasattr(Vehicle, 'canSell'):
	@override(Vehicle, 'canSell')
	def Vehicle_canSell(baseFunc, baseSelf):
		return BigWorld.player().isOffline or baseFunc(baseSelf)

def _set_login_ui_error(login_view, msg_u, code=0):
	if login_view is None:
		return
	try:
		msg_s = msg_u.encode('utf-8') if isinstance(msg_u, unicode) else str(msg_u)
	except Exception:
		msg_s = str(msg_u)
	try:
		if hasattr(login_view, 'setErrorMessage'):
			login_view.setErrorMessage(msg_s, int(code))
			return
	except Exception:
		pass
	try:
		login_view.call('login.setErrorMessage', [msg_s, int(code)])
	except Exception:
		pass


if Login is not None:
	try:
		Login.onRegister = lambda self, *a, **kw: None
	except Exception:
		pass

if Login is not None and hasattr(Login, 'onSetRememberPassword'):
	@override(Login, 'onSetRememberPassword')
	def Login_onSetRememberPassword(baseFunc, baseSelf, *args, **kwargs):
		try:
			remember = bool(args[-1]) if args else bool(kwargs.get('remember', True))
			baseSelf._Login__rememberPwd = remember
			from gui.mods.offhangar import cbt_profiles_and_locale as _cbt_pal
			_reg = _cbt_pal.load_profiles_registry()
			_reg['remember_pwd'] = remember
			if not remember:
				_reg['remembered_token'] = ''
			_cbt_pal.save_profiles_registry(_reg)
		except Exception:
			pass
		try:
			return baseFunc(baseSelf, *args, **kwargs)
		except TypeError:
			try:
				return baseFunc(baseSelf, bool(args[-1]) if args else True)
			except Exception:
				return None

if Login is not None and hasattr(Login, 'populateUI'):
	@override(Login, 'populateUI')
	def Login_populateUI(baseFunc, baseSelf, proxy):
		_worker_diag('Login.populateUI called')
		_apply_cbt_hosts()
		try:
			from gui import GUI_SETTINGS
			if hasattr(GUI_SETTINGS, '_GuiSettings__settings') and isinstance(GUI_SETTINGS._GuiSettings__settings, dict):
				GUI_SETTINGS._GuiSettings__settings['rememberPassVisible'] = True
				GUI_SETTINGS._GuiSettings__settings['registrationUrl'] = ''
				GUI_SETTINGS._GuiSettings__settings['clearLoginValue'] = False
		except Exception:
			pass
		# Pre-populate login_info in Settings so the Login box shows the clean player pseudonym
		# (never '@offline.loc') and the Server dropdown defaults to 'W.o.T OB2.0 CBT'.
		_last_nick = ''
		_rem_pwd = True
		_has_saved_token = False
		try:
			from gui.mods.offhangar import cbt_profiles_and_locale as _cbt_pal
			_reg = _cbt_pal.load_profiles_registry()
			_last_nick = str(_reg.get('last_nickname') or '')
			_rem_pwd = bool(_reg.get('remember_pwd', True))
			_has_saved_token = bool(_rem_pwd and _reg.get('remembered_token'))
			try:
				baseSelf._Login__isRememberPwdShown = True
				baseSelf._Login__rememberPwd = _rem_pwd
			except Exception:
				pass
			import Settings
			_up = getattr(getattr(Settings, 'g_instance', None), 'userPrefs', None)
			if _up is not None and _up.has_key(Settings.KEY_LOGIN_INFO):
				_li = _up[Settings.KEY_LOGIN_INFO]
				_li.writeString('user', _last_nick)
				_li.writeString('server', 'W.o.T OB2.0 CBT')
				_li.writeBool('rememberPwd', _rem_pwd)
				if not _has_saved_token:
					_li.writeString('token2', '')
		except Exception:
			pass

		# Auto-connect ONLY in headless authority_worker mode or when a CLI .wotreplay is being opened directly
		_direct_auto = _is_worker_mode()
		if not _direct_auto:
			try:
				from gui.mods.offhangar import replay_machinima as _rm_l
				if _rm_l.get_replay_manager().has_pending_cli_autostart():
					_direct_auto = True
			except Exception:
				pass
			try:
				import BattleReplay as _br_l
				if getattr(BigWorld, '_offh_vanilla_wotreplay_path', None) or (getattr(_br_l, 'g_replayCtrl', None) is not None and getattr(_br_l.g_replayCtrl, 'isPlaying', False)):
					LOG_NOTE('Login_populate: native BattleReplay is playing -> skipping offline login completely')
					return
			except Exception:
				pass

		# Suppress stock auto-login inside baseFunc so the player sees the interactive Login screen
		_orig_cm_connect = connectionManager.connect
		if not _direct_auto:
			connectionManager.connect = lambda *a, **kw: None
		try:
			baseFunc(baseSelf, proxy)
		finally:
			connectionManager.connect = _orig_cm_connect

		_fix_fullscreen_cursor_focus()
		BigWorld.callback(0.1, _fix_fullscreen_cursor_focus)

		if _is_worker_mode():
			_apply_worker_mode()
		if _direct_auto:
			_worker_diag('Login direct auto-connect (worker/replay)')
			_orig_cm_connect('W.o.T OB2.0 CBT', OFFLINE_LOGIN, OFFLINE_PWD, False, False, False)
			return

		try:
			_is_ru = _offh_is_ru_locale()
			_ver_str = _offh_full_version()
			try:
				baseSelf._Login__isRememberPwdShown = True
				baseSelf._Login__rememberPwd = _rem_pwd
			except Exception:
				pass
			try:
				baseSelf.call('login.setVersion', [_ver_str])
			except Exception:
				pass
			try:
				baseSelf.call('login.setDefaultValues', [
					_last_nick,
					'********' if _has_saved_token else '',
					_rem_pwd,
					True,
					False,
					False
				])
			except Exception:
				pass

			def _apply_login_flash_overrides():
				try:
					baseSelf.call('login.setVersion', [_ver_str])
				except Exception:
					pass
				try:
					_movie = getattr(baseSelf, 'movie', None)
					if _movie is not None:
						_lbl_nick = 'Псевдоним:' if _is_ru else 'Nickname:'
						for _vp in (
							'_root.main.form.register_mc._visible',
							'_root.main.register_mc._visible',
							'_root.main.form.registrationLink._visible',
							'_root.main.registrationLink._visible',
							'_root.main.form.createAccountBtn._visible',
							'_root.main.createAccountBtn._visible',
							'_root.form.register_mc._visible',
							'_root.form.registrationLink._visible',
						):
							try:
								_movie.setVariable(_vp, False)
							except Exception:
								pass
						for _lp in (
							'_root.main.form.login_label.text',
							'_root.main.form.loginLabel.text',
							'_root.form.login_label.text',
						):
							try:
								_movie.setVariable(_lp, _lbl_nick)
							except Exception:
								pass
				except Exception:
					pass

			_apply_login_flash_overrides()
			BigWorld.callback(0.1, _apply_login_flash_overrides)
			BigWorld.callback(0.35, _apply_login_flash_overrides)

			if _last_nick:
				_hint = _cbt_pal.tr('login_hint_existing', nick=unicode(_last_nick, 'utf-8', 'replace'))
			else:
				_hint = _cbt_pal.tr('login_hint_new')
			BigWorld.callback(0.15, lambda: _set_login_ui_error(baseSelf, _hint, 0))
		except Exception:
			pass


if Login is not None and hasattr(Login, 'onLogin'):
	@override(Login, 'onLogin')
	def Login_onLogin(baseFunc, baseSelf, *args, **kwargs):
		try:
			import re as _re_login
			from gui.mods.offhangar import cbt_profiles_and_locale as _cbt_pal
			_clean_args = list(args)
			while _clean_args:
				_a0 = _clean_args[0]
				if isinstance(_a0, basestring) and _a0.strip().lower() in ('login.onlogin', 'onlogin'):
					_clean_args.pop(0)
					continue
				if isinstance(_a0, (int, long, float)) and not isinstance(_a0, bool):
					_clean_args.pop(0)
					continue
				if isinstance(_a0, basestring) and _re_login.match(r'^\d+\.\d+$', _a0.strip()):
					_clean_args.pop(0)
					continue
				break
			user = _clean_args[0] if len(_clean_args) > 0 else ''
			password = _clean_args[1] if len(_clean_args) > 1 else ''
			host = _clean_args[2] if len(_clean_args) > 2 else 'W.o.T OB2.0 CBT'
			_rem_pwd = bool(getattr(baseSelf, '_Login__rememberPwd', True))
			_pwd_in = password
			if _pwd_in == '********':
				_pwd_in = '__SAVED_CBT_TOKEN__'
			if not _pwd_in:
				_pwd_in = '1'
			try:
				ok, prof, msg_u = _cbt_pal.authenticate_or_create_profile(user, _pwd_in, remember_pwd=_rem_pwd)
			except Exception as _profile_err:
				import traceback as _profile_tb
				LOG_ERROR('CBT_PROFILE activation/auth failed: %s\\n%s' % (
					str(_profile_err), _profile_tb.format_exc()))
				_set_login_ui_error(baseSelf,
					u'Profile data could not be loaded. Please check python.log.', 2)
				return
			if not ok or not isinstance(prof, dict):
				_err_code = 1 if (not user or not str(user).strip()) else 2
				_set_login_ui_error(baseSelf, msg_u, _err_code)
				LOG_NOTE('CBT_LOGIN rejected user=%r: %s' % (user, msg_u.encode('utf-8') if isinstance(msg_u, unicode) else str(msg_u)))
				return
			_nick = str(prof.get('nickname') or 'Player')
			LOG_NOTE('CBT_LOGIN authenticated nick=%s dbid=%s new=%s remember=%s server=W.o.T OB2.0 CBT' % (
				_nick, str(prof.get('dbid')), str(prof.get('is_new_profile')), str(_rem_pwd)))
			# Persist clean player nickname & rememberPwd into Settings.KEY_LOGIN_INFO so preferences.xml remembers it
			try:
				import Settings
				_up = getattr(getattr(Settings, 'g_instance', None), 'userPrefs', None)
				if _up is not None and _up.has_key(Settings.KEY_LOGIN_INFO):
					_li = _up[Settings.KEY_LOGIN_INFO]
					_li.writeString('user', _nick)
					_li.writeString('server', 'W.o.T OB2.0 CBT')
					_li.writeBool('rememberPwd', _rem_pwd)
			except Exception:
				pass
			try:
				BigWorld.flushPythonLog()
			except Exception:
				pass
			def _do_cbt_connect():
				try:
					connectionManager.connect(OFFLINE_SERVER_ADDRESS, OFFLINE_LOGIN, OFFLINE_PWD, False, False, False)
				except Exception as _ce_inner:
					LOG_ERROR('CBT_LOGIN deferred connect err: %s' % str(_ce_inner))
			BigWorld.callback(0.02, _do_cbt_connect)
		except Exception as _le:
			import traceback as _login_tb
			LOG_ERROR('CBT_LOGIN exception: %s\\n%s' % (str(_le), _login_tb.format_exc()))
			_set_login_ui_error(baseSelf, u'Profile data could not be loaded. Please check python.log.', 2)

if CommonPage is not None and hasattr(CommonPage, 'setClanInfo'):
	@override(CommonPage, 'setClanInfo')
	def CommonPage_setClanInfo(baseFunc, baseSelf, clanInfo):
		# Header name: dynamically read active CBT profile nickname
		try:
			from gui.mods.offhangar import cbt_profiles_and_locale as _cbt_pal
			_nick = _cbt_pal.get_active_nickname()
		except Exception:
			_nick = OFFLINE_NICKNAME
		_nm = str(_nick)
		if clanInfo is not None and len(clanInfo) > 1:
			_nm = '%s [%s]' % (_nm, clanInfo[1])
		baseSelf.call('common.nameResponse', [_nm, False, clanInfo is not None])

@override(Account.PlayerAccount, 'onBecomePlayer')
def Account_onBecomePlayer_nickname(baseFunc, baseSelf):
	baseFunc(baseSelf)
	# Hangar header shows the raw login value otherwise: force the active profile nickname.
	try:
		from gui.mods.offhangar import cbt_profiles_and_locale as _cbt_pal
		baseSelf.name = _cbt_pal.get_active_nickname()
	except Exception:
		pass

@override(Account.PlayerAccount, '__init__')
def Account_init(baseFunc, baseSelf):
	baseSelf.isOffline = not getattr(baseSelf, 'name', '')
	try:
		from gui.mods.offhangar import cbt_profiles_and_locale as _cbt_pal
		_active_nick = _cbt_pal.get_active_nickname()
	except Exception:
		_active_nick = OFFLINE_NICKNAME
	try:
		baseSelf.name = _active_nick
	except Exception:
		pass
	if baseSelf.isOffline:
		baseSelf.fakeServer = FakeServer()
		baseSelf.name = _active_nick
		baseSelf.serverSettings = OFFLINE_SERVER_SETTINGS
		baseSelf._offhangar_arena = _OfflineArenaStub()
		baseSelf._offhangar_vehicle_stub = _OfflineVehicleStub()
		baseSelf._offhangar_allow_world_clear = False
		baseSelf._offline_allow_become_non_player = False
		baseSelf._offhangar_stats501_streak = 0

	baseFunc(baseSelf)

	if baseSelf.isOffline:
		BigWorld.player(baseSelf)

@override(Account.PlayerAccount, '__getattribute__')
def Account_getattribute(baseFunc, baseSelf, name):
	if name == 'vehicle' and baseSelf.isOffline:
		mock = getattr(baseSelf, '_offhangar_mock_veh', None)
		if mock is not None:
			return mock
		return baseFunc(baseSelf, name)
	if name == 'team' and baseSelf.isOffline:
		return getattr(baseSelf, '_offhangar_team', 1)
	if name == 'inputHandler' and baseSelf.isOffline:
		orig_ih = baseFunc(baseSelf, name)
		if orig_ih and not hasattr(orig_ih, 'onCameraChanged'):
			import Event
			orig_ih.onCameraChanged = Event.Event()
			orig_ih.onPostmortemVehicleChanged = Event.Event()
		return orig_ih
	if name in ('arenaTypeID', 'arenaUniqueID') and baseSelf.isOffline:
		try:
			return baseSelf.arena.arenaType.id
		except:
			return 1
	if name == 'setForcedGuiControlMode' and baseSelf.isOffline:
		return lambda *args, **kwargs: None
	if name == 'onMinimapCellClicked' and baseSelf.isOffline:
		def _offh_onMinimapCellClicked(cellIdx):
			try:
				import time as _t_mc
				_now_mc = _t_mc.time()
				_last_mc = float(getattr(baseSelf, '_offh_last_minimap_click_at', 0.0) or 0.0)
				if (_now_mc - _last_mc) < 0.85:
					return
				baseSelf._offh_last_minimap_click_at = _now_mc
				from chat_shared import CHAT_COMMANDS
				from ChatManager import chatManager
				from ClientChat import ClientChat
				channelID = getattr(chatManager, 'battleTeamChannelID', 1) or 1
				cmd = getattr(CHAT_COMMANDS, 'ATTENTIONTOCELL', None)
				if cmd is not None:
					ClientChat.sendChannelChatCommand(baseSelf, channelID, cmd, int16Arg=int(cellIdx))
			except Exception:
				pass
		return _offh_onMinimapCellClicked
	if name == 'onChatShortcut' and baseSelf.isOffline:
		def _offh_onChatShortcut(cmdName):
			try:
				from chat_shared import CHAT_COMMANDS
				from ChatManager import chatManager
				from ClientChat import ClientChat
				cmd = getattr(CHAT_COMMANDS, cmdName, None)
				if cmd is not None:
					channelID = getattr(chatManager, 'battleTeamChannelID', 1) or 1
					ClientChat.sendChannelChatCommand(baseSelf, channelID, cmd)
			except Exception:
				pass
		return _offh_onChatShortcut
	if name == 'onChatShortcutTarget' and baseSelf.isOffline:
		def _offh_onChatShortcutTarget(cmdName, targetId):
			try:
				from chat_shared import CHAT_COMMANDS
				from ChatManager import chatManager
				from ClientChat import ClientChat
				cmd = getattr(CHAT_COMMANDS, cmdName, None)
				if cmd is not None and targetId:
					channelID = getattr(chatManager, 'battleTeamChannelID', 1) or 1
					ClientChat.sendChannelChatCommand(baseSelf, channelID, cmd, int64Arg=int(targetId))
			except Exception:
				pass
		return _offh_onChatShortcutTarget
	if name == 'playerVehicleID' and baseSelf.isOffline:
		if getattr(baseSelf, '_offh_spoof_pvid_for_pm', False):
			return -999
		ctx = getattr(baseSelf, '_offhangar_battle_ctx', None) or {}
		vid = ctx.get('playerVehicleID', 0)
		if vid:
			return vid
		if getattr(baseSelf, '_offhangar_player_vehicle_id', 0):
			return baseSelf._offhangar_player_vehicle_id
		try:
			from CurrentVehicle import g_currentVehicle
			vobj = getattr(g_currentVehicle, 'vehicle', None) or getattr(g_currentVehicle, '_CurrentVehicle__vehicle', None) or getattr(g_currentVehicle, 'item', None)
			if vobj is not None:
				return getattr(vobj, 'inventoryId', 0) or getattr(vobj, 'invID', 0)
		except Exception:
			LOG_CURRENT_EXCEPTION()
		return 0
	if name == 'vehicleTypeDescriptor' and baseSelf.isOffline:
		# In battle, return the REAL battle-tank descriptor (set by offline_battle
		# at spawn via player._offhangar_td). Without this the offline account
		# always handed back a fresh tier-1 MS-1 here, so the crosshair
		# penetration marker, tracer shell speed/gravity and maxHealth all used
		# MS-1 data, not the tank being driven. Also stops allocating a new
		# descriptor on every access.
		_offh_td = getattr(baseSelf, '_offhangar_td', None)
		if _offh_td is not None:
			return _offh_td
		try:
			from items import vehicles
			return vehicles.VehicleDescr(typeName='ussr:MS-1')
		except Exception:
			pass
		vehStub = getattr(baseSelf, '_offhangar_vehicle_stub', None)
		if vehStub is None:
			vehStub = _OfflineVehicleStub()
			baseSelf._offhangar_vehicle_stub = vehStub
		td = getattr(vehStub, 'typeDescriptor', None)
		if td is None:
			vehStub.typeDescriptor = _OfflineVehicleStub._TypeDescriptorStub()
			td = vehStub.typeDescriptor
		return td
	if name == 'onGunShotChanged' and baseSelf.isOffline:
		import Event
		if not hasattr(baseSelf, '_offhangar_onGunShotChanged'):
			baseSelf._offhangar_onGunShotChanged = Event.Event()
		return baseSelf._offhangar_onGunShotChanged
	if name in ('addModel', 'delModel') and baseSelf.isOffline:
		# The offline player is the ACCOUNT entity, so Entity.addModel parents the
		# model to the account's own transform/chunk instead of the battle world. The
		# only offline caller is ProjectileMover (shell tracers) - they were built,
		# moved and lit correctly yet never drawn. Hand back the global model API,
		# what every offline effect that DOES show uses.
		# This must be served here: entity method slots are read-only, so assigning
		# player.addModel raises instead of overriding.
		try:
			import sys as _sysm
			_ob = _sysm.modules.get('gui.mods.offhangar.offline_battle')
			if _ob is not None:
				return _ob._offh_player_add_model if name == 'addModel' else _ob._offh_player_del_model
		except Exception:
			pass
	if name == 'selectPlayer' and baseSelf.isOffline:
		# Clicking a name in the players panel. Scaleform Battle.selectPlayer calls
		# player.selectPlayer(vehID) and the offline player is a PlayerAccount, which
		# has no such method - ten AttributeError tracebacks per battle. Retail maps it
		# to picking that vehicle as the post-mortem spectator target (Avatar.selectPlayer:
		# only a LIVING ally, and only while the GUI owns the controls), so do the same.
		def _offh_select_player(vehID, _p=baseSelf):
			try:
				import sys as _syss
				_ob2 = _syss.modules.get('gui.mods.offhangar.offline_battle')
				if not getattr(_p, '_offh_spectating', False):
					return
				_vid = int(vehID)
				_pvid = getattr(_p, 'playerVehicleID', -1)
				_mv = (_ob2.G_MOCK_VEHICLES if _ob2 is not None else {}) or {}
				_t = _mv.get(_vid)
				if _vid != _pvid and (_t is None or (getattr(_t, 'health', 0) or 0) <= 0):
					return
				# The spectator tick rebuilds its target list every frame; steer it by id.
				_p._offh_spec_want = _vid
			except Exception:
				pass
		return _offh_select_player
	if name == 'arena' and baseSelf.isOffline:
		return getattr(baseSelf, '_offhangar_arena', None)
	if name in ('cell', 'base', 'server') and baseSelf.isOffline:
		name = 'fakeServer'
	
	try:
		return baseFunc(baseSelf, name)
	except (AttributeError, KeyError):
		raise
	except Exception:
		return None

def _offh_patch_techtree():
	"""Fix TechTree ResearchPage module buying, mounting, and inventory sync."""
	try:
		import sys
		if 'gui.Scaleform.techtree.custom_items' not in sys.modules:
			import gui.Scaleform.techtree.custom_items
		_tt_ci = sys.modules['gui.Scaleform.techtree.custom_items']
		_inv_item_cls = getattr(_tt_ci, '_InventoryItem', None)
		if _inv_item_cls is not None and not getattr(_inv_item_cls, '_offh_invid_patched', False):
			_orig_ii_init = _inv_item_cls.__init__
			def _safe_ii_init(self, itemTypeIdx, id=None, *a, **kw):
				_orig_ii_init(self, itemTypeIdx, id, *a, **kw)
				if not hasattr(self, 'inventoryId'):
					self.inventoryId = self.id
			_inv_item_cls.__init__ = _safe_ii_init
			_inv_item_cls._offh_invid_patched = True
		_inv_veh_cls = getattr(_tt_ci, 'InventoryVehicle', None)
		if _inv_veh_cls is not None and not getattr(_inv_veh_cls, '_offh_install_patched', False):
			def _offh_isPossibleInstallComponent(self, compactDescr, *a, **kw):
				return (True, 'ok')
			_inv_veh_cls.isPossibleInstallComponent = _offh_isPossibleInstallComponent
			_inv_veh_cls._offh_install_patched = True
	except Exception:
		pass
	try:
		import sys
		if 'gui.Scaleform.techtree.data' not in sys.modules:
			import gui.Scaleform.techtree.data
		_ttd = sys.modules['gui.Scaleform.techtree.data']
		_items_data_cls = getattr(_ttd, '_ItemsData', None)
		if _items_data_cls is not None and not getattr(_items_data_cls, '_offh_getinv_patched', False):
			_orig_getInvItem = _items_data_cls.getInvItem
			def _safe_getInvItem(self, itemCD):
				res = _orig_getInvItem(self, itemCD)
				if res is None:
					try:
						if hasattr(self, '_rootCD') and itemCD == self._rootCD:
							return self.getRootItem()
						return self.getItem(itemCD)
					except Exception:
						return None
				return res
			_items_data_cls.getInvItem = _safe_getInvItem
			_items_data_cls._offh_getinv_patched = True
		cls = getattr(_ttd, 'ResearchItemsData', None)
		if cls is not None and not getattr(cls, '_offh_inv_patched', False):
			_orig = cls.invalidateInstalled
			def _safe_invalidateInstalled(self, *a, **kw):
				try:
					return _orig(self, *a, **kw)
				except AttributeError:
					return []
			cls.invalidateInstalled = _safe_invalidateInstalled
			_orig_inv_upd = cls.invalidateInventory
			def _safe_invalidateInventory(self, *a, **kw):
				try:
					return _orig_inv_upd(self, *a, **kw)
				except AttributeError:
					return []
			cls.invalidateInventory = _safe_invalidateInventory
			cls._offh_inv_patched = True
	except Exception:
		pass
	try:
		import sys
		if 'gui.Scaleform.techtree.ResearchPage' not in sys.modules:
			import gui.Scaleform.techtree.ResearchPage
		_rp_mod = sys.modules['gui.Scaleform.techtree.ResearchPage']
		_rp_cls = getattr(_rp_mod, 'ResearchPage', None)
		if _rp_cls is not None and not getattr(_rp_cls, '_offh_buy_patched', False):
			_orig_rp_buy = getattr(_rp_cls, 'request4BuyAndInstall', None)
			if callable(_orig_rp_buy):
				def _offh_request4BuyAndInstall(self, itemCD):
					res = _orig_rp_buy(self, itemCD)
					try:
						import BigWorld
						def _redraw_tt(_s=self):
							try:
								if hasattr(_s, 'redrawResearchItems'):
									_s.redrawResearchItems()
							except Exception:
								pass
						BigWorld.callback(0.05, _redraw_tt)
						BigWorld.callback(0.18, _redraw_tt)
					except Exception:
						pass
					return res
				_rp_cls.request4BuyAndInstall = _offh_request4BuyAndInstall
			_rp_cls._offh_buy_patched = True
	except Exception:
		pass

@override(Account.PlayerAccount, 'onBecomePlayer')
def Account_onBecomePlayer(baseFunc, baseSelf):
	import time
	baseSelf._offline_boot_time = time.time()
	_offh_patch_techtree()
	if not hasattr(baseSelf, 'newFakeModel'):
		def newFakeModel():
			import BigWorld
			# Effects attach nodes to this model; a blank Model('') makes
			# Model.node() raise ('not supported for blank models').
			try:
				return BigWorld.Model('objects/fake_model.model')
			except Exception:
				return BigWorld.Model('')
		baseSelf.newFakeModel = newFakeModel
	baseFunc(baseSelf)
	_ensure_postmortem_event(getattr(baseSelf, 'inputHandler', None))
	if baseSelf.isOffline:
		_worker_diag('Account.onBecomePlayer isOffline=True worker=%s' % _is_worker_mode())
		if _is_worker_mode():
			_apply_worker_mode()
			LOG_NOTE('Account.onBecomePlayer: worker mode detected, scheduling battle auto-launch')
			try:
				with open('offline-worker.ready', 'w') as _rf:
					_rf.write('worker_online\n')
				from gui.mods.offhangar import paths as _paths_wm
				_mpath = os.path.join(getattr(_paths_wm, 'USER_DIR', 'offhangar_user'), 'offline-worker.ready')
				with open(_mpath, 'w') as _rf:
					_rf.write('worker_online\n')
			except Exception:
				pass
			def _worker_launch(retries=30):
				_worker_diag('Worker checking LAN status (retries=%d)' % retries)
				net_client = getattr(baseSelf, '_offhangar_network_client', None)
				if net_client is not None and getattr(net_client, 'ready', False):
					_worker_diag('Worker LAN client is ready and connected!')
					return
				if net_client is None or not net_client.running:
					_worker_diag('Executing start_offline_random_from_hangar in worker')
					try:
						start_offline_random_from_hangar(baseSelf, 0)
					except Exception as _ex:
						_worker_diag('start_offline_random_from_hangar EXCEPTION: %s' % str(_ex))
				if retries > 0:
					BigWorld.callback(1.5, lambda: _worker_launch(retries - 1))
			BigWorld.callback(0.5, _worker_launch)
		else:
			# v7.4: If a genuine retail 0.8.2 .wotreplay is playing in C++ BattleReplay, do NOT hijack it with OfflineAccount!
			try:
				import BattleReplay as _br_chk
				_br_c = getattr(_br_chk, 'g_replayCtrl', None)
				if getattr(BigWorld, '_offh_vanilla_wotreplay_path', None) or (_br_c is not None and getattr(_br_c, 'isPlaying', False)):
					LOG_NOTE('REPLAY_DIRECT_BOOT: native BattleReplay is playing -> yielding completely to native C++ BattleReplay!')
					return
			except Exception:
				pass
			try:
				from gui.mods.offhangar.data import getOfflineShopCacheData, getOfflineInventory, getOfflineStats
				from gui.mods.offhangar.session_guards import normalize_offline_stats
				if hasattr(baseSelf, 'shop') and baseSelf.shop is not None and not getattr(baseSelf.shop, '_Shop__cache', None):
					baseSelf.shop._Shop__cache = getOfflineShopCacheData()
				if hasattr(baseSelf, 'inventory') and baseSelf.inventory is not None and not getattr(baseSelf.inventory, '_Inventory__cache', None):
					_inv_d = getOfflineInventory()
					baseSelf._offhangar_inventory = _inv_d.get('inventory')
					baseSelf.inventory.synchronize(True, _inv_d)
				if hasattr(baseSelf, 'stats') and baseSelf.stats is not None and not getattr(baseSelf.stats, '_Stats__cache', None):
					_sts_d = getOfflineStats()
					normalize_offline_stats(_sts_d.get('stats', {}))
					baseSelf.stats.synchronize(True, _sts_d)
				if hasattr(baseSelf, 'syncData') and baseSelf.syncData is not None:
					baseSelf.syncData._AccountSyncData__isSynchronized = True
			except Exception as _pre_err:
				LOG_ERROR('Account.onBecomePlayer pre-seed err: %s' % str(_pre_err))
			try:
				from gui.mods.offhangar import offline_battle as _ob_login
				_ob_login._offh_install_premium_expiry_guard()
				_ob_login._offh_install_hangar_data_guards()
				_ob_login._offh_install_lobby_iface_trace()
			except Exception:
				pass
			try:
				from gui.mods.offhangar import _constants as _c_gui
				_gui_ctx_bytes = getattr(_c_gui, 'OFFLINE_GUI_CTX', OFFLINE_GUI_CTX)
			except Exception:
				_gui_ctx_bytes = OFFLINE_GUI_CTX
			try:
				from gui.mods.offhangar import replay_machinima as _rm_boot
				if _rm_boot.get_replay_manager().has_pending_cli_autostart():
					BigWorld._offh_autostart_replay_launched = True
					LOG_NOTE('REPLAY_DIRECT_BOOT: launching offhangar replay battle directly from Account.onBecomePlayer without loading Hangar!')
					from gui.mods.offhangar.offline_battle import start_offline_random_from_hangar as _start_rp_battle
					BigWorld.callback(0.05, lambda: _start_rp_battle(baseSelf, 0))
					return
			except Exception as _rp_b_err:
				LOG_ERROR('REPLAY_DIRECT_BOOT err: %s' % str(_rp_b_err))
			LOG_NOTE('Account.onBecomePlayer calling showGUI for nick=%s' % getattr(baseSelf, 'name', '?'))
			baseSelf.showGUI(_gui_ctx_bytes)
			_fix_fullscreen_cursor_focus()
			BigWorld.callback(0.2, _fix_fullscreen_cursor_focus)
			try:
				from gui.mods.offhangar import offline_battle as _ob_tail
				BigWorld.callback(0.15, lambda: _ob_tail._offh_hangar_complete_chain('login+0.15s'))
				BigWorld.callback(0.35, lambda: _ob_tail._offh_hangar_waiting_check('login+0.35s'))
				BigWorld.callback(0.95, lambda: _ob_tail._offh_hangar_push_data('login+0.95s'))
				BigWorld.callback(1.25, lambda: _ob_tail._offh_hangar_waiting_check('login+1.25s'))
				BigWorld.callback(1.80, lambda: _ob_tail._offh_hangar_push_data('login+1.80s'))
				BigWorld.callback(2.50, lambda: _ob_tail._offh_hangar_iface_check('login+2.50s'))
				BigWorld.callback(3.20, lambda: _ob_tail._offh_hangar_push_data('login+3.20s'))
			except Exception as _lt_err:
				LOG_ERROR('Account.onBecomePlayer lobby tail schedule err: %s' % str(_lt_err))
			def _poll_cbt_welcome(_tries=25):
				try:
					from gui.Scaleform.Waiting import Waiting
					if Waiting.isVisible() and _tries > 0:
						BigWorld.callback(0.4, lambda: _poll_cbt_welcome(_tries - 1))
						return
				except Exception:
					pass
				try:
					from CurrentVehicle import g_currentVehicle as _cv_w
					if not _cv_w.isPresent() and _tries > 5:
						BigWorld.callback(0.4, lambda: _poll_cbt_welcome(_tries - 1))
						return
				except Exception:
					pass
				try:
					from gui.mods.offhangar import cbt_profiles_and_locale as _cbt_pal
					_cbt_pal._show_native_gui_welcome_modal = lambda *a, **k: False
					if not _cbt_pal.show_cbt_welcome_notification() and _tries > 0 and _cbt_pal.should_show_cbt_welcome():
						BigWorld.callback(0.5, lambda: _poll_cbt_welcome(_tries - 1))
				except Exception:
					pass
			BigWorld.callback(1.6, _poll_cbt_welcome)

@override(Account.PlayerAccount, 'onBecomeNonPlayer')
def Account_onBecomeNonPlayer(baseFunc, baseSelf):
	import traceback
	LOG_DEBUG('Account.onBecomeNonPlayer() called! Traceback:')
	for line in traceback.format_stack():
		LOG_DEBUG(line.strip())
	try:
		if getattr(baseSelf, 'isDestroyed', False):
			return
		if getattr(baseSelf, 'isOffline', False) and not getattr(baseSelf, '_offline_allow_become_non_player', False):
			LOG_DEBUG('OfflineStub.skip onBecomeNonPlayer')
			return
		baseFunc(baseSelf)
	except Exception:
		pass

@override(BigWorld, 'clearEntitiesAndSpaces')
def BigWorld_clearEntitiesAndSpaces(baseFunc, *args):
	player = BigWorld.player()
	if getattr(player, 'isOffline', False) and not getattr(player, '_offhangar_allow_world_clear', False):
		return
	baseFunc(*args)

@override(BigWorld, 'connect')
def BigWorld_connect(baseFunc, server, loginParams, progressFn):
	_is_off = (server == OFFLINE_SERVER_ADDRESS)
	if not _is_off:
		try:
			from gui.mods.offhangar import cbt_profiles_and_locale as _cbt_pal
			_is_off = _cbt_pal.is_offline_server(server)
		except Exception:
			_is_off = (str(server) in ('W.o.T OB2.0 CBT', 'offline.loc'))
	if _is_off:
		LOG_DEBUG('BigWorld.connect to CBT offline server:', server)
		progressFn(1, "LOGGED_ON", {})
		_cur_pl = BigWorld.player()
		if _cur_pl is not None and getattr(_cur_pl, '__class__', None) and _cur_pl.__class__.__name__ in ('PlayerAccount', 'Account'):
			try:
				from gui.mods.offhangar import offline_battle as _ob_reconn
				_ob_reconn._offh_hangar_complete_chain('reconnect')
				BigWorld.callback(0.25, lambda: _ob_reconn._offh_hangar_push_data('reconnect+0.25s'))
			except Exception:
				pass
			return
		BigWorld.createEntity('Account', BigWorld.createSpace(), 0, (0, 0, 0), (0, 0, 0), {})
	else:
		baseFunc(server, loginParams, progressFn)


import game as _game_module
_orig_game_fini = _game_module.fini
def _offline_game_fini():
	try:
		player = BigWorld.player()
		if player is not None:
			player._offline_allow_become_non_player = True
			player._offhangar_allow_world_clear = True
			try:
				from gui.mods.offhangar.network_battle import stop_for_player
				stop_for_player(player)
			except Exception:
				pass
	except Exception:
		pass
	try:
		import Settings
		if getattr(Settings, 'g_instance', None) is not None:
			Settings.g_instance.save()
	except Exception:
		pass
	try:
		BigWorld.savePreferences()
	except Exception:
		pass
	try:
		BigWorld.flushPythonLog()
	except Exception:
		pass
	try:
		from gui.mods.offhangar import offh_multithread as _omt_exit
		_omt_exit.get_coordinator().shutdown_process()
	except Exception:
		pass
	try:
		import ctypes
		ctypes.windll.kernel32.ExitProcess(0)
	except Exception:
		pass
	try:
		os._exit(0)
	except Exception:
		pass
	try:
		_orig_game_fini()
	except Exception:
		pass
_game_module.fini = _offline_game_fini

# v10.4: Clean DXVK / D3D9 Termination & BigWorld.quit hook (ExitProcess terminates lingering Vulkan background threads immediately)
_orig_bw_quit = getattr(BigWorld, 'quit', None)
def _offline_bw_quit(*args, **kwargs):
	try:
		import Settings
		if getattr(Settings, 'g_instance', None) is not None:
			Settings.g_instance.save()
	except Exception:
		pass
	try:
		BigWorld.savePreferences()
	except Exception:
		pass
	try:
		BigWorld.flushPythonLog()
	except Exception:
		pass
	try:
		from gui.mods.offhangar import offh_multithread as _omt_exit
		_omt_exit.get_coordinator().shutdown_process()
	except Exception:
		pass
	try:
		import ctypes
		ctypes.windll.kernel32.ExitProcess(0)
	except Exception:
		pass
	try:
		import os
		os._exit(0)
	except Exception:
		pass
	if callable(_orig_bw_quit):
		try:
			return _orig_bw_quit(*args, **kwargs)
		except Exception:
			pass

if _orig_bw_quit is not None:
	BigWorld.quit = _offline_bw_quit



def _offline_enqueue_random_cmd_id():
	return getattr(AccountCommands, 'CMD_ENQUEUE_RANDOM', 700)

def _offline_enqueue_tutorial_cmd_id():
	return getattr(AccountCommands, 'CMD_ENQUEUE_TUTORIAL', 737)


def _install_offline_account__do_cmd_hook():
	if '_PlayerAccount__doCmd' not in dir(Account.PlayerAccount):
		LOG_DEBUG('Offline.__doCmd missing on PlayerAccount')
		return
	try:
		@override(Account.PlayerAccount, '__doCmd')
		def PlayerAccount___doCmd(baseFunc, baseSelf, doCmdMethod, cmd, callback, *args):
			if not getattr(baseSelf, 'isOffline', False):
				return baseFunc(baseSelf, doCmdMethod, cmd, callback, *args)
			if doCmdMethod != 'doCmdInt3' or (cmd != _offline_enqueue_random_cmd_id() and cmd != _offline_enqueue_tutorial_cmd_id()):
				return baseFunc(baseSelf, doCmdMethod, cmd, callback, *args)
			# Some 0.8.x command indexes alias random enqueue with server-stats.
			# Preserve the all-zero stats request instead of treating it as a battle.
			if cmd == _offline_enqueue_random_cmd_id() and len(args) >= 3 and args[0] == 0 and args[1] == 0 and args[2] == 0:
				return baseFunc(baseSelf, doCmdMethod, cmd, callback, *args)
			getRid = getattr(baseSelf, '_PlayerAccount__getRequestID', None)
			if not callable(getRid):
				LOG_DEBUG('Offline.__doCmd ENQUEUE_RANDOM skip no __getRequestID')
				return baseFunc(baseSelf, doCmdMethod, cmd, callback, *args)
			rid = getRid()
			if rid is None:
				return baseFunc(baseSelf, doCmdMethod, cmd, callback, *args)
			respMap = getattr(baseSelf, '_PlayerAccount__onCmdResponse', None)
			if callback is not None and respMap is not None:
				respMap[rid] = callback
			vehInvID = args[0] if args else 0

			def _ack_and_boot():
				try:
					baseSelf.onCmdResponse(rid, AccountCommands.RES_SUCCESS, '')
				except Exception:
					LOG_CURRENT_EXCEPTION()
				# The direct Account path bypasses FakeServer/__router entirely.
				# Enter the same idempotent queue gate only after its native ACK.
				try:
					start_offline_random_from_hangar(baseSelf, vehInvID)
				except Exception:
					LOG_CURRENT_EXCEPTION()

			LOG_DEBUG('Offline.__doCmd ENQUEUE_RANDOM', rid, vehInvID)
			BigWorld.callback(0.0, _ack_and_boot)
			return rid
	except Exception:
		LOG_CURRENT_EXCEPTION()


def _install_offline_enqueue_public_hooks():
	if hasattr(Account.PlayerAccount, 'enqueueRandom'):
		@override(Account.PlayerAccount, 'enqueueRandom')
		def PlayerAccount_enqueueRandom(baseFunc, baseSelf, *args, **kwargs):
			if getattr(baseSelf, 'isOffline', False):
				LOG_DEBUG('Offline.enqueueRandom IGNORED')
				return
			return baseFunc(baseSelf, *args, **kwargs)
	else:
		LOG_DEBUG('Offline.enqueueRandom missing')

	# Cancel button in the "searching for battle" panel. Retail dequeueRandom sends
	# CMD_DEQUEUE_RANDOM to the BASE entity, which does not exist offline, so the
	# click did nothing at all and the battle still started when the fake queue
	# timer ran out. Flag the cancel and fire onDequeuedRandom ourselves: that is
	# what clears isInRandomQueue and returns the hangar UI to its normal state.
	if hasattr(Account.PlayerAccount, 'dequeueRandom'):
		@override(Account.PlayerAccount, 'dequeueRandom')
		def PlayerAccount_dequeueRandom(baseFunc, baseSelf, *args, **kwargs):
			if not getattr(baseSelf, 'isOffline', False):
				return baseFunc(baseSelf, *args, **kwargs)
			baseSelf._offhangar_queue_cancelled = True
			baseSelf._offhangar_queue_pending = False
			baseSelf._offhangar_queue_generation = getattr(baseSelf, '_offhangar_queue_generation', 0) + 1
			LOG_DEBUG('Offline.dequeueRandom -> queue cancelled')
			try:
				from gui.mods.offhangar.network_battle import stop_for_player
				stop_for_player(baseSelf)
			except Exception:
				LOG_CURRENT_EXCEPTION()
			try:
				baseSelf.onDequeuedRandom()
			except Exception:
				LOG_CURRENT_EXCEPTION()
			# The Prebattle page does NOT close itself on Exit - it only sends the
			# dequeue and then waits for the SERVER to release the vehicle lock:
			#     g_playerEvents.onVehicleLockChanged -> __lockChange -> showHangar()
			# Offline nothing ever unlocks it, so the search screen just stayed up and
			# the click looked dead. Fire the same event the server would.
			try:
				from PlayerEvents import g_playerEvents as _pev
				from AccountCommands import LOCK_REASON as _LR
				from CurrentVehicle import g_currentVehicle as _cv
				_inv = _cv.vehicle.inventoryId
				LOG_DEBUG('Offline.dequeueRandom -> unlock vehicle', _inv)
				_pev.onVehicleLockChanged(_inv, _LR.NONE)
			except Exception:
				LOG_CURRENT_EXCEPTION()
			return
	else:
		LOG_DEBUG('Offline.dequeueRandom missing')

	if hasattr(Account.PlayerAccount, 'enqueueTutorial'):
		@override(Account.PlayerAccount, 'enqueueTutorial')
		def PlayerAccount_enqueueTutorial(baseFunc, baseSelf, *args, **kwargs):
			if getattr(baseSelf, 'isOffline', False):
				LOG_DEBUG('Offline.enqueueTutorial IGNORED')
				return
			return baseFunc(baseSelf, *args, **kwargs)

	candidates = []
	for name in dir(Account.PlayerAccount):
		if not callable(getattr(Account.PlayerAccount, name)):
			continue
		low = name.lower()
		if 'tutorial' in low or 'bootcamp' in low or 'sandbox' in low:
			continue
		if 'enqueue' in low and 'random' in low and name != 'enqueueRandom' and not name.startswith('on'):
			candidates.append(name)
	if candidates:
		LOG_DEBUG('Offline.enqueueExtraCandidates', candidates)
	for methodName in candidates:
		try:
			def _bind(nm):
				@override(Account.PlayerAccount, nm)
				def _enqueueAlt(baseFunc, baseSelf, *args, **kwargs):
					if getattr(baseSelf, 'isOffline', False):
						LOG_DEBUG('Offline.intercepted IGNORED', nm, args)
						return
					return baseFunc(baseSelf, *args, **kwargs)
			_bind(methodName)
		except Exception:
			LOG_CURRENT_EXCEPTION()


def _install_offline_battle_transport_hooks():
	_install_offline_account__do_cmd_hook()
	_install_offline_enqueue_public_hooks()


def _install_offline_avatar_guards():
	def _ensure_offline_avatar_state(baseSelf):
		class DummyMProv(object):
			target = None
		defaults = {
			'_PlayerAvatar__stepsTillInit': 1,
			'_PlayerAvatar__isSpaceInitialized': False,
			'_PlayerAvatar__setOwnVehicleMatrixTimerID': 0,
			'_PlayerAvatar__isForcedGuiControlMode': False,
			'_PlayerAvatar__ownVehicleMProv': DummyMProv(),
			'_PlayerAvatar__shotWaitingTimerID': 0,
			'_PlayerAvatar__fireNonFatalDamageTriggerID': 0,
			'playerVehicleID': 0,
		}
		for key, value in defaults.iteritems():
			if not hasattr(baseSelf, key):
				setattr(baseSelf, key, value)
		if not hasattr(baseSelf, 'name') or baseSelf.name in ('Player', 'offline_player', None):
			try:
				from gui.mods.offhangar.core.cbt_profiles_and_locale import get_active_nickname
				baseSelf.name = get_active_nickname() or 'Player'
			except Exception:
				baseSelf.name = 'Player'
		if not hasattr(baseSelf, 'arena'):
			baseSelf.arena = _OfflineArenaStub()
		if not hasattr(baseSelf, '_offhangar_vehicle_stub'):
			baseSelf._offhangar_vehicle_stub = _OfflineVehicleStub()
		vehStub = baseSelf._offhangar_vehicle_stub
		for attrName in ('_Avatar__vehicleAttached', '_PlayerAvatar__vehicleAttached', '_Avatar__vehicle'):
			if not hasattr(baseSelf, attrName) or getattr(baseSelf, attrName) is None:
				try:
					setattr(baseSelf, attrName, vehStub)
				except TypeError:
					pass
				except Exception:
					LOG_CURRENT_EXCEPTION()

	seen = set()
	for className in ('Avatar', 'PlayerAvatar'):
		avatarCls = getattr(AvatarModule, className, None)
		if avatarCls is None or not hasattr(avatarCls, 'onEnterWorld'):
			continue
		if id(avatarCls) in seen:
			continue
		seen.add(id(avatarCls))

		@override(avatarCls, 'onEnterWorld')
		def _avatar_onEnterWorld(baseFunc, baseSelf, _className=className, *args, **kwargs):
			_ensure_offline_avatar_state(baseSelf)
			try:
				if args:
					return baseFunc(baseSelf, *args, **kwargs)
				return baseFunc(baseSelf, [])
			except KeyError as ex:
				if 'fake_model.model' in str(ex):
					LOG_DEBUG('OfflineAvatar.ignore missing model', _className, ex)
					return
				raise

		if hasattr(avatarCls, 'onLeaveWorld'):
			@override(avatarCls, 'onLeaveWorld')
			def _avatar_onLeaveWorld(baseFunc, baseSelf, _className=className, *args, **kwargs):
				_ensure_offline_avatar_state(baseSelf)
				try:
					return baseFunc(baseSelf, *args, **kwargs)
				except AttributeError as ex:
					msg = str(ex)
					if 'playerVehicleID' in msg or '_PlayerAvatar__stepsTillInit' in msg or '_PlayerAvatar__setOwnVehicleMatrixTimerID' in msg:
						LOG_DEBUG('OfflineAvatar.ignore leave attr', _className, ex)
						return
					raise
				except ValueError as ex:
					msg = str(ex)
					if 'py_cancelCallback' in msg:
						LOG_DEBUG('OfflineAvatar.ignore leave callback', _className, ex)
						return
					raise

		if hasattr(avatarCls, 'getVehicleAttached'):
			@override(avatarCls, 'getVehicleAttached')
			def _avatar_getVehicleAttached(baseFunc, baseSelf, *args, **kwargs):
				try:
					veh = baseFunc(baseSelf, *args, **kwargs)
					if veh is not None:
						return veh
				except Exception:
					LOG_CURRENT_EXCEPTION()
				_ensure_offline_avatar_state(baseSelf)
				return getattr(baseSelf, '_offhangar_vehicle_stub')


def _install_offline_input_guards():
	accIhCls = getattr(Account, 'AccountInputHandler', None)
	if accIhCls is not None and hasattr(accIhCls, '__init__'):
		@override(accIhCls, '__init__')
		def _accIh_init(baseFunc, baseSelf, *args, **kwargs):
			baseFunc(baseSelf, *args, **kwargs)
			_ensure_postmortem_event(baseSelf)
		LOG_DEBUG('OfflineInput.patched AccountInputHandler.__init__')

	aihCls = getattr(AvatarInputHandlerModule, 'AvatarInputHandler', None)
	LOG_DEBUG('OfflineInput._install guards: aihCls:', aihCls)
	if aihCls is not None:
		LOG_DEBUG('OfflineInput._install guards: hasattr start:', hasattr(aihCls, 'start'))
	if aihCls is None or not hasattr(aihCls, 'start'):
		LOG_DEBUG('OfflineInput._install guards: EARLY RETURN!')
		return

	if hasattr(aihCls, '__init__'):
		@override(aihCls, '__init__')
		def _aih_init(baseFunc, baseSelf, *args, **kwargs):
			baseFunc(baseSelf, *args, **kwargs)
			_ensure_postmortem_event(baseSelf)
		LOG_DEBUG('OfflineInput.patched AvatarInputHandler.__init__')


def _install_windows_manager_guards():
	try:
		from gui import WindowsManager
		wmCls = getattr(WindowsManager, 'WindowsManager', None)
		if wmCls is not None:
			orig_destroyBattle = getattr(wmCls, 'destroyBattle', None)
			if orig_destroyBattle is not None and not getattr(orig_destroyBattle, '_offh_wrapped', False):
				def _safe_destroyBattle(self, *a, **kw):
					bw = getattr(self, '_WindowsManager__battleWindow', None)
					if bw is not None:
						try: bw.close()
						except Exception: pass
					try:
						orig_destroyBattle(self, *a, **kw)
					except Exception: pass
					setattr(self, '_WindowsManager__window', None)
					setattr(self, '_WindowsManager__battleWindow', None)
				_safe_destroyBattle._offh_wrapped = True
				_safe_destroyBattle._offh_orig = orig_destroyBattle
				wmCls.destroyBattle = _safe_destroyBattle
				LOG_DEBUG('OfflineBattle.patched WindowsManager.destroyBattle for safe Scaleform teardown')
	except Exception:
		LOG_CURRENT_EXCEPTION()


try:
	from gui.mods.offhangar.session_guards import install_current_vehicle_persistence_hook
	install_current_vehicle_persistence_hook()
	LOG_NOTE('CURRENT_VEHICLE_PERSISTENCE installed')
except Exception as _cv_persist_err:
	LOG_ERROR('CURRENT_VEHICLE_PERSISTENCE install failed: %s' % str(_cv_persist_err)[:120])
_install_offline_battle_transport_hooks()
_install_offline_avatar_guards()
_install_offline_input_guards()
_install_windows_manager_guards()


try:
	if hasattr(connectionManager, 'disconnect'):
		_orig_cm_disc = connectionManager.disconnect
		def _cbt_cm_disconnect(*args, **kwargs):
			try:
				_orig_cm_disc(*args, **kwargs)
			except Exception:
				pass
			try:
				from gui import WindowsManager
				wm = getattr(WindowsManager, 'g_windowsManager', None)
				if wm is not None and getattr(wm, 'battleWindow', None) is None and hasattr(wm, 'showLogin'):
					BigWorld.callback(0.05, wm.showLogin)
			except Exception:
				pass
		connectionManager.disconnect = _cbt_cm_disconnect
except Exception:
	pass
