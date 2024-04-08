from abc import ABC, abstractmethod
from typing import Any

class Db(ABC):

    @abstractmethod
    async def exists(self, key: str) -> bool:
        pass

    @abstractmethod
    async def get(self, key: str) -> dict[str, Any]:
        pass

    @abstractmethod
    async def put(self, key: str, data: dict[str, Any]) -> None:
        pass

    @abstractmethod
    async def delete(self, key) -> bool:
        pass

    @abstractmethod
    async def get_list(self, key) -> list[str]:
        pass