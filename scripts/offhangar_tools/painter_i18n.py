# -*- coding: utf-8 -*-
"""UI strings for paint_map.py, English and Russian.

Map NAMES are deliberately not in here: the game client ships Wargaming's own
official translations in res/text/LC_MESSAGES/arenas.mo, so the painter reads
them from there instead of inventing its own. That also means a RU player sees
exactly the names they see in game.

Tank class abbreviations follow the Russian game's own conventions - TT/CT/LT/
PT/SAU rather than transliterated English - because that is what a RU player
reads on their own screen.
"""

LANGS = ('en', 'ru')

S = {
    # --- toolbar groups ---
    'g.mode':   ('mode', u'режим'),
    'g.team':   ('team', u'команда'),
    'g.class':  ('class', u'класс'),
    'g.view':   ('view', u'вид'),
    'g.edit':   ('edit', u'правка'),
    'g.file':   ('file', u'файл'),

    # --- modes ---
    'b.point':  ('Point', u'Точка'),
    'b.route':  ('Route', u'Маршрут'),
    'b.avoid':  ('Avoid', u'Запрет'),
    'b.astar':  ('A* test', u'Тест A*'),
    'b.allow':  ('Allow', u'Разрешить'),
    't.allow':  ('Draw an area where the grid guessed WRONG - cells it blocked as\nbuildings are handed back as drivable. Click corners, Enter to close.\nOverrides inference only: it can un-block a slope guess but NEVER\nwater or missing ground, because those are measurements. A painted\nAvoid still wins over a painted Allow.',
                 u'Нарисовать зону, где сетка ОШИБЛАСЬ: клетки, помеченные\nкак здания, снова станут проходимыми.\nПереопределяет только догадку о склоне и НИКОГДА воду.\nЗапрет сильнее разрешения.'),
    't.point':  ('Place a destination for the selected team and classes.\n'
                 'Bots of that class pick one and A* to it.',
                 u'Поставить точку назначения для выбранной команды и классов.\n'
                 u'Боты этого класса выбирают её и едут туда по A*.'),
    't.route':  ('Draw an ORDERED route. Click each step, Enter to finish.\n'
                 'A* takes the shortest line; a route says "go THIS way" -\n'
                 'the part a pathfinder cannot infer. Bots A* between\n'
                 'consecutive steps, so the grid still handles the detail.',
                 u'Нарисовать УПОРЯДОЧЕННЫЙ маршрут. Щелчок — шаг, Enter — готово.\n'
                 u'A* выбирает кратчайший путь; маршрут говорит «ехать ИМЕННО так» —\n'
                 u'то, что поиск пути сам не выведет. Между соседними шагами боты\n'
                 u'идут по A*, так что детали остаются за сеткой.'),
    't.avoid':  ('Draw a keep-out area. Click corners, Enter to close.\n'
                 'Painted areas mark grid cells BLOCKED, so A* routes\n'
                 'around them with no extra runtime code.',
                 u'Нарисовать запретную зону. Щелчок — угол, Enter — замкнуть.\n'
                 u'Закрашенные зоны помечают клетки сетки как НЕПРОХОДИМЫЕ,\n'
                 u'и A* обходит их без единой строчки нового кода.'),
    't.astar':  ('Validation, not painting: click two points and see the\n'
                 'real route bots would drive. Needs a baked grid.',
                 u'Проверка, а не рисование: щёлкните две точки и увидите\n'
                 u'настоящий маршрут ботов. Нужна готовая сетка.'),

    # --- team ---
    't.team':   ('New points and routes are saved for this team.\n'
                 'A position is not symmetric - the ridge team 1 attacks over is\n'
                 'the one team 2 defends from. "both" applies to either side.',
                 u'Новые точки и маршруты сохраняются для этой команды.\n'
                 u'Позиция не симметрична: гребень, через который наступает\n'
                 u'команда 1, — тот же, с которого обороняется команда 2.\n'
                 u'«обе» — для любой стороны.'),
    'team.1':   ('team1', u'команда1'),
    'team.2':   ('team2', u'команда2'),
    'team.0':   ('both', u'обе'),

    # --- classes (RU uses the game's own abbreviations) ---
    'cls.heavy':  ('hea', u'ТТ'),
    'cls.medium': ('med', u'СТ'),
    'cls.light':  ('lig', u'ЛТ'),
    'cls.td':     ('td', u'ПТ'),
    'cls.spg':    ('spg', u'САУ'),
    'name.heavy':  ('heavy', u'тяжёлый'),
    'name.light':  ('light', u'лёгкий'),
    'name.td':     ('td', u'ПТ-САУ'),
    'name.spg':    ('spg', u'САУ'),
    't.class':  ('Toggle %s for NEW points. Several classes can share one point;\n'
                 'the marker takes the colour of the first.',
                 u'Включить/выключить «%s» для НОВЫХ точек. Одну точку могут делить\n'
                 u'несколько классов; маркер берёт цвет первого.'),
    't.clsfilter': ('Filter the VIEW by %s.\nNothing lit = show every class.\n'
                    'This does not change what new points are tagged with -\n'
                    'that is the class row in the toolbar.',
                    u'Фильтр ОТОБРАЖЕНИЯ по «%s».\nНичего не выбрано — показаны все классы.\n'
                    u'Это не меняет теги новых точек — за них отвечает\n'
                    u'ряд классов на панели.'),

    # --- view ---
    'b.layer':  ('Layer', u'Слой'),
    't.layer':  ('Cycle the reference image:\n'
                 '  blend       minimap + our probed grid\n'
                 "  minimap     the game's own - the ONLY source showing water\n"
                 '  terrain 4k  original 4096x4096 texture, ~3.4 px/m. Roads and\n'
                 '              field edges legible. No water, no buildings.\n'
                 '  grid        our passability alone',
                 u'Переключить подложку:\n'
                 u'  смесь       миникарта + наша сетка проходимости\n'
                 u'  миникарта   игровая — ЕДИНСТВЕННАЯ, где видна вода\n'
                 u'  рельеф 4k   исходная текстура 4096x4096, ~3.4 пикс/м. Дороги\n'
                 u'              и края полей читаются. Без воды и без зданий.\n'
                 u'  сетка       только наша проходимость'),
    'b.teamflt': ('TeamFlt', u'Фильтр'),
    't.teamflt': ('Show only the current team, to unclutter the map.',
                  u'Показывать только текущую команду, чтобы не загромождать карту.'),
    'b.wg':     ('WG pts', u'Узлы WG'),
    't.wg':     ("Show Wargaming's own extracted nav nodes as white rings -\n"
                 'what bots choose from when nothing is painted.',
                 u'Показать извлечённые узлы навигации Wargaming белыми кольцами —\n'
                 u'из них боты выбирают, когда ничего не нарисовано.'),
    'v.blend':   ('blend', u'смесь'),
    'v.minimap': ('minimap', u'миникарта'),
    'v.terrain': ('terrain 4k', u'рельеф 4k'),
    'v.grid':    ('grid', u'сетка'),

    # --- edit / file ---
    'b.undo':   ('Undo', u'Отменить'),
    't.undo':   ('Undo the last point, route or area.',
                 u'Отменить последнюю точку, маршрут или зону.'),
    'b.delete': ('Delete', u'Удалить'),
    't.delete': ('Delete whatever is nearest the cursor.',
                 u'Удалить то, что ближе всего к курсору.'),
    'b.save':   ('Save', u'Сохранить'),
    't.save':   ('Write <map>.paint.json.\nTurns red on unsaved changes.\n'
                 'You are asked before switching maps or closing.',
                 u'Записать <карта>.paint.json.\nКраснеет при несохранённых изменениях.\n'
                 u'Спросит перед сменой карты и перед закрытием.'),
    'b.reload': ('Reload', u'Перечитать'),
    't.reload': ('Discard changes and reload from disk.',
                 u'Отменить изменения и перечитать с диска.'),
    'b.folder': ('Folder', u'Папка'),
    't.folder': ('Open the folder the painted JSON goes to.',
                 u'Открыть папку, куда сохраняется JSON.'),
    'b.png':    ('PNG', u'PNG'),
    't.png':    ('Save the canvas as a PNG next to the JSON.\n'
                 'Xbox Game Bar will not record this window - Tk draws through\n'
                 'plain GDI with no swapchain, so Game Bar either refuses it or\n'
                 'captures black. This grabs the pixels directly instead.',
                 u'Сохранить холст в PNG рядом с JSON.\n'
                 u'Xbox Game Bar это окно не запишет: Tk рисует через обычный GDI\n'
                 u'без swapchain, поэтому Game Bar либо отказывается, либо пишет\n'
                 u'чёрный кадр. Здесь пиксели снимаются напрямую.'),
    'b.rec':    ('REC', u'ЗАПИСЬ'),
    't.rec':    ('Record the canvas to an animated GIF.\n'
                 'Built in because there is no working alternative on Windows 10:\n'
                 "Snipping Tool's recorder is Windows 11 only, and Xbox Game Bar\n"
                 'cannot capture a plain GDI window like this one.\n'
                 'Caps at %d frames (~%ds) so memory stays bounded.',
                 u'Записать холст в анимированный GIF.\n'
                 u'Встроено, потому что на Windows 10 рабочих альтернатив нет:\n'
                 u'запись в «Ножницах» есть только в Windows 11, а Xbox Game Bar\n'
                 u'не снимает обычные GDI-окна вроде этого.\n'
                 u'Предел — %d кадров (~%d с), чтобы память не росла.'),
    'b.lang':   ('RU', u'EN'),
    't.lang':   ('Switch the interface to Russian.\n'
                 'Map names then come from the game\'s own arenas.mo, so you see\n'
                 'exactly the names your client shows.',
                 u'Переключить интерфейс на английский.\n'
                 u'Названия карт берутся из игрового arenas.mo, так что они\n'
                 u'совпадают с тем, что показывает клиент.'),

    # --- side panel ---
    'h.map':      ('MAP', u'КАРТА'),
    'h.classes':  ('SHOW CLASSES', u'ПОКАЗАТЬ КЛАССЫ'),
    'h.coverage': ('COVERAGE', u'ПОКРЫТИЕ'),
    'h.items':    ('PAINTED ITEMS', u'НАРИСОВАННОЕ'),
    'h.legend':   ('GRID LEGEND', u'ЛЕГЕНДА СЕТКИ'),
    'b.prev':   ('< prev', u'< пред'),
    'b.next':   ('next >', u'след >'),
    'b.mirror': (u'Mirror team1 → team2', u'Отразить команду1 → команду2'),
    't.mirror': ('Copy every team-1 point and route, reflected through the map\n'
                 'centre, as team 2. Most WoT maps are roughly symmetric, so this\n'
                 'halves the work - then fix the parts that are not symmetric\n'
                 '(Himmelsdorf and Ensk notably are not).',
                 u'Скопировать все точки и маршруты команды 1, отразив их через\n'
                 u'центр карты, на команду 2. Большинство карт WoT примерно\n'
                 u'симметричны, так что это вдвое сокращает работу — потом\n'
                 u'поправьте несимметричные места (Химмельсдорф и Энск таковы).'),
    'b.reassign': ('Reassign', u'Переназначить'),
    't.reassign': ('Set the selected item to the CURRENT team and classes,\n'
                   'instead of deleting it and placing it again.',
                   u'Назначить выбранному объекту ТЕКУЩУЮ команду и классы,\n'
                   u'вместо того чтобы удалять и ставить заново.'),
    't.delsel': ('Delete the selected item.   [Del]',
                 u'Удалить выбранный объект.   [Del]'),
    'l.role':   ('role / note for selected', u'роль / заметка для выбранного'),
    't.role':   ('Free text stored with the item (e.g. "ridge", "city push").\n'
                 'Enter applies it. Documentation only - it rides along in the\n'
                 'JSON so the intent is not lost.',
                 u'Произвольный текст, сохраняемый с объектом («гребень», «в город»).\n'
                 u'Enter — применить. Только пометка: она едет в JSON, чтобы\n'
                 u'замысел не потерялся.'),
    't.map':    ('Every map with an arena_def - all 33.\n'
                 '[grid] means a baked nav grid exists for it, which\n'
                 'enables passability shading and the A* test.\n'
                 '[painted] means it already has a profile saved.',
                 u'Все карты с arena_def — всего 33.\n'
                 u'[сетка] — есть готовая сетка навигации: доступны заливка\n'
                 u'проходимости и тест A*.\n'
                 u'[готово] — профиль для карты уже сохранён.'),
    't.coverage': ('Which team+class combinations already have at least one\n'
                   'point or route on THIS map. Tells you what is left,\n'
                   'both within a map and across the 33.',
                   u'Для каких сочетаний команда+класс на ЭТОЙ карте уже есть\n'
                   u'хотя бы одна точка или маршрут. Показывает, что осталось —\n'
                   u'и по карте, и по всем 33.'),
    't.items':  ('Everything painted on this map.\n'
                 'Click to highlight it; Delete removes it.',
                 u'Всё, что нарисовано на этой карте.\n'
                 u'Щелчок — подсветить, Delete — удалить.'),
    't.legend': ('What the grid colours mean. Only meaningful on a map with a\n'
                 'baked grid - everything reads "not probed" without one.',
                 u'Что означают цвета сетки. Имеет смысл только для карты\n'
                 u'с готовой сеткой — без неё всё «не проверено».'),
    'tag.grid':    ('grid', u'сетка'),
    'tag.painted': ('painted', u'готово'),

    # --- legend ---
    'lg.passable': ('passable', u'проходимо'),
    'lg.water':    ('water', u'вода'),
    'lg.unreach':  ('cannot be reached', u'недостижимо'),
    'lg.painted':  ('painted avoid', u'запрет (кисть)'),
    'm.warn.painted': ('WARNING: inside one of your own avoid areas',
                       u'ВНИМАНИЕ: внутри вашей же запретной зоны'),
    'm.warn.unreach': ('WARNING: nothing can drive here (a roof? a sealed yard?)',
                       u'ВНИМАНИЕ: сюда нельзя доехать (крыша? замкнутый двор?)'),
    'm.audit.steps': ('[bad steps: %s]', u'[плохие шаги: %s]'),
    'm.unreachcount': ('%d of %d painted points cannot be driven to - marked in amber',
                       u'%d из %d точек недостижимы — отмечены жёлтым'),
    'm.mode.allow': ('draw an ALLOW area - hands blocked cells back (Enter to close)',
                     u'рисовать зону РАЗРЕШЕНИЯ (Enter — замкнуть)'),
    'm.k.allow':   ('allow area', u'зона разрешения'),
    'lg.noground': ('no ground', u'нет земли'),
    'lg.unknown':  ('not probed', u'не проверено'),

    # --- dialogs ---
    'd.unsaved':   ('Unsaved changes', u'Несохранённые изменения'),
    'd.saveclose': ('Save before closing?', u'Сохранить перед закрытием?'),
    'd.saveswitch': ('Save %s before switching maps?',
                     u'Сохранить «%s» перед сменой карты?'),
    'd.reload':    ('Reload', u'Перечитать'),
    'd.reloadq':   ('Discard unsaved changes and reload from disk?',
                    u'Отменить несохранённые изменения и перечитать с диска?'),
    'd.mirror':    ('Mirror', u'Отражение'),
    'd.mirrorq':   ('Team 2 already has %d items. Add %d mirrored on top?',
                    u'У команды 2 уже есть объектов: %d. Добавить отражённых: %d?'),

    # --- status messages ---
    'm.gridloaded': ('grid loaded', u'сетка загружена'),
    'm.nogrid.play': ('This map has no navmesh yet. Paint something here and save, '
                      'then play one battle on this map - painting it is what asks '
                      'for the mesh. Reopen the map afterwards for passability '
                      'shading, the A* test and the audit.',
                      u'Для этой карты '
                      u'ещё нет навигационной '
                      u'сетки. Сыграйте один '
                      u'бой на ней в игре — '
                      u'сетка создаётся '
                      u'автоматически и '
                      u'сохраняется.'),
    'm.astaroff': ('that point is outside the map - nothing to path over',
                   u'точка вне карты'),
    'm.astarsealed': ('that point is sealed off by your avoid areas - bots '
                      'cannot get there at all',
                      u'точка отрезана '
                      u'запретными зонами'),
    'm.astarsnapped': ('(%d end(s) moved to the nearest drivable cell, as the game does)',
                       u'(%d точка/и смещены '
                       u'к ближайшей проезжей ячейке)'),
    'd.gridmismatch': ('That navmesh was built for a different map size. Import anyway?',
                       u'Эта сетка для карты '
                       u'другого размера. Всảё равно?'),
    'm.stalemesh': ('This navmesh was measured before the grid covered the whole '
                    'map, so it stops short. Play one battle here to re-measure it.',
                    u'Сетка устарела — '
                    u'сыграйте бой, чтобы '
                    u'пересчитать её.'),
    'b.reset': ('Reset', u'Сброс'),
    't.reset': ('Clear everything painted for this map and delete its saved '
                'profile (undoable until you close)',
                u'Очистить всё для '
                u'этой карты и удалить '
                u'сохранённый профиль'),
    'd.resettitle': ('Reset this map?', u'Сбросить карту?'),
    'd.resetq': ('Clear all %d painted items on %s and delete its saved profile?'
                 ' A .bak copy is kept, and undo still works until you close.',
                 u'Удалить все %d объектов '
                 u'на %s и сохранённый профиль?'),
    'm.resetdone': ('reset - %d items cleared and the saved profile deleted (.bak kept)',
                    u'сброшено: %d объектов'),
    'm.resetcleared': ('reset - %d items cleared',
                       u'сброшено: %d объектов'),
    'm.resetempty': ('nothing painted here to reset',
                     u'здесь нечего сбрасывать'),
    'm.nogrid.title': ('No navmesh for this map yet',
                       u'Нет навигационной '
                       u'сетки для этой карты'),
    'm.import.title': ('Import map data', u'Импорт данных карты'),
    'm.import.bad': ('could not import: %s', u'не удалось импортировать: %s'),
    'm.import.grid': ('navmesh imported (%dx%d) - reopen the map to use it',
                      u'сетка импортирована (%dx%d)'),
    'm.import.prof': ('imported %d points, %d routes, %d avoid areas',
                      u'импорт: %d точек, %d маршрутов, %d зон'),
    'm.import.gen': ('loaded the game-generated routes (%d) - edit and save to override them',
                     u'загружены автомаршруты (%d)'),
    'b.import': ('Import', u'Импорт'),
    't.import': ('Import a navmesh (.grid) or a profile (.json) from anywhere - '
                 'including the routes the game generated for this map',
                 u'Импорт сетки (.grid) '
                 u'или профиля (.json)'),
    'm.nogrid':  ('NO baked grid - painting works, A* test and passability do not',
                  u'СЕТКИ НЕТ — рисование работает, тест A* и проходимость — нет'),
    'm.mode.dest':  ('place points', u'ставить точки'),
    'm.mode.route': ('draw a route (Enter to finish)',
                     u'рисовать маршрут (Enter — готово)'),
    'm.mode.avoid': ('draw an avoid area (Enter to close)',
                     u'рисовать запретную зону (Enter — замкнуть)'),
    'm.mode.astar': ('A* probe: click start then end',
                     u'тест A*: щёлкните начало, затем конец'),
    'm.needgrid': ('A* needs a baked grid - play one battle on this map first',
                   u'Для теста A* нужна готовая сетка — сыграйте один бой на этой карте'),
    'm.pickclass': ('pick at least one class (1-5)',
                    u'выберите хотя бы один класс (1-5)'),
    'm.newpoints': ('new points: %s', u'новые точки: %s'),
    'm.showing':   ('showing: %s', u'показаны: %s'),
    'm.allclasses': ('all classes', u'все классы'),
    'm.none':      ('none', u'нет'),
    'm.teamto':    ('team -> %s', u'команда -> %s'),
    'm.teamfilter': ('team filter: %s', u'фильтр команды: %s'),
    'm.only':      ('only %s', u'только %s'),
    'm.off':       ('off', u'выкл'),
    'm.saved':     ('saved %d points, %d routes, %d avoid',
                    u'сохранено: точек %d, маршрутов %d, зон %d'),
    'm.loaded':    ('loaded %d points, %d routes, %d avoid',
                    u'загружено: точек %d, маршрутов %d, зон %d'),
    'm.cancelled': ('cancelled', u'отменено'),
    'm.undone':    ('undone', u'отменено'),
    'm.nothingundo': ('nothing to undo', u'отменять нечего'),
    'm.nothingnear': ('nothing close enough', u'рядом ничего нет'),
    'm.nothingsel': ('nothing selected in the list', u'в списке ничего не выбрано'),
    'm.deleted':   ('deleted a %s', u'удалено: %s'),
    'm.k.dest':    ('point', u'точка'),
    'm.k.route':   ('route', u'маршрут'),
    'm.k.avoid':   ('area', u'зона'),
    'm.drawing':   ('%s: %d points, %.0f m%s (Enter to finish)',
                    u'%s: точек %d, %.0f м%s (Enter — готово)'),
    'm.droplast':  ('dropped last point (%d left)',
                    u'последняя точка убрана (осталось %d)'),
    'm.routeneeds': ('a route needs at least 2 points',
                     u'для маршрута нужно минимум 2 точки'),
    'm.areaneeds': ('an area needs at least 3 points',
                    u'для зоны нужно минимум 3 точки'),
    'm.routesaved': ('route: %d steps, %.0f m, %s %s',
                     u'маршрут: шагов %d, %.0f м, %s %s'),
    'm.areablocks': ('avoid area: blocks %d grid cells',
                     u'запретная зона: перекрыто клеток — %d'),
    'm.areaclosed': ('avoid area closed', u'запретная зона замкнута'),
    'm.astarnone': ('A*: NO PATH between those points',
                    u'A*: МЕЖДУ ЭТИМИ ТОЧКАМИ ПУТИ НЕТ'),
    'm.astarok':   ('A*: %d cells -> %d waypoints, %.0f m',
                    u'A*: клеток %d -> путевых точек %d, %.0f м'),
    'm.astarnext': ('A*: click the destination', u'A*: щёлкните точку назначения'),
    'm.mirrored':  ('mirrored %d points, %d routes onto team 2 - now fix the '
                    'asymmetric parts',
                    u'отражено на команду 2: точек %d, маршрутов %d — теперь '
                    u'поправьте несимметричные места'),
    'm.nomirror':  ('nothing on team 1 to mirror', u'у команды 1 нечего отражать'),
    'm.reassigned': ('reassigned to %s %s', u'переназначено: %s %s'),
    'm.noteam':    ('avoid areas have no team or class',
                    u'у запретных зон нет команды и класса'),
    'm.noteset':   ('note set', u'заметка сохранена'),
    'm.selected':  ('selected %s', u'выбрано: %s'),
    'm.nounder':   ('nothing under the cursor', u'под курсором ничего нет'),
    'm.recording': ('recording... press REC again to stop',
                    u'идёт запись... нажмите ЗАПИСЬ ещё раз, чтобы остановить'),
    'm.recframes': ('REC %d frames (%.1fs) - press REC to stop',
                    u'ЗАПИСЬ: кадров %d (%.1f с) — ЗАПИСЬ для остановки'),
    'm.recnone':   ('nothing recorded', u'ничего не записано'),
    'm.recsaved':  ('saved %s - %d frames, %.1fs at %d fps, %.1f MB',
                    u'сохранено %s — кадров %d, %.1f с при %d к/с, %.1f МБ'),
    'm.exported':  ('exported %s (%dx%d)', u'экспортировано %s (%dx%d)'),
    'm.recfail':   ('record failed: %s', u'ошибка записи: %s'),
    'm.giffail':   ('gif write failed: %s', u'ошибка сохранения GIF: %s'),
    'm.nograb':    ('PIL ImageGrab unavailable', u'PIL ImageGrab недоступен'),
    'm.exportfail': ('export failed: %s', u'ошибка экспорта: %s'),
    'm.decoding':  ('decoding color_tex.dds 4096x4096 ...',
                    u'распаковка color_tex.dds 4096x4096 ...'),
    'm.terrainok': ('original 4096x4096 terrain (no water, no buildings)',
                    u'исходный рельеф 4096x4096 (без воды и зданий)'),
}

_lang = ['en']


def set_lang(code):
    _lang[0] = code if code in LANGS else 'en'


def lang():
    return _lang[0]


def L(key, *args):
    pair = S.get(key)
    if not pair:
        return key
    txt = pair[1] if _lang[0] == 'ru' and len(pair) > 1 and pair[1] else pair[0]
    if args:
        try:
            return txt % args
        except (TypeError, ValueError):
            return txt
    return txt


_ARENA_CACHE = {}


def map_display_name(map_name, arenas_mo):
    """The game's own translated map name, e.g. 02_malinovka -> Малиновка.

    Read from the client's arenas.mo rather than translated here, so a RU user
    sees exactly the name their client shows. Falls back to the file name.
    """
    if _lang[0] != 'ru':
        return map_name
    if not _ARENA_CACHE:
        try:
            import gettext
            f = open(arenas_mo, 'rb')
            try:
                t = gettext.GNUTranslations(f)
            finally:
                f.close()
            _ARENA_CACHE['t'] = t
        except Exception:
            _ARENA_CACHE['t'] = None
    t = _ARENA_CACHE.get('t')
    if t is None:
        return map_name
    key = '%s/name' % map_name
    got = t.gettext(key)
    return got if got and got != key else map_name
