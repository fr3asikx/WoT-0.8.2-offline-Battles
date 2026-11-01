# -*- coding: utf-8 -*-
"""
core/runtime_state.py — Единый реестр глобальных переменных и состояния боя (v8.9 Modular).

Все ключевые структуры состояния боя (список активных танков/ботов, генерация боя,
таймеры старта, директор ИИ, навигатор, кэши приказов и укрытий) собраны и документированы
здесь, чтобы не искать их по 39 тысячам строк `offline_battle.py`.
"""

class BattleRuntimeState(object):
    def __init__(self):
        # Активные танки и сущности боя (id -> MockVehicle)
        self.mock_vehicles = {}
        # Счётчик поколения боя (увеличивается при каждом входе/выходе из боя)
        self.battle_gen = 0
        # Серверное время перехода арены в period=3 (активный бой)
        self.battle_start_time = None
        # Экземпляр тактического директора ботов (BotDirectorV2 / BattleDirector)
        self.bot_director = None
        # Экземпляр 4-метрового навигатора A* (TerrainNavigator)
        self.ai_navigator = None
        # Экземпляр локального водителя ботов (LocalDriver)
        self.ai_driver = None
        # Состояние покадрового планировщика квот ИИ (_offh_ai_frame_budget_plan)
        self.ai_frame_budget = None
        # Кэш локальных укрытий ботов (bot_id -> dict)
        self.ai_local_covers = {}
        # Таймстампы логов выцеливания уязвимых зон (kind -> timestamp)
        self.weakspot_log_at = {}

    def sync_from_globals(self, g):
        """Синхронизирует поля из словаря globals() модуля offline_battle."""
        if not isinstance(g, dict):
            return
        self.mock_vehicles = g.get('G_MOCK_VEHICLES') or g.get('mock_vehicles') or self.mock_vehicles
        self.battle_gen = int(g.get('g_offh_battle_gen', self.battle_gen) or 0)
        self.battle_start_time = g.get('g_offh_battle_start_time', self.battle_start_time)
        self.bot_director = g.get('g_offh_bot_director', self.bot_director)
        self.ai_navigator = g.get('g_offh_ai_navigator', self.ai_navigator)
        self.ai_frame_budget = g.get('g_offh_ai_frame_budget', self.ai_frame_budget)
        self.ai_local_covers = g.get('g_offh_ai_local_covers', self.ai_local_covers)
        self.weakspot_log_at = g.get('g_offh_weakspot_log_at', self.weakspot_log_at)


STATE = BattleRuntimeState()
