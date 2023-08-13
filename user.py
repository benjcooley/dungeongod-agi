import os
import yaml
from db import Db

class User():

    def __init__(self, db: Db, data: dict[str, any]):
        self.db = db
        self.data = data
        self.name = data["name"]
        self.id = data["id"]

    async def save(self) -> None:
        await self.db.put(f"{self.user_path}/user", self.data)

    @property
    def user_path(self) -> str:
        return f"users/{self.id}"

# internal cache of users
__users = {}

async def get_user(db: Db, name: str, id: int) -> User:
    global __users
    data = { "name": name, "id": id }
    user_key = f"users/{id}/user"
    user = __users.get(user_key)
    if user is None:
        saved_data = await db.get(user_key)
        resave = False
        if saved_data:
            if saved_data["name"] != data["name"]:
                resave = True
            saved_data.update(data)
            data = saved_data
        else:
            resave = True
        user = User(db, data)
        if resave:
            await user.save()
        __users[user_key] = user
    return user
