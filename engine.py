import copy
import os
import yaml
from utils import is_valid_filename
from user import User
from db import Db

class Engine():

    def __init__(self, db: Db):
        self.db: Db = db
        self.lobby: dict[str, any] = {} 
        with open(f"data/lobby.yaml", "r") as f:
            self.lobby = yaml.load(f, Loader=yaml.FullLoader)
        self.modules: dict[str, any] = {}
        with open(f"data/modules/modules.yaml", "r") as f:
            self.module_infos = yaml.load(f, Loader=yaml.FullLoader)
        self.characters: dict[str, any] = {}        
        with open(f"data/characters/characters.yaml", "r") as f:
            self.characters = yaml.load(f, Loader=yaml.FullLoader)
        self.chars_by_full_name = {}
        for name, char in self.characters["characters"].items():
            char["info"]["basic"]["name"] = name
            full_name = char["info"]["basic"]["full_name"]
            self.chars_by_full_name[full_name] = char
        self.default_party_name = ""
        self.default_party: dict[str, any] = {}
        self.channel_states: dict[str, any] = {}
        self.rules_cache: dict[str, any] = {}
        self.party_cache: dict[str, any] = {}
        self.module_cache: dict[str, any] = {}
        self.channel_state_cache: dict[str, any] = {}

    def set_defaults(self, party_name: str, module_name: str) -> None:
        self.default_party_name = party_name
        with open(f"data/parties/{party_name}/party.yaml", "r") as f:
            self.default_party = yaml.load(f, Loader=yaml.FullLoader)
        self.default_module_name = module_name

    async def load_rules(self, rules_path: str) -> dict[str, any]:
        rules = self.rules_cache.get(rules_path)
        if rules is None:
            with open(f"{rules_path}/rules.yaml", "r") as f:
                rules = yaml.load(f, Loader=yaml.FullLoader)
                self.rules_cache[rules_path] = rules
        return rules

    async def can_play_game(self, 
                      user: User, 
                      module_name: str, 
                      party_name: str) -> tuple[str, bool]:
        if module_name not in self.module_infos["modules"]:
            return (f"Game module {module_name} does not exist.", True)
        if not self.party_exists(user, party_name):
            return (f"The party {party_name} does not exist for user {user.name}.", True)
        err_str, err, party = await self.load_party(user, party_name)
        if err:
            return (err_str, err)
        module_info = self.module_infos["modules"][module_name]
        error_str = ""
        if "levels" in module_info:
            module_levels = module_info["levels"]
            level_items = module_levels.split("-")
            min_level = int(level_items[0])
            max_level = int(level_items[1])
            for char in party["characters"].values():
                char_name = char["info"]["basic"]["full_name"]
                char_level = char["stats"]["basic"].get("level", 1)
                if char_level < min_level or char_level > max_level:
                    error_str += f"Party character {char_name} is level {char_level}. Allowed levels are {module_levels}.\n"
        if "players" in module_info:
            module_players = module_info["players"]
            player_items = module_players.split("-")
            min_players = int(player_items[0])
            max_players = int(player_items[1])
            num_players = len(party["characters"])
            if num_players < min_players or num_players > max_players:
                error_str += f"Party size is {num_players}. Allowed party sizes are {module_players}.\n"
        if error_str != "":
            return (f"Party {party_name} is unable to play module {module_name}.\n" + error_str, True)
        return ("ok", False)

    async def can_resume_game(self, user: User, 
                        save_game_name: str = None, 
                        module_name: str = None, 
                        party_name: str = None) -> tuple[str, bool]:
        if save_game_name is None:
            save_game_name = "latest"
        latest_game_path = f"{user.user_path}/save_games/{save_game_name}"
        exists = await self.db.exists(latest_game_path)
        if not exists:
            # We can just start a new gme if we know what party/module
            if module_name is not None and party_name is not None:
                return await self.can_play_game(user, module_name=module_name, party_name=party_name)
            else:
                return ("There is no current game in progress for user {user.name}.", True)
        return ("ok", False)

    async def party_exists(self, user: User, party_name: str) -> bool:
        party_path = f"{user.user_path}/parties/{party_name}"
        if party_path in self.party_cache:
            return True
        return await self.db.exists(party_path)

    async def create_party(self, user: User, party_name: str) -> tuple[str, bool, dict[str, any]]:
        if not is_valid_filename(party_name):
            return (f"{party_name} is not a valid name for a party.", True, None)
        exists = await self.party_exists(user, party_name)
        if exists:
            return (f"Party '{party_name}' already exists.", True, None)
        party = {}
        party["characters"] = {}
        party_path = f"{user.user_path}/parties/{party_name}"
        await self.db.put(party_path, party)
        self.party_cache[party_path] = party
        return ("ok", False, party)
    
    async def list_parties(self, user: User) -> tuple[str, bool, list[str]]:
        parties_path = f"{user.user_path}/parties"
        parties = await self.db.get_list(parties_path)
        return ("ok", False, parties)

    async def load_party(self, user: User, party_name: str) -> tuple[str, bool, dict[str, any]]:
        party_path = f"{user.user_path}/parties/{party_name}"
        party = await self.db.get(party_path)        
        if not party:
            return (f"Party {party_name} doesn't exist.", True, None)
        self.party_cache[party_path] = party
        return ("ok", False, party)

    async def save_party(self, user: User, party_name: str) -> tuple[str, bool]:
        party_path = f"{user.user_path}/parties/{party_name}"
        party = self.party_cache.get(party_path)
        if not party:
            return (f"Party {party_name} isn't loaded", True)
        await self.db.put(party_path, party)
        return ("ok", False)
    
    async def load_default_party(self, user: User) -> tuple[str, bool]:
        party_path = f"{user.user_path}/parties/{self.default_party_name}"
        default_party = self.party_cache.get(party_path)
        exists = await self.party_exists(user, self.default_party_name)
        if not exists:
            self.party_cache[party_path] = self.default_party
            err_str, err = await self.save_party(user, self.default_party_name)
            if err:
                return (err_str, err, None)
        return ("ok", False, default_party)

    def module_exists(self, module_name: str) -> bool:
        return module_name in self.module_infos["modules"]
 
    async def load_module(self, module_name: str) -> tuple[str, bool, dict[str, any]]:
        if not self.module_exists(module_name):
            return (f"Module {module_name} doesn't exist.", True, None)
        module_path = f"data/modules/{module_name}"
        module = self.module_cache.get(module_path)
        if not module:
            with open(f"{module_path}/module.yaml", "r") as f:
                module = yaml.load(f, Loader=yaml.FullLoader)
        self.module_cache[module_path] = module
        return ("ok", False, module)

    def get_module_info(self, module_name: str) -> tuple[str, bool, dict[str, any]]:
        if module_name not in self.module_infos["modules"]:
            return (f"{module_name} does not exist.", True, None)
        module_info = self.module_infos["modules"][module_name]
        return ("ok", False, module_info)
    
    def list_modules(self) -> tuple[str, bool, dict[str, any]]:
        return ("ok", False, self.module_infos["modules"])
    
    def get_character(self, char_name: str) -> tuple[str, bool, dict[str, any]]:
        char = self.characters["characters"].get(char_name)
        if char is None:
            char = self.chars_by_full_name.get(char_name)
        if char is None:
            return (f"{char_name} doesn't exist", True, None)
        return ("ok", False, char)
     
    def char_exists(self, char_name: str) -> bool:
        _, _, char = self.get_character(char_name)
        return char is not None

    def get_char_list(self, query_type: str, query: any) -> tuple[str, bool, list[dict[str, any]]]:
        if query_type not in ["level", "race", "class", "name"]:
            return (f"{query_type} is not a valid query type", True, None)
        chars = []
        if query_type == "level":
            level = (query if isinstance(query, int) else int(query))
            for char in self.characters["characters"].values():
                if char["stats"]["basic"]["level"] == level:
                    chars.append(char)
        elif query_type == "race":
            race = query
            for char in self.characters["characters"].values():
                if char["info"]["basic"]["race"] == race:
                    chars.append(char)
        elif query_type == "class":
            class_ = query
            for char in self.characters["characters"].values():
                if char["info"]["basic"]["class"] == class_:
                    chars.append(char)
        elif query_type == "name":
            nm = query.lower()
            for char in self.characters["characters"].values():
                if nm in char["info"]["basic"]["full_name"].lower():
                    chars.append(char)
        return ("ok", False, chars)

    async def add_char_to_party(self, user: User, party_name: str, char_name: str) -> tuple[str, bool]:
        err_str, err, party = await self.load_party(user, party_name)
        if err:
            return (err_str, err)
        err_str, err, char = self.get_character(char_name)
        if err:
            return (err_str, err)
        char_name = char["info"]["basic"]["name"]
        char_full_name = char["info"]["basic"]["full_name"]
        if char_name in party["characters"]:
            return (f"Character {char_full_name} is already in party {party_name}.", True)
        party["characters"][char_name] = copy.deepcopy(char)
        err_str, err = await self.save_party(user, party_name)
        if err:
            return (err_str, err)
        return ("ok", False)

    async def remove_char_from_party(self, user: User, party_name: str, char_name: str) -> tuple[str, bool]:
        err_str, err, party = await self.load_party(user, party_name)
        if err:
            return (err_str, err)
        found_char = None
        for name, char in party["characters"].items():
            if char_name == name or char_name == char["info"]["basic"]["full_name"]:
                found_char = char
                break
        if found_char is None:
            return (f"No character named {char_name} in the party {party_name}.", True)
        char = found_char
        char_name = char["info"]["basic"]["name"]
        del party["characters"][char_name]
        err_str, err = await self.save_party(user, party_name)
        if err:
            return (err_str, err)
        return ("ok", False)
    
    async def get_channel_state(self, guild_id: int, channel_id: int) -> dict[str, any]:
        channel_state_key = f"guilds/{guild_id}/channels/{channel_id}"
        channel_state = self.channel_state_cache.get(channel_state_key)
        if channel_state is not None:
            return channel_state
        channel_state = await self.db.get(channel_state_key)
        self.channel_state_cache[channel_state_key] = channel_state
        return channel_state

    async def set_channel_state(self, guild_id: int, channel_id: int, channel_state: dict[str, any]) -> None:
        channel_state_key = f"guilds/{guild_id}/channels/{channel_id}"
        self.channel_state_cache[channel_state_key] = channel_state
        await self.db.put(channel_state_key, channel_state)
