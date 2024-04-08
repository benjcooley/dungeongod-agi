from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Callable, Type, TYPE_CHECKING

if TYPE_CHECKING:
    from agent import Agent
    from db_access import Db
    from game import Game, ChatGameDriver
    from lobby import Lobby, ChatLobbyDriver
    from user import User

class Engine(ABC):

    @abstractmethod
    def __init__(self, db: Db, logging: bool = False):
        pass

    @property
    @abstractmethod
    def db(self) -> Db:
        pass

    @property
    @abstractmethod
    def game_prompts(self) -> dict[str, str]:
        pass

    @property
    @abstractmethod
    def lobby_prompts(self) -> dict[str, str]:
        pass

    @abstractmethod
    def set_defaults(self, party_name: str, module_name: str) -> None:
        pass

    @abstractmethod
    async def get_channel_state(self, guild_id: int, channel_id: int) -> dict[str, Any]:
        pass

    @abstractmethod
    async def set_channel_state(self, guild_id: int, channel_id: int, channel_state: dict[str, Any]) -> None:
        pass

    @abstractmethod
    async def can_play_game(self, 
                      user: User, 
                      module_name: str, 
                      party_name: str) -> tuple[str, bool]:
        pass

    @abstractmethod
    async def can_resume_game(self, user: User, 
                        save_game_name: str|None = None, 
                        module_name: str|None = None, 
                        party_name: str|None = None) -> tuple[str, bool]:
        pass

    @property
    @abstractmethod
    def default_party_name(self) -> str:
        pass

    @abstractmethod
    async def party_exists(self, user: User, party_name: str) -> bool:
        pass

    @abstractmethod
    async def list_parties(self, user: User) -> tuple[str, bool, list[str]]:
        pass

    @abstractmethod
    async def load_default_party(self, user: User) -> tuple[str, bool, dict[str, Any]|None]:
        pass

    @property
    @abstractmethod
    def default_module_name(self) -> str:
        pass

    @abstractmethod
    def module_exists(self, module_name: str) -> bool:
        pass

    @abstractmethod
    def list_modules(self) -> tuple[str, bool, dict[str, Any]]:
        pass

    @abstractmethod
    def create_game(self,
                    user: User, 
                    start_game_action: str = "new_game",
                    module_name: str = "", 
                    party_name: str = "",
                    save_game_name: str = "") -> Game:
        pass

    @abstractmethod
    def create_lobby(self,
                     user: User) -> Lobby:
        pass

    @abstractmethod
    def create_chatbot_agent(self,
                             agent_tag: str,
                             model: str) -> Agent:
        pass    

    @abstractmethod
    def create_chatbot_game(self,
                            user: User, 
                            agent: Agent,
                            start_game_action: str = "new_game",
                            module_name: str = "", 
                            party_name: str = "",
                            save_game_name: str = "") -> ChatGameDriver:
        pass

    @abstractmethod
    def create_chatbot_lobby(self,
                    user: User, 
                    agent: Agent,
                    start_game_action: str = "new_game",
                    module_name: str = "", 
                    party_name: str = "",
                    save_game_name: str = "") -> ChatLobbyDriver:
        pass

class EngineManager:

    logging: bool = True
    engines: dict[str, Type[Engine]] = {}
    
    @staticmethod
    def register_engine(name: str, engine_class: Type[Engine]) -> None:
        EngineManager.engines[name] = engine_class

    @staticmethod
    def get_engine(name: str) -> Type[Engine]:
        engine_class = EngineManager.engines.get(name)
        assert engine_class is not None
        return engine_class
