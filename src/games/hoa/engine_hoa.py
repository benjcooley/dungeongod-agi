import copy
import os
import yaml

from agent import Agent
from config import config_all
from db_access import Db
from engine import Engine
from game import Game, ChatGameDriver
from lobby import Lobby, ChatLobbyDriver
from typing import Any, Callable, Type, TypedDict
from user import User
from utils import is_valid_filename

class ChatbotDef(TypedDict):
    create_chatbot_agent: Callable[[str, str], Agent]
    create_chatbot_game: Callable[[Engine, User, Agent, str, str, str, str], ChatGameDriver]
    create_chatbot_lobby: Callable[[Engine, User, Agent], ChatLobbyDriver]

class EngineHoa(Engine):

    chatbot_defs: dict[str, ChatbotDef] = {}

    game_type: Type[Game]
    lobby_type: Type[Lobby]

    def __init__(self, db: Db, logging: bool = False):
        self._db: Db = db
        self.logging = logging
        self.base_path = "data/games/hoa"
        self.agent_id = "default"
        self.model_id = "default"
        self.load_chatbot_prompts()
        self.rules: dict[str, Any] = {}
        with open(f"{self.base_path}/rules/rules.yaml", "r") as f:
            self.rules = yaml.load(f, Loader=yaml.FullLoader)
        self.modules: dict[str, Any] = {}
        with open(f"{self.base_path}/modules/modules.yaml", "r") as f:
            self.module_infos = yaml.load(f, Loader=yaml.FullLoader)
        self.characters: dict[str, Any] = {}
        with open(f"{self.base_path}/characters/characters.yaml", "r") as f:
            self.characters = yaml.load(f, Loader=yaml.FullLoader)
        self.chars_by_full_name = {}
        for name, char in self.characters["characters"].items():
            char["info"]["basic"]["name"] = name
            full_name = char["info"]["basic"]["full_name"]
            self.chars_by_full_name[full_name] = char
        self._default_party_name = ""
        self.default_party: dict[str, Any] = {}
        self.channel_states: dict[str, Any] = {}
        self.party_cache: dict[str, Any] = {}
        self._default_module_name = ""
        self.module_cache: dict[str, Any] = {}
        self.channel_state_cache: dict[str, Any] = {}

    @property
    def db(self) -> Db:
        return self._db

    @property
    def game_prompts(self) -> dict[str, str]:
        return self._game_prompts

    @property
    def lobby_prompts(self) -> dict[str, str]:
        return self._lobby_prompts

    def set_defaults(self, party_name: str, module_name: str) -> None:
        self._default_party_name = party_name
        with open(f"{self.base_path}/parties/{party_name}/party.yaml", "r") as f:
            self.default_party = yaml.load(f, Loader=yaml.FullLoader)
        self._default_module_name = module_name

    def load_chatbot_prompts(self) -> None:
        # Will load prompts customized for the given chatbot agent type or chatbot type
        lobby_prompts_path = f"{self.base_path}/prompts/lobby_prompts.yaml"
        with open(lobby_prompts_path, "r") as f:
            self._lobby_prompts: dict[str, str] = yaml.load(f, Loader=yaml.FullLoader)
        game_prompts_path = f"{self.base_path}/prompts/game_prompts.yaml"
        with open(game_prompts_path, "r") as f:
            self._game_prompts: dict[str, str] = yaml.load(f, Loader=yaml.FullLoader)

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
        assert party is not None
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
                        save_game_name: str|None = None, 
                        module_name: str|None = None, 
                        party_name: str|None = None) -> tuple[str, bool]:
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

    async def create_party(self, user: User, party_name: str) -> tuple[str, bool, dict[str, Any]|None]:
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
    
    @property
    def default_party_name(self) -> str:
        return self._default_party_name
        
    async def list_parties(self, user: User) -> tuple[str, bool, list[str]]:
        parties_path = f"{user.user_path}/parties"
        parties = await self.db.get_list(parties_path)
        return ("ok", False, parties)

    async def load_party(self, user: User, party_name: str) -> tuple[str, bool, dict[str, Any]|None]:
        if party_name == self._default_party_name:
            return await self.load_default_party(user)
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
    
    async def load_default_party(self, user: User) -> tuple[str, bool, dict[str, Any]|None]:
        party_path = f"{user.user_path}/parties/{self._default_party_name}"
        default_party = await self.db.get(party_path)        
        if default_party is None:
            default_party = copy.deepcopy(self.default_party)
            self.party_cache[party_path] = default_party
            err_str, err = await self.save_party(user, self._default_party_name)
            if err:
                return (err_str, err, None)
        else:
            self.party_cache[party_path] = default_party
        return ("ok", False, default_party)

    @property
    def default_module_name(self) -> str:
        return self._default_module_name

    def module_exists(self, module_name: str) -> bool:
        return module_name in self.module_infos["modules"]
 
    async def load_module(self, module_name: str) -> tuple[str, bool, dict[str, Any]|None]:
        if not self.module_exists(module_name):
            return (f"Module {module_name} doesn't exist.", True, None)
        module_path = f"{self.base_path}/modules/{module_name}"
        module = self.module_cache.get(module_path)
        if not module:
            with open(f"{module_path}/module.yaml", "r") as f:
                module = yaml.load(f, Loader=yaml.FullLoader)
        self.module_cache[module_path] = module
        return ("ok", False, module)

    def get_module_info(self, module_name: str) -> tuple[str, bool, dict[str, Any]|None]:
        if module_name not in self.module_infos["modules"]:
            return (f"{module_name} does not exist.", True, None)
        module_info = self.module_infos["modules"][module_name]
        return ("ok", False, module_info)
    
    def list_modules(self) -> tuple[str, bool, dict[str, Any]]:
        return ("ok", False, self.module_infos["modules"])
    
    def get_character(self, char_name: str) -> tuple[str, bool, dict[str, Any]|None]:
        char = self.characters["characters"].get(char_name)
        if char is None:
            char = self.chars_by_full_name.get(char_name)
        if char is None:
            return (f"{char_name} doesn't exist", True, None)
        return ("ok", False, char)
     
    def char_exists(self, char_name: str) -> bool:
        _, _, char = self.get_character(char_name)
        return char is not None

    def get_char_list(self, query_type: str, query: Any) -> tuple[str, bool, list[dict[str, Any]]|None]:
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
        assert party is not None
        err_str, err, char = self.get_character(char_name)
        if err:
            return (err_str, err)
        assert char is not None
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
        assert party is not None
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
    
    async def get_channel_state(self, guild_id: int, channel_id: int) -> dict[str, Any]:
        channel_state_key = f"guilds/{guild_id}/channels/{channel_id}"
        channel_state = self.channel_state_cache.get(channel_state_key)
        if channel_state is not None:
            return channel_state
        channel_state = await self.db.get(channel_state_key)
        self.channel_state_cache[channel_state_key] = channel_state
        return channel_state

    async def set_channel_state(self, guild_id: int, channel_id: int, channel_state: dict[str, Any]) -> None:
        channel_state_key = f"guilds/{guild_id}/channels/{channel_id}"
        self.channel_state_cache[channel_state_key] = channel_state
        await self.db.put(channel_state_key, channel_state)

    @staticmethod
    def register_chatbot(name: str, chatbot_def: ChatbotDef) -> None:
        EngineHoa.chatbot_defs[name] = chatbot_def

    def create_game(self,
                    user: User, 
                    start_game_action: str = "new_game",
                    module_name: str = "", 
                    party_name: str = "",
                    save_game_name: str = "") -> Game:
        return EngineHoa.game_type(self, 
                                   user, 
                                   start_game_action=start_game_action, 
                                   module_name=module_name,
                                   party_name=party_name,
                                   save_game_name=save_game_name)

    def create_lobby(self,
                     user: User) -> Lobby:
        return EngineHoa.lobby_type(self, user)

    def create_chatbot_agent(self,
                             agent_tag: str,
                             model_endpoint: str) -> Agent:
        endpoint_cfg = config_all["model_endpoints"][model_endpoint]
        agent_id: str = endpoint_cfg["agent"]
        create_chatbot_agent = EngineHoa.chatbot_defs[agent_id]["create_chatbot_agent"]
        agent = create_chatbot_agent(agent_tag, model_endpoint)
        self.agent_id = agent.agent_id
        self.model_id = agent.primary_model_id
        self.load_chatbot_prompts()
        return agent

    def create_chatbot_game(self,
                            user: User, 
                            agent: Agent,
                            start_game_action: str = "new_game",
                            module_name: str = "", 
                            party_name: str = "",
                            save_game_name: str = "") -> ChatGameDriver:
        create_chatbot_game = EngineHoa.chatbot_defs[agent.agent_id]["create_chatbot_game"]
        return create_chatbot_game(self, user, agent, start_game_action, module_name, party_name, save_game_name)
    
    def create_chatbot_lobby(self,
                    user: User, 
                    agent: Agent,
                    start_game_action: str = "new_game",
                    module_name: str = "", 
                    party_name: str = "",
                    save_game_name: str = "") -> ChatLobbyDriver:
        create_chatbot_lobby = EngineHoa.chatbot_defs[agent.agent_id]["create_chatbot_lobby"]
        return create_chatbot_lobby(self, user, agent)