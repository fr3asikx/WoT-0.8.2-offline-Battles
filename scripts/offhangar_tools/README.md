# Bot-profile painter

Draws where bots go on each map: destinations, ordered routes, and keep-out
areas, per team and per tank class. Writes a JSON profile the mod reads at the
start of every battle.

```
python paint_map.py                 # opens on your last map
python paint_map.py 05_prohorovka   # straight to one map
```

Needs Python 3 with **Pillow** and **Tk** (`pip install pillow`). It ships inside
the mod at `res_mods/0.8.2/scripts/offhangar_tools/` and finds the game by walking
up from its own location, so it works wherever inside the install it sits.

---

## The one thing to understand

**Painting is optional. The mod already works without it.**

There are three levels, and each falls through to the next:

| level | where it comes from | when it applies |
|---|---|---|
| painted profile | this tool | always wins |
| generated routes | the mod, automatically | when nothing is painted |
| chase nearest enemy | original behaviour | when the map has no navmesh |

So you only paint a map when you disagree with what the mod worked out by
itself. A map you never touch still gets sensible bots.

---

## Before you can paint a map

A map needs a **navmesh** — a measurement of what is drivable. It can only be
made from the running game, so the editor cannot produce one.

**Painting a map is what asks for its mesh.** Paint anything here and save, then
play one battle on that map: it is measured in the background, saved, and every
battle after that loads it instantly. Reopen the map here and everything works.

There is no setting to remember. A map nobody has painted is never measured, and
its bots behave exactly as they did before - which is why nothing map-specific
ships with the mod.

Without a mesh you can still place points and routes — the coordinates are read
from the map's own arena definition — but you lose passability shading, the A\*
test, and the audit.

---

## What you can draw

**Point** — a destination. Bots of that team and class pick one and drive to it.

**Route** — an *ordered* polyline. This is the important one: A\* already finds
the shortest line, so a route is how you say *go THIS way* — the part a
pathfinder cannot infer. Bots A\* between consecutive steps, so the route
carries your intent and the grid handles local detail.

**Avoid** — a keep-out area. Painted areas mark grid cells blocked, so A\*
routes around them.

Every item belongs to a team and one or more classes. A position is not
symmetric — the ridge team 1 attacks over is the one team 2 defends from — so
`Mirror team1 → team2` reflects your work rather than copying it.

---

## Behaviour worth knowing

**The whole map space is shown**, not just the playable arena. Some maps put
their arena off-centre inside the space (Himmelsdorf's runs
`-300…400`, centred on +50), so showing only the bounds cut real map off the
edge. You can see everything; bots can only use what is inside the arena.

**The audit runs after every paint change.** It re-checks every destination and
route step against the grid and flags anything the game cannot drive to —
amber in the list with the bad step numbers named, and a ring-and-cross on the
canvas. This matters because a step that was fine when you placed it becomes
unusable the moment you paint an avoid area around it, and nothing would
otherwise tell you. The runtime recovers by snapping to the nearest reachable
cell, so an unfixed one means a bot quietly going somewhere other than where
you put it.

**Team numbers are corrected at runtime if they disagree with the map.** The
editor takes team 1/2 from the map's arena definition; the mod assigns them its
own way, and on some maps the two disagree. Rather than trust either, the mod
compares where each team's routes start against where that team's base actually
is, and swaps the labels if they are inverted. You do not need to do anything —
but if bots seem to head for the wrong end, that is what the log line
`PROFILE orientation:` is telling you about.

**Import** takes a `.grid` (a navmesh someone else baked) or any profile
`.json`. Most useful for the mod's own `<map>.routes.json`, which loads the
routes the game generated so you start from what the bots already do rather than
a blank map.

---

## Where files live

```
.../0.8.2/scripts/offhangar_tools/          this tool
<game>/offhangar_user/nav_dump/<map>.grid   navmeshes you measured
<game>/offhangar_user/nav_dump/*.routes.json  routes the mod generated
.../mods/offhangar/painted/<map>.paint.json your profiles  <- what you save
```

Your own navmesh always wins over a shipped one. Delete anything under
`nav_dump/` and it is simply remade.

---

## Keys

| | |
|---|---|
| `p` `w` `x` `a` | point / route / avoid / A\* test |
| `1`–`5`, `h m l t s` | class filter |
| `Enter` | close the area or route you are drawing |
| `u` / `d` | undo / delete nearest |
| `v` | cycle the background layer |
| `f` | team filter |
| `Ctrl+click` | select an item on the canvas |
| `F12` | export a PNG |

Saving writes to the installed mod, so a paint is live on the next battle.
