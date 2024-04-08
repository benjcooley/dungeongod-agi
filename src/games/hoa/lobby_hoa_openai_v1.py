import yaml

from agent import Agent
from agents.agent_openai_v1 import OpenAIAgentV1
from engine import Engine
from games.hoa.engine_hoa import EngineHoa
from games.hoa.lobby_hoa import LobbyHoa
from lobby import ChatLobbyDriver
from user import User
from utils import extract_arguments
from typing import Any, cast

class LobbyHoaOpenAIV1(ChatLobbyDriver):
    
    def __init__(self, engine: Engine, user: User, agent: Agent) -> None:
        self._engine: EngineHoa = cast(EngineHoa, engine)
        self.user: User = user
        self._agent: OpenAIAgentV1 = cast(OpenAIAgentV1, agent)
        self._lobby: LobbyHoa = LobbyHoa(engine, user)
        self.response_id = 0
        self.lobby_prompts = self._engine.lobby_prompts
        self.lobby_prefix = [self._agent.make_message("user", self.lobby_prompts["lobby_prompt"], "prefix", keep=True)]
        self.messages = [ self._agent.make_message("assistant", "I'm Ready!", "referee", True) ]

    @property
    def action_image_path(self) -> str|None:
        return self._lobby.action_image_path

    @action_image_path.setter
    def action_image_path(self, value: str|None) -> None:
        self._lobby.action_image_path = value

    async def start_lobby(self) -> str:
        self._lobby.start_lobby()
        resume_prompt = self.lobby_prompts["resume_lobby_prompt"]
        return await self.system_action(resume_prompt, \
                           'call do_action("resume")', \
                           'Lobby Agent, you must use [call do_action("resume")] to start the game. Please try again.\n')

    @property
    def is_started(self) -> bool:
        return self._lobby.is_started

    @property
    def start_the_game(self) -> bool:
        return self._lobby.start_the_game

    @start_the_game.setter
    def start_the_game(self, value: bool) -> None:
        self._lobby.start_the_game = value    

    @property
    def start_game_action(self) -> str:
        return self._lobby.start_game_action

    @property
    def start_game_party_name(self) -> str:
        return self._lobby.start_game_party_name

    @property
    def start_game_module_name(self) -> str:
        return self._lobby.start_game_module_name

    @property
    def start_game_save_game_name(self) -> str:
        return self._lobby.start_game_save_game_name

    async def generate(self, query: str, source: str, 
                       primary: bool = True, keep: bool = False, chunk_handler: Any = None) -> str:
        instr_prompt = "<INSTRUCTIONS>" in query
        instr_prefix_prompt= "<PLAYER>" in query
        if instr_prompt or instr_prefix_prompt:
            # Strip Any per-response instructions and Any player tags for msg history. This reduces token usage
            # overall as AI only needs per-response instructions once.
            instr_msg = self._agent.make_message("user", query, source, keep=keep)
            if instr_prefix_prompt:
                query_only = query.split("<PLAYER>")[1]
            else:
                query_only = query.split("<INSTRUCTIONS>")[0]
            query_only = query_only.strip(" \t\n")
            query_msg = self._agent.make_message("user", query_only, source, keep=keep)
        else:
            instr_msg = query_msg = self._agent.make_message("user", query, source, keep=keep)
        msgs = self.lobby_prefix + \
            self.messages + \
            [ instr_msg ]
        resp = await self._agent.generate(msgs, primary, chunk_handler=chunk_handler)
        resp_msg = self._agent.make_message("assistant", resp, "lobby", keep=False)
        self.messages.append(query_msg)
        self.messages.append(resp_msg)
        return resp_msg["content"]

    async def system_action(self, query: str, expected_action: str|None = None, retry_msg: str|None = None, chunk_handler: Any = None) -> str:
        return await self.process_action("system", query, expected_action, retry_msg, chunk_handler=chunk_handler)

    async def player_action(self, query: str, chunk_handler: Any = None) -> str:
        return await self.process_action("player", query, chunk_handler=chunk_handler)
            
    async def process_action(self, 
                             source: str, 
                             query: str, 
                             expected_action: str|None = None, 
                             retry_msg: str|None = None, 
                             chunk_handler: Any = None) -> str:
        self._lobby.action_image_path = None
        is_system = (source == "system")
        if is_system:
            resp = await self.generate(query, source)
        else:
            resp = await self.generate(self.lobby_prompts["action_instr_prompt"] + "\n\n" + query, source)
        if expected_action is not None and expected_action not in resp:
            assert retry_msg is not None
            while expected_action not in resp:
                resp = await self.generate(retry_msg, "system", primary=True, keep=False)
        processed_resp = await self.process_response(query, resp, 1, chunk_handler=chunk_handler)
        if self._lobby.action_image_path is not None:
            processed_resp = processed_resp + "\n" + "@image: " + self._lobby.action_image_path
            self._lobby.action_image_path = None
        return processed_resp

    async def process_response(self, query: str, response: str, level: int, chunk_handler: Any = None) -> str:
        if level == 4:
            return response.strip(" \n\t")
        lines = response.split("\n")
        results = ""
        for line in lines:
            line = line.strip()
            if line == "PASS" and query != "":
                return await self.generate(query, "lobby", primary=False)
            elif line == "NOT ALLOWED":
                return await self.generate("This requst or action is not allowed.", "lobby", primary=False)
            if "do_action(" in line:
                args = extract_arguments(line, 4)
                game_resp = await self._lobby.do_action(args[0], args[1], args[2], args[3])
                results += game_resp + "\n"
        if results != "":
            response = "<RESPONSE>\n" + \
                        results
            ai_result = await self.generate(response, "lobby", primary=False, chunk_handler=chunk_handler)
            self.response_id += 1
            if "call do_action(" in ai_result:
                ai_result = await self.process_response("", ai_result, level + 1)                
            return ai_result.strip(" \t\n")
        else:
            return response

    @property
    def action_list(self) -> list[dict[str, Any]]:
        return self._lobby.action_list

    def clear_action_list(self) -> None:
        self._lobby.clear_action_list()
