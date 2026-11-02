# World of Tanks 0.8.2 — Offline Battles 2.0

🇺🇸EN:
**Offline Battles 2.0** is a major update to the standalone offline modification for **World of Tanks 0.8.2** ([fr3asikx/WoT-0.8.2-offline-Battles](https://github.com/fr3asikx/WoT-0.8.2-offline-Battles)). Play full 15v15 battles against tactical AI bots and enjoy the complete 0.8.2 garage experience locally on your PC without an internet connection or external server.

**DOWNLOAD RELEASE FROM HERE (СКАЧАТЬ ГОТОВЫЙ БИЛД ЗДЕСЬ): https://github.com/fr3asikx/WoT-0.8.2-offline-Battles/releases/tag/release**

## Key Features in 2.0

* **Full Offline Garage & Progression**: Buy, sell, and research vehicles across all 6 nations of patch 0.8.2, mount modules, equipment, consumables, and camouflage, train crew members, and track service record statistics and medals with persistent local profiles.
* **Map & team selection in the hangar** Buttons next to the Battle! button let you pick any of the 33 classic maps, switch between Standard Battle / Assault / Encounter, and cycle team size — *15×15, 12×12, 10×10, 7×7, 5×5, 3×3, 1×1*.
* **Unified Tactical Bot AI**:
  * All bots deploy in top configurations and operate on a unified Hardmode tactical AI foundation.
  * Class-based flank distribution (heavies push city and brawl corridors, mediums and lights flank and spot, TDs hold support lines, SPGs provide indirect fire).
  * Enemy reload & gun-line awareness, cover peeking, hull angling, track-assist targeting, low-HP focus fire, and coordinated base defense when the friendly cap circle is threatened.
  * Smooth hill climbing on steep mountain maps (*Dragon Ridge*, *Westfield*, *El Halluf*, *Abbey*) and navigation through destructible obstacles.
* **Native bot physics** Every bot runs on its own WGVehiclePhysics2 + WGVehicleFilter2 native physics body the same engine the stock client uses for the player vehicle. Tanks actually push against terrain, slide on slopes, and react to collisions instead of floating through them.
* **Prebaked navigation graphs for all 33 maps** Pathfinding is built on precomputed navigation meshes with real terrain heights, water/cliff hazard marking, and 3D mountain occlusion checks. Bots find routes around buildings and ridgelines rather than trying to drive through them.
* **RU / EN Localization**: Automatic client language detection.
* **Battle replay recording** Every battle saves a standard `.wotreplay` file you can watch back or share.
* **Random battle atmosphere, 2012 style** Over 1,000 era-accurate RU/EU nicknames, dynamic in-battle chat with tactical callouts, and the **Ace Bot Hall of Fame** - the mod's developers, consultants, and CBT testers drive their signature tanks alongside you.
* **RU / EN localization** Language is picked automatically based on your client locale.

### Installation

1. Get a clean **World of Tanks 0.8.2** client (available at [wot-offline.com](https://dl.wot-offline.com/World-of-tanks-pre1.0/0.8.02)).
2. Download `Offline_Battles_2.0_release.zip` from the **Releases** page.
3. Extract into your WoT folder 'res_mods'.
4. Run `WorldOfTanks.exe`, enter any nickname on the login screen, and click **Login**.
5. Use the buttons next to **Battle!** in the garage header to select a map, mode, and team size, then go fight.

### Credits

**`kistoprav`** — creator & developer of Offline Battles 2.0

**[`fr3asikx`](https://github.com/fr3asikx/WoT-0.8.2-offline-Battles)** — creator of the WoT-0.8.2-offline-Battles repository and the **Lead Developer** 0.8.2 offline mod, for consultations and collaboration throughout the project

**[`pengw0048`](https://github.com/pengw0048/wot-offline-battles)** — author of wot-offline-battles, for research and consultations on bot physics and navigation

**`Sudo` (`sudo ./god`) & `Arrofur`** — developers of OFFHANGAR 2.0 for modern WoT versions. Big thanks for their help, `Arrofur`'s testing sessions, and the bot system that inspired a lot of the AI work here

**`FireToG`** — developer of the 0.9.3 Offline Mod, CBT tester. Thanks for testing early builds, sharing code, and the honest feedback

**CBT testers:**
- **[`KorbenDallas` (Mikhail WoT)](https://www.youtube.com/watch?v=lzVvsMd0g8g)** — WoT offline mods blogger/streamer
- **`AnatoliKarpov`** — private server developer, community admin
- **`Brigadier`** — community moderator/admin

**[WoT Offline Center](https://wot-offline.global/) community** — for the support and feature requests that shaped a lot of what ended up in this release!

**[SigmaTel71](https://github.com/SigmaTel71/mod_offhangar_legacy)**, **`DrWeb7_1`**, **`goofy67`** — for the original `mod_offhangar_legacy` foundation

And a very personal thank you to **my wife** - for a month of patience, care, and putting up with me disappearing into this project every evening ❤️.

## License
This project includes code from [mod_offhangar_legacy](https://github.com/SigmaTel71/mod_offhangar_legacy) by SigmaTel71. 
Both that project and this project are licensed under the GNU General Public License v3.0 (GPLv3). See the [LICENSE](LICENSE) file for details.

## Legal Disclaimer & License

This project is a non-commercial modification for the classic version of World of Tanks 0.8.2, created strictly for educational and research purposes. This repository contains only modification scripts and does not include any original game executables or protected game assets. All rights to the "World of Tanks" trademark and game assets belong to Wargaming.net and/or Lesta Games.

## 🇷🇺 RU

**Offline Battles 2.0 (release)** — масштабное обновление оффлайн-мода для классического клиента **World of Tanks 0.8.2** (на базе [fr3asikx/WoT-0.8.2-offline-Battles](https://github.com/fr3asikx/WoT-0.8.2-offline-Battles)).

Полноценные бои 15×15 с тактическими ботами, рабочий ангар с прогрессией и сохранениями профиля — всё без интернета и стороннего сервера.

---

### Что в версии 2.0

* **Полноценный оффлайн-ангар** Покупка и продажа техники всех 6 наций патча 0.8.2, исследование веток и модулей, установка оборудования, снаряжения, камуфляжей. Обучение экипажа, казарма, склад, достижения и статистика — всё сохраняется локально между сессиями.

* **Выбор карты и формата прямо в ангаре** Кнопки рядом с **«В бой!»** позволяют выбрать любую из **33 классических карт**, переключить режим (**Стандартный бой / Штурм / Встречный бой**) и формат команды: `15×15`, `12×12`, `10×10`, `7×7`, `5×5`, `3×3`, `1×1`.

* **Нативная физика ботов** Каждый бот работает на собственном физическом теле `WGVehiclePhysics2 + WGVehicleFilter2` - том самом движке, который клиент использует для танка игрока. Боты по-настоящему упираются в рельеф, съезжают на склонах и реагируют на столкновения.

* **Запечённые навигационные графы для всех 33 карт** Маршруты строятся по предрасчитанным сеткам с реальными высотами рельефа, разметкой воды и обрывов, и проверкой блокировки горных хребтов в 3D. Боты объезжают здания и скалы — а не пытаются сквозь них проехать.

**Тактический ИИ ботов — хардмод**
Все боты выходят в бой в полной топовой комплектации:
- Распределение по направлениям по классу: ТТ продавливают тяжёлые коридоры, СТ и ЛТ работают по флангам и свету, ПТ занимают позиции поддержки, САУ стреляют с закрытых позиций
- Оценка КД и направления орудия противника перед выкатом, игра от укрытий, ромбование, сбитие гусениц
- Фокус шотных целей, своевременный возврат на сброс захвата своей базы
- Уверенное прохождение затяжных подъёмов («Хребет Дракона», «Вестфилд», «Эль-Халлуф», «Монастырь») без застреваний и полётов в воду

* **Запись реплеев** Каждый бой сохраняется в стандартный `.wotreplay` — можно пересмотреть или поделиться.

* **Стабильные многобоевые сессии**
Память боевого пространства корректно освобождается между боями — нет вылетов и зависаний при переходе во второй и третий бой подряд.

* **Атмосфера рандома 2012-го** Более 1000 аутентичных RU/EU никнеймов эпохи, живой боевой чат с тактическими командами и **Зал славы ботов-асов** - в боях на своих любимых танках встречаются разработчики, консультанты и тестеры мода.

* **Локализация RU / EN** Язык определяется автоматически по настройкам клиента.

---

### Установка и запуск

1. Скачайте чистый клиент **World of Tanks 0.8.2** (например, с [wot-offline.com](https://dl.wot-offline.com/World-of-tanks-pre1.0/0.8.02)).
2. Скачайте `Offline_Battles_2.0_release.zip` из раздела **Releases**.
3. Распакуйте архив в корневую папку игры WoT `res_mods` из архива.
4. Запустите `WorldOfTanks.exe`, введите любой никнейм и нажмите **«Войти»**.
5. Кнопками рядом с **«В бой!»** выберите карту, режим и формат команды — и в бой.

---

### Благодарности

**`kistoprav`** — автор и разработчик Offline Battles 2.0

**[`fr3asikx`](https://github.com/fr3asikx/WoT-0.8.2-offline-Battles)** — создатель репозитория WoT-0.8.2-offline-Battles и главный разработчик оффлайн мода для 0.8.2, за консультации и совместное развитие проекта

**[`pengw0048`](https://github.com/pengw0048/wot-offline-battles)** — автор wot-offline-battles, за исследования физики и навигации ботов

**`Sudo` (`sudo ./god`) и `Arrofur`** — разработчики OFFHANGAR 2.0 для современных версий WoT. Огромное спасибо за помощь, тесты от `Arrofur` и за их систему ботов, которая многое определила в нашем AI

**`FireToG`** — разработчик оффлайн-мода для 0.9.3, тестер ЗБТ. За тестирование ранних сборок, обмен наработками и честную обратную связь

**Команда ЗБТ-тестеров:**
- **[`KorbenDallas` (Михаил WoT)](https://www.youtube.com/watch?v=lzVvsMd0g8g)** — блогер по оффлайн-модам WoT
- **`AnatoliKarpov`** — разработчик приватного сервера, администратор сообщества
- **`Brigadier`** — администратор и модератор сообщества

**Сообществу [WoT Offline Center](https://wot-offline.global/)** — за поддержку и идеи, которые просили реализовать за всё время разработки. Без всех вас этого мода бы не было!

**[SigmaTel71](https://github.com/SigmaTel71/mod_offhangar_legacy)**, **`DrWeb7_1`**, **`goofy67`** — за классическое наследие `mod_offhangar_legacy` и архивные материалы

И отдельно - **любимой Жене**: за месяц терпения, заботы и за то, что не выгнала, пока я пропадал в этом проекте каждый вечер ❤️.

---

### Лицензия и правовая информация

Проект включает код из [mod_offhangar_legacy](https://github.com/SigmaTel71/mod_offhangar_legacy) (автор SigmaTel71). Оба проекта распространяются под **GNU General Public License v3.0 (GPLv3)**.

Это некоммерческая модификация классической версии 0.8.2, созданная в исследовательских и архивных целях. Репозиторий содержит только скрипты модификации — без оригинальных исполняемых файлов и защищённых ресурсов игры. Все права на «World of Tanks» принадлежат Wargaming.net и/или Lesta Games.
