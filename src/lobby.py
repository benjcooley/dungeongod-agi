from abc import ABC, abstractmethod
from agent import Agent
from engine import Engine
from user import User
from typing import Any

class Lobby(ABC):

    @abstractmethod
    def __init__(self, engine: Engine, user: User) -> None:
        pass

    @property
    @abstractmethod
    def action_image_path(self) -> str|None:
        pass

    @action_image_path.setter
    @abstractmethod
    def action_image_path(self, value: str|None) -> None:
        pass

    @abstractmethod
    def start_lobby(self) -> None:
        pass

    @property
    @abstractmethod
    def is_started(self) -> bool:
        pass

    @property
    @abstractmethod
    def start_the_game(self) -> bool:
        pass

    @start_the_game.setter
    @abstractmethod
    def start_the_game(self, value: bool) -> None:
        pass    

    @property
    @abstractmethod
    def start_game_action(self) -> str:
        pass

    @property
    @abstractmethod
    def start_game_party_name(self) -> str:
        pass

    @property
    @abstractmethod
    def start_game_module_name(self) -> str:
        pass

    @property
    @abstractmethod
    def start_game_save_game_name(self) -> str:
        pass

    @abstractmethod
    async def do_action(self, 
                        action: Any, 
                        arg1: Any = None, 
                        arg2: Any = None, 
                        arg3: Any = None) -> str:
        pass

    @property
    @abstractmethod    
    def action_list(self) -> list[dict[str, Any]]:
        pass
    
    @abstractmethod
    def clear_action_list(self) -> None:
        pass

class ChatLobbyDriver(ABC):

    @abstractmethod
    def __init__(self, engine: Engine, user: User, agent: Agent)  -> None:
        pass

    @property
    @abstractmethod
    def action_image_path(self) -> str|None:
        pass

    @action_image_path.setter
    @abstractmethod
    def action_image_path(self, value: str|None) -> None:
        pass

    @abstractmethod
    async def start_lobby(self) -> str:
        pass

    @property
    @abstractmethod
    def is_started(self) -> bool:
        pass

    @property
    @abstractmethod
    def start_the_game(self) -> bool:
        pass

    @start_the_game.setter
    @abstractmethod
    def start_the_game(self, value: bool) -> None:
        pass    

    @property
    @abstractmethod
    def start_game_action(self) -> str:
        pass

    @property
    @abstractmethod
    def start_game_party_name(self) -> str:
        pass

    @property
    @abstractmethod
    def start_game_module_name(self) -> str:
        pass

    @property
    @abstractmethod
    def start_game_save_game_name(self) -> str:
        pass

    @abstractmethod
    async def player_action(self, query: str, chunk_handler: Any = None) -> str:
        pass

    @property
    @abstractmethod    
    def action_list(self) -> list[dict[str, Any]]:
        pass
    
    @abstractmethod
    def clear_action_list(self) -> None:
        pass


    