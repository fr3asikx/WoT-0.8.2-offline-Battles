"""Paint bot profiles - destinations, routes and avoid-areas - for every map.

    python paint_map.py                 # opens with a map selector
    python paint_map.py 02_malinovka    # opens straight on that map

Build-time tool. Never ships with the mod; it writes the JSON that does.

NO BAKED GRID IS REQUIRED TO PAINT
    World coordinates come from the map's own arena_def `boundingBox`, read
    offline out of res/scripts/arena_defs/<map>.xml - verified against the game's
    own log for Malinovka (-500,-500,500,500) and parsing cleanly for all 33
    maps. So every map is paintable today.

    A baked grid (offhangar_user/nav_dump/<map>.grid, produced by playing one
    battle) adds three things and gates nothing:
      * passability shading, so you can see what bots consider drivable
      * a warning when you place a point somewhere undrivable
      * the A* test, which draws the real route bots would take

WHAT THE THREE PAINT TYPES ARE FOR
    Point   a destination. Bots of the matching team+class pick one and A* to it.
    Route   an ORDERED polyline. A* takes the shortest passable line, which is
            often straight across open ground; a route says "go THIS way" - the
            part a pathfinder cannot infer. Bots A* between consecutive steps,
            so the route carries intent and the grid handles local detail.
    Avoid   a keep-out area. Painted areas mark grid cells BLOCKED, so A* routes
            around them with no new runtime code at all.

TEAM MATTERS
    A position is not symmetric: the ridge team 1 attacks over is the one team 2
    defends from. WG models this too - their `AiZoneEntryPoint` carries a `team`
    field. Every point and route is team 1, team 2, or both.

REFERENCE LAYERS (v)
    blend       minimap + grid, so water/blocked reads against the terrain
    minimap     spaces/<map>/mmap.dds - the game's own, and the ONLY source that
                shows WATER
    terrain 4k  maps/landscape/<map>/color_tex.dds, 4096x4096 - the ORIGINAL
                texture at ~3.4 px/m. Roads and field edges are legible. Has NO
                water and NO buildings, so it complements rather than replaces.
    grid        our probed passability alone
"""
import io
import json
import math
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(HERE, 'painter_settings.json')


def _looks_like_game(d):
    """A WoT 0.8.2 install has these three, and nothing else does."""
    return (d and os.path.isdir(os.path.join(d, 'res', 'packages'))
            and os.path.isdir(os.path.join(d, 'res', 'scripts', 'arena_defs'))
            and os.path.isfile(os.path.join(d, 'WorldOfTanks.exe')))


def find_game(explicit=None):
    """Locate the WoT install.

    The painter ships inside the mod, so the common case is that it is sitting
    in <game>/offhangar_tools/ and the answer is one directory up. Everything
    else is a fallback for a copy that has been moved somewhere else.

    Order: explicit argument, remembered setting, our own location, the usual
    install paths. Returns None rather than guessing wrong - the UI then asks.
    """
    cands = []
    if explicit:
        cands.append(explicit)
    try:
        if os.path.isfile(SETTINGS):
            with io.open(SETTINGS, encoding='utf-8') as f:
                cands.append(json.load(f).get('game_dir'))
    except Exception:
        pass
    # Bundled, the tool ships at
    #   <game>/res_mods/0.8.2/scripts/offhangar_tools/
    # so the game root is four levels up - but walk further than that anyway, so
    # a copy dropped anywhere inside the install still finds it.
    _d = HERE
    for _ in range(6):
        _d = os.path.abspath(os.path.join(_d, '..'))
        cands.append(_d)
    cands.append(os.environ.get('WOT_082_DIR'))
    for drive in 'CDEFGH':
        for tail in (r':\WOT_classic\WoT', r':\Games\World_of_Tanks',
                     r':\World_of_Tanks', r':\Games\WoT'):
            cands.append(drive + tail)
    for c in cands:
        if _looks_like_game(c):
            return os.path.abspath(c)
    return None


def set_game_dir(d):
    """Point every derived path at this install."""
    global GAME, OLD_GUI, ARENA_DEFS, PACKAGES, NAV_DUMP, MOD_DIR, OUT_DIR, ARENAS_MO
    GAME = d
    OLD_GUI = os.path.join(GAME, 'res/packages/gui.pkg')
    ARENA_DEFS = os.path.join(GAME, 'res/scripts/arena_defs')
    PACKAGES = os.path.join(GAME, 'res/packages')
    NAV_DUMP = os.path.join(GAME, 'offhangar_user/nav_dump')
    ARENAS_MO = os.path.join(GAME, 'res/text/LC_MESSAGES/arenas.mo')
    # Profiles are written into the INSTALLED mod, so a paint is live on the
    # next battle. The repo tree is used instead when we are running from it,
    # so development does not write into res_mods.
    repo = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                        'scripts', 'client', 'gui', 'mods', 'offhangar')
    installed = os.path.join(GAME, 'res_mods', '0.8.2', 'scripts', 'client',
                             'gui', 'mods', 'offhangar')
    MOD_DIR = repo if os.path.isdir(repo) else installed
    OUT_DIR = os.path.join(MOD_DIR, 'painted')
    return GAME



GAME = find_game()
if GAME is None:
    # Keep the module importable; the UI reports it and offers a chooser.
    GAME = ''
    OLD_GUI = ARENA_DEFS = PACKAGES = NAV_DUMP = ARENAS_MO = ''
    MOD_DIR = OUT_DIR = ''
else:
    set_game_dir(GAME)

import importlib.util as _ilu

# Load these BY PATH - MOD_DIR holds the mod's own logging.py, which would
# shadow the stdlib logging that PIL imports.
_s = _ilu.spec_from_file_location('offh_nav_grid', os.path.join(MOD_DIR, 'nav_grid.py'))
NG = _ilu.module_from_spec(_s)
_s.loader.exec_module(NG)
_s = _ilu.spec_from_file_location('offh_pxml', os.path.join(HERE, 'packedxml.py'))
PX = _ilu.module_from_spec(_s)
_s.loader.exec_module(PX)
_s = _ilu.spec_from_file_location('offh_bot_routes', os.path.join(MOD_DIR, 'bot_routes.py'))
BR = _ilu.module_from_spec(_s)
_s.loader.exec_module(BR)
_s = _ilu.spec_from_file_location('offh_i18n', os.path.join(HERE, 'painter_i18n.py'))
I18N = _ilu.module_from_spec(_s)
_s.loader.exec_module(I18N)
L = I18N.L

PREFS = os.path.join(HERE, 'painter_prefs.json')


def load_prefs():
    try:
        return json.load(open(PREFS))
    except Exception:
        return {}


def save_prefs(d):
    try:
        json.dump(d, open(PREFS, 'w'))
    except Exception:
        pass

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from PIL import Image, ImageTk

CLASSES = ('heavy', 'medium', 'light', 'td', 'spg')
CLASS_KEYS = {'1': 'heavy', '2': 'medium', '3': 'light', '4': 'td', '5': 'spg',
              'h': 'heavy', 'm': 'medium', 'l': 'light', 't': 'td', 's': 'spg'}
CLASS_COLOUR = {'heavy': '#e74c3c', 'medium': '#f39c12', 'light': '#2ecc71',
                'td': '#3498db', 'spg': '#9b59b6'}
# Bright amber, deliberately in no class palette: a flagged item must not read
# as "a light tank point".
WARN_COLOUR = '#ffd23f'
LINEBREAK2 = chr(10) + chr(10)
TEAM_OUTLINE = {1: '#ffffff', 2: '#000000', 0: '#888888'}
TEAM_NAME = {1: 'team1', 2: 'team2', 0: 'both'}

# The canvas is dark map imagery; wrapping it in default light-grey Tk chrome
# fought with it constantly. One dark palette throughout, and the class colours
# become the only saturated thing on screen - which is what you want to read.
BG      = '#1b1d21'
PANEL   = '#25282e'
RAISED  = '#31353d'
FG      = '#dfe1e5'
MUTED   = '#8b9099'
ACCENT  = '#4a9eff'
BORDER  = '#3a3f47'

STATE_LABEL = (('lg.passable', (150, 190, 120)), ('lg.painted', (200, 60, 60)),
               ('lg.unknown', (120, 120, 120)))

STATE_COLOUR = {
    NG.PASSABLE: (150, 190, 120),
    NG.BLOCKED_PAINTED: (200, 60, 60),
    NG.UNKNOWN: (120, 120, 120),
}


# --- map discovery ----------------------------------------------------------
def arena_bounds(map_name):
    """(minX, minZ, maxX, maxZ) from the map's own arena_def, read offline."""
    p = os.path.join(ARENA_DEFS, map_name + '.xml')
    if not os.path.exists(p):
        return None
    try:
        s = PX.parse(open(p, 'rb').read())
        bb = PX.get(s, 'boundingBox')
        bl = [float(v) for v in bb['bottomLeft'][0].split()]
        ur = [float(v) for v in bb['upperRight'][0].split()]
        return (bl[0], bl[1], ur[0], ur[1])
    except Exception:
        return None


def team_bases(map_name):
    """{team: [(x, z), ...]} from the arena_def, read offline.

    You are painting positions RELATIVE to where the teams start, so without
    these on screen you are painting blind. Verified against the game's own log
    for Malinovka: team1 (75.6, -391.9), team2 (-372.7, 108.1).
    """
    p = os.path.join(ARENA_DEFS, map_name + '.xml')
    if not os.path.exists(p):
        return {}
    out = {}
    try:
        s = PX.parse(open(p, 'rb').read())
        gt = PX.get(s, 'gameplayTypes') or {}
        for mode in ('ctf', 'assault', 'domination'):
            node = gt.get(mode)
            if not node or not isinstance(node[0], dict):
                continue
            tb = node[0].get('teamBasePositions')
            if not tb or not isinstance(tb[0], dict):
                continue
            for tname, val in tb[0].items():
                if not tname.startswith('team'):
                    continue
                try:
                    t = int(tname[4:])
                except ValueError:
                    continue
                pts = []
                for _k, v in (val[0].items() if isinstance(val[0], dict) else []):
                    xy = v[0] if isinstance(v[0], (list, tuple)) else v
                    if isinstance(xy, (list, tuple)) and len(xy) >= 2:
                        pts.append((float(xy[0]), float(xy[1])))
                if pts:
                    out.setdefault(t, []).extend(pts)
            if out:
                break
    except Exception:
        pass
    return out


def discover_maps():
    out = []
    if not os.path.isdir(ARENA_DEFS):
        return out
    for f in sorted(os.listdir(ARENA_DEFS)):
        if not f.endswith('.xml') or not f[0].isdigit():
            continue
        name = f[:-4]
        b = arena_bounds(name)
        if not b:
            continue
        out.append({'name': name, 'bounds': b,
                    'grid': os.path.exists(os.path.join(NAV_DUMP, name + '.grid')),
                    'painted': os.path.exists(os.path.join(OUT_DIR, name + '.paint.json'))})
    return out


def space_extent(map_name):
    try:
        import struct
        z = zipfile.ZipFile(os.path.join(PACKAGES, '%s.pkg' % map_name))
        n = [x for x in z.namelist() if x.endswith('space.settings')]
        if not n:
            return None
        d = z.read(n[0])
        if struct.unpack_from('<I', d, 0)[0] != 0x62A14E45:
            return None
        s = PX.parse(d)
        b = PX.get(s, 'bounds')
        if not b:
            return None
        cs = PX.get(s, 'chunkSize') or 100.0
        return (b['maxX'][0] - b['minX'][0] + 1) * float(cs)
    except Exception:
        return None


def load_map_asset(map_name, which, size):
    import io as _io
    try:
        if which == 'gui':
            z = zipfile.ZipFile(OLD_GUI)
            name = 'gui/maps/icons/map/%s.png' % map_name
            if name not in z.namelist():
                return None
            return Image.open(_io.BytesIO(z.read(name))).convert('RGB').resize(size, Image.LANCZOS)
        stem = 'color_tex' if which == 'colortex' else 'mmap'
        z = zipfile.ZipFile(os.path.join(PACKAGES, '%s.pkg' % map_name))
        hit = None
        for n in z.namelist():
            l = n.lower()
            if not l.endswith('.dds') or stem not in l:
                continue
            if stem == 'color_tex' and 'landscape' not in l:
                continue
            hit = n
            break
        if hit is None:
            return None
        return Image.open(_io.BytesIO(z.read(hit))).convert('RGB').resize(size, Image.LANCZOS)
    except Exception:
        return None


def point_in_poly(x, z, poly):
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, zi = poly[i]
        xj, zj = poly[j]
        if (zi > z) != (zj > z) and x < (xj - xi) * (z - zi) / (zj - zi + 1e-12) + xi:
            inside = not inside
        j = i
    return inside


# Recording budget. Frames are held in memory as palette images, so this is
# bounded on purpose: 200 x 560x560 in P mode is about 60 MB.
REC_FPS = 8
REC_MAX_FRAMES = 200
REC_WIDTH = 560


def _hex(rgb):
    return '#%02x%02x%02x' % rgb


def style_button(b, active=False, colour=None):
    b.config(bd=0, highlightthickness=0, relief='flat',
             activeforeground=FG, activebackground=RAISED,
             bg=(colour or ACCENT) if active else PANEL,
             fg='#101216' if active else FG)


class Tooltip(object):
    """Hover help, so every control explains itself without the docs open."""

    def set_text(self, text):
        self.text = text

    def __init__(self, widget, text):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind('<Enter>', self.show, add='+')
        widget.bind('<Leave>', self.hide, add='+')
        widget.bind('<ButtonPress>', self.hide, add='+')

    def show(self, _e=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry('+%d+%d' % (x, y))
        tk.Label(self.tip, text=self.text, justify='left', background='#0f1114',
                 foreground=FG, relief='solid', borderwidth=1, font=('Consolas', 8),
                 highlightbackground=BORDER, wraplength=430).pack(ipadx=6, ipady=4)

    def hide(self, _e=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


def mesh_path(name):
    """Where this map's mesh is, user copy first, or None.

    Mirrors the runtime's own lookup order: the user's own bake wins over the
    one shipped with the mod.
    """
    for d in (NAV_DUMP, os.path.join(MOD_DIR, 'navmesh')):
        p = os.path.join(d, name + '.grid')
        if os.path.isfile(p):
            return p
    return None


def _default_map(names):
    """Remembered map if it still has a mesh, else any measured map, else the
    first one - so the editor opens on something usable."""
    last = load_prefs().get('last_map')
    if last in names and mesh_path(last):
        return last
    for n in names:
        if mesh_path(n):
            return n
    return last if last in names else names[0]


class Painter(object):
    VIEWS = ('blend', 'minimap', 'terrain 4k', 'grid')
    VIEW_KEYS = ('v.blend', 'v.minimap', 'v.terrain', 'v.grid')
    MODES = {'p': 'dest', 'w': 'route', 'x': 'avoid', 'a': 'astar'}
    MODE_HELP = {'dest': 'place points', 'route': 'draw a route (Enter to finish)',
                 'avoid': 'draw an avoid area (Enter to close)',
                 'astar': 'A* probe: click start then end'}
    # Nominal canvas edge. Replaced per-instance by _pick_canvas() with
    # whatever the screen can actually afford - painting accuracy is limited by
    # how many pixels a cell gets, and a 700 px map on a 1440 px screen was
    # throwing away more than half the available precision.
    CANVAS = 760
    CANVAS_MIN = 560
    CANVAS_MAX = 1600
    CHROME_H = 120           # toolbar + status bar + window frame
    CHROME_W = 640           # the left panel

    def __init__(self, root, map_name=None):
        self.root = root
        self.maps = discover_maps()
        if not self.maps:
            raise SystemExit('no arena_defs found under %s' % ARENA_DEFS)
        names = [m['name'] for m in self.maps]
        # Open on a map that HAS a mesh where possible. Landing on an
        # unmeasured one greets the user with a dialog before they have done
        # anything, and the first thing they see should be a working map.
        self.map_name = map_name if map_name in names else _default_map(names)

        # shell state (survives a map switch)
        self.sel = set(['heavy'])
        self.team = 1
        self.filter_team = False
        self.cls_filter = set()          # empty = show every class
        self.view = 0
        self.show_wg = True
        self.mode = 'dest'
        self._i18n = []          # (widget, kind, key, args) for retranslation
        self._tips = []          # (Tooltip, key, args)
        self.rec_frames = None
        self.rec_times = None
        self.rec_stop = False
        self._msg = ''

        I18N.set_lang(load_prefs().get('lang', 'en'))
        root.title('offhangar bot-profile painter')
        root.configure(bg=BG)
        self._build_toolbar(root)
        body = tk.Frame(root, bg=BG)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._build_side(body)
        self.canvas = tk.Canvas(body, width=getattr(self, 'W', self.CANVAS),
                                height=getattr(self, 'H', self.CANVAS),
                                highlightthickness=0, bg=BG, cursor='crosshair')
        self.canvas.pack(side=tk.LEFT, padx=(0, 8), pady=8)
        tk.Frame(root, bg=BORDER, height=1).pack(side=tk.TOP, fill=tk.X)
        self.status = tk.Label(root, anchor='w', font=('Consolas', 9), justify='left',
                               bg=PANEL, fg=FG, padx=8, pady=4)
        self.status.pack(side=tk.TOP, fill=tk.X)

        root.protocol('WM_DELETE_WINDOW', self.on_close)
        self.canvas.bind('<Button-1>', self.on_left)
        self.canvas.bind('<Control-Button-1>', self.on_ctrl_left)
        self.canvas.bind('<Leave>', lambda e: (self.canvas.delete('preview'), None)[1])
        self.canvas.bind('<Button-3>', self.on_right)
        self.canvas.bind('<Motion>', self.on_move)
        root.bind('<Key>', self.on_key)
        root.bind('<Return>', lambda e: self.finish())
        root.bind('<Escape>', lambda e: self.cancel())
        root.bind('<Tab>', self.cycle_team)
        root.bind('<Control-s>', lambda e: self.save())
        root.bind('<F12>', lambda e: self.export_png())
        root.bind('<F9>', lambda e: self.toggle_record())
        self.load_map(self.map_name)

    # -- map switching ------------------------------------------------------
    def load_map(self, name, confirm=True):
        if confirm and getattr(self, 'dirty', False):
            r = messagebox.askyesnocancel(L('d.unsaved'),
                                          L('d.saveswitch',
                                            I18N.map_display_name(self.map_name, ARENAS_MO)))
            if r is None:
                self.map_var.set(self.map_name)
                return
            if r:
                self.save()
        self.map_name = name
        info = [m for m in self.maps if m['name'] == name][0]
        b = info['bounds']
        gp = mesh_path(name)
        self.has_grid = gp is not None
        if self.has_grid:
            self.g = NG.NavGrid.load(gp)
            # A mesh whose rect disagrees with this map's arena would put the
            # passability shading over the wrong area, so treat it as absent -
            # one battle re-measures it.
            _want = (b[0], b[1], b[2], b[3])
            if not self.g.fits(_want):
                self.has_grid = False
                self.g = NG.NavGrid(_want, name)
                self._stale_mesh = True
        else:
            # A bounds-only grid: every cell UNKNOWN. Painting needs nothing more
            # than the coordinate mapping, so this keeps all 33 maps usable while
            # only the passability checks and the A* test go quiet.
            #
            # Built over the whole SPACE, matching what the game measures: the
            # arena rectangle is a gameplay limit, not the edge of the map, and
            # a placeholder that stopped at it would disagree with every baked
            # grid and put painted points outside the cells that exist.
            self.g = NG.NavGrid((b[0], b[1], b[2], b[3]), name)
            self._stale_mesh = False
        g = self.g
        # Pick the largest whole number of pixels per cell that still fits the
        # screen. Whole pixels keep the grid crisp and keep to_canvas/to_world
        # exact inverses; the leftover is given back rather than left as dead
        # margin, so the map is as large as the display allows.
        # The canvas IS the arena rectangle, because the minimap image covers
        # exactly that. Everything then agrees by construction: picture, grid and
        # coordinates share one rect, so nothing paintable is off-grid.
        #
        # This is also why Himmelsdorf looked "super cropped": the picture was
        # being cropped as though it covered the whole 1000 m space, when it
        # actually covers the 700 m arena - so a third was cut off and the rest
        # shifted by the arena's 50 m offset.
        self.disp = (b[0], b[1], b[2], b[3])
        # Size against the SPACE, which is what the canvas now shows - sizing
        # against the arena made Himmelsdorf 1857 px, taller than the screen.
        _span = max(self.disp[2] - self.disp[0], self.disp[3] - self.disp[1])
        self.scale = self._pick_scale(_span / float(g.cell))
        _mpp = g.cell / float(self.scale)            # metres per pixel
        self.W = max(64, int(round((self.disp[2] - self.disp[0]) / _mpp)))
        self.H = max(64, int(round((self.disp[3] - self.disp[1]) / _mpp)))
        self.canvas.config(width=self.W, height=self.H)

        self.destinations, self.routes, self.avoid = [], [], []
        self.draft, self.undo_stack = [], []
        self.astar_from = self.astar_path = None
        self.selected = None
        self.dirty = False

        self.wg_nodes = self._load_wg_nodes()
        self.bases = team_bases(name)
        self.reach = None        # cells actually drivable, for warnings only
        self.bad = []            # painted coordinates the game cannot use
        # The picture covers the whole space and so does the canvas, so there
        # is nothing to crop. Loaded BEFORE the grid layer, which uses it as its
        # backdrop outside the arena.
        self.mini_img = (load_map_asset(name, 'mmap', (self.W, self.H))
                         or load_map_asset(name, 'gui', (self.W, self.H)))
        self.grid_img = self._grid_layer()
        self.layers = {'grid': self.grid_img}
        self._compose()
        self.load()
        self.map_var.set(self._map_label(name))
        self.flash(u'%s  %s' % (I18N.map_display_name(name, ARENAS_MO),
                               L('m.gridloaded') if self.has_grid else L('m.nogrid')))
        if not self.has_grid:
            # Painting still works off the arena bounds, but passability, the
            # A* test and the audit are all blind without a mesh - and the fix
            # is one battle, so say so rather than let it be discovered.
            self._offer_generate(name)
        # After the banner, so an unreachable-points warning is what stays on
        # screen - it is the more useful of the two.
        self._recompute_reach()
        self.refresh_items()
        self.redraw()

    # -- UI construction ----------------------------------------------------
    def _build_toolbar(self, root):
        bar = tk.Frame(root, bg=PANEL)
        bar.pack(side=tk.TOP, fill=tk.X)
        tk.Frame(root, bg=BORDER, height=1).pack(side=tk.TOP, fill=tk.X)
        self.buttons = {}

        def group(gkey):
            tk.Frame(bar, bg=BORDER, width=1).pack(side=tk.LEFT, fill=tk.Y, pady=5, padx=(7, 0))
            w = tk.Label(bar, text=L(gkey), font=('Consolas', 7), fg=MUTED, bg=PANEL)
            w.pack(side=tk.LEFT, padx=(6, 3))
            self._i18n.append((w, 'text', gkey, ()))

        def button(tkey, cmd, tipkey, key=None, targs=(), tipargs=()):
            b = tk.Button(bar, text=L(tkey, *targs), command=cmd,
                          font=('Consolas', 8), padx=7, pady=3)
            style_button(b)
            b.pack(side=tk.LEFT, padx=1, pady=4)
            t = Tooltip(b, L(tipkey, *tipargs) + (('   [%s]' % key) if key else ''))
            self._i18n.append((b, 'text', tkey, targs))
            self._tips.append((t, tipkey, tipargs, key))
            return b

        group('g.mode')
        for key, mode in (('p', 'dest'), ('w', 'route'), ('x', 'avoid'), ('a', 'astar')):
            bk = {'dest': 'b.point', 'route': 'b.route',
                  'avoid': 'b.avoid', 'astar': 'b.astar'}[mode]
            tk_ = {'dest': 't.point', 'route': 't.route',
                   'avoid': 't.avoid', 'astar': 't.astar'}[mode]
            self.buttons['mode_' + mode] = button(bk, lambda m=mode: self.set_mode(m), tk_, key)

        group('g.team')
        for t in (1, 2, 0):
            self.buttons['team_%d' % t] = button(
                'team.%d' % t, lambda tt=t: self.set_team(tt), 't.team', 'Tab')

        group('g.class')
        for c in CLASSES:
            self.buttons['cls_' + c] = button(
                'cls.' + c, lambda cc=c: self.toggle_class(cc), 't.class',
                {'heavy': '1', 'medium': '2', 'light': '3', 'td': '4', 'spg': '5'}[c],
                tipargs=(L('name.' + c),))

        group('g.view')
        button('b.layer', self.cycle_view, 't.layer', 'v')
        self.buttons['filter'] = button('b.teamflt', self.toggle_filter, 't.teamflt', 'f')
        self.buttons['wg'] = button('b.wg', self.toggle_wg, 't.wg', 'r')

        group('g.edit')
        button('b.undo', self.undo, 't.undo', 'u')
        button('b.delete', self.delete_near, 't.delete', 'd')

        group('g.file')
        self.save_btn = tk.Button(bar, text=L('b.save'), command=self.save,
                                  font=('Consolas', 8, 'bold'), padx=10, pady=3)
        style_button(self.save_btn)
        self.save_btn.pack(side=tk.LEFT, padx=(2, 1), pady=4)
        st = Tooltip(self.save_btn, L('t.save'))
        self._tips.append((st, 't.save', (), None))
        button('b.reload', self.reload, 't.reload')
        button('b.import', self.do_import, 't.import')
        button('b.reset', self.reset_map, 't.reset')
        self.buttons['rec'] = button('b.rec', self.toggle_record,
               't.rec', 'F9', tipargs=(REC_MAX_FRAMES, REC_MAX_FRAMES // REC_FPS))
        button('b.png', self.export_png, 't.png', 'F12')
        button('b.folder', self.open_folder, 't.folder')
        self.buttons['lang'] = button('b.lang', self.toggle_lang, 't.lang')

    def _build_side(self, parent):
        side = tk.Frame(parent, width=258, bg=BG)
        side.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 6), pady=8)
        side.pack_propagate(False)

        def head(hkey):
            w = tk.Label(side, text=L(hkey), font=('Consolas', 7, 'bold'), anchor='w',
                         fg=MUTED, bg=BG)
            w.pack(fill=tk.X, pady=(8, 2))
            self._i18n.append((w, 'text', hkey, ()))
        self._head = head

        head('h.map')
        self.map_var = tk.StringVar(value=self.map_name)
        st = ttk.Style()
        try:
            st.theme_use('clam')
        except tk.TclError:
            pass
        st.configure('D.TCombobox', fieldbackground=PANEL, background=PANEL,
                     foreground=FG, arrowcolor=FG, bordercolor=BORDER, lightcolor=PANEL,
                     darkcolor=PANEL, selectbackground=PANEL, selectforeground=FG)
        self.map_box = ttk.Combobox(side, textvariable=self.map_var, state='readonly',
                                    style='D.TCombobox',
                                    font=('Consolas', 8), values=self._map_labels())
        self.map_box.pack(fill=tk.X)
        self.map_box.bind('<<ComboboxSelected>>', self._on_map_pick)
        self._tips.append((Tooltip(self.map_box, L('t.map')), 't.map', (), None))
        nav = tk.Frame(side, bg=BG)
        nav.pack(fill=tk.X, pady=(4, 0))
        for bkey, d in (('b.prev', -1), ('b.next', 1)):
            b = tk.Button(nav, text=L(bkey), font=('Consolas', 8), pady=2,
                          command=lambda dd=d: self._step_map(dd))
            style_button(b)
            b.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
            self._i18n.append((b, 'text', bkey, ()))

        head('h.classes')
        cf = tk.Frame(side, bg=BG)
        cf.pack(fill=tk.X)
        self.cls_btn = {}
        for c in CLASSES:
            b = tk.Button(cf, text=L('cls.' + c), font=('Consolas', 8), padx=2, pady=2,
                          command=lambda cc=c: self.toggle_cls_filter(cc))
            style_button(b)
            b.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
            self._i18n.append((b, 'text', 'cls.' + c, ()))
            self._tips.append((Tooltip(b, L('t.clsfilter', L('name.' + c))),
                               't.clsfilter', ('#name.' + c,), None))
            self.cls_btn[c] = b

        head('h.coverage')
        self.cov = tk.Label(side, font=('Consolas', 8), anchor='w', justify='left',
                            fg=FG, bg=PANEL, padx=6, pady=4)
        self.cov.pack(fill=tk.X)
        self._tips.append((Tooltip(self.cov, L('t.coverage')), 't.coverage', (), None))

        mb = tk.Button(side, text=L('b.mirror'), font=('Consolas', 8), pady=3,
                       command=self.mirror_team)
        style_button(mb)
        mb.pack(fill=tk.X, pady=(4, 0))
        self._i18n.append((mb, 'text', 'b.mirror', ()))
        self._tips.append((Tooltip(mb, L('t.mirror')), 't.mirror', (), None))

        head('h.items')
        lf = tk.Frame(side, bg=BG)
        lf.pack(fill=tk.BOTH, expand=True)
        sb = tk.Scrollbar(lf, bg=PANEL, troughcolor=BG, bd=0, highlightthickness=0)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.items = tk.Listbox(lf, font=('Consolas', 8), yscrollcommand=sb.set,
                                activestyle='none', exportselection=False,
                                bg=PANEL, fg=FG, bd=0, highlightthickness=0,
                                selectbackground=ACCENT, selectforeground='#101216')
        self.items.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.config(command=self.items.yview)
        self.items.bind('<<ListboxSelect>>', self._on_item_pick)
        self.items.bind('<Delete>', lambda e: self.delete_selected())
        self._tips.append((Tooltip(self.items, L('t.items')), 't.items', (), None))
        ef = tk.Frame(side, bg=BG)
        ef.pack(fill=tk.X, pady=(4, 0))
        rb = tk.Button(ef, text=L('b.reassign'), font=('Consolas', 8), pady=2,
                       command=self.reassign_selected)
        style_button(rb)
        rb.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
        self._i18n.append((rb, 'text', 'b.reassign', ()))
        self._tips.append((Tooltip(rb, L('t.reassign')), 't.reassign', (), None))
        db = tk.Button(ef, text=L('b.delete'), font=('Consolas', 8), pady=2,
                       command=self.delete_selected)
        style_button(db)
        db.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
        self._i18n.append((db, 'text', 'b.delete', ()))
        self._tips.append((Tooltip(db, L('t.delsel')), 't.delsel', (), None))
        self._build_legend(side)
        _rl = tk.Label(side, text=L('l.role'), font=('Consolas', 7),
                       anchor='w', fg=MUTED, bg=BG)
        _rl.pack(fill=tk.X, pady=(8, 2))
        self._i18n.append((_rl, 'text', 'l.role', ()))
        self.role_var = tk.StringVar()
        re_ = tk.Entry(side, textvariable=self.role_var, font=('Consolas', 8),
                       bg=PANEL, fg=FG, insertbackground=FG, bd=0,
                       highlightthickness=1, highlightbackground=BORDER,
                       highlightcolor=ACCENT)
        re_.pack(fill=tk.X, ipady=3)
        re_.bind('<Return>', lambda e: self.set_role())
        self._tips.append((Tooltip(re_, L('t.role')), 't.role', (), None))

    def _build_legend(self, side):
        self._head('h.legend')
        lg = tk.Frame(side, bg=PANEL)
        lg.pack(fill=tk.X)
        for lkey, rgb in STATE_LABEL:
            row = tk.Frame(lg, bg=PANEL)
            row.pack(fill=tk.X, padx=6, pady=1)
            tk.Frame(row, bg=_hex(rgb), width=11, height=11).pack(side=tk.LEFT, pady=2)
            w = tk.Label(row, text=' ' + L(lkey), font=('Consolas', 7), fg=MUTED,
                         bg=PANEL, anchor='w')
            w.pack(side=tk.LEFT)
            self._i18n.append((w, 'legend', lkey, ()))
        self._tips.append((Tooltip(lg, L('t.legend')), 't.legend', (), None))

    def _recompute_reach(self):
        """Which cells can actually be driven to, for WARNINGS only.

        Nothing here blocks anything - blocking is the user's paint and only
        that. But a point on a rooftop is perfectly "passable" and still
        undrivable, so the tool should say so rather than let it be discovered
        in a battle. Informing is not deciding.
        """
        self.reach = None
        if not self.has_grid:
            return
        g = self.g
        # Drop the previous paint, re-derive, then apply the CURRENT areas, so
        # the grid always matches what is actually drawn. derive() on its own
        # never releases a painted cell (by design - a human's decision outranks
        # a later measurement), so without clear_painted() a deleted or reset
        # avoid area vanished from the drawing while its cells stayed blocked
        # forever: the A* test kept routing around nothing.
        g.clear_painted()
        g.derive()
        if self.avoid:
            g.apply_avoid(self.avoid, point_in_poly)
        else:
            g.build_clearance()
        self.grid_img = self._grid_layer()
        self.layers['grid'] = self.grid_img
        self._compose()
        # Use the GAME's own flood fill, seeded the same way (every base, both
        # teams). This had a second copy of that logic which seeded from only the
        # first base it found - with enough paint the two spawns can end up in
        # disconnected regions, and the painter would then call one team's whole
        # route set unreachable while the game happily drove it. A tool whose job
        # is to predict the runtime must not reimplement the runtime.
        seeds = []
        for t in sorted(self.bases or {}):
            for bx, bz in self.bases[t]:
                seeds.append((bx, bz))
        if not seeds:
            seeds = [((g.x0 + g.x1) / 2.0, (g.z0 + g.z1) / 2.0)]
        g.build_reach(seeds)
        self.reach = g.reach
        if not self.reach:
            return
        self.bad = self.audit()
        if hasattr(self, 'items'):
            self.refresh_items()
        # audit() already walked all of this, and distinguishes painted from
        # unreachable from off-map instead of lumping them together. Counting it
        # a second time here only created a second answer to drift from.
        tot = sum(len(r['points']) for r in self.routes) + len(self.destinations)
        if self.bad:
            self.flash(L('m.unreachcount', len(self.bad), tot))

    def _map_label(self, name):
        m = [x for x in self.maps if x['name'] == name]
        if not m:
            return name
        m = m[0]
        # In RU the game's OWN name is shown, with the file name kept in
        # brackets so the entry is still identifiable and sortable.
        disp = I18N.map_display_name(name, ARENAS_MO)
        label = name if disp == name else u'%s (%s)' % (disp, name)
        if m['grid']:
            label += u' [%s]' % L('tag.grid')
        if m['painted']:
            label += u' [%s]' % L('tag.painted')
        return label

    def _map_labels(self):
        return [self._map_label(m['name']) for m in self.maps]

    def _on_map_pick(self, _e=None):
        want = self.map_var.get()
        for m in self.maps:
            if self._map_label(m['name']) == want:
                self.load_map(m['name'])
                return
        self.load_map(want.split(' ')[0])

    def _step_map(self, d):
        names = [m['name'] for m in self.maps]
        i = (names.index(self.map_name) + d) % len(names)
        self.load_map(names[i])

    # -- item list ----------------------------------------------------------
    def refresh_items(self):
        self.items.delete(0, tk.END)
        self.index_map = []
        # Rows the audit objected to are flagged rather than merely counted, so
        # the bad step can be found instead of hunted for.
        _flag = {}
        for (k, n, si, _x, _z, _r) in (self.bad or []):
            _flag.setdefault((k, n), []).append(si)
        for n, d in enumerate(self.destinations):
            self.items.insert(tk.END, '%sP %-5s %-16s (%.0f,%.0f)'
                              % ('! ' if ('dest', n) in _flag else '',
                                 TEAM_NAME[d.get('team', 0)][-1] if d.get('team') else 'b',
                                 ','.join(d.get('classes') or []), d['pos'][0], d['pos'][1]))
            self.index_map.append(('dest', n))
            cs = d.get('classes') or []
            if ('dest', n) in _flag:
                self.items.itemconfig(tk.END, fg=WARN_COLOUR)
            elif cs:
                self.items.itemconfig(tk.END, fg=CLASS_COLOUR.get(cs[0], FG))
        for n, r in enumerate(self.routes):
            _f = _flag.get(('route', n))
            self.items.insert(tk.END, '%sR %-5s %-16s %d steps %.0fm%s'
                              % ('! ' if _f else '',
                                 TEAM_NAME[r.get('team', 0)][-1] if r.get('team') else 'b',
                                 ','.join(r.get('classes') or []), len(r['points']),
                                 self._length(r['points']),
                                 (' ' + L('m.audit.steps',
                                          ','.join(str(i + 1) for i in _f))) if _f else ''))
            self.index_map.append(('route', n))
            cs = r.get('classes') or []
            if _f:
                self.items.itemconfig(tk.END, fg=WARN_COLOUR)
            elif cs:
                self.items.itemconfig(tk.END, fg=CLASS_COLOUR.get(cs[0], FG))
        for n, _a in enumerate(self.avoid):
            self.items.insert(tk.END, 'X avoid area %d' % (n + 1))
            self.index_map.append(('avoid', n))
            self.items.itemconfig(tk.END, fg='#ff6b6b')
        if hasattr(self, 'buttons'):
            self._refresh_toolbar()
        if hasattr(self, 'cov'):
            self._refresh_coverage()

    def _on_item_pick(self, _e=None):
        s = self.items.curselection()
        self.selected = self.index_map[s[0]] if s else None
        if self.selected and hasattr(self, 'role_var'):
            kind, idx = self.selected
            if kind == 'dest':
                self.role_var.set(self.destinations[idx].get('role', ''))
            elif kind == 'route':
                self.role_var.set(self.routes[idx].get('name', ''))
            else:
                self.role_var.set('')
        self.redraw()

    def delete_selected(self):
        if not self.selected:
            self.flash(L('m.nothingsel'))
            return
        kind, idx = self.selected
        self.snapshot()
        {'dest': self.destinations, 'route': self.routes, 'avoid': self.avoid}[kind].pop(idx)
        self.selected = None
        self.touch()
        self.flash(L('m.deleted', L('m.k.' + kind)))
        self.refresh_items()
        self.redraw()

    def mirror_team(self):
        """Reflect team 1 through the map centre onto team 2."""
        src = [d for d in self.destinations if d.get('team') == 1]
        srcr = [r for r in self.routes if r.get('team') == 1]
        if not src and not srcr:
            self.flash(L('m.nomirror'))
            return
        have = ([d for d in self.destinations if d.get('team') == 2]
                + [r for r in self.routes if r.get('team') == 2])
        if have and not messagebox.askokcancel(L('d.mirror'),
                L('d.mirrorq', len(have), len(src) + len(srcr))):
            return
        g = self.g
        mx, mz = (g.x0 + g.x1) / 2.0, (g.z0 + g.z1) / 2.0

        def mir(x, z):
            return [round(2 * mx - x, 1), round(2 * mz - z, 1)]

        self.snapshot()
        for d in src:
            self.destinations.append({'pos': mir(d['pos'][0], d['pos'][1]),
                                      'classes': list(d.get('classes') or []),
                                      'team': 2, 'role': d.get('role', '')})
        for r in srcr:
            self.routes.append({'points': [mir(x, z) for x, z in r['points']],
                                'classes': list(r.get('classes') or []),
                                'team': 2, 'name': r.get('name', '')})
        self.touch()
        self.flash(L('m.mirrored', len(src), len(srcr)))
        self.refresh_items()
        self.redraw()

    def reassign_selected(self):
        if not self.selected:
            self.flash(L('m.nothingsel'))
            return
        kind, idx = self.selected
        if kind == 'avoid':
            self.flash(L('m.noteam'))
            return
        if not self.sel:
            self.flash(L('m.pickclass'))
            return
        self.snapshot()
        item = (self.destinations if kind == 'dest' else self.routes)[idx]
        item['team'] = self.team
        item['classes'] = sorted(self.sel)
        self.touch()
        self.flash(L('m.reassigned', L('team.%d' % self.team),
                     ','.join(L('cls.' + x) for x in sorted(self.sel))))
        self.refresh_items()
        self.redraw()

    def set_role(self):
        if not self.selected:
            return
        kind, idx = self.selected
        if kind == 'avoid':
            return
        self.snapshot()
        item = (self.destinations if kind == 'dest' else self.routes)[idx]
        item['role' if kind == 'dest' else 'name'] = self.role_var.get()
        self.touch()
        self.flash(L('m.noteset'))
        self.refresh_items()

    def _refresh_coverage(self):
        rows = []
        for t in (1, 2):
            done = set()
            for it in self.destinations + self.routes:
                if it.get('team') in (t, 0):
                    done.update(it.get('classes') or [])
            rows.append('t%d ' % t + ' '.join(c[:3] if c in done else '...' for c in CLASSES))
        rows.append('   ' + ' '.join(c[:3] for c in CLASSES))
        self.cov.config(text='\n'.join(rows))

    def _refresh_toolbar(self):
        for m in ('dest', 'route', 'avoid', 'astar'):
            b = self.buttons.get('mode_' + m)
            if b:
                style_button(b, self.mode == m, ACCENT)
        for t in (1, 2, 0):
            b = self.buttons.get('team_%d' % t)
            if b:
                style_button(b, self.team == t, '#ffc966')
        for c in CLASSES:
            b = self.buttons.get('cls_' + c)
            if b:
                style_button(b, c in self.sel, CLASS_COLOUR[c])
            fb = getattr(self, 'cls_btn', {}).get(c)
            if fb:
                style_button(fb, c in self.cls_filter, CLASS_COLOUR[c])
        rb = self.buttons.get('rec')
        if rb:
            style_button(rb, self.rec_frames is not None, '#ff5555')
        for name, flag in (('filter', self.filter_team), ('wg', self.show_wg)):
            b = self.buttons.get(name)
            if b:
                style_button(b, flag, ACCENT)
        if self.dirty:
            style_button(self.save_btn, True, '#ff7676')
            self.save_btn.config(text='Save \u25cf')
        else:
            style_button(self.save_btn)
            self.save_btn.config(text='Save')

    # -- toolbar actions ----------------------------------------------------
    def set_mode(self, m):
        if m == 'astar' and not self.has_grid:
            self.flash(L('m.needgrid'))
            return
        self.mode = m
        self.canvas.config(cursor='crosshair' if m != 'astar' else 'tcross')
        self.draft = []
        self.astar_from = self.astar_path = None
        self.flash(L('m.mode.' + m))
        self.redraw()

    def set_team(self, t):
        self.team = t
        self.flash(L('m.teamto', L('team.%d' % t)))
        self.redraw()

    def toggle_class(self, c):
        self.sel.symmetric_difference_update([c])
        self.flash(L('m.newpoints', ','.join(L('cls.' + x) for x in sorted(self.sel)) or L('m.none')))
        self.redraw()

    def toggle_cls_filter(self, c):
        self.cls_filter.symmetric_difference_update([c])
        self.flash(L('m.showing', ','.join(L('cls.' + x) for x in sorted(self.cls_filter)) or L('m.allclasses')))
        self.redraw()

    def cycle_view(self):
        self.view = (self.view + 1) % len(self.VIEWS)
        self.redraw()

    def toggle_filter(self):
        self.filter_team = not self.filter_team
        self.flash(L('m.teamfilter', L('m.only', L('team.%d' % self.team))
                            if self.filter_team else L('m.off')))
        self.redraw()

    def toggle_wg(self):
        self.show_wg = not self.show_wg
        self.redraw()

    def reload(self):
        if self.dirty and not messagebox.askokcancel(L('d.reload'), L('d.reloadq')):
            return
        self.destinations, self.routes, self.avoid = [], [], []
        self.undo_stack = []
        self.load()
        self.dirty = False
        self.refresh_items()
        self.redraw()

    def toggle_record(self):
        if getattr(self, 'rec_frames', None) is None:
            self.rec_frames = []
            self.rec_times = []
            self.rec_stop = False
            self.flash(L('m.recording'))
            self.root.after(50, self._rec_tick)
        else:
            self.rec_stop = True

    def _rec_tick(self):
        """Grab one frame, then reschedule. Runs off the Tk event loop so the UI
        stays responsive while recording."""
        if self.rec_frames is None:
            return
        if self.rec_stop or len(self.rec_frames) >= REC_MAX_FRAMES:
            self._rec_finish()
            return
        import time as _t
        _t0 = _t.time()
        try:
            im = self._grab_canvas()
            if im.size[0] > REC_WIDTH:
                im = im.resize((REC_WIDTH, int(im.size[1] * REC_WIDTH / float(im.size[0]))),
                               Image.LANCZOS)
            self.rec_frames.append(im.convert('P', palette=Image.ADAPTIVE, colors=128))
            self.rec_times.append(_t0)
            self.flash(L('m.recframes', len(self.rec_frames),
                        self.rec_times[-1] - self.rec_times[0]))
        except Exception as e:
            self.flash(L('m.recfail', e))
            self._rec_finish()
            return
        # Subtract the time the grab actually took. Scheduling a flat interval
        # ADDS to it, which held an 8 fps target to about 4 on a 3440x1440
        # screen - and then the GIF would play back at double speed.
        _spent = (_t.time() - _t0) * 1000.0
        self.root.after(max(5, int(1000.0 / REC_FPS - _spent)), self._rec_tick)

    def _grab_scale(self):
        """Physical pixels per logical pixel, measured once.

        ImageGrab works in physical pixels while winfo_* reports logical ones,
        so a scaled display would crop wrong without this. Measured once because
        the measurement itself needs a full-screen grab.
        """
        if getattr(self, '_gscale', None) is None:
            from PIL import ImageGrab
            self._gscale = (ImageGrab.grab().size[0]
                            / float(self.root.winfo_screenwidth()))
        return self._gscale

    def _grab_canvas(self):
        """Grab JUST the canvas rectangle.

        Grabbing the whole screen and cropping cost ~200 ms on a 3440x1440
        display, which held recording to about 3 fps against the 8 asked for.
        """
        from PIL import ImageGrab
        c = self.canvas
        k = self._grab_scale()
        x, y = c.winfo_rootx(), c.winfo_rooty()
        w, h = c.winfo_width(), c.winfo_height()
        return ImageGrab.grab(bbox=(int(x * k), int(y * k),
                                    int((x + w) * k), int((y + h) * k)))

    def _rec_finish(self):
        frames = self.rec_frames or []
        times = self.rec_times or []
        self.rec_frames = None
        self.rec_times = None
        self.rec_stop = False
        if len(frames) < 2:
            self.flash(L('m.recnone'))
            self.redraw()
            return
        if not os.path.isdir(OUT_DIR):
            os.makedirs(OUT_DIR)
        out = os.path.join(OUT_DIR, '%s_capture.gif' % self.map_name)
        # Use the MEASURED interval, not the requested one, or a run that could
        # not keep up plays back too fast.
        span = (times[-1] - times[0]) if len(times) > 1 else 0.0
        per = int(span * 1000.0 / max(1, len(frames) - 1)) if span else int(1000.0 / REC_FPS)
        per = max(20, min(500, per))
        try:
            frames[0].save(out, save_all=True, append_images=frames[1:],
                           duration=per, loop=0, optimize=True)
            self.flash(L('m.recsaved', os.path.basename(out), len(frames), span,
                        int(round(1000.0 / per)), os.path.getsize(out) / 1048576.0))
        except Exception as e:
            self.flash(L('m.giffail', e))
        self.redraw()

    def export_png(self):
        """Save what is on the canvas, as a file.

        Xbox Game Bar cannot record this window: Tk draws through plain GDI with
        no swapchain, and Game Bar's capture path wants a composited/DirectX
        surface, so it either refuses the window or records black. Nothing in
        the app can change that - so it exports the pixels itself.
        """
        try:
            from PIL import ImageGrab
        except ImportError:
            self.flash(L('m.nograb'))
            return
        if not os.path.isdir(OUT_DIR):
            os.makedirs(OUT_DIR)
        self.root.lift()
        self.root.update()
        self.root.after(120, self._grab_now)

    def _grab_now(self):
        out = os.path.join(OUT_DIR, '%s.png' % self.map_name)
        try:
            im = self._grab_canvas()
            im.save(out)
            self.flash(L('m.exported', os.path.basename(out), im.size[0], im.size[1]))
        except Exception as e:
            self.flash(L('m.exportfail', e))

    def toggle_lang(self):
        I18N.set_lang('ru' if I18N.lang() == 'en' else 'en')
        d = load_prefs()
        d['lang'] = I18N.lang()
        save_prefs(d)
        self.retranslate()

    def retranslate(self):
        """Re-label every registered widget and tooltip in place.

        Rebuilding the whole UI would lose the current selection, the draft
        being drawn and the scroll position; relabelling keeps all of it.
        """
        for w, kind, key, args in self._i18n:
            try:
                txt = L(key, *args)
                w.config(text=(' ' + txt) if kind == 'legend' else txt)
            except tk.TclError:
                pass
        for t, key, args, sc in self._tips:
            # '#name.x' means "look this up in the CURRENT language first".
            real = tuple(L(a[1:]) if isinstance(a, str) and a.startswith('#') else a
                         for a in args)
            t.set_text(L(key, *real) + (('   [%s]' % sc) if sc else ''))
        self.map_box.config(values=self._map_labels())
        self.map_var.set(self._map_label(self.map_name))
        self.refresh_items()
        self.redraw()

    def open_folder(self):
        if not os.path.isdir(OUT_DIR):
            os.makedirs(OUT_DIR)
        try:
            os.startfile(OUT_DIR)
        except Exception:
            self.flash(OUT_DIR)

    def on_close(self):
        if self.dirty:
            r = messagebox.askyesnocancel(L('d.unsaved'), L('d.saveclose'))
            if r is None:
                return
            if r:
                self.save()
        self.root.destroy()

    def touch(self):
        self.dirty = True

    def snapshot(self):
        """Push a full copy before mutating.

        The first version pushed (kind, index) and undid by popping that index -
        which silently removes the WRONG item once anything earlier has been
        deleted, because the indices have shifted underneath. Demonstrated and
        replaced. A map holds tens of items, so copying is free.
        """
        import copy
        self.undo_stack.append((copy.deepcopy(self.destinations),
                                copy.deepcopy(self.routes),
                                copy.deepcopy(self.avoid)))
        del self.undo_stack[:-40]

    # -- data ---------------------------------------------------------------
    def _load_wg_nodes(self):
        for p in (os.path.join(HERE, '%s.routes.json' % self.map_name),
                  os.path.join(MOD_DIR, 'routes', '%s.routes.json' % self.map_name)):
            if os.path.exists(p):
                try:
                    return json.load(open(p))['nodes']['pos']
                except Exception:
                    pass
        return []

    def out_path(self):
        return os.path.join(OUT_DIR, '%s.paint.json' % self.map_name)

    def reset_map(self):
        """Clear everything painted for this map, and remove its saved profile.

        Painting a map is real work, so this asks first, keeps the .bak the save
        path already writes, and stays UNDOABLE in memory - undo brings the
        painting back and the next save re-creates the file.
        """
        n = len(self.destinations) + len(self.routes) + len(self.avoid)
        if not n and not os.path.exists(self.out_path()):
            self.flash(L('m.resetempty'))
            return
        if not messagebox.askokcancel(
                L('d.resettitle'),
                L('d.resetq', n, I18N.map_display_name(self.map_name, ARENAS_MO))):
            return
        self.snapshot()
        self.destinations, self.routes, self.avoid = [], [], []
        self.draft = []
        self.selected = None
        self.astar_from = self.astar_path = None
        removed = False
        try:
            if os.path.exists(self.out_path()):
                # Keep the same one-generation backup the save path relies on,
                # so a reset is no more destructive than an overwrite.
                try:
                    import shutil
                    shutil.copyfile(self.out_path(), self.out_path() + '.bak')
                except Exception:
                    pass
                os.remove(self.out_path())
                removed = True
        except Exception as e:
            self.flash(L('m.import.bad', str(e)))
            return
        for m in self.maps:
            if m['name'] == self.map_name:
                m['painted'] = False
        self.dirty = False
        self._recompute_reach()
        self.refresh_items()
        self.redraw()
        self.flash(L('m.resetdone', n) if removed else L('m.resetcleared', n))

    def save(self):
        if not os.path.isdir(OUT_DIR):
            os.makedirs(OUT_DIR)
        # Keep one generation back: painting a map is 10+ minutes of human
        # work and an accidental overwrite would be miserable.
        if os.path.exists(self.out_path()):
            try:
                import shutil
                shutil.copyfile(self.out_path(), self.out_path() + '.bak')
            except Exception:
                pass
        # Built by the MOD's own bot_routes.profile_document, so the writer and
        # the reader cannot drift apart as the format evolves.
        doc = BR.profile_document(self.map_name,
                                  (self.g.x0, self.g.z0, self.g.x1, self.g.z1),
                                  self.destinations, self.routes, self.avoid)
        with open(self.out_path(), 'w') as f:
            json.dump(doc, f, indent=1)
        self.dirty = False
        for m in self.maps:
            if m['name'] == self.map_name:
                m['painted'] = True
        self.map_box.config(values=self._map_labels())
        self.flash(L('m.saved', len(self.destinations), len(self.routes), len(self.avoid)))

    def load(self):
        p = self.out_path()
        if not os.path.exists(p):
            return
        try:
            d = json.load(open(p))
            # Validate through the mod's parser so the painter can never save
            # something the game would then reject. Files written before the
            # format was versioned have no 'format' key - accept those too
            # rather than making the user repaint.
            if d.get('format') and BR.parse_profile(d) is None:
                self.flash('profile rejected by the loader - wrong format or newer version')
                return
            self.destinations = d.get('destinations', [])
            self.routes = d.get('routes', [])
            self.avoid = d.get('avoid', [])
        except Exception as e:
            print('load failed: %s' % e)

    # -- geometry -----------------------------------------------------------
    def _grid_layer(self):
        """The passability grid, placed where it belongs on the full-space canvas.

        The grid only covers the arena bounds, which is a sub-rectangle of the
        space now that the whole map is shown - so it is pasted at its own
        position rather than stretched over everything.
        """
        g = self.g
        # The map, with ONLY the painted keep-out areas tinted over it.
        #
        # Shading passable ground drew a coloured rectangle around the arena
        # bounds - the "green box" - which said nothing useful: the arena is not
        # a place bots are forbidden, it is just where the grid happens to have
        # cells. The one thing worth seeing is what YOU have blocked, so that is
        # the only thing drawn.
        base = (self.mini_img.copy() if getattr(self, 'mini_img', None)
                else Image.new('RGB', (self.W, self.H), (40, 40, 40)))
        try:
            m = Image.new('L', (g.nx, g.nz), 0)
            mp = m.load()
            any_blocked = False
            for i in range(g.n):
                if g.state[i] == NG.BLOCKED_PAINTED:
                    ix, iz = g.coords(i)
                    mp[ix, g.nz - 1 - iz] = 150      # tint strength
                    any_blocked = True
            if any_blocked:
                x0, y0 = self.to_canvas(g.x0, g.z1)  # arena's top-left corner
                x1, y1 = self.to_canvas(g.x1, g.z0)
                w = max(1, int(round(x1 - x0)))
                h = max(1, int(round(y1 - y0)))
                tint = Image.new('RGB', (w, h), STATE_COLOUR[NG.BLOCKED_PAINTED])
                base.paste(tint, (int(round(x0)), int(round(y0))),
                           m.resize((w, h), Image.NEAREST))
        except Exception:
            pass
        return base

    def _pick_scale(self, cells):
        """Largest integer px-per-cell the screen can show."""
        try:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
        except Exception:
            sw, sh = 1280, 800
        room = min(max(self.CANVAS_MIN, sh - self.CHROME_H),
                   max(self.CANVAS_MIN, sw - self.CHROME_W),
                   self.CANVAS_MAX)
        return max(1, int(room // max(1, cells)))

    def to_world(self, px, py):
        """Canvas pixel -> world (x, z), the exact inverse of to_canvas.

        This used to snap to the CENTRE of whatever cell was clicked, which quietly
        made the outer half-cell of the map unreachable: on Prokhorovka the far edge
        was +495 no matter how far right you clicked, and on a coarse map that is
        the whole outer strip. The edges are exactly where the interesting painting
        is - flanks, ridge lines, the routes that hug a border - so the mapping is
        now continuous and only CLAMPED to the bounds.
        """
        dx0, dz0, dx1, dz1 = self.disp
        fx = min(1.0, max(0.0, px / float(self.W))) if self.W else 0.0
        fy = min(1.0, max(0.0, py / float(self.H))) if self.H else 0.0
        x = dx0 + fx * (dx1 - dx0)
        z = dz1 - fy * (dz1 - dz0)             # canvas y grows downward
        return (x, z)

    def to_canvas(self, x, z):
        """World -> canvas pixel, against the DISPLAYED rect.

        The displayed rect is the whole map space, not the arena bounds: on
        Himmelsdorf the arena is 700 m of a 1000 m space, so cropping to it threw
        away the railway yard and everything else outside the red line - which is
        still map you want to see while painting.
        """
        dx0, dz0, dx1, dz1 = self.disp
        return ((x - dx0) / float(dx1 - dx0) * self.W,
                (dz1 - z) / float(dz1 - dz0) * self.H)

    def visible(self, item):
        if self.filter_team and item.get('team', 0) not in (0, self.team):
            return False
        if self.cls_filter and not (set(item.get('classes') or []) & self.cls_filter):
            return False
        return True

    def _length(self, pts):
        return sum(math.hypot(pts[k][0] - pts[k - 1][0], pts[k][1] - pts[k - 1][1])
                   for k in range(1, len(pts)))

    # -- layers -------------------------------------------------------------
    def _compose(self):
        if self.mini_img is None:
            self.layers['minimap'] = self.grid_img
            self.layers['blend'] = self.grid_img
        else:
            self.layers['minimap'] = self.mini_img
            # grid_img is ALREADY the map plus the painted areas, so blending it
            # against the map again would only wash the whole canvas out.
            self.layers['blend'] = self.grid_img

    def current_layer(self):
        name = self.VIEWS[self.view]
        if name == 'terrain 4k' and name not in self.layers:
            self.flash(L('m.decoding'))
            self.status.update_idletasks()
            im = load_map_asset(self.map_name, 'colortex', (self.W, self.H))
            self.layers[name] = im if im is not None else self.grid_img
            self.flash(L('m.terrainok'))
        return self.layers.get(name, self.grid_img)

    # -- rendering ----------------------------------------------------------
    def redraw(self):
        self.photo = ImageTk.PhotoImage(self.current_layer())
        c = self.canvas
        c.delete('all')
        c.create_image(0, 0, anchor='nw', image=self.photo)

        for t, pts in (self.bases or {}).items():
            for bx, bz in pts:
                cx, cy = self.to_canvas(bx, bz)
                col = '#ffffff' if t == 1 else '#000000'
                c.create_oval(cx - 11, cy - 11, cx + 11, cy + 11, outline=col, width=3)
                c.create_text(cx, cy, text=str(t), fill=col, font=('Consolas', 10, 'bold'))

        if self.show_wg:
            for p in self.wg_nodes:
                cx, cy = self.to_canvas(p[0], p[2])
                c.create_oval(cx - 2, cy - 2, cx + 2, cy + 2, outline='#ffffff')

        for n, a in enumerate(self.avoid):
            pts = []
            for x, z in a['poly']:
                pts.extend(self.to_canvas(x, z))
            if len(pts) >= 6:
                c.create_polygon(pts, fill='#ff0000', stipple='gray50',
                                 outline='#ffff00' if self.selected == ('avoid', n) else '#ff5555',
                                 width=3 if self.selected == ('avoid', n) else 2)

        for n, r in enumerate(self.routes):
            if not self.visible(r):
                continue
            cs = r.get('classes') or []
            col = CLASS_COLOUR.get(cs[0], '#ffffff') if cs else '#ffffff'
            hot = self.selected == ('route', n)
            pts = []
            for x, z in r['points']:
                pts.extend(self.to_canvas(x, z))
            if len(pts) >= 4:
                c.create_line(pts, fill='#ffff00' if hot else col, width=5 if hot else 3,
                              arrow=tk.LAST, arrowshape=(12, 14, 5),
                              dash=(6, 4) if r.get('team') == 2 else None)
            for k, (x, z) in enumerate(r['points']):
                cx, cy = self.to_canvas(x, z)
                c.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill=col,
                              outline=TEAM_OUTLINE.get(r.get('team', 0), '#888888'))
                c.create_text(cx, cy - 11, text=str(k + 1), fill=col,
                              font=('Consolas', 7, 'bold'))

        for n, d in enumerate(self.destinations):
            if not self.visible(d):
                continue
            cx, cy = self.to_canvas(d['pos'][0], d['pos'][1])
            cs = d.get('classes') or []
            col = CLASS_COLOUR.get(cs[0], '#ffffff') if cs else '#ffffff'
            hot = self.selected == ('dest', n)
            rad = 9 if hot else 7
            c.create_oval(cx - rad, cy - rad, cx + rad, cy + rad, fill=col,
                          outline='#ffff00' if hot else TEAM_OUTLINE.get(d.get('team', 0), '#888888'),
                          width=3 if hot else 2)
            if len(cs) > 1:
                c.create_text(cx, cy, text=str(len(cs)), fill='#000000',
                              font=('Consolas', 7, 'bold'))

        hv = getattr(self, 'hover', None)
        if hv and hv != self.selected:
            kind, idx = hv
            src = self.destinations[idx]['pos'] if kind == 'dest' else self.routes[idx]['points'][0]
            hx, hy = self.to_canvas(src[0], src[1])
            c.create_oval(hx - 12, hy - 12, hx + 12, hy + 12, outline='#ffffff', width=1)

        if self.draft:
            pts = []
            for x, z in self.draft:
                pts.extend(self.to_canvas(x, z))
            if len(pts) >= 4:
                c.create_line(pts, fill='#ffff66', width=2, dash=(4, 3))
            for x, z in self.draft:
                cx, cy = self.to_canvas(x, z)
                c.create_oval(cx - 3, cy - 3, cx + 3, cy + 3, fill='#ffff66')

        # Audit flags LAST, so they sit on top of whatever they are objecting to.
        # A ring plus a cross: visible against every class colour and against the
        # avoid-area fill, and legible without relying on hue alone.
        for (_k, _n, _si, _bx, _bz, _r) in (self.bad or []):
            bx, by = self.to_canvas(_bx, _bz)
            c.create_oval(bx - 13, by - 13, bx + 13, by + 13,
                          outline=WARN_COLOUR, width=3)
            c.create_line(bx - 7, by - 7, bx + 7, by + 7, fill=WARN_COLOUR, width=2)
            c.create_line(bx - 7, by + 7, bx + 7, by - 7, fill=WARN_COLOUR, width=2)

        if self.astar_path:
            pts = []
            for x, z in self.astar_path:
                pts.extend(self.to_canvas(x, z))
            if len(pts) >= 4:
                c.create_line(pts, fill='#ffff00', width=3)
        if self.astar_from is not None:
            cx, cy = self.to_canvas(*self.astar_from)
            c.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, outline='#ffff00', width=2)
        self.update_status(None)

    def flash(self, msg):
        self._msg = msg
        self.update_status(None)

    def update_status(self, world):
        g = self.g
        parts = ['%s %dx%d@%.0fm%s' % (self.map_name, g.nx, g.nz, g.cell,
                                       '' if self.has_grid else ' (no grid)'),
                 L(self.VIEW_KEYS[self.view]),
                 'MODE=%s' % self.mode.upper(),
                 'TEAM=%s%s' % (TEAM_NAME[self.team], '*' if self.filter_team else ''),
                 'new=%s' % (','.join(sorted(self.sel)) or 'none'),
                 'P%d R%d X%d' % (len(self.destinations), len(self.routes), len(self.avoid))]
        if world:
            i = g.cell_at(world[0], world[1])
            if i is not None:
                parts.append('(%.0f,%.0f)%s' % (
                    world[0], world[1],
                    ' %s y=%.0f' % (NG.STATE_NAMES[g.state[i]], g.ground[i])
                    if self.has_grid else ''))
        if self._msg:
            parts.append('| ' + self._msg)
        self.status.config(text='  '.join(parts))
        if hasattr(self, 'buttons'):
            self._refresh_toolbar()

    # -- events -------------------------------------------------------------
    def on_move(self, e):
        w = self.to_world(e.x, e.y)
        self.update_status(w)
        # Rubber band: without it you cannot see the segment you are ABOUT to
        # add, which makes drawing a route guesswork.
        self.canvas.delete('preview')
        if self.draft and self.mode in ('route', 'avoid'):
            lx, lz = self.draft[-1]
            cx, cy = self.to_canvas(lx, lz)
            self.canvas.create_line(cx, cy, e.x, e.y, fill='#ffff66', width=2,
                                    dash=(3, 3), tags='preview')
            if self.mode == 'avoid' and len(self.draft) >= 2:
                fx, fy = self.to_canvas(*self.draft[0])
                self.canvas.create_line(e.x, e.y, fx, fy, fill='#ff8866', width=1,
                                        dash=(2, 4), tags='preview')
        # Hover highlight, so you can see what Delete or ctrl+click would take.
        h = self._nearest(w)
        if h != getattr(self, 'hover', None):
            self.hover = h
            self.redraw()

    def _nearest(self, w, reach=3.0):
        best = (None, None, (self.g.cell * reach) ** 2)
        for n, d in enumerate(self.destinations):
            if not self.visible(d):
                continue
            dd = (d['pos'][0] - w[0]) ** 2 + (d['pos'][1] - w[1]) ** 2
            if dd < best[2]:
                best = ('dest', n, dd)
        for n, r in enumerate(self.routes):
            if not self.visible(r):
                continue
            for x, z in r['points']:
                dd = (x - w[0]) ** 2 + (z - w[1]) ** 2
                if dd < best[2]:
                    best = ('route', n, dd)
        return (best[0], best[1]) if best[0] else None

    def _offer_generate(self, name):
        """Say how a navmesh comes into existence. There is one rule.

        The mesh is measured from the running game, so the editor cannot make
        one - but painting IS the request. Paint anything for this map, save,
        and the next battle here measures it. No option to set, nothing else to
        remember, and a map nobody paints is never measured at all.
        """
        self.flash(L('m.nogrid.play'))
        try:
            messagebox.showinfo(L('m.nogrid.title'), L('m.nogrid.play'))
        except Exception:
            pass

    def do_import(self):
        """Bring in a navmesh or a profile from anywhere.

        Three things arrive through here and they are told apart by content, not
        by where the user found them:
          * a .grid  - a navmesh someone else baked, copied into place so this
                       map becomes fully editable without playing it;
          * a profile .json - points/routes/avoid areas, replacing what is open;
          * the game's own <map>.routes.json - the auto-generated routes, which
                       is the useful case: it turns "start from a blank map"
                       into "start from what the bots already do".
        """
        path = filedialog.askopenfilename(
            title=L('m.import.title'),
            initialdir=NAV_DUMP if os.path.isdir(NAV_DUMP) else GAME,
            filetypes=[('Navmesh / profile', '*.grid *.json'),
                       ('Navmesh', '*.grid'), ('Profile', '*.json'),
                       ('All files', '*.*')])
        if not path:
            return
        try:
            if path.lower().endswith('.grid'):
                g = NG.NavGrid.load(path)            # parse before trusting it
                dst = os.path.join(NAV_DUMP, self.map_name + '.grid')
                if not g.fits((self.g.x0, self.g.z0, self.g.x1, self.g.z1)):
                    if not messagebox.askokcancel(
                            L('m.import.title'), L('d.gridmismatch')):
                        return
                d = os.path.dirname(dst)
                if d and not os.path.isdir(d):
                    os.makedirs(d)
                with open(path, 'rb') as fsrc:
                    blob = fsrc.read()
                with open(dst, 'wb') as fdst:
                    fdst.write(blob)
                self.flash(L('m.import.grid', g.nx, g.nz))
                self.open_map(self.map_name)
                return
            with io.open(path, encoding='utf-8') as f:
                doc = json.load(f)
            prof = BR.parse_profile(doc)
            if prof is None:
                self.flash(L('m.import.bad', 'not a profile'))
                return
            self.snapshot()
            self.destinations = list(doc.get('destinations') or [])
            self.routes = list(doc.get('routes') or [])
            self.avoid = list(doc.get('avoid') or [])
            self.touch()
            self._recompute_reach()
            self.refresh_items()
            self.redraw()
            gen = sum(1 for r in self.routes if r.get('generated'))
            self.flash(L('m.import.gen', gen) if gen else
                       L('m.import.prof', len(self.destinations),
                         len(self.routes), len(self.avoid)))
        except Exception as e:
            self.flash(L('m.import.bad', str(e)))

    def audit(self):
        '''Every painted coordinate the game will not be able to use.

        The placement-time warning only fires as you click, so a step that was
        fine when you placed it becomes unusable the moment you paint an avoid
        area around it - silently. A real Ruinberg profile had SIX steps in that
        state and nothing said so; the game recovers by snapping to the nearest
        reachable cell, which means the bot quietly goes somewhere other than
        where it was told. Re-run after anything that changes the paint.

        Returns [(kind, idx, step, x, z, reason)]; empty when the profile is
        clean. Reports only - it never edits the user's data.
        '''
        bad = []
        if not self.has_grid:
            return bad
        g = self.g

        def why(x, z):
            i = g.cell_at(x, z)
            if i is None:
                return 'offgrid'
            if g.state[i] == NG.BLOCKED_PAINTED:
                return 'painted'
            if self.reach is not None and i not in self.reach:
                return 'unreach'
            return None

        for n, d in enumerate(self.destinations):
            r = why(d['pos'][0], d['pos'][1])
            if r:
                bad.append(('dest', n, 0, d['pos'][0], d['pos'][1], r))
        for n, rt in enumerate(self.routes):
            for si, (x, z) in enumerate(rt['points']):
                r = why(x, z)
                if r:
                    bad.append(('route', n, si, x, z, r))
        return bad

    def _warn(self, w):
        if not self.has_grid:
            return ''
        i = self.g.cell_at(w[0], w[1])
        if i is None:
            return '  WARNING: off-grid'
        if self.g.state[i] == NG.BLOCKED_PAINTED:
            return '  ' + L('m.warn.painted')
        if self.reach is not None and i not in self.reach:
            return '  ' + L('m.warn.unreach')
        return ''

    def on_left(self, e):
        w = self.to_world(e.x, e.y)
        if self.mode == 'astar':
            if self.astar_from is None:
                self.astar_from = w
                self.astar_path = None
                self.flash(L('m.astarnext'))
            else:
                self.run_astar(self.astar_from, w)
                self.astar_from = None
        elif self.mode == 'dest':
            if not self.sel:
                self.flash(L('m.pickclass'))
                return
            self.snapshot()
            self.destinations.append({'pos': [round(w[0], 1), round(w[1], 1)],
                                      'classes': sorted(self.sel), 'team': self.team,
                                      'role': ''})
            self.touch()
            self.flash(u'%s %s%s' % (L('team.%d' % self.team),
                                     ','.join(L('cls.' + x) for x in sorted(self.sel)),
                                     self._warn(w)))
            self.refresh_items()
        else:
            self.draft.append([round(w[0], 1), round(w[1], 1)])
            self.flash(L('m.drawing', L('m.k.' + self.mode), len(self.draft),
                          self._length(self.draft), self._warn(w)))
        self.redraw()

    def on_ctrl_left(self, e):
        """Select on the canvas. Selection used to be list-only, which meant
        hunting for the row that matched the thing you were looking at."""
        hit = self._nearest(self.to_world(e.x, e.y))
        self.selected = hit
        if hit:
            try:
                self.items.selection_clear(0, tk.END)
                i = self.index_map.index(hit)
                self.items.selection_set(i)
                self.items.see(i)
                self._on_item_pick()
            except ValueError:
                pass
            self.flash(L('m.selected', L('m.k.' + hit[0])))
        else:
            self.flash(L('m.nounder'))
        self.redraw()
        return 'break'

    def on_right(self, e):
        if self.draft:
            self.draft.pop()
            self.flash(L('m.droplast', len(self.draft)))
            self.redraw()

    def finish(self):
        if self.mode == 'route':
            if len(self.draft) < 2:
                self.flash(L('m.routeneeds'))
                return
            if not self.sel:
                self.flash(L('m.pickclass'))
                return
            self.snapshot()
            self.routes.append({'points': self.draft, 'classes': sorted(self.sel),
                                'team': self.team, 'name': ''})
            self.touch()
            self.flash(L('m.routesaved', len(self.draft), self._length(self.draft),
                          L('team.%d' % self.team),
                          ','.join(L('cls.' + x) for x in sorted(self.sel))))
            self.draft = []
        elif self.mode == 'avoid':
            if len(self.draft) < 3:
                self.flash(L('m.areaneeds'))
                return
            self.snapshot()
            self.avoid.append({'poly': self.draft})
            self._recompute_reach()
            self.touch()
            self.flash(L('m.areablocks', self.cells_in(self.draft))
                       if self.has_grid else L('m.areaclosed'))
            self.draft = []
        self.refresh_items()
        self.redraw()

    def cancel(self):
        self.draft = []
        self.astar_from = self.astar_path = None
        self.selected = None
        self.flash(L('m.cancelled'))
        self.redraw()

    def cycle_team(self, e=None):
        self.set_team({1: 2, 2: 0, 0: 1}[self.team])
        return 'break'          # stop Tab moving keyboard focus

    def cells_in(self, poly):
        g = self.g
        xs = [p[0] for p in poly]
        zs = [p[1] for p in poly]
        n = 0
        for iz in range(g.nz):
            for ix in range(g.nx):
                cx, cz = g.center(g.index(ix, iz))
                if cx < min(xs) or cx > max(xs) or cz < min(zs) or cz > max(zs):
                    continue
                if point_in_poly(cx, cz, poly):
                    n += 1
        return n

    def run_astar(self, a, b):
        """Probe a route between two clicks, the way the GAME would.

        This used to hand A* the raw clicked cells. Click a metre inside an
        avoid area - or anywhere the flood fill cannot get to - and it simply
        reported "no path", which reads as the pathfinder failing when really
        the endpoint was never usable. The runtime snaps a destination to the
        nearest reachable cell before pathing, so the probe does the same and
        says when it had to.
        """
        g = self.g
        ends = []
        snapped = 0
        for (x, z) in (a, b):
            i = g.cell_at(x, z)
            if i is None:
                self.astar_path = None
                self.flash(L('m.astaroff'))
                return
            if not (g.passable(i) and g.can_reach(i)):
                j = g.nearest_passable(i, radius=10, reachable=True)
                if j is None:
                    self.astar_path = None
                    self.flash(L('m.astarsealed'))
                    return
                i, snapped = j, snapped + 1
            ends.append(i)
        p = g.astar(ends[0], ends[1])
        if not p:
            self.astar_path = None
            self.flash(L('m.astarnone'))
            return
        sm = g.smooth(p)
        self.astar_path = g.path_world(sm)
        msg = L('m.astarok', len(p), len(sm), self._length(self.astar_path))
        if snapped:
            msg = msg + '  ' + L('m.astarsnapped', snapped)
        self.flash(msg)

    def on_key(self, e):
        k = (e.keysym or '').lower()
        c = (e.char or '').lower()
        if isinstance(e.widget, (tk.Listbox, ttk.Combobox, tk.Entry)) and k not in ('escape',):
            return
        if c in CLASS_KEYS:
            self.toggle_class(CLASS_KEYS[c])
        elif c in self.MODES:
            self.set_mode(self.MODES[c])
        elif k == 'v':
            self.cycle_view()
        elif k == 'f':
            self.toggle_filter()
        elif k == 'r':
            self.toggle_wg()
        elif k == 'u':
            self.undo()
        elif k == 'd':
            self.delete_near()
        elif k in ('prior', 'next'):
            self._step_map(-1 if k == 'prior' else 1)
        else:
            self.update_status(None)

    def undo(self):
        if not self.undo_stack:
            self.flash(L('m.nothingundo'))
            return
        self.destinations, self.routes, self.avoid = self.undo_stack.pop()
        self.selected = None
        self.astar_from = self.astar_path = None
        self.touch()
        # Restoring the avoid list is not enough - the GRID has to be re-painted
        # from it, or an undone delete leaves the drawing and the blocking
        # disagreeing, and the audit and A* test both answer from stale cells.
        self._recompute_reach()
        self.flash(L('m.undone'))
        self.refresh_items()
        self.redraw()

    def delete_near(self):
        px = self.canvas.winfo_pointerx() - self.canvas.winfo_rootx()
        py = self.canvas.winfo_pointery() - self.canvas.winfo_rooty()
        w = self.to_world(px, py)
        best = (None, None, (self.g.cell * 3) ** 2)
        for n, d in enumerate(self.destinations):
            dd = (d['pos'][0] - w[0]) ** 2 + (d['pos'][1] - w[1]) ** 2
            if dd < best[2]:
                best = ('dest', n, dd)
        for n, r in enumerate(self.routes):
            for x, z in r['points']:
                dd = (x - w[0]) ** 2 + (z - w[1]) ** 2
                if dd < best[2]:
                    best = ('route', n, dd)
        if best[0]:
            self.snapshot()
            {'dest': self.destinations, 'route': self.routes}[best[0]].pop(best[1])
            self.touch()
            self.flash(L('m.deleted', L('m.k.' + best[0])))
            self.refresh_items()
            self.redraw()
        else:
            self.flash(L('m.nothingnear'))


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else None
    if name and name.endswith('.grid'):
        name = os.path.basename(name)[:-5]
    root = tk.Tk()
    Painter(root, name)
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    root.geometry('+%d+%d' % (max(0, (root.winfo_screenwidth() - w) // 2),
                              max(0, (root.winfo_screenheight() - h) // 2 - 20)))
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
