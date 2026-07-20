# WoT-0.8.2-offline-Battles
World of tanks offline Mod to play in Version 0.8.2 

This project is a offline mod for world of tanks 0.8.2 to play the client fully offline on your own pc.
If you need the client here is a download link: https://dl.wot-offline.com/World-of-tanks-pre1.0/0.8.02/EU/World_of_Tanks_0.08.02.00.00_EU_0335_SD.7z
We also have a Discord Server for centralized versioning and partnered with other projects: https://discord.gg/HTGvB7bgAM
We also have a archive to safe every old WoT version to preserve History on https://Wot-offline.com

This project is a non-commercial modification for the legacy version of World of Tanks (0.8.2), created solely for educational and research purposes. Key Points:

This project does not contain server-side software (BigWorld Server), does not emulate network protocols for multiplayer, and does not bypass official server authentication.
All functionality is implemented by mocking client-side calls. Modifications in Login.py redirect the login flow to the local Manager.py script.
All displayed stats (gold, credits, XP) are local variables within the script and only exist during the offline session. They have no connection to real accounts or official databases.
This repository contains only the Python source code for the modification. It does not include original game binaries (.exe), textures, models, sounds, or any other copyrighted material.
All rights to the "World of Tanks" trademark and game assets belong to Wargaming.net and/or Lesta Games. The author of this project makes no claim to the intellectual property of these companies.

## License
This project includes code from [mod_offhangar_legacy](https://github.com/SigmaTel71/mod_offhangar_legacy) by SigmaTel71. 
Both that project and this project are licensed under the GNU General Public License v3.0 (GPLv3). See the [LICENSE](LICENSE) file for details.
