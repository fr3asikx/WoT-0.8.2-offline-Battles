# Bot AI — how to edit it, end to end

Short version of the workflow. Controls live in the tooltips and `README.md`;
internals live in `CLAUDE.md` section F and `PLAN-bot-pathfinding.md`. This file
is only the order you do things in, and what is actually wired up.

## The mental model

Two independent layers. Keeping them separate is what makes the whole thing
tractable:

| layer | answers | comes from |
|---|---|---|
| **destinations** | *where* should this bot go? | baked data — WG's nodes today, your painted profiles later |
| **pathfinding** | *how* does it get there? | a grid sampled from the live map at battle start, then A* |

The grid needs no authoring and works on all 33 maps. Destinations are the part
that benefits from a human.

## What is wired today

| stage | status | flag |
|---|---|---|
| Nav grid built per battle + dumped to disk | **working** | `nav_grid` |
| A* pathfinding + path following | **working**, measured | `bot_pathfinding` |
| WG route destinations | **working**, but only Malinovka is baked | `bot_routes` |
| Painted profile save format + loader | **done**, 82 checks | — |
| Painted profiles driving bots | **connected**, 17 chain checks | — |
| Force a specific map for testing | **done** | `force_map` |
| Bake grids for all maps in one run | not built | — |
| Load a baked grid instead of rebuilding | not built | — |

Measured on Malinovka, exact counts from `NAVPATH stats`: bots spent **14.2%** of
their life in the stuck-reverse with pathfinding off, **5–7%** with it on, over
60k bot-ticks, with zero A* failures. Before any of this it was ~30%.

## The workflow

### 1. Get a grid for the map (one battle per map)

`nav_grid: true` is the default. Play or idle through **one battle** on the map.
The grid builds during the countdown while bots are frozen — ~20 000 probes over
~50 frames, plus a few seconds waiting for terrain to stream in — then writes:

    <game>/offhangar_user/nav_dump/<map>.grid      (~50 KB)

Log line to confirm:

    NAVGRID <map>: 100x100 @10.0m | coverage 100% | {...} | pass=7
    NAVGRID dumped to offhangar_user\nav_dump\<map>.grid

Coverage climbing from 0% is normal — terrain is not queryable for the first
frames and streams in by distance.

**This is the only step that needs the game.** Everything below is desktop work.

### 2. Check the grid is right

    python check_navgrid.py <game>/offhangar_user/nav_dump/02_malinovka.grid

Eight automatic checks plus a render of the grid beside the game's own minimap.
The two that actually settle correctness:

* **route nodes on passable cells** — cross-validates the grid against the WG
  nodes, which were verified by a *completely independent* probe path. 112/116
  on Malinovka.
* **a path exists across the map** — if that works and avoids water, the grid is
  usable.

### 3. Paint the profile

    python paint_map.py            # map selector, all 33 maps

No grid is needed to paint — world coordinates come from each map's own
`arena_def`. A grid adds passability shading, the undrivable-placement warning,
and the A* test.

Suggested order per map, ~10 minutes:

1. Look at the **terrain 4k** layer (`v`) to see roads and field edges, and the
   numbered rings showing where each team spawns.
2. **Point** mode: place destinations for team 1, one class row at a time. The
   COVERAGE panel ticks off team×class combinations as you go.
3. **Route** mode where the shortest line would be wrong — A* will happily send
   a bot straight across open ground; a route says "go round".
4. **Avoid** mode over anywhere bots should not go - and this is the only thing
   that blocks. The grid has no opinion of its own: a cell is drivable if the map
   has ground there. Water does not block, walled-off pockets do not block. If
   you want a lake off-limits, paint it - switch to the **minimap** layer (`v`),
   which is the one source that shows water clearly.
   The climb rule still applies to MOVEMENT: A* will not step up a 10 m wall, so
   roofs stay unreachable without anything marking them blocked.
5. **Mirror team1 → team2**, then fix the asymmetric parts. Most WoT maps are
   roughly symmetric; Himmelsdorf and Ensk are not.
6. **A\* test** a couple of long legs to confirm the routes are drivable.
7. Save (`ctrl+s`). Writes `offhangar/painted/<map>.paint.json`.

### 3b. Sync the profile into the install

**Easy to forget.** The painter writes into the REPO
(`scripts/.../offhangar/painted/`), because that is the version-controlled copy,
but the mod reads from the INSTALL (`res_mods/.../offhangar/painted/`). Run
`sync-to-resmods.ps1` after saving or the game will report
`PROFILE: none painted` and quietly fall back to WG nodes.

Re-sync after EVERY save. A profile saved again while you were checking the
previous one will otherwise leave the install a version behind.

### 4. Test it in game

Set `force_map` in `<game>/offhangar_user/config.json` to the map you painted
(`02_malinovka`, or just `malinovka`) so you get it every battle instead of
waiting for the roll. **Clear it when you are done.**

Priority per bot, falling through at every level so any map works at whatever
level it has data for:

1. a painted **route** for its team+class — the strongest statement of intent
2. a painted **point** for its team+class
3. the WG node pool
4. nearest living enemy (behaviour before any of this)

Painted **avoid areas** become blocked grid cells after the grid settles, which
A* already honours. The `.grid` dump stays raw terrain deliberately — baking
paint into it would make every dump stale the moment a profile is edited.

Log lines that confirm it:

    PROFILE 02_malinovka: 12 destinations, 3 routes, 1 avoid areas
    PROFILE avoid: 1 areas blocked 168 grid cells
    PROFILE assign: bot=1004 team=1 class=heavy -> painted route #2
    NAVPATH stats: stuck X% ...

If a profile is missing or unreadable you get
`PROFILE: none painted for <map> - using baked WG nodes` and nothing breaks.

## Config

In `<game>/offhangar_user/config.json` (not the `res_mods` defaults — the sync
script overwrites those):

    nav_grid          build + dump the grid          default true
    bot_pathfinding   actually steer along A*         default true
    bot_routes        use baked WG destinations       default true
    force_map         play one map every battle       default "" (random)

Set `bot_pathfinding: false` to A/B against the old straight-line behaviour in
the same session.

## Reading the log

`debug_logging: true`, then `<game>/python.log`. Four lines matter:

    NAVGRID <map>: ... coverage N%          grid building
    ROUTES <map>: N/116 usable (X%) ...     destination validation, with reject reasons
    ROUTE assign: bot=… class=heavy -> …    one per bot
    NAVPATH stats: stuck X% ... ok=N fail=N the metric; compare across runs

`python.log` is a **rolling tail** — it truncates from the top, so a line you
expect may simply have scrolled off. Check the newest battle by finding the last
`NAVGRID: building` and reading forward from there.

## Known open item

Three bots per battle still reach deep water and drown on Malinovka. Paths avoid
water, the feeler checks water, and the stuck-reverse guard has **never fired**
(`wet-reverses blocked=0`), so the entry mode is genuinely unidentified. A
one-shot `WET ENTRY` diagnostic is armed and will print the bot's full state the
next time it happens.
