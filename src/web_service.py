from apiflask import APIFlask, Schema, abort
from apiflask.fields import String, Boolean

import asyncio
import os
import shortuuid
import uuid

from agent import Agent
from config import ERROR_LOGGING, config
from engine import Engine, EngineManager
from firestoredb import FirestoreDb
from flask import request
from game import Game
from lobby import Lobby
from urllib.parse import quote
from user import User, get_user
from typing import Any, cast, Callable, Type

event_loop = asyncio.get_event_loop()
assert event_loop and not event_loop.is_closed()

MESSAGE_RATE_LIMIT = int(os.getenv('MESSAGE_RATE_LIMIT') or 5)

active_sessions: dict[str, Any] = {}
channel_states: dict[str, Any] = {}

CHANNEL_PREFIX: str = config.get("channel_prefix", "")
GAME: str = config.get("game", "")
APP_NAME: str = config.get("app_name", "")
APP_SERVER: str = config.get("app_server", "")
APP_DATA_URL: str = config.get("app_data_url", "")

engine_class: Type[Engine] = EngineManager.get_engine(GAME)
engine: Engine = engine_class(FirestoreDb(), logging=ERROR_LOGGING)
engine.set_defaults(config["default_party_name"], config["default_module_name"])

SAVE_GAME_NAME: str = "SAVE1001"
USER_NAME: str = "user001"
USER_ID: str = "100000000"

class GameSession:

    def __init__(self, game_user: User, game: Game, lobby: Lobby) -> None:
        self.game_user: User = game_user
        self.game: Game = game
        self.lobby: Lobby = lobby

sessions: dict[str, GameSession]

async def start_session(user_name: str, user_id: str) -> GameSession:

    global game_user
    global game

    game_user = await get_user(engine.db, user_name, str(user_id))

    lobby = engine.create_lobby(game_user)

    game = engine.create_game(game_user, 
                              start_game_action="new_game",
                              module_name=config["default_module_name"],
                              party_name=config["default_party_name"],
                              save_game_name=SAVE_GAME_NAME)
    await game.start_game()

    return GameSession(game_user, game, lobby)

game_sessions: dict[str, GameSession] = {}

async def get_game_session(user_id: str) -> GameSession:
    global game_sessions
    game_session = game_sessions.get(user_id)
    if game_session is None:
        game_session = cast(GameSession, await start_session(USER_NAME, user_id))
        game_sessions[user_id] = game_session
    return game_session

app = APIFlask(APP_NAME)
app.servers = [ { "name": APP_NAME, "url": APP_SERVER } ]

class Results(Schema):
    success = Boolean()
    response = String()
    private_hint = String()
    error = String()
    image_to_show = String()

def make_response(resp_tuple: tuple[str, bool]) -> dict[str, Any]:
    resp, err = resp_tuple
    if err:
        return { "success": False, "error": resp }
    else:
        return { "success": True, "response": resp }    

# -------------------------------------------------------------------------------------------

class ActionArgs(Schema):
    arg1 = String()
    arg2 = String()
    arg3 = String()
    arg4 = String()

@app.post('/do_action/<string:action>')
@app.input(ActionArgs(partial=True))  # -> json_data
@app.output(Results)
@app.doc(operation_id="do_action")
async def do_action(action: str, json_data: dict[str, Any]):
        """
        Does a game engine action and returns the results.
        """
        user_id = USER_ID
        if "Openai-Conversation-Id" in request.headers:
             uuid_key = uuid.UUID(request.headers["Openai-Conversation-Id"])
             user_id = shortuuid.encode(uuid_key)
        game_session = await get_game_session(user_id)
        game = game_session.game
        resp = make_response(await game.do_action(
            action,
            json_data.get("arg1"),
            json_data.get("arg2"),
            json_data.get("arg3"),
            json_data.get("arg4")))
        private_hint = game.get_query_hint()
        if private_hint is not None:
             resp["private_hint"] = private_hint
        if game.action_image_path is not None:
            resp["image_to_show"] = APP_DATA_URL + quote(game.action_image_path)
            game.action_image_path = None
        return resp

@app.get('/privacy')
@app.output(Results)
@app.doc(operation_id="privacy")
async def privacy():
        """
        Returns the privacy policy.
        """    
        return { 
                "success": True, 
                "response": engine.lobby_prompts.get("privacy_policy", "")
               }
