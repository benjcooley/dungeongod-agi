import os
import yaml
from db_access import Db
from typing import Any

class FileDb(Db):

    async def exists(self, key: str) -> bool:
        path = f"db/{key}.yaml"
        return os.path.exists(path)

    async def get(self, key: str) -> dict[str, Any]|None:
        path = f"db/{key}.yaml"
        data = None
        if os.path.exists(path):
            with open(path, "r") as f:
                data = yaml.load(f, Loader=yaml.FullLoader)
                return data
        return None

    async def put(self, key: str, data: dict[str, Any]) -> None:
        base_key = os.path.dirname(key)
        base_path = f"db/{base_key}"
        if not os.path.exists(base_path):
            os.makedirs(base_path)
        path = f"db/{key}.yaml"
        with open(path, "w") as f:
            yaml.dump(data, f)

    async def delete(self, key) -> bool:
        path = f"db/{key}.yaml"
        if os.path.exists(path):
            os.unlink(path)
            return True
        return False

    async def get_list(self, key) -> list[str]:
        path = f"db/{key}"
        if not os.path.exists(path):
            return []
        dirs = os.listdir(path)
        dirs.sort()
        return dirs
