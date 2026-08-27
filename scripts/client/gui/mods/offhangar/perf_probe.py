# -*- coding: utf-8 -*-
'''Frame and tick probe for the offline battle.

Why this exists: the tick optimisations can only be judged against a number,
and the harness that produced offhangar_user/perf_tick.csv was removed from the
source at some point - the CSV is still there, nothing writes it any more. This
is that harness, rebuilt, plus the two things the old one could not answer.

What it measures
----------------
frame_ms   wall time between two consecutive _aih_tick entries. The tick
           reschedules itself with BigWorld.callback(0.0, ...), so that gap is
           one whole frame: our Python plus everything the engine does.
tick_ms    time spent inside _aih_tick itself.
nontick    frame_ms - tick_ms, i.e. the renderer, Scaleform and the rest of the
           client. With 30 bots this was 23.66 ms, which caps the frame rate
           near 42 FPS however fast the Python side gets - so this is the
           number that decides whether 60 FPS is reachable at all.

The two new answers
-------------------
1. dump_graphics() writes what the 0.8.2 client ACTUALLY renders at. The
   preferences.xml in %APPDATA%\\Wargaming.net\\WorldOfTanks belongs to a modern
   WoT (schema version 5, D3D11, HAVOK_ENABLED) and has nothing to do with this
   DX9 client, and the in-game settings dialog is broken here - python.log logs
   'settingsDialog is not found in flash by name popupNN' every time it opens.
   So the settings were never actually knowable. BigWorld.graphicsSettings()
   is the ground truth.

2. The GUI A/B (config perf_gui_ab). Scaleform is a prime suspect for the
   non-tick milliseconds: the battle GUI redraws 30 vehicle markers and a full
   player panel. Toggling the 'Visibility/GUI' watcher on and off on a timer
   puts frames with and without the GUI in the same CSV, and the difference of
   the two frame_ms averages IS the GUI cost. Off by default - while it runs the
   interface blinks, so it is a measuring tool, not something to play with.

Everything per-frame here is plain float arithmetic. Rows are buffered and
flushed every few seconds, because synchronous file I/O on the main thread
would measure the logger instead of the client - the same trap the config file
warns about for debug_logging.
'''

import os
import time

CSV_NAME = 'perf_tick.csv'
_HEADER = 't,fps,frame_ms,tick_ms,tick_max_ms,tick_pct,nontick_ms,bots,alive,gui,map\n'

# Seconds of frames folded into one CSV row.
_WINDOW = 1.0
# Rows kept in memory before touching the disk.
_FLUSH_ROWS = 8


def _now():
    # On Windows / Python 2 this is QueryPerformanceCounter, which is what frame
    # timing needs. time.time() only has ~15 ms resolution here and would report
    # garbage for a 4 ms tick.
    return time.clock()


class _Probe(object):

    def __init__(self):
        self.enabled = False
        self.path = None
        self._rows = []
        self._gui_ab = 0.0
        self._gui_on = True
        self._gui_rows = 0
        self._graphics_dumped = False
        self._reset_window()
        self._t0 = None
        self._tick_start = None
        self._session_start = None

    # ---- lifecycle -------------------------------------------------------

    def start(self):
        '''Called once when a battle begins. Safe to call again.'''
        try:
            from gui.mods.offhangar._constants import CONFIG_OPTIONS as _c
            self.enabled = bool(_c.get('perf_debug', False))
            self._gui_ab = float(_c.get('perf_gui_ab', 0.0) or 0.0)
        except Exception:
            self.enabled = False
            self._gui_ab = 0.0
        if not self.enabled:
            return
        try:
            from gui.mods.offhangar import user_config
            self.path = user_config.user_data_path(CSV_NAME)
        except Exception:
            self.enabled = False
            return
        self._rows = []
        self._t0 = None
        self._tick_start = None
        self._session_start = _now()
        self._gui_on = True
        self._gui_rows = 0
        self._reset_window()
        # Fresh file per battle: appending would blend two runs into one graph.
        try:
            handle = open(self.path, 'wb')
            handle.write(_HEADER)
            handle.close()
        except (OSError, IOError), error:
            # Say so. A probe that silently turns itself off leaves someone
            # staring at a stale CSV wondering why nothing changed.
            print '[OFFHANGAR][PERF] cannot write %s: %s' % (self.path, error)
            self.enabled = False
            return
        print '[OFFHANGAR][PERF] measuring to %s (gui_ab=%s)' % (self.path, self._gui_ab)

    def stop(self):
        '''Battle over: flush what is left and put the GUI back.'''
        if not self.enabled:
            return
        self._flush(force=True)
        self._set_gui(True)
        self.enabled = False

    # ---- per frame -------------------------------------------------------

    def begin(self):
        '''Top of _aih_tick.'''
        if not self.enabled:
            return
        now = _now()
        if self._t0 is not None:
            frame = now - self._t0
            # A frame longer than half a second is a load hitch or a breakpoint,
            # not a frame - averaging it in would swamp the whole window.
            if 0.0 < frame < 0.5:
                self._frames += 1
                self._frame_sum += frame
        self._t0 = now
        self._tick_start = now

    def end(self):
        '''Just before _aih_tick reschedules itself.'''
        if not self.enabled or self._tick_start is None:
            return
        now = _now()
        tick = now - self._tick_start
        if 0.0 <= tick < 0.5:
            self._tick_sum += tick
            if tick > self._tick_max:
                self._tick_max = tick
        self._ticks += 1
        if (now - self._window_start) >= _WINDOW:
            self._close_window(now)

    def _context(self):
        '''Bot counts and map name. Read once per CSV row, never per frame.

        alive is reported separately from bots because a dead bot stays in
        G_MOCK_VEHICLES as a wreck: late in a battle the total still says 30
        while only a handful are still driving and shooting, and a row that
        cannot tell those apart makes the frame rate look far better than it is
        for an actual fight.
        '''
        bots = 0
        alive = 0
        name = ''
        try:
            import sys
            module = sys.modules.get('gui.mods.offhangar.offline_battle')
            mocks = getattr(module, 'G_MOCK_VEHICLES', None) or {}
            bots = len(mocks)
            for mock in mocks.itervalues():
                if getattr(mock, 'isAlive', False) and (getattr(mock, 'health', 0) or 0) > 0:
                    alive += 1
        except Exception:
            pass
        self._alive = alive
        try:
            import BigWorld
            arena = getattr(BigWorld.player(), 'arena', None)
            name = str(getattr(getattr(arena, 'arenaType', None), 'geometryName', '') or '')
        except Exception:
            pass
        return bots, name

    # ---- window bookkeeping ---------------------------------------------

    def _reset_window(self):
        self._window_start = _now()
        self._frames = 0
        self._frame_sum = 0.0
        self._ticks = 0
        self._tick_sum = 0.0
        self._tick_max = 0.0
        self._bots = 0
        self._alive = 0
        self._map = ''

    def _close_window(self, now):
        frames = self._frames
        # Ticks and frame deltas are counted separately: the very first tick of a
        # battle has no predecessor to measure a frame against, so dividing the
        # tick sum by the frame count would inflate tick_ms in the first row.
        ticks = self._ticks or 1
        if frames > 0:
            frame_ms = (self._frame_sum / frames) * 1000.0
            tick_ms = (self._tick_sum / ticks) * 1000.0
            fps = (frames / (self._frame_sum or 1e-06))
            pct = (tick_ms / frame_ms * 100.0) if frame_ms > 0.0 else 0.0
            self._bots, self._map = self._context()
            self._rows.append('%.2f,%.1f,%.3f,%.3f,%.3f,%.1f,%.3f,%d,%d,%d,%s\n' % (
                now - self._session_start,
                fps,
                frame_ms,
                tick_ms,
                self._tick_max * 1000.0,
                pct,
                frame_ms - tick_ms,
                self._bots,
                self._alive,
                1 if self._gui_on else 0,
                self._map,
            ))
        self._reset_window()
        # The GUI is flipped ONLY here, on a whole number of rows. Flipping it
        # mid-window would average frames with and without Scaleform into the
        # same row and the experiment would measure nothing.
        if self._gui_ab >= 1.0:
            self._gui_rows += 1
            if self._gui_rows >= int(self._gui_ab):
                self._gui_rows = 0
                self._set_gui(not self._gui_on)
        if len(self._rows) >= _FLUSH_ROWS:
            self._flush()

    def _flush(self, force=False):
        if not self._rows:
            return
        rows = self._rows
        self._rows = []
        try:
            handle = open(self.path, 'ab')
            handle.write(''.join(rows))
            handle.close()
        except (OSError, IOError):
            if not force:
                self.enabled = False

    # ---- GUI A/B ---------------------------------------------------------

    def _set_gui(self, on):
        # Only ever poke the engine when the A/B experiment is actually running.
        # The first version called this from stop() on EVERY battle end, which
        # meant a plain measuring run still wrote to a native engine watcher for
        # no reason - the one place in this module that reaches past Python.
        if self._gui_ab < 1.0 and on:
            self._gui_on = True
            return
        self._gui_on = on
        try:
            import BigWorld
            BigWorld.setWatcher('Visibility/GUI', '1' if on else '0')
        except Exception:
            # Watcher missing in this build - drop the experiment rather than
            # writing a gui column that lies.
            self._gui_ab = 0.0
            self._gui_on = True


PROBE = _Probe()


def dump_graphics():
    '''Log what this client actually renders at.

    Written with print rather than LOG_DEBUG on purpose: debug_logging is false
    in the shipped config, so LOG_DEBUG would swallow exactly the one line this
    whole question hangs on. print lands in python.log either way. Runs once per
    client session.
    '''
    if PROBE._graphics_dumped:
        return
    PROBE._graphics_dumped = True
    # Gated on perf_debug like everything else here. It was not, which meant
    # perf_debug=false still ran a native BigWorld call at every battle start -
    # the probe could not actually be switched off, and a module that claims to
    # be inert has to really be inert before it can be ruled out as a suspect.
    try:
        from gui.mods.offhangar._constants import CONFIG_OPTIONS as _c
        if not bool(_c.get('perf_debug', False)):
            return
    except Exception:
        return
    try:
        import BigWorld
        settings = BigWorld.graphicsSettings()
    except Exception, error:
        print '[OFFHANGAR][GFX] BigWorld.graphicsSettings() unavailable: %s' % (error,)
        return
    print '[OFFHANGAR][GFX] --- BigWorld.graphicsSettings() ---'
    for entry in settings:
        try:
            label = entry[0]
            active = entry[1]
            options = entry[2]
            count = len(options) if options is not None else 0
            # The option list itself has to be printed, not just its length.
            # 'active=0 of 5' does not say whether index 0 is the best or the
            # worst setting - BigWorld does not fix that order, and guessing it
            # wrong inverts every conclusion drawn from this dump.
            print '[OFFHANGAR][GFX] %-30s active=%s of %d  options=%.160r' % (
                label, active, count, options)
        except Exception:
            print '[OFFHANGAR][GFX] %r' % (entry,)
    print '[OFFHANGAR][GFX] --- end ---'
    # Resolution decides whether the client is GPU bound at all. A 2012 DX9
    # renderer on modern hardware normally is not, and that is the difference
    # between "max settings cost nothing" and "max settings cost everything".
    try:
        import BigWorld
        print '[OFFHANGAR][GFX] screenSize=%r fullScreen=%r' % (
            BigWorld.screenSize(), BigWorld.isFullScreen())
    except Exception:
        pass
