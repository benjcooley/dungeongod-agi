from abc import ABC

class Db(ABC):

    async def exists(self, key: str) -> bool:
        pass

    async def get(self, key: str) -> dict[str, any]:
        pass

    async def put(self, key: str, data: dict[str, any]) -> None:
        pass

    async def delete(self, key) -> bool:
        pass

    async def get_list(self, key) -> list[str]:
        pass