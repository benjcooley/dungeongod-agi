from agent import Agent
from engine import Engine
from user import User
import copy
from datetime import datetime, timedelta
import json
import os
import os.path as path
import random
import yaml
import pydash
from utils import find_case_insensitive, any_to_int, parse_date_time, time_difference_mins, \
    escape_path_key, check_for_image, extract_arguments

class Lobby():
    
    def __init__(self, engine: Engine, user: User, agent: Agent) -> None:
        self.engine: Engine = engine
        self.user: User = user
        self.agent: Agent = agent
        self.action_image_path = None
        self.response_id = 0
        with open("data/lobby.yaml", 'r') as f:
            self.lobby = yaml.load(f, Loader=yaml.FullLoader)
        self.lobby_prefix = [Agent.make_message("user", self.lobby["lobby_prompt"], "prefix", keep=True)]
        self.messages = [ Agent.make_message("assistant", "I'm Ready!", "referee", True) ]
        self.start_the_game = False
        self.start_game_action = "new_game"        
        self.start_game_module_name = None
        self.start_game_party_name = None
        self.start_game_save_game_name = "latest"
        self.lobby_active = False

    async def generate(self, query: str, source: str, 
                       primary: bool = True, keep: bool = False, chunk_handler: any = None) -> str:
        instr_prompt = "<INSTRUCTIONS>" in query
        instr_prefix_prompt= "<PLAYER>" in query
        if instr_prompt or instr_prefix_prompt:
            # Strip any per-response instructions and any player tags for msg history. This reduces token usage
            # overall as AI only needs per-response instructions once.
            instr_msg = Agent.make_message("user", query, source, keep=keep)
            if instr_prefix_prompt:
                query_only = query.split("<PLAYER>")[1]
            else:
                query_only = query.split("<INSTRUCTIONS>")[0]
            query_only = query_only.strip(" \t\n")
            query_msg = Agent.make_message("user", query_only, source, keep=keep)
        else:
            instr_msg = query_msg = Agent.make_message("user", query, source, keep=keep)
        msgs = self.lobby_prefix + \
            self.messages + \
            [ instr_msg ]
        resp = await self.agent.generate(msgs, primary, keep, chunk_handler=chunk_handler)
        resp_msg = Agent.make_message("assistant", resp, "lobby", keep=False)
        self.messages.append(query_msg)
        self.messages.append(resp_msg)
        return resp_msg["content"]

    async def system_action(self, query: str, expected_action: str = None, retry_msg: str = None, chunk_handler: any = None) -> str:
        return await self.process_action("system", query, expected_action, retry_msg, chunk_handler=chunk_handler)

    async def player_action(self, query: str, chunk_handler: any = None) -> str:
        return await self.process_action("player", query, chunk_handler=chunk_handler)
            
    async def process_action(self, source: str, query: str, 
                             expected_action: str = None, retry_msg: str = None, 
                             chunk_handler: any = None) -> str:
        self.action_image_path = None
        is_system = (source == "system")
        if is_system:
            resp = await self.generate(query, source)
        else:
            resp = await self.generate(self.lobby["action_instr_prompt"] + "\n\n" + query, source)
        if expected_action is not None and expected_action not in resp:
            while expected_action not in resp:
                resp = await self.generate(retry_msg, "system", primary=True, keep=False)
        processed_resp = await self.process_response(query, resp, 1, chunk_handler=chunk_handler)
        if self.action_image_path is not None:
            processed_resp = processed_resp + "\n" + "@image: " + self.action_image_path
            self.action_image_path = None
        return processed_resp

    async def process_response(self, query: str, response: str, level: int, chunk_handler: any = None) -> str:
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
            if "next_turn(" in line:
                args = extract_arguments(line, 4)
                game_resp = await self.next_turn(args[0], args[1], args[2], args[3])
                results += game_resp + "\n"
        if results != "":
            response = "<RESPONSE>\n" + \
                        results
            ai_result = await self.generate(response, "lobby", primary=False, chunk_handler=chunk_handler)
            self.response_id += 1
            if "call next_turn(" in ai_result:
                ai_result = await self.process_response("", ai_result, level + 1)                
            return ai_result.strip(" \t\n")
        else:
            return response

    async def start_lobby(self) -> str:
        self.lobby_active = True        
        resume_prompt = self.lobby["resume_lobby_prompt"]
        return await self.system_action(resume_prompt, \
                           'call next_turn("resume")', \
                           'Lobby Agent, you must use [call next_turn("resume")] to start the game. Please try again.\n')

    # -------------------
    # Lobby Actions
    # -------------------

    async def resume(self) -> tuple[str, bool]:
        resp = self.lobby["start_lobby_prompt"]
        self.action_image_path = "data/images/lobby.jpg"
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

    async def list_chars(self, query_type: str, filter: str) -> tuple[str, bool]:
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
        full_name = char["info"]["basic"]["full_name"]
        self.action_image_path = check_for_image("data/characters/images", full_name)
        return (str(char) + "\n\n" + self.lobby["describe_stats_instructions"] + "\n", False)

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
        self.start_the_game = True
        self.start_game_action = "new_game"
        self.start_game_module_name = module_name
        self.start_game_party_name = party_name
        self.start_game_save_game_name = "latest"
        self.lobby_active = False
        return ("ok", False)

    async def resume_game(self) -> tuple[str, bool]:
        # Signal to outer controller we need to start a game
        self.start_the_game = True
        self.start_game_action = "resume_game"
        self.start_game_module_name = None
        self.start_game_party_name = None
        self.start_game_save_game_name = "latest"
        self.lobby_active = False
        return ("ok", False)

    async def load_game(self, save_game_name: str) -> tuple[str, bool]:
        # Signal to outer controller we need to start a game
        self.start_the_game = True
        self.start_game_action = "resume_game"
        self.start_game_module_name = None
        self.start_game_party_name = None
        self.start_game_save_game_name = save_game_name
        self.lobby_active = False
        return ("ok", False)

    async def next_turn(self, action: any, arg1: any = None, arg2: any = None, arg3: any = None) -> str:
        
        resp = ""
        error = False

        if self.agent.logging:
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
            if self.agent.logging:
                print(f"  ERROR: {resp}")
            return resp

        return resp
