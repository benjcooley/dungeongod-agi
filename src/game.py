from abc import ABC, abstractmethod
from agent import Agent
from engine import Engine
from user import User
from typing import Any

Obj = dict[str, Any]

class Game(ABC):

    @abstractmethod
    def __init__(self, 
                 engine: Engine, 
                 user: User, 
                 start_game_action: str = "new_game",
                 module_name: str = "", 
                 party_name: str = "",
                 save_game_name: str = "") -> None:
        pass

    @property
    @abstractmethod
    def action_image_path(self) -> str|None:
        pass
    @action_image_path.setter
    @abstractmethod
    def action_image_path(self, value: str|None) -> None:
        pass

    @property
    @abstractmethod    
    def is_started(self) -> bool:
        pass
    
    @property
    @abstractmethod    
    def game_over(self) -> bool:
        pass

    @abstractmethod
    async def start_game(self) -> str:
        pass

    @property
    @abstractmethod
    def exit_to_lobby(self) -> bool:
        pass

    @exit_to_lobby.setter
    @abstractmethod
    def exit_to_lobby(self, value: bool) -> None:
        pass    
 
    @abstractmethod
    def parse_simple_action(self, query: str) -> tuple[str, list[str]]|None:
        pass

    @abstractmethod
    def get_query_hint(self) -> str|None:
        pass

    @abstractmethod
    async def do_action(self, action: Any, 
                  subject: Any = None, 
                  object: Any = None, 
                  extra: Any = None, 
                  extra2: Any = None) -> tuple[str, bool]:  
        pass  

    @property
    @abstractmethod    
    def action_list(self) -> list[dict[str, Any]]:
        pass
    
    @abstractmethod
    def clear_action_list(self) -> None:
        pass

    @property
    @abstractmethod
    def characters(self) -> dict[str, Obj]:
        pass  

    @property
    @abstractmethod
    def areas(self) -> dict[str, Obj]:
        pass

    @property
    @abstractmethod
    def locations(self) -> dict[str, Obj]:
        pass

    @property
    @abstractmethod
    def monster_types(self) -> dict[str, Obj]:
        pass

    @property
    @abstractmethod
    def module_monster_types(self) -> dict[str, Obj]:
        pass

    @property
    @abstractmethod
    def monsters(self) -> dict[str, Obj]:
        pass

    @property
    @abstractmethod
    def player_map(self) -> dict[str, list[str]]:
        pass

class ChatGameDriver(ABC):

    @abstractmethod
    def __init__(self, 
                 engine: Engine, 
                 user: User, 
                 start_game_action: str = "new_game",
                 module_name: str = "", 
                 party_name: str = "",
                 save_game_name: str = "") -> None:
        pass

    @property
    @abstractmethod    
    def game(self) -> Game:
        pass

    @property
    @abstractmethod    
    def is_started(self) -> bool:
        pass

    @property
    @abstractmethod    
    def game_over(self) -> bool:
        pass

    @abstractmethod
    async def start_game(self) -> str:
        pass

    @property
    @abstractmethod
    def exit_to_lobby(self) -> bool:
        pass

    @exit_to_lobby.setter
    @abstractmethod
    def exit_to_lobby(self, value: bool) -> None:
        pass

    @abstractmethod    
    async def timer_update(self, dt: float) -> str:
        pass
        
    @abstractmethod    
    async def player_action(self, 
                            query: str, 
                            chunk_handler: Any|None = None) -> str:
        pass

    @abstractmethod
    def split_dialog(self, resp) -> list[str|bytes]:
        pass

    @property
    @abstractmethod
    def button_tag(self) -> str|None:
        pass
    @button_tag.setter
    @abstractmethod
    def button_tag(self, v: str|None) -> None:
        pass

    @abstractmethod
    async def get_buttons(self, button_tag: str, state: dict[str, Any]) -> tuple[str|None, bool]:
        pass

    @abstractmethod
    async def call_actions(self, query: str, actions: list[tuple[str, list[str]]]) -> str:
        pass

    @property
    @abstractmethod    
    def action_list(self) -> list[dict[str, Any]]:
        pass
    
    @abstractmethod
    def clear_action_list(self) -> None:
        pass

    @property
    @abstractmethod
    def characters(self) -> dict[str, Obj]:
        pass  

    @property
    @abstractmethod
    def areas(self) -> dict[str, Obj]:
        pass

    @property
    @abstractmethod
    def locations(self) -> dict[str, Obj]:
        pass

    @property
    @abstractmethod
    def monster_types(self) -> dict[str, Obj]:
        pass

    @property
    @abstractmethod
    def module_monster_types(self) -> dict[str, Obj]:
        pass

    @property
    @abstractmethod
    def monsters(self) -> dict[str, Obj]:
        pass

    @property
    @abstractmethod
    def player_map(self) -> dict[str, list[str]]:
        pass
