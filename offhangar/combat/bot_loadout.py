# -*- coding: utf-8 -*-
"""
combat/bot_loadout.py — Комплектация модулей, орудий и оборудования ботов (v8.9 Modular).

Содержит расчёт ценности орудий (`score_bot_gun`, `score_bot_gun_hard`) с безопасной
распаковкой `Math.Vector2` (`piercingPower`, `damage`) и установку топовых башен/орудий/шасси.
"""
import random
try:
    from gui.mods.offhangar.logging import LOG_NOTE, LOG_DEBUG
except Exception:
    def LOG_NOTE(*a, **k): pass
    def LOG_DEBUG(*a, **k): pass


def score_bot_gun(gun_dict, vtags=(), hard_mode=False):
    """Оценивает эффективность орудия по уровню, пробитию ББ, разовому урону (альфе), ДПМ и скорости снаряда."""
    try:
        shots = gun_dict.get('shots', ()) or ()
        if not shots:
            return 0.0
        s0 = shots[0]
        pp = s0.get('piercingPower', (100, 100))
        try:
            pen0 = float(pp[0])
        except Exception:
            pen0 = float(pp or 100.0)
        sh0 = s0.get('shell', {})
        dmg = sh0.get('damage', (100, 100))
        try:
            dmg0 = float(dmg[0])
        except Exception:
            dmg0 = float(dmg or 100.0)
        cal = float(sh0.get('caliber', 75.0) or 75.0)
        spd = float(s0.get('speed', 600.0) or 600.0)
        rld = max(1.5, float(gun_dict.get('reloadTime', 6.0) or 6.0))
        lvl = float(gun_dict.get('level', 1) or 1)
        if 'SPG' in vtags and 'AT-SPG' not in vtags:
            return lvl * 120.0 + dmg0 * 0.5 + cal
        is_low_pen_derp = (cal >= 75.0 and pen0 <= 70.0 and spd <= 500.0)
        score = lvl * 60.0 + pen0 * 2.8 + dmg0 * 0.65 + (dmg0 / rld) * 8.0 + spd * 0.08
        if is_low_pen_derp:
            score -= 220.0
        return score
    except Exception:
        return 0.0


def upgrade_bot_components(td, tier='hard'):
    tier = 'hard'
    """Устанавливает модули танка бота (шасси, двигатель, рацию, башню и орудие) по уровню скилла."""
    if td is None or getattr(td, 'type', None) is None:
        return
    vtype = td.type
    vname = str(getattr(vtype, 'name', 'unknown'))
    try:
        ch_list = getattr(vtype, 'chassis', ()) or ()
        eng_list = getattr(vtype, 'engines', ()) or ()
        rad_list = getattr(vtype, 'radios', ()) or ()
        tur_slots = getattr(vtype, 'turrets', ()) or ()
        tur_list = tur_slots[0] if (tur_slots and len(tur_slots) > 0) else ()
        if tier == 'easy':
            if random.random() < 0.20 and ch_list and tur_list:
                try: td.installComponent(ch_list[-1]['compactDescr'])
                except Exception: pass
                st_tur = tur_list[0]
                guns = st_tur.get('guns', ()) or ()
                if len(guns) > 1:
                    try: td.installTurret(st_tur['compactDescr'], guns[min(1, len(guns) - 1)]['compactDescr'])
                    except Exception: pass
            LOG_DEBUG('BOT_LOADOUT_MODULES veh=%s tier=easy config=stock gun=%s hp=%d' % (
                vname, str((getattr(td, 'gun', None) or {}).get('name', '?')), int(getattr(td, 'maxHealth', 0) or 0)))
            return
        if len(ch_list) > 1:
            try: td.installComponent(ch_list[-1]['compactDescr'])
            except Exception: pass
        vtags = getattr(vtype, 'tags', ()) or ()
        if tier == 'medium':
            if len(eng_list) > 1:
                eng_idx = max(0, len(eng_list) // 2)
                try: td.installComponent(eng_list[eng_idx]['compactDescr'])
                except Exception: pass
            if len(rad_list) > 1:
                rad_idx = max(0, len(rad_list) // 2)
                try: td.installComponent(rad_list[rad_idx]['compactDescr'])
                except Exception: pass
            if tur_list:
                tur = tur_list[-1] if (len(tur_list) > 1 and random.random() < 0.70) else tur_list[0]
                guns = list(tur.get('guns', ()) or ())
                if guns:
                    sorted_guns = sorted(guns, key=lambda gg: score_bot_gun(gg, vtags, False))
                    gun = sorted_guns[-1] if (len(sorted_guns) == 1 or random.random() < 0.65) else sorted_guns[-2]
                else:
                    gun = None
                if tur is not None and gun is not None:
                    try: td.installTurret(tur['compactDescr'], gun['compactDescr'])
                    except Exception: pass
            LOG_DEBUG('BOT_LOADOUT_MODULES veh=%s tier=medium config=partial gun=%s hp=%d' % (
                vname, str((getattr(td, 'gun', None) or {}).get('name', '?')), int(getattr(td, 'maxHealth', 0) or 0)))
        else:
            if len(eng_list) > 1:
                try: td.installComponent(eng_list[-1]['compactDescr'])
                except Exception: pass
            if len(rad_list) > 1:
                try: td.installComponent(rad_list[-1]['compactDescr'])
                except Exception: pass
            if tur_list:
                tur = tur_list[-1]
                guns = list(tur.get('guns', ()) or ())
                gun = max(guns, key=lambda gg: score_bot_gun(gg, vtags, True)) if guns else None
                if tur is not None and gun is not None:
                    try: td.installTurret(tur['compactDescr'], gun['compactDescr'])
                    except Exception: pass
            LOG_DEBUG('BOT_LOADOUT_MODULES veh=%s tier=hard config=top_elite gun=%s hp=%d' % (
                vname, str((getattr(td, 'gun', None) or {}).get('name', '?')), int(getattr(td, 'maxHealth', 0) or 0)))
    except Exception as mod_err:
        LOG_DEBUG('BOT_LOADOUT_MODULES err veh=%s tier=%s: %s' % (vname, tier, str(mod_err)))
