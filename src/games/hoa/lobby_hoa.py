from user import User
from utils import check_for_image

from engine import Engine
from .engine_hoa import EngineHoa
from lobby import Lobby
from typing import Any, cast

class LobbyHoa(Lobby):
    
    def __init__(self, engine: Engine, user: User) -> None:
        self.engine: EngineHoa = cast(EngineHoa, engine)
        self.user: User = user
        self.prompts: dict[str, str] = self.engine.lobby_prompts
        self._action_image_path: str|None = None
        self._start_the_game: bool = False
        self._start_game_action: str = "new_game"        
        self._start_game_module_name: str = ""
        self._start_game_party_name: str = ""
        self._start_game_save_game_name: str = "latest"
        self._action_list: list[dict[str, Any]] = []
        self._lobby_active: bool = False

    def start_lobby(self) -> None:
        self._lobby_active = True        

    @property
    def is_started(self) -> bool:
        return self._lobby_active

    @property
    def action_image_path(self) -> str | None:
        return self._action_image_path
    
    @action_image_path.setter
    def action_image_path(self, v: str | None) -> None:
        self._action_image_path = v

    @property
    def start_the_game(self) -> bool:
        return self._start_the_game

    @start_the_game.setter
    def start_the_game(self, value: bool) -> None:
        self._start_the_game = value    

    @property
    def start_game_action(self) -> str:
        return self._start_game_action

    @property
    def start_game_party_name(self) -> str:
        return self._start_game_party_name

    @property
    def start_game_module_name(self) -> str:
        return self._start_game_module_name

    @property
    def start_game_save_game_name(self) -> str:
        return self._start_game_save_game_name

    # -------------------
    # Lobby Actions
    # -------------------

    async def resume(self) -> tuple[str, bool]:
        resp = self.prompts["start_lobby_prompt"]
        self._action_image_path = "data/images/lobby.jpg"
        return (resp, False)

    async def create_party(self, party_name: str) -> tuple[str, bool]:
        if not isinstance(party_name, str):
            return ( "Empty party name", True)
        err_str, err, _ = await self.engine.create_party(self.user, party_name)
        return err_str, err

    async def add_char(self, party_name: str, char_name: str) -> tuple[str, bool]:
        if not isinstance(party_name, str):
            return ("Empty party name", True)
        if not isinstance(char_name, str):
            return ("Empty character name", True)
        if self.engine.char_exists(party_name):
            temp = party_name
            party_name = char_name
            char_name = temp
        return await self.engine.add_char_to_party(self.user, party_name, char_name)

    async def remove_char(self, party_name: str, char_name: str) -> tuple[str, bool]:
        if not isinstance(party_name, str):
            return ("Empty party name", True)
        if not isinstance(char_name, str):
            return ("Empty character name", True)
        if self.engine.char_exists(party_name):
            temp = party_name
            party_name = char_name
            char_name = temp        
        return await self.engine.remove_char_from_party(self.user, party_name, char_name)

    async def list_parties(self) -> tuple[str, bool]:
        err_str, err, party_names = await self.engine.list_parties(self.user)
        if err:
            return (err_str, err)
        resp = ""
        for party_name in party_names:
            resp = resp + party_name + "\n"
        if resp == "":
            resp = "No parties have been created yet.\n"
        return (resp, False)
    
    async def describe_party(self, party_name: str) -> tuple[str, bool]:
        if not isinstance(party_name, str):
            return ("Empty party name", True)
        err_str, err, party = await self.engine.load_party(self.user, party_name)
        if err:
            return (err_str, err)
        assert party is not None
        resp = ""
        for char in party["characters"].values():
            char_info = {}
            char_info["full_name"] = char["info"]["basic"]["full_name"]
            char_info["race"] = char["info"]["basic"]["race"]
            char_info["class"] = char["info"]["basic"]["class"]
            char_info["level"] = char["stats"]["basic"]["level"]
            resp = resp + str(char_info) + "\n"
        return (resp, False)
    
    async def list_modules(self) -> tuple[str, bool]:
        err_str, err, module_infos = self.engine.list_modules()
        if err:
            return (err_str, err)
        resp = ""
        for module_name, module in module_infos.items():
            info = {}
            info["module_name"] = module_name
            info["short_description"] = module["short_description"]
            resp = resp + str(info) + "\n"
        return (resp, False)
    
    async def describe_module(self, module_name: str) -> tuple[str, bool]:
        if not isinstance(module_name, str):
            return ("Empty module name", True)
        err_str, err, module_info = self.engine.get_module_info(module_name)
        if err:
            return (err_str, err)
        return (str(module_info) + "\n", False)

    async def list_chars(self, query_type: str, filter: str|int) -> tuple[str, bool]:
        if not isinstance(query_type, str) or query_type not in ["class", "level", "race", "name"]:
            query_type = "name"
            filter = "A"
        if query_type in ["class", "race", "name"] and (not isinstance(filter, str) or filter == ""):
            if query_type == "class":
                filter = "Adventurer"
            elif query_type == "race":
                filter = "Human"
            elif query_type == "name":
                filter = "A"
        elif query_type == "level":
            if filter is None or filter == "":
                filter = 1
            else:
                filter = int(filter)
        err_str, err, char_list = self.engine.get_char_list(query_type, filter)
        if err:
            return (err_str, err)
        assert char_list is not None
        resp = ""
        count = 0
        for char in char_list:
            char_info = {}
            char_info["full_name"] = char["info"]["basic"]["full_name"]
            char_info["race"] = char["info"]["basic"]["race"]
            char_info["class"] = char["info"]["basic"]["class"]
            char_info["level"] = char["stats"]["basic"]["level"]
            resp = resp + str(char_info) + "\n"
            count += 1
            if count >= 20:
                break
        return (f"Character list results for query_type: {query_type}, with filter: {filter}\n\n" + resp, False)
    
    async def describe_char(self, char_name: str) -> tuple[str, bool]:
        if not isinstance(char_name, str):
            return ("Empty character name", True)       
        err_str, err, char = self.engine.get_character(char_name)
        if err:
            return (err_str, err)
        assert char is not None
        full_name = char["info"]["basic"]["full_name"]
        self._action_image_path = check_for_image("data/characters/images", full_name)
        return (str(char) + "\n\n" + self.prompts["describe_stats_instructions"] + "\n", False)

    async def start_game(self, module_name: str, party_name: str) -> tuple[str, bool]:
        if not isinstance(module_name, str):
            return ("Empty module name", True)
        if not isinstance(party_name, str):
            return ("Empty party name name", True)
        if self.engine.module_exists(party_name):
            temp = module_name
            module_name = party_name
            party_name = temp
        err_str, err = await self.engine.can_play_game(self.user, module_name, party_name)
        if err:
            return (err_str, err)
        # Signal to outer controller we need to start a game
        self._start_the_game = True
        self._start_game_action = "new_game"
        self._start_game_module_name = module_name
        self._start_game_party_name = party_name
        self._start_game_save_game_name = "latest"
        self._lobby_active = False
        return ("ok", False)

    async def resume_game(self) -> tuple[str, bool]:
        # Signal to outer controller we need to start a game
        self._start_the_game = True
        self._start_game_action = "resume_game"
        self._start_game_module_name = ""
        self._start_game_party_name = ""
        self._start_game_save_game_name = "latest"
        self._lobby_active = False
        return ("ok", False)

    async def load_game(self, save_game_name: str) -> tuple[str, bool]:
        # Signal to outer controller we need to start a game
        self._start_the_game = True
        self._start_game_action = "resume_game"
        self._start_game_module_name = ""
        self._start_game_party_name = ""
        self._start_game_save_game_name = save_game_name
        self._lobby_active = False
        return ("ok", False)

    async def do_action(self, action: Any, arg1: Any = None, arg2: Any = None, arg3: Any = None) -> str:
        
        resp = ""
        error = False

        if self.engine.logging:
            print(f"  LOBBY ACTION: {action} {arg1} {arg2} {arg3}")
        
        match action:
            case "resume":
                resp, error = await self.resume()
            case "create_party":
                party_name = arg1
                resp, error = await self.create_party(party_name)
            case "add_char":
                party_name = arg2
                char_name = arg1
                resp, error = await self.add_char(party_name, char_name)
            case "remove_char":
                party_name = arg2
                char_name = arg1
                resp, error = await self.remove_char(party_name, char_name)
            case "list_parties":
                resp, error = await self.list_parties()
            case "describe_party":
                party_name = arg1
                resp, error = await self.describe_party(party_name)
            case "list_modules":
                resp, error = await self.list_modules()
            case "describe_module":
                module_name = arg1
                resp, error = await self.describe_module(module_name)
            case "list_chars":
                query_type = arg1
                filter = arg2
                resp, error = await self.list_chars(query_type, filter)                
            case "describe_char":
                char_name = arg1
                resp, error = await self.describe_char(char_name)
            case "start_game":
                module_name = arg1
                party_name = arg2
                resp, error = await self.start_game(module_name, party_name)
            case "resume_game":
                resp, error = await self.resume_game()
            case "load_game":
                save_game_name = arg1
                resp, error = await self.load_game(save_game_name)
            case _:
                resp = f"unknown action {action}'"
                error = True    

        if error:
            if self.engine.logging:
                print(f"  ERROR: {resp}")
            return resp

        self._action_list.append({ "action": action, "arg1": arg1, "arg2": arg2, "arg3": arg3, "arg4": None})

        return resp

    @property
    def action_list(self) -> list[dict[str, Any]]:
        return self._action_list
    
    def clear_action_list(self) -> None:
        self._action_list.clear()