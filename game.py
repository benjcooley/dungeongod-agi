from agent import Agent
from db import Db
from engine import Engine
from user import User
import asyncio
import copy
from datetime import datetime, timedelta
import json
import os.path as path
import random
import yaml
import re
import pydash
from drawing import draw_dialog_image
from utils import find_case_insensitive, any_to_int, parse_date_time, \
    time_difference_mins, escape_path_key, check_for_image, extract_arguments

def cur_value(obj: dict[str, any], path: str, value: str) -> any:
    return pydash.get(obj, path + ".cur_" + value) or pydash.get(obj, path + "." + value)

def make_arg_str(cmd: str, args: list[str]) -> str:
    arg_str = f"\"{cmd}\""
    for arg in args:
        if arg is None:
            break
        arg_str += ", "
        if isinstance(arg, str):
            arg_str += f"\"{arg}\""
        else:
            arg_str += str(arg)
    return arg_str

def strip_unique_id(name: str) -> str:
    if "#" in name:
        return name.split("#")[0]
    else:
        return name

class Game():

    def __init__(self, 
                 engine: Engine, 
                 user: User, 
                 agent: Agent,
                 start_game_action: str = "new_game",
                 module_name: str = "", 
                 party_name: str = "",
                 save_game_name: str = "") -> None:
        self.engine: Engine = engine
        self.db = engine.db
        self.user: User = user
        self.agent: Agent = agent
        self.start_game_action = start_game_action
        self.module_name = module_name
        self.party_name = party_name
        self.game_started = False
        self.cur_location: dict[str, any] = {}
        self.cur_location_state: dict[str, any] = {}
        self.cur_location_script: dict[str, any] | None = None
        self.cur_encounter: dict[str, any] | None = None
        self.cur_location_enter_time = datetime.now()
        self.object_map: dict[str, any] = {}
        self.save_game_name = save_game_name
        self.game_state: dict[str, any] = {}
        self.action_image_path: str | None = None
        self.button_tag: str | None = None
        self.response_id = 1
        self.skip_turn = False
        self.exit_to_lobby = False        
        self.messages = [ Agent.make_message("assistant", "I'm Ready!", "referee", True) ]
        # Random encounter vars
        self.random_encounter_last_time: datetime = datetime.now()
        self.random_encounter_rand_sel_val: float = random.random()
        self.random_encounter_rand_time_val: float = random.random()
        # Random event vars
        self.random_event_last_time: datetime = datetime.now()
        self.random_event_rand_sel_val: float = random.random()
        self.random_event_rand_time_val: float = random.random()

    @property
    def is_started(self) -> bool:
        return self.game_started

    async def start_game(self) -> str:
        if self.game_started:
            return "This game has already been started."
        self.game_started = True
        if self.start_game_action == "new_game":
            assert self.module_name is not None and self.save_game is not None
            await self.new_game()
        elif self.start_game_action == "resume_game":
            if not self.save_game_name:
                self.save_game_name = "latest"
            await self.load_game(self.save_game_name)
        else:
            assert False
        resume_prompt = self.agent.make_prompt(self.prompts["resume_game_prompt"], self.module["info"])
        return await self.system_action(resume_prompt, \
                           'call do_action("resume")', \
                           'AI Referee, you must use [call do_action("resume")] to start the game. Please try again.\n')

    # A per user response hint to focus the AI (not usually used unless AI has trouble)
    def add_query_hint(self, query) -> str:
        return query + "\n\n" + \
            self.prompts["instructions_prompt"] + "\n\n" + \
            self.cur_location_script["hint"] + "\n\n"

    def filter_messages(self, sources: list[str], max_msgs: int = -1) -> list[dict[str, any]]:
        filtered_msgs: list[str] = []
        for msg in self.messages:
            if msg["source"] in sources:
                filtered_msgs.append(msg)
        if max_msgs != -1:
            filtered_msgs = filtered_msgs[-max_msgs:]
        return filtered_msgs

    async def generate(self, instr_query: str, query: str, mode: str, source: str, 
                       primary: bool = True, keep: bool = False, chunk_handler: any = None) -> str:
        query_msg = Agent.make_message("user", query, source, keep=keep)
        if not instr_query:
            instr_query = query
        if instr_query != query:
            instr_msg = Agent.make_message("user", instr_query, source, keep=keep)
        else:
            instr_msg = query_msg
        match mode:
            case "exploration_action" | "encounter_action":
                prefix = (self.exploration_prefix if mode == "exploration_action" else self.encounter_prefix)
                # We get the whole msg stack for the "actioner" query (actioner and engine responses).
                msgs = prefix + \
                    self.filter_messages(["player", "actioner", "engine", "referee"]) + \
                    [ instr_msg ]
                resp = await self.agent.generate(msgs, primary, keep, chunk_handler=chunk_handler)
                resp_msg = Agent.make_message("assistant", resp, "actioner", keep=False)
            case "engine_response" | "referee_response":
                # For the user friendly "referee" response we only need player/referee msgs.
                msgs = self.response_prefix + \
                    self.filter_messages(["player", "referee"]) + \
                    [ instr_msg ]
                # Don't use GPT-4 for big responses
                if instr_msg["tokens"] > 200:
                    primary = True
                resp = await self.agent.generate(msgs, primary, keep, chunk_handler=chunk_handler)
                resp_msg = Agent.make_message("assistant", resp, "referee", keep=False)
            case "dialog_choices":
                prefix = self.response_prefix
                # For dialog choices we only need player/referee messages.
                msgs = prefix + \
                    self.filter_messages(["player", "referee"]) + \
                    [ instr_msg ]
                resp = await self.agent.generate(msgs, primary, keep, chunk_handler=chunk_handler)
                resp_msg = Agent.make_message("assistant", resp, "dialogee", keep=False)
        self.messages.append(query_msg)
        self.messages.append(resp_msg)
        return resp_msg["content"]

    # Hand parse some simple actions so they don't round trip to the AI.
    async def parse_simple_action(self, query: str) -> str:
        query = query.strip(".?! \n")
        if query.startswith("Go to ") or query.startswith("go to "):
            query = "go " + query[6:]
        query = query.replace(" go to ", " go ")
        query = query.replace(" the ", " ")
        query = query.replace(" our ", " ")
        query = query.replace(" to ", ", ")
        query = query.replace(" on ", ", ")
        query = query.replace(" with ", ", ")
        query = query.replace(" picks up ", " pickup ")
        lowq = query.lower()
        for prefix in ["we will ", "we'll ", "we're going to ", "we ", "what's in my ", 
                       "what are my ", "show me ", "show ", "give me ", "what are ", "what's "]:
            pl = len(prefix)
            if lowq.startswith(prefix):
                lowq = lowq[pl:]
                query = query[pl:]
        query = query.replace("'s ", " ")
        query = query.replace("'es ", " ")
        lowq_l = lowq.split(" ")
        q_l = query.split(" ")
        l = len(lowq_l)
        if l == 0:
            return ""
        if self.is_character_name(q_l[0]) and l > 1:
            q_l[0], q_l[1] = q_l[1], q_l[0]
            lowq_l[0], lowq_l[1] = lowq_l[1], lowq_l[0]
        cmd = None
        args = []
        match lowq_l[0]:
            case "exit" | "lobby" | "quit":
               if l == 1:
                    cmd = "lobby"
                    args = []
            case "go" | "goto":
               if l >= 2:
                    cmd = "go"
                    args = [ " ".join(q_l[1:]) ]
            case "pickup":
                if l > 2:
                    cmd = "pickup"
                    args = q_l[1:]
            case "search":
                if l == 1:
                    cmd = "search"
                    args = []
                else:
                    cmd = "search"
                    args = [ " ".join(q_l[1:]) ]
            case "drops" | "drop":
                if l > 2:
                    cmd = "drop"
                    args = q_l[1:]
            case "stats" | "abilities" | "attributes" | "skills":
                if l == 1:
                    cmd = "stats"
                    args = []
                else:
                    cmd = "stats"
                    args = [ " ".join(q_l[1:]) ]
            case "invent" | "inventory":
                if l >= 2:
                    cmd = "invent"
                    args = [ " ".join(q_l[1:]) ]
            case "look":
                if l > 1:
                    cmd = "look"
                    args = [ " ".join(q_l[1:]) ]
                else:
                    cmd = "look"
                    args = []
            case "help":
                if l > 1:
                    cmd = "help"
                    args = [ " ".join(q_l[1:]) ]
            case "cast" | "casts":
                if l >= 3:
                    l2 = " ".join(q_l[2:]).split(", ")
                    if l2 == 2:
                        cmd = "cast"
                        args = [ q_l[1], q_l[2], l2[1] ]
            case "use" | "uses":
                if l >= 3:
                    l2 = " ".join(q_l[2:]).split(", ")
                    if l2 == 2:
                        cmd = "use"
                        args = [ q_l[1], l2[0], l2[1] ]
            case "attack" | "attacks":
                if l > 2:
                    cmd = "attack"
                    args = [ q_l[1], " ".join(q_l[2:]) ]
            case "shoot" | "shoots":
                if l > 2:
                    cmd = "shoot"
                    args = [ q_l[1], " ".join(q_l[2:]) ]
            case _:
                pass
        if cmd is None:
            return ""
        resp = await self.call_action(cmd, args)
        return resp
 
    async def system_action(self, query: str, 
                            expected_action: str = None, retry_msg: str = None, 
                            chunk_handler: any = None) -> str:
        return await self.process_action("system", query, 
                                         expected_action, retry_msg, 
                                         chunk_handler=chunk_handler)

    async def player_action(self, query: str, chunk_handler: any = None) -> str:
        return await self.process_action("player", query, chunk_handler=chunk_handler)

    async def call_action(self, action: str, args: list[any]) -> str:
        arg_str = make_arg_str(action, args)
        response = f"<HIDDEN>\ncall do_action({arg_str})\n"
        cmd_msg = Agent.make_message("assistant", response, "actioner", keep=False)
        self.messages.append(cmd_msg)
        if self.agent.logging:
            print(cmd_msg["content"])
        resp = await self.process_response(arg_str.replace(",", ""), response, level=1)
        return self.post_action_update(resp)

    async def process_action(self, source: str, query: str, 
                             expected_action: str = None, retry_msg: str = None, 
                             chunk_handler: any = None) -> str:
        self.action_image_path = None
        is_system = (source == "system")
        resp = ""
        # Try the simple parser (local and way faster for simple responses)
        if not is_system:
            resp = await self.parse_simple_action(query)
        # Send to the AI
        if not resp:
            if is_system:
                action_instr = query
            else:
                if self.cur_location_script and "hint" in self.cur_location_script:
                    query = self.add_query_hint(query)
                action_instr = "<PLAYER>\n" + query + "\n\n" + self.prompts["action_instr_prompt"] + "\n"
            action_mode = self.cur_game_state_name + "_action"
            resp = await self.generate(action_instr, query, action_mode, is_system , primary=True, keep=False)
            if not is_system:
                trans_action = self.evaluate_transitions()
                if trans_action is not None:
                    resp = resp + trans_action
            if expected_action is not None and expected_action not in resp:
                while expected_action not in resp:
                    resp = await self.generate("", retry_msg, action_mode, "system", primary=True, keep=False)
            processed_resp = await self.process_response(query, resp, level=1, chunk_handler=chunk_handler)
            resp = self.post_action_update(processed_resp, is_system=is_system)
        return resp

    def post_action_update(self, resp: str, is_system: bool = False) -> str:
        # Process the final response
        if not is_system and not self.skip_turn:
            self.inc_cur_time(self.turn_period)
        if self.action_image_path is not None:
            resp = resp + "\n" + "@image: " + self.action_image_path
            self.action_image_path = None
        # Turn off turn skip flag
        self.skip_turn = False
        # Check for any buttons to show
        self.button_tag = self.check_for_buttons()
        return resp

    async def process_response(self, query: str, response: str, level: int, chunk_handler: any = None) -> str:
        if level == 4:
            return response.strip(" \n\t")
        query, results, num_calls = await self.process_game_actions(query, response)
        # Do we need to pass this to the AI for further processing? If not just return it.
        if num_calls == 1 and results.startswith("<RESULTS>\n"):
            results = results[10:]
        # Check for additional responses.
        if results != "" and not self.skip_turn:
            addl_resp = self.get_addl_response()
            if addl_resp:
                addl_resp_instr = results.strip("\n") + "\n\n" + addl_resp
                resp_list = addl_resp_instr.split("<INSTRUCTIONS>\n")
                has_instr = len(resp_list) > 1
                mode = (self.cur_game_state_name + "_action" if has_instr else "engine_response")
                addl_query = resp_list[0]
                ai_addl_resp = await self.generate(addl_resp_instr, addl_query, mode, "engine", primary=False)
                _, addl_results, _ = await self.process_game_actions("", ai_addl_resp)
                results = results.strip("\n") + "\n\n" + addl_results
        # Pass it to the AI.
        return await self.referee_response(query, results, level, chunk_handler=chunk_handler)

    async def process_game_actions(self, query: str, response: str) -> tuple[str, str, int]:
        lines = response.split("\n")
        prefix = self.get_response_prefix()
        results = ""
        num_calls = 0
        for line in lines:
            line = line.strip()
            if (line == "NO ACTION" or "do_action(\"talk\"," in line) and query != "":
                if line == "NO ACTION":
                    self.skip_turn = True
                results = ""
                break
            elif line == "NOT ALLOWED":
                # Action AI says this is an invalid player action. Ignore it.
                self.skip_turn = True
                results = self.prompts["not_allowed_response"]
                break
            if "do_action(\"" in line:
                args = extract_arguments(line, 5)
                game_resp, _ = await self.do_action(args[0], args[1], args[2], args[3], args[4])
                results += game_resp + "\n"
                num_calls += 1
        if not self.skip_turn or num_calls > 1:
            results = prefix + results + self.after_process_actions()
        return (query, results, num_calls)

    async def referee_response(self, query: str, results: str, level: int, chunk_handler: any = None) -> str: 
        if results.startswith("<RESULTS>\n"):
            return results[10:]
        if results != "":
            instr_query = "<RESPONSE>\n" + \
                        (self.get_current_game_state_str() + "\n\n" if not self.skip_turn else "") + \
                        self.get_response_start() + \
                        results + \
                        self.get_response_end()
            resp_list = instr_query.split("<INSTRUCTIONS>\n")
            # If there are any instructions we use the action prefix for the game state so we get the state
            # specific rules. Otherwise we just let the AI create a narrative response (no rules needed).
            has_instr = len(resp_list) > 1
            query = resp_list[0] # Instructions will be at the end
            ai_result = await self.generate(instr_query, query, "engine_response", "engine", primary=False, keep=False,
                                           chunk_handler=chunk_handler)
            self.response_id += 1
            if "call do_action(" in ai_result:
                ai_result = await self.process_response("", ai_result, level + 1)                
        else:
            # Action AI says this is a user query, so pass query through, referee handles it.
            player_query = "<PLAYER>\n" + query
            plqyer_query_instr = player_query + "\n\n" + self.prompts["respond_to_message_prompt"]
            ai_result = await self.generate(plqyer_query_instr, player_query, "referee_response", "referee", primary=False, keep=False,
                                           chunk_handler=chunk_handler)
        ai_result.strip(" \t\n")
        return ai_result

    def init_help_index(self) -> None:
        for help in self.rules["help"].values():
            text_help = { "type": "text", "help": help["help"] }
            for keyword in help["keywords"]:
                self.help_index[keyword] = text_help
        all_spells = { "name": "all", "type": "spells" }
        self.help_index["spells"] = all_spells
        self.help_index["all spells"] = all_spells
        for spell_name in self.rules["spells"].keys():
            self.help_index[spell_name.lower()] = { "name": spell_name, "type": "spell" }
        magic_categories = { "name": "magic categories", "type": "magic_categories" }
        self.help_index["magic categories"] = magic_categories
        self.help_index["magic types"] = magic_categories
        self.help_index["magic"] = magic_categories
        for magic_category_name in self.rules["magic_categories"].keys():
            category_lower = (magic_category_name + " Magic").lower()
            self.help_index[category_lower] = { "name": magic_category_name, "type": "magic_categories" }
        for equipment_name in self.rules["equipment"].keys():
            self.help_index[equipment_name.lower()] = { "name": equipment_name, "type": "equipment" }

    async def init_game(self) -> None:
        self.init_session_state()
        self.init_object_map()
        if self.cur_location_name is not None:
            self.cur_location = self.module["locations"][self.cur_location_name]
            if self.cur_script_state:
                self.cur_location_script = self.cur_location["script"][self.cur_script_state]
            self.cur_location_state = self.game_state["location_states"][self.cur_location_name]
        else:
            self.set_location(self.module["starting_location_name"])
        if self.cur_game_state_name == "encounter":
            encounter = self.get_cur_location_encounter()
            if encounter is not None:
                self.cur_encounter = encounter
            else:
                self.cur_game_state_name = "exploration"

    async def load_module(self) -> None:
        _, err, self.module = await self.engine.load_module(self.module_name)
        assert not err
        self.rules = await self.engine.load_rules(self.rules_path)
        self.prompts = await self.engine.load_prompts(self.rules_path)
        # Prefix messages we prepend to our message stream which has the rules/instructions for various modes
        self.exploration_prefix = [Agent.make_message("user", self.prompts["exploration_prompt"], "prefix", keep=True)]
        self.encounter_prefix = [Agent.make_message("user", self.prompts["encounter_prompt"], "prefix", keep=True)]
        self.response_prefix = [Agent.make_message("user", self.prompts["response_prompt"], "prefix", keep=True)]
        self.help_index = {}
        self.init_help_index()

    async def new_game(self) -> None:
        await self.load_module()
        _, err, party = await self.engine.load_party(self.user, self.party_name)
        assert not err
        game_path = 'data/new_game_state.yaml'
        with open(game_path, 'r') as f:
            self.game_state = yaml.load(f, Loader=yaml.FullLoader)
            self.game_state["characters"] = copy.deepcopy(party["characters"])
            self.game_state["npcs"] = copy.deepcopy(self.module["npcs"])
            self.game_state["monsters"] = copy.deepcopy(self.module["monsters"])
            self.game_state["info"]["party_name"] = self.party_name
            self.game_state["info"]["module_name"] = self.module_name
            self.game_state["state"]["last_effect_uid"] = 1000
            self.game_state["state"]["last_object_uid"] = 1000
            self.cur_game_state_name = self.module["starting_game_state"]
            self.cur_location_name = None
            self.cur_time = self.module["starting_time"]
        for loc_name, loc in self.module["locations"].items():
            self.location_states[loc_name] = copy.deepcopy(loc.get("state", {}))
        await self.init_game()
        await self.save_game(wait_done=True)

    async def load_game(self, save_name: str = "latest") -> None:
        save_key = f'{self.user.user_path}/save_games/{save_name}'
        game_state = await self.db.get(save_key)
        if game_state:
            self.game_state = game_state
            self.module_name = self.game_state["info"]["module_name"]
            self.party_name = self.game_state["info"]["party_name"]
            await self.load_module()
            await self.init_game()
        else:
            await self.new_game()

    async def save_game(self, save_name: str = "latest", wait_done: bool = False) -> None:
        save_key = f'{self.user.user_path}/save_games/{save_name}'
        if wait_done:
            await self.db.put(save_key, self.game_state)
        else:
            # If we're going to do this async, make a copy of the state first
            asyncio.create_task(self.db.put(save_key, copy.deepcopy(self.game_state)))

    def init_session_state(self) -> None:
        # Temporary session states (disappear when session is over)
        self.session_state = {
            "characters": {},
            "npcs": {},
            "monsters": {},
            "locations": {},
            "areas": {}
        }

    def set_agent(self, agent: Agent) -> None:
        self.agent = agent

    @property
    def characters(self) -> dict[str, any]:
        return self.game_state["characters"]

    @property
    def npcs(self) -> dict[str, any]:
        return self.game_state["npcs"]

    @property
    def monsters(self) -> dict[str, any]:
        return self.game_state["monsters"]

    @property
    def monster_types(self) -> dict[str, any]:
        return self.rules["monster_types"]

    @property
    def module_monster_types(self) -> dict[str, any]:
        return self.module["monster_types"]

    def describe_party_basic(self) -> str:
        desc = ""
        for _char_name, char in self.characters.items():
            desc += json.dumps(char["info"]["basic"]) + " " + json.dumps(char["stats"]["basic"]) + "\n"
        return desc
    
    def describe_party_all(self) -> str:
        desc = ""
        for _char_name, char in self.characters.items():
            desc += json.dumps(char) + "\n"
        return desc    

    @property
    def cur_location_name(self) -> str:
        return self.game_state["state"]["cur_location_name"]
    
    @cur_location_name.setter
    def cur_location_name(self, value: str) -> None:
        self.game_state["state"]["cur_location_name"] = value

    @property
    def prev_location_name(self) -> str:
        return self.game_state["state"].get("prev_location_name", "")
    
    @prev_location_name.setter
    def prev_location_name(self, value: str) -> None:
        self.game_state["state"]["prev_location_name"] = value

    @property
    def cur_script_state(self) -> str:
        return self.cur_location_state.get("cur_script_state", "")
    
    @cur_script_state.setter
    def cur_script_state(self, value: str) -> None:
        self.cur_location_state["cur_script_state"] = value

    @property
    def module_path(self) -> str:
        return f"data/modules/{self.module_name}"

    @property
    def rules_path(self) -> str:
        return f"data/rules/{self.module['info']['game']}/{self.module['info']['game_version']}"

    @property
    # Premade parties (not user parties)
    def parties_path(self) -> str:
        return f"data/parties/{self.party_name}"

    @property
    def locations(self) -> dict[str, dict[str, any]]:
        return self.module["locations"]

    @property
    def cur_area_name(self) -> str:
        if "areas" not in self.module:
            return ""
        return self.cur_location.get("area", "")

    @property
    def cur_area(self) -> dict[str, dict[str, any]]:
        area_name = self.cur_area_name
        return self.module["areas"].get(area_name, {})

    @property
    def location_states(self) -> dict[str, dict[str, any]]:
        return self.game_state["location_states"]

    @property
    def game_over(self) -> bool:
        return self.game_state.get("game_over", False)
    
    @game_over.setter
    def game_over(self, value: bool) -> None:
        self.game_state["state"]["game_over"] = value 
        if value:
            self.return_to_lobby = True

    @property
    def cur_game_state_name(self) -> str:
        return self.game_state["state"]["cur_game_state"]
    
    @cur_game_state_name.setter
    def cur_game_state_name(self, value: str) -> None:
        self.game_state["state"]["cur_game_state"] = value

    @property
    def cur_time(self) -> str:
        return self.game_state["state"]["cur_time"]
    
    @cur_time.setter
    def cur_time(self, value: str) -> None:
        self.game_state["state"]["cur_time"] = value

    @property
    def cur_time_dt(self) -> datetime.time:
        return parse_date_time(self.cur_time)
    
    @cur_time_dt.setter
    def cur_time_dt(self, value: datetime.time) -> None:
        self.cur_time = value.strftime("%b %-d %Y %-H:%M")

    def inc_cur_time(self, mins: int) -> None:
        self.cur_time_dt = self.cur_time_dt + timedelta(minutes=mins)

    @property
    def cur_time_12hr(self) -> str:
        return self.cur_time_dt.strftime("%-I:%M:%p")

    @property
    def cur_date_time_12hr(self) -> str:
        return self.cur_time_dt.strftime("%b %-d %Y %-I:%M:%p")     

    @property
    def turn_period(self) -> int:
        return self.rules["turn_period"]

    @property
    def cur_time(self) -> str:
        return self.game_state["state"]["cur_time"]
    
    @cur_time.setter
    def cur_time(self, value: str) -> None:
        self.game_state["state"]["cur_time"] = value

    @property
    def location_since(self) -> str:
        return self.game_state["state"]["location_since"]
    
    @location_since.setter
    def location_since(self, value: str) -> None:
        self.game_state["state"]["location_since"] = value        

    @property
    def location_elapsed_mins(self) -> str:
        return time_difference_mins(self.cur_time, self.location_since)

    @property
    def script_state_since(self) -> str:
        return self.game_state["state"]["script_state_since"]
    
    @script_state_since.setter
    def script_state_since(self, value: str) -> None:
        self.game_state["state"]["script_state_since"] = value 

    @property
    def script_state_elapsed_mins(self) -> str:
        return time_difference_mins(self.cur_time, self.script_state_since)

    def get_state_value(self, target: dict[str, any], path: str) -> any:
        match path:
            case "stats.basic.cur_health":
                return self.get_cur_health(target)
            case _:
                return pydash.get(target, path)

    def set_state_value(self, target: dict[str, any], path: str, value: any) -> None:
        match path:
            case "stats.basic.cur_health":
                self.set_cur_health(target, value)
            case _:
                pydash.set_(target, path, value)

    def is_character(self, char_name: str) -> bool:
        return char_name in self.characters
    
    def describe_equipment(self, equipment_name: str) -> tuple[str, any]:
        equipment_type = self.rules["equipment"].get(equipment_name)
        if equipment_type is None:
            return (f"no equipment type {equipment_name}", True)
        image_path = check_for_image(self.rules_path + "/images", equipment_name, "equipment")
        if image_path:
            self.action_image_path = image_path
        return ("EQUIPMENT TYPE:\n" + json.dumps(equipment_type) + "\n", False)

    def find_item(self, char_name_or_any: str, maybe_item_name: str) -> tuple[dict, dict]:
        if char_name_or_any == "any":
            for char_name in self.game_state["characters"].keys():
                found_char, found_item = self.find_item(char_name, maybe_item_name)
                if found_item:
                    return (found_char, found_item)
        else:
            char_name = char_name_or_any
            char = self.game_state["characters"].get(char_name)
            if char is None:
                return (None, None)
            item = char["items"].get(maybe_item_name)
            if item is not None:
                return (char, item)
        return (None, None)
    
    def has_item(self, char_name_or_any: str, maybe_item_name: str) -> bool:
        char, _ = self.find_item(char_name_or_any, maybe_item_name)
        return char is not None
    
    def add_item(self, parent: dict[str, any], item: dict[str, any]) -> tuple[str, bool]:
        parent_type = parent["type"]
        parent_unique_name = parent["unique_name"]
        assert parent_type in [ "monster", "character", "npc", "location_state", "item" ]
        prev_parent_unique_name = item.get("parent")
        if prev_parent_unique_name == parent_unique_name:
            # already here
            return ("ok", False)
        if prev_parent_unique_name is not None:
            prev_parent = self.get_object(prev_parent_unique_name)
            assert prev_parent is not None
            # Note, if this is a qty item like arrows, a "new" item might be returned with the given qty.
            (item, _, _) = self.remove_item(prev_parent, item)
        # Check moving qty for items which use qty (gold, arrows, etc.)
        if "qty" in item:
            # Look for item with same name (not it's unique name)
            _, target_item = find_case_insensitive(parent["items"], item["name"])
            if target_item is not None:
                # Just add qty to existing item
                target_item["qty"] = target_item.get("qty", 1) + item["qty"]
                return ("ok", False)
        item_unique_name = self.get_or_add_unique_name(item["name"], item)
        parent["items"][item_unique_name] = item
        item["parent"] = parent_unique_name
        self.add_to_object_map(item)
        return ("ok", False)

    def remove_item(self, parent: dict[str, any], item: dict[str, any] | str, qty: int | None = None) -> tuple[dict, str, bool]:
        # TODO: Fix general add/remove
        assert parent["type"] in [ "monster", "character", "npc", "location_state", "item" ]
        if isinstance(item, str):
            item_name: str = item
            _, item = find_case_insensitive(parent["items"], item)
            if item is None:
                return (None, f"'{item_name}' not found", True)
        else:
            item_name = item["name"]
        item_qty = item.get("qty", 1)
        if qty is None:
            qty = item_qty
        if qty > item_qty:
            parent_name = parent["name"]
            return (None, f"'{parent_name}' has only {item_qty} of '{item_name}'", True)
        if qty == item_qty:
            # Delete the existing item and return the whole item
            self.remove_from_object_map(item)
            item_unique_name = item["unique_name"]
            del parent["items"][item_unique_name]
            del item["parent"]
            return (item, "ok", False)
        else:
            # Remove 'qty' of items for existing item, return a new item
            item["qty"] = item_qty - qty
            ret_item = copy.deepcopy(item)
            del ret_item["parent"]
            del ret_item["unique_name"]
            ret_item["qty"] = qty
            return (ret_item, "ok", False)

    def move_all_items(self, from_obj: dict[str, any], to_obj: dict[str, any]) -> None:
        for _, item in from_obj["items"].items():
            self.remove_item(from_obj, item)
            self.add_item(to_obj)

    @staticmethod
    def get_item_list_desc(items: dict[str, any]) -> list[str]:
        item_descs = []
        for item_name, item in items.items():
            if "name" in item:
                item_name = item["name"]
            if "qty" in item and item["qty"] > 1:
                item_descs.append(f"{item['qty']} {item_name}")
            else:
                item_descs.append(item_name)       
        return item_descs 

    def get_usable_items(self, char: dict[str, any]) -> list[dict[str, any]]:
        usable_items = []
        for item in char["items"].values():
            merged_item = self.get_merged_item(item)
            if "usable" in merged_item:
                usable_items.append(merged_item)
        return usable_items

    def set_location(self, new_loc_name: str) -> None:
        if self.cur_location_name == new_loc_name:
            return
        # end any encounter at previous location
        if self.cur_game_state_name == "encounter":
            self.end_encounter()
        # get the new location (we assume it exists!)
        new_loc = self.module["locations"].get(new_loc_name)
        new_loc_state = self.game_state["location_states"].get(new_loc_name)
        if new_loc_state is None:
            new_loc_state = copy.deepcopy(new_loc.get("state", {}))
            if "items" not in new_loc_state:
                new_loc_state["items"] = {}
            self.game_state["location_states"][new_loc_name] = new_loc_state
        self.prev_location_name = self.cur_location_name
        self.prev_area_name = self.cur_area_name
        self.cur_location_name = new_loc_name
        self.cur_location = new_loc
        self.cur_location_state = new_loc_state
        self.location_since = self.cur_time
        self.time_entered_location = datetime.now()
        if self.cur_script_state != "" and "script" in self.cur_location:
            self.cur_location_script = self.cur_location["script"][self.cur_script_state]
        else:
            self.cur_location_script = None
        self.script_state_since = self.cur_time
        # Reset timers
        if self.cur_area_name != self.prev_area_name:
            self.random_encounter_last_time = datetime.now()
            self.random_event_last_time = datetime.now()
        # Check for encounter at the new location
        if self.get_cur_location_encounter() is not None:
            self.start_encounter()

    def get_current_game_state_str(self) -> str:
        return f"state: {self.cur_game_state_name}, location: {self.cur_location_name}, time: {self.cur_date_time_12hr}" 

    @staticmethod
    def merge_topics(target: dict[str,str], from_topics: dict[str, str]) -> None:
         for npc_name, npc_topics in from_topics.items():
            target[npc_name] = target.get(npc_name, {})
            target[npc_name].update(npc_topics)

    def get_merged_topics(self) -> dict[str, str]:
        all_topics = {}
        npcs = self.cur_location.get("npcs", [])
        npc_topics = {}
        for npc_name in npcs:
            npc = self.module["npcs"][npc_name]
            if "topics" in npc:
                npc_topics[npc_name] = copy.deepcopy(npc["topics"])
        Game.merge_topics(all_topics, npc_topics)
        Game.merge_topics(all_topics, copy.deepcopy(self.cur_location.get("topics", {})))
        if self.cur_location_script:
            Game.merge_topics(all_topics, copy.deepcopy(self.cur_location_script.get("topics", {})))
        return all_topics

    def describe_topics(self) -> str:
        all_topics = self.get_merged_topics()
        if len(all_topics) == 0:
            return ""
        topic_npc_list = []
        for npc_name, topics in all_topics.items():
            topics_str = json.dumps(list(topics.keys()))
            topic_npc_list.append(f" {npc_name}: {topics_str}")
        return ", ".join(topic_npc_list)

    def describe_tasks(self) -> str:
        task_desc = ""
        tasks = self.cur_location.get("tasks", {})
        if self.cur_location_script is not None:
            tasks.update(self.cur_location_script.get("tasks", {}))
        for task_name, task in tasks.items():
            if task_name not in self.game_state["tasks_completed"] or \
                    self.game_state["tasks_completed"][task_name] == False:
                task_desc += task["description"] + "\n"
        return task_desc

    def exit_blocked(self, loc_name: str, exit_name: str) -> bool:
        blocked = self.game_state["exits_blocked"].get(f"{loc_name}/{exit_name}", None)
        if blocked is not None:
            return blocked
        return True # For now just assume an exit not explicitly blocked is unblocked

    def describe_exits(self) -> str:
        exits = self.get_merged_exits()
        exit_names = []
        for exit_name, _ in exits.items():
            if not self.exit_blocked(self.cur_location_name, exit_name):
                exit_names.append(f'"{exit_name}"')
        return ", ".join(exit_names)

    def get_random_character(self) -> dict:
        chars = []
        for char in self.characters.values():
            _, can_do = Game.can_do_actions(char)
            if can_do:
                chars.append(char)
        if len(chars) == 0:
            return None
        rand_idx = random.randint(0, len(chars) - 1)
        return chars[rand_idx]

    def get_monster_type(self, monster_type_name: str) -> dict[str, any]:
        return self.module_monster_types.get(monster_type_name) or \
            self.monster_types.get(monster_type_name)

    def evaluate_transitions(self) -> str:
        if self.cur_location_script is None:
            return ""
        if "transitions" not in self.cur_location_script:
            return ""
        next_state = None
        for trans_name, trans in self.cur_location_script["transitions"].items():
            if "condition" in trans:
                cond = trans["condition"]
                if "elapsed_time" in cond:
                    if self.script_state_elapsed_mins >= cond["elapsed_time"]:
                        next_state = trans_name
                        break
        if next_state is not None:
            return f'<HIDDEN>\ncall do_action("next", "{next_state}")\n'
        else:
            return None

    @staticmethod
    def make_image_tag(image: str) -> str:
        if image == "":
            return ""
        return "@image: " + image + "\n"

    def location_image_path(self, if_first_time: bool = False) -> str:
        image_path = None
        if self.cur_location_script is not None and "image" in self.cur_location_script:
            image_path = check_for_image(self.module_path, self.cur_location_script["image"])
        elif "image" in self.cur_location:
            session_loc = self.session_state["locations"][self.cur_location_name] = self.session_state["locations"].get(self.cur_location_name, {})
            if if_first_time and session_loc.get("players_have_seen", False):
                return None
            session_loc["players_have_seen"] = True
            image_path = check_for_image(self.module_path, self.cur_location.get("image", ""))
        return image_path

    def other_image(self, name, type_name) -> str:
        image_path = check_for_image(self.module_path + "/images", name, type_name)
        if image_path is not None:
            return image_path
        image_path = check_for_image(self.rules_path + "/images", name, type_name)
        if image_path is not None:
            return image_path
        image_path = check_for_image(self.parties_path + "/images", name, type_name)
        return image_path

    @staticmethod
    def die_roll(dice: str, advantage_disadvantage = None) -> int:
        if advantage_disadvantage:
            if advantage_disadvantage == "advantage":
                return max(Game.die_roll(dice), Game.die_roll(dice))
            elif advantage_disadvantage == "disadvantage":
                return min(Game.die_roll(dice), Game.die_roll(dice))
            else:
                raise RuntimeError("Invalid advantage/disadvantage id")
        if dice is None or dice == "":
            return 0
        match dice:
            case "d4":
                return random.randint(1, 4)
            case "d6":
                return random.randint(1, 6)
            case "d8":
                return random.randint(1, 8)
            case "d12":
                return random.randint(1, 12)
            case "d20":
                return random.randint(1, 20)
        return 0

    def is_character_name(self, maybe_char_name: str) -> bool:
        return maybe_char_name in self.game_state["characters"]

    def is_encounter_monster_name(self, maybe_monster_name: str) -> bool:
        return self.cur_encounter is not None and \
            maybe_monster_name in self.cur_encounter["monsters"]
    
    def get_encounter_monster_or_npc(self, maybe_name: str) -> dict[str, any] | None:
        if self.cur_encounter is not None and \
                maybe_name in self.cur_encounter["monsters"]:
            unique_name = self.cur_encounter["monsters"][maybe_name]
            return self.get_object(unique_name)  
        return None

    def is_nearby_npc_name(self, maybe_name: str) -> bool:
        if self.cur_location_script is not None:
            npcs = self.cur_location_script.get("npcs", [])
            return maybe_name in npcs
        npcs = self.cur_location.get("npcs", []) 
        return maybe_name in npcs

    def get_nearby_npc(self, maybe_name: str) -> dict[str, any]:
        if self.cur_location_script is not None:
            npcs = self.cur_location_script.get("npcs", [])
            if maybe_name in npcs:
                return self.get_object(maybe_name)
        npcs = self.cur_location.get("npcs", []) 
        if maybe_name in npcs:
            return self.get_object(maybe_name)
        return None

    def is_nearby_being_name(self, maybe_being_name: str) -> bool:
        return self.is_character_name(maybe_being_name) or \
            self.is_encounter_monster_name(maybe_being_name) or \
            self.is_nearby_npc_name(maybe_being_name)
    
    def get_nearby_being(self, maybe_name: str) -> dict[str, any] | None:
        if maybe_name in self.game_state["characters"]:
            return self.game_state["characters"][maybe_name]
        monster = self.get_encounter_monster_or_npc(maybe_name)
        if monster is not None:
            return monster
        return self.get_nearby_npc(maybe_name)

    @staticmethod
    def is_character(maybe_char: dict[str, any]) -> bool:
        return maybe_char["type"] == "character"

    @staticmethod
    def is_monster(maybe_monster: dict[str, any]) -> bool:
        return maybe_monster["type"] == "monster"

    @staticmethod
    def is_npc(maybe_npc: dict[str, any]) -> bool:
        return maybe_npc["type"] == "npc"

    @staticmethod
    def is_item(maybe_item: dict[str, any]) -> bool:
        return maybe_item["type"] == "item"

    @staticmethod
    def is_location_state(maybe_loc_state: dict[str, any]) -> bool:
        return maybe_loc_state["type"] == "location_state"

    @staticmethod
    def get_skill_ability_modifier(being: dict[str, any], skill_ability: str) -> tuple[str, str, str]:
        if skill_ability in being.get("stats", {}).get("skills", {}):
            advantage_disadvantage = \
                ("disadvantage" if being.get("disadvantage", {}).get("skills", {}).get(skill_ability) else None) or \
                ("advantage" if being.get("advantage", {}).get("skills", {}).get(skill_ability) else None)
            return ( "skills", pydash.get(being, "stats.skills." + skill_ability, ""), advantage_disadvantage or "" )
        if skill_ability in being.get("stats", {}).get("abilities", {}):
            advantage_disadvantage = \
                ("disadvantage" if being.get("disadvantage", {}).get("abilities", {}).get(skill_ability) else None) or \
                ("advantage" if being.get("advantage", {}).get("abilities", {}).get(skill_ability) else None)
            return ( "abilities", pydash.get(being, "stats.abilities." + skill_ability, ""), advantage_disadvantage or "" )
        return ( "", "", "" )
    
    @staticmethod
    def skill_ability_check(being: dict[str, any], skill_ability: str, against: int) -> tuple[str, bool]:
        _, mod_die, adv_dis = Game.get_skill_ability_modifier(being, skill_ability)
        if mod_die is None:
            return (f"no skill or ability {skill_ability}", False)
        d20_roll = Game.die_roll("d20", adv_dis)
        mod_roll = Game.die_roll(mod_die)
        success = d20_roll + mod_roll >= against
        resp = f"Rolled {skill_ability} check d20 {d20_roll} {adv_dis} + {mod_die} {mod_roll} = {d20_roll + mod_roll} vs {against} - "
        if success:
            resp += "SUCCEEDED!"
        else:
            resp += "FAILED!"
        return (resp, success)

    @staticmethod
    def skill_ability_check_against(being: dict[str, any], skill_ability1: str, target: dict[str, any], skill_ability2: str) -> tuple[str, bool]:
        _, mod_die1, adv_dis1 = Game.get_skill_ability_modifier(being, skill_ability2)
        if mod_die1 is None:
            return (f"no skill or ability {skill_ability1}", False)
        d20_roll1 = Game.die_roll("d20", adv_dis1)
        mod_roll1 = Game.die_roll(mod_die1)
        _, mod_die2, adv_dis2 = Game.get_skill_ability_modifier(target, skill_ability2)
        if mod_die2 is None:
            return (f"no skill or ability {skill_ability2}", False)
        d20_roll2 = Game.die_roll("d20", adv_dis2)
        mod_roll2 = Game.die_roll(mod_die2)
        success = d20_roll1 + mod_roll1 >= d20_roll2 + mod_roll2
        being_name = Game.get_encounter_or_normal_name(being)
        target_name = Game.get_encounter_or_normal_name(target)
        resp = f"{being_name} {skill_ability1} {adv_dis1} rolled {d20_roll1 + mod_roll1}" + \
             f" vs {target_name} {skill_ability2} {adv_dis2} rolled {d20_roll2 + mod_roll2} "
        if success:
            resp += "SUCCEEDED!"
        else:
            resp += "FAILED!"
        return (resp, success)

    @staticmethod
    def get_equipped_weapon(being: dict[str, any]) -> dict[str, any]:
        if "equpped" in being:
            equipped_weapon_name = being["equipped"]
            if "items" in being:
                return being["items"].get(equipped_weapon_name)
        return None
    
    @staticmethod
    def get_damage_die(being: dict[str, any]) -> str:
        if being.get("equipped", None):
            weapon = Game.get_equipped_weapon(being)
            if weapon is not None:
                return weapon["damage"]
        if "attack" in being["basic"]:
            return being["basic"]["attack"]
        return "d4"

    @staticmethod
    def has_ability(being: dict[str, any], ability: str) -> bool:
        return ability in being.get("stats", {}).get("abilities", {})

    @staticmethod
    def is_dead(being: dict[str, any]) -> bool:
        return being.get("dead", False)

    def set_is_dead(self, being: dict[str, any], dead: bool) -> None:
        if Game.is_dead(being):
            return
        being["dead"] = dead
        # Add the npc, char, monster's corpse to the items in the room. Make sure their inventory is still
        # accessible
        if dead:
            being_name = being['name']
            corpse_item =  { "name": f"{being_name}'s Corpse", "type": "Corpse", "target_unique_name": being_name }
            self.add_item(self.cur_location_state, corpse_item)

    @staticmethod
    def has_escaped(being: dict[str, any]) -> bool:
        return being["encounter"].get("escaped", False)

    @staticmethod
    def set_has_escaped(being: dict[str, any], escaped: bool) -> None:
        if Game.has_escaped(being) == escaped:
            return
        being["encounter"]["escaped"] = escaped

    @staticmethod
    def is_asleep(being: dict[str, any]):
        return "asleep" in being.get("cur_state", [])
    
    @staticmethod
    def is_paralyzed(being: dict[str, any]):
        return "paralyzed" in being.get("cur_state", [])

    @staticmethod
    def is_frozen(being: dict[str, any]):
        return "frozen" in being.get("cur_state", [])

    @staticmethod
    def is_unconscious(being: dict[str, any]):
        return "unconscious" in being.get("cur_state", [])

    @staticmethod
    def is_stunned(being: dict[str, any]):
        return "stunned" in being.get("cur_state", [])

    @staticmethod
    def is_immobilized(being: dict[str, any]):
        return "immobilized" in being.get("cur_state", [])

    @staticmethod
    def get_cur_health(being: dict[str, any]) -> int:
        basic_stats = being["stats"]["basic"]
        if "cur_health" not in basic_stats:
            basic_stats["cur_health"] = basic_stats["health"]
        return basic_stats["cur_health"]

    @staticmethod
    def can_do_actions(being: dict[str, any]) -> tuple[str, bool]:
        if Game.is_dead(being):
            return ("is dead", False)
        if "cur_state" in being and len(being["cur_state"]) > 0:
            states = []
            if Game.is_paralyzed(being):
                states.append("paralyzed")
            if Game.is_frozen(being):
                states.append("frozen")
            # Can be only one of these at a time (stunned, unconscious, asleep)
            if Game.is_stunned(being):
                states.append("stunned")
            elif Game.is_unconscious(being):
                states.append("unconscious")
            elif Game.is_asleep(being):
                states.append("asleep")
            if len(states) > 0:
                return ("is " + ", ".join(states), False)
        return ("", True)

    @staticmethod
    def get_cur_defense(being: dict[str, any]) -> str:
        basic_stats = being["stats"]["basic"]
        if "cur_defense" not in basic_stats:
            basic_stats["cur_defense"] = basic_stats["defense"]
        return basic_stats["cur_defense"]

    def set_cur_health(self, being: dict[str, any], value: int) -> int:
        basic_stats = being["stats"]["basic"]
        if "cur_health" not in basic_stats:
            basic_stats["cur_health"] = basic_stats["health"]
        basic_stats["cur_health"] = value
        if basic_stats["cur_health"] < 0:
            basic_stats["cur_health"] = 0
        if basic_stats["cur_health"] > basic_stats["health"]:
            basic_stats["cur_health"] = basic_stats["health"]
        if basic_stats["cur_health"] == 0 and not Game.is_dead(being):
            self.set_is_dead(being, True)
        return basic_stats["cur_health"]

    def get_players_alive(self) -> int:
        chars_alive = 0
        for char in self.game_state["characters"].values():
            if not Game.is_dead(char):
                chars_alive += 1
        return chars_alive
    
    def merge_monster(self, monster_name: str, monster_def: dict[str, any]) -> dict[str, any]:
        monster_type = monster_def["monster_type"]
        monster_merged = copy.deepcopy(self.get_monster_type(monster_type))
        monster_merged.update(monster_def)
        monster_name_no_number = monster_name.strip("0123456789 ")
        monster_merged["name"] = monster_name_no_number
        if monster_name_no_number == monster_type or monster_name_no_number in self.object_map:
            self.get_or_add_unique_name(monster_name_no_number, monster_merged)
        else:
            monster_merged["unique_name"] = monster_name_no_number
        monster_merged["type"] = "monster"
        return monster_merged

    @staticmethod
    def can_attack(attacker: dict[str, any], attack_type: str) -> None:
        # attack_type is "melee" or "ranged"
        if Game.is_monster(attacker):
            if attack_type == "melee":
                return attacker.get("melee_attack") is not None
            else:
                return attacker.get("ranged_attack") is not None
        if "equipped" not in attacker:
            return False
        weapon_name = attacker["equipped"].get(attack_type + "_weapon")
        return weapon_name is not None

    def get_merged_item(self, org_item: dict[str, any]) -> dict[str, any]:
        item_name = strip_unique_id(org_item["name"])
        if "rules_item" in org_item:
            rules_item_name = org_item["rules_item"]
        else:
            rules_item_name = item_name
        item = copy.deepcopy(self.rules["equipment"].get(rules_item_name, {}))
        item.update(org_item)
        return item      

    def get_merged_equipped_weapon(self, attacker: dict[str, any], attack_type: str) -> None:
        # attack_type is "melee" or "ranged"
        if Game.is_monster(attacker):
            if attack_type == "melee":
                return attacker.get("melee_attack") # Monsters must have a melee attack
            else:
                return attacker.get("ranged_attack") # Don't necessarily have a range attack
        if "equipped" not in attacker:
            return None
        weapon_name = attacker["equipped"].get(attack_type + "_weapon")
        if weapon_name is None:
            return None
        _, orig_weapon = find_case_insensitive(attacker["items"], weapon_name)
        weapon = self.get_merged_item(orig_weapon)
        return weapon
    
    def get_merged_exits(self) -> dict[str, any]:
        exits = copy.deepcopy(self.cur_location.get("exits", {}))
        if self.cur_location_script and "exits" in self.cur_location_script:
            exits.update(self.cur_location_script["exits"])
        if "exits" in self.cur_location_state:
            exits.update(self.cur_location_state["exits"])
        return exits

    def get_merged_npcs(self) -> list[str]:
        npcs = copy.deepcopy(self.cur_location.get("npcs", []))
        if self.cur_location_script and "npcs" in self.cur_location_script:
            npcs += self.cur_location_script["npcs"]
        if "npcs" in self.cur_location_state:
            npcs += self.cur_location_state["npcs"]
        return npcs

    def get_merged_usables(self) -> dict[str, any]:
        usables = copy.deepcopy(self.cur_location.get("usables", {}))
        if self.cur_location_script and "usables" in self.cur_location_script:
            usables.update(self.cur_location_script["usables"])
        if "usables" in self.cur_location_state:
            usables.update(self.cur_location_state["usables"])
        return usables
    
    def get_merged_poi(self) -> dict[str, any]:
        poi = copy.deepcopy(self.cur_location.get("poi", {}))
        if self.cur_location_script and "poi" in self.cur_location_script:
            poi.update(self.cur_location_script["poi"])
        if "poi" in self.cur_location_state:
            poi.update(self.cur_location_state["poi"])
        return poi    

    def get_dialog_hints(self) -> dict[str, any]:
        if self.cur_location_script and "dialog_hints" in self.cur_location_script:
            return self.cur_location_script["dialog_hints"]
        if "dialog_hints" in self.cur_location:
            return self.cur_location["dialog_hints"]
        if self.cur_area and "dialog_hints" in self.cur_area:
            return self.cur_area["dialog_hints"]
        return ""
    
    def get_story_summary(self) -> dict[str, any]:
        if self.cur_location_script and "story_summary" in self.cur_location_script:
            return self.cur_location_script["story_summary"]
        if "story_summary" in self.cur_location:
            return self.cur_location["story_summary"]
        if self.cur_area and "story_summary" in self.cur_area:
            return self.cur_area["story_summary"]
        return ""
    
    def get_spells_of_type(self, 
                           char: dict[str, any], 
                           magic_ability: str, 
                           spell_types: list[str]) -> list[str]:
        if "stats" not in char or "abilities" not in char["stats"]:
            return []
        if magic_ability not in char["stats"]["abilities"]:
            return []
        assert magic_ability.endswith(" Magic")
        char_level: int = char["stats"]["basic"].get("level", 1)
        magic_category = magic_ability[:-6]
        spells = self.rules["spells"]           
        found_spells: list[str] = [] 
        for spell_name, spell in spells.items():
            if spell["category"] != magic_category:
                continue
            if spell["type"] not in spell_types:
                continue
            spell_level = spell.get("level", 1)
            if spell_level > char_level:
                continue
            found_spells.append(spell_name)
        found_spells.sort()
        return found_spells

    def get_char_magic_abilities(self, char: dict[str, any], spell_types: list[str] = None) -> list[str]:
        if "stats" not in char or "abilities" not in char["stats"]:
            return []
        magic_abilities: list[str] = []
        for ability in char["stats"]["abilities"]:
            if ability.endswith(" Magic"):
                if spell_types is not None:
                    spells = self.get_spells_of_type(char, ability, spell_types)
                    if len(spells) == 0:
                        continue
                magic_abilities.append(ability)
        magic_abilities.sort()
        return magic_abilities

    # OBJECT MAP ----------------------------------------------------------
    
    @property
    def last_object_uid(self) -> int:
        return self.game_state["state"]["last_object_uid"]

    @last_object_uid.setter
    def last_object_uid(self, value: int) -> None:
        self.game_state["state"]["last_object_uid"] = value

    def get_or_add_unique_name(self, obj_name: str, obj: dict[str, any]) -> str:
        if "unique_name" not in obj:
            uid = self.last_object_uid
            self.last_object_uid += 1
            unique_name = f"{obj_name}#{uid}"
            obj["unique_name"] = unique_name
        else:
            unique_name = obj["unique_name"]
        return unique_name
    
    def make_object_path(self, obj: dict[str, any]) -> str:
        key = escape_path_key(obj["unique_name"])
        obj_type = obj["type"]
        if obj_type == "item":
            parent_unique_name = obj["parent"]
            parent_path = self.object_map.get(parent_unique_name)
            assert parent_path is not None
            return f"{parent_path}.items.{key}"
        else:
            return f"{obj_type}s.{key}"

    # Char, Monster, NPC Items
    def add_object_items(self, parent: dict[str, any], items: dict[str, any]) -> None:
        parent_unique_name = parent["unique_name"]
        parent["items"] = parent_items = parent.get("items", {})
        items_copy = copy.deepcopy(items)
        for item_name, item in items_copy.items():
            if "name" not in item:
                item["name"] = item_name
            item["type"] = "item"
            item["parent"] = parent_unique_name
            self.add_to_object_map(item)
            item_unique_name = item["unique_name"]
            parent_items[item_unique_name] = item

    def init_object_map(self) -> None:
        self.object_map = {}
        self.game_state["state"]["last_object_uid"] = 1000
        for obj_type in [ "character", "monster", "npc", "game_state", "location_state" ]:
            obj_dict_name = f"{obj_type}s"
            obj_dict = self.game_state[obj_dict_name] = self.game_state.get(obj_dict_name, {})
            for obj_name, obj in obj_dict.items():
                obj["name"] = obj_name
                obj["unique_name"] = obj_name
                obj["type"] = obj_type
                self.add_to_object_map(obj)
                obj_items = obj.get("items", {})
                obj["items"] = {}
                self.add_object_items(obj, obj_items)
    
    def remove_from_object_map(self, obj: dict[str, any]) -> None:
        unique_name = obj["unique_name"]
        assert obj["type"] not in [ "location_state", "npc" ] # These can't be removed!
        del self.object_map[unique_name]
        if "items" in obj:
            items = obj["items"]
            for item in items.values():
                self.remove_from_object_map(item)
            
    def add_to_object_map(self, obj: dict[str, any]) -> None:
        obj_type = obj["type"]
        obj_name = obj["name"]
        unique_name = obj.get("unique_name")
        if unique_name is None:
            if obj_type == "item" or obj_name in self.object_map:
                unique_name = self.get_or_add_unique_name(obj_name, obj)
            else:
                unique_name = obj["unique_name"] = obj["name"]
        path = self.make_object_path(obj)                
        self.object_map[unique_name] = path

    def get_object(self, unique_name: str) -> dict[str, any] | None:
        if unique_name is None:
            return None
        path = self.object_map.get(unique_name)
        if path is None:
            return None
        return pydash.get(self.game_state, path)

    # EFFECTS ----------------------------------------------------------

    @property
    def effects(self) -> list:
        return self.game_state["effects"]

    @property
    def mods(self) -> dict[str, any]:
        return self.game_state["mods"]

    @property
    def last_effect_uid(self) -> int:
        return self.game_state["state"]["last_effect_uid"]
    
    @last_effect_uid.setter
    def last_effect_uid(self, value: int) -> None:
        self.game_state["state"]["last_effect_uid"] = value

    def get_mod_path(mod: dict[str, any]) -> str:
        if "damage" in mod or "heal" in mod:
            return "stats.basic.cur_health"
        elif "target_ai_state" in mod:
            return "states.cur_ai_states" 
        return mod.get("path")

    def apply_effect_mod(self, target: dict[str, any], path: str, prev_value: any, mod: dict[str, any]) -> any:
        if path is None:
            path = mod.get("path")
        key = None
        if "set" in mod:
            mode = "set"
        elif "add" in mod or "heal" in mod:
            mode = "add"
        elif "heal" in mod:
            key = "heal"
            path = "stats.basic.cur_health"
            mode = "add"
        elif "sub" in mod:
            mode = "sub"
        elif "damage" in mod:
            key = "damage"
            path = "stats.basic.cur_health"
            mode = "sub"
        elif "add_line" in mod:
            mode = "add_line"
        elif "mul" in mod:
            mode = "mul"
        elif "append" in mod:
            mode = "append"
        elif "or" in mod:
            mode = "or"
        else:
            raise RuntimeError(f"invalid effect mode")
        value = mod[key or mode]
        if (mode == "add" or mode == "sub") and isinstance(value, str):
            value = Game.die_roll(value)
        if mode != "set" and prev_value is None:
            var_path_items = path.split(".")[-1]
            var_name = var_path_items[-1]
            if var_name.startswith("cur_"):
                prev_path = ".".join(var_path_items[:-1]) + "." + var_name[4:]
                prev_value = self.get_state_value(prev_path)
            else:
                if mode == "add" or mode == "mul":
                    prev_value = 0
                elif mode == "append" or (isinstance(value, str) and mode == "or"):
                    prev_value = []
                elif isinstance(value, bool) and mode == "or":
                    prev_value = False
                else:
                    raise RuntimeError("invalid prev value in apply_effect_mod()")
        if mode == "set":
            new_value = value
            self.set_state_value(target, path, new_value)
        elif mode == "add":
            new_value = prev_value + value
            self.set_state_value(target, new_value)
        elif mode == "add_line":
            new_value =  (value if prev_value == "" else prev_value + "\n" + value)
            self.set_state_value(target, new_value)
        elif mode == "mul":
            new_value = int(prev_value * value)
            self.set_state_value(target, path, new_value)
        elif mode == "append":
            prev_value.append(copy.deepcopy(value))
            new_value = prev_value
        elif mode == "or":
            if isinstance(value, bool):
                new_value = prev_value or value
            elif value not in prev_value:
                prev_value.append(copy.deepcopy(value))
                new_value = prev_value
        return new_value

    def apply_effect_mods(self, target: dict[str, any], path: str, mod_list: list[dict[str, any]]) -> any:
        prev_value = None
        for mod in mod_list:
            prev_value = self.apply_effect_mod(target, path, prev_value, mod)
        return prev_value

    def apply_simple_effect(self, effect_id: str, effect_def: dict[str, any], target: dict[str, any]) -> tuple[str, bool]:
        target_name = target.get("name", "target")
        match effect_id:
            case "heal":
                die = effect_def["heal"]["die"]
                value = Game.die_roll(die)
                new_health = self.set_cur_health(target, self.get_cur_health(target) + value)
                max_health = target["stats"]["basic"]["health"]
                if new_health == max_health:
                    return (f" - heal {die} {value} - new health is: {new_health} - {target_name} fully restored!\n", False)
                else:
                    return (f" - heal {die} {value} - new health is: {new_health} (of max: {max_health})\n", False)
            case "damage":
                die = effect_def["damage"]["die"]
                value = Game.die_roll(die)
                new_health = self.set_cur_health(target, self.get_cur_health(target) - value)
                max_health = target["stats"]["basic"]["health"]
                if new_health == 0:
                    return (f" - damage {die} {value} - new health is: {new_health} - {target_name} DIES!\n", False)
                else:
                    return (f" - damage {die} {value} - new health is: {new_health} (of max: {max_health})\n", False)
            case _:
                raise RuntimeError(f"unknown simple effect {effect_id}")

    def apply_effects(self, action: str, name: str, source: dict[str, any], targets: list[dict[str, any]], verb: str = None) -> tuple[str, bool]:
        effect_src = source
        # If source supports verbs, get the proper verb (i.e. a switch that can be on/off, or torch)
        if "verbs" in source:
            if verb is None:
                if "default_verb" in source:
                    verb = source["default_verb"]
                else:
                    verb = action
            if verb not in source["verbs"]:
                return (f"don't know how to {verb} {name}", True)
            effect_src = source["verbs"][verb]
        # Get description of action (we use this to tell the AI what it was)
        if verb is None:
            verb = action
        desc = f"  \"{verb}\" - {name}"
        if len(targets) > 0:
            desc += " on/with " + ", ".join([target.get("name", "") for target in targets])
        # Apply the action
        duration = effect_src.get("duration")       
        turns = effect_src.get("turns")
        check = effect_src.get("check")
        resp = desc
        if duration is not None or turns is not None or check is not None:
            # Things that have a temporary effect
            self.last_effect_uid += 1
            effect_uid = self.last_effect_uid
            effect_targets = {}
            mod_paths = []
            for target in targets:
                target_unique_name = target["unique_name"]
                # We add a modifier to a property path, and recalculate the value with all modifiers.
                for effect_def in effect_src["effects"]:
                    mod_path = effect_def["path"]
                    mod_set = self.mods[target_unique_name] = self.mods.get(target_unique_name, {})
                    mod_list = mod_set[mod_path] = mod_set.get(mod_path, [])
                    effect_mod = copy.deepcopy(effect_def)
                    del effect_mod["path"] # don't need this
                    effect_mod["uid"] = effect_uid
                    mod_list.append()
                    self.apply_effect_mods(target, mod_path, mod_list)
                    mod_paths.append(mod_path)
                effect_targets[target["unique_name"]] = { mod_paths: mod_paths }
            effect = { "uid": effect_uid, "description": desc, "start_time": self.cur_time, "targets": effect_targets }
            # Time limit for this effect
            if duration is not None:
                effect["duration"] = duration
            elif turns is not None:
                effect["turns"] = turns
            elif check is not None:
                effect["check"] = copy.deepcopy(check)
            # Add to list of currently active effects
            self.effects.append(effect)
        else:
            # Things that have a permanent effect
            for target in targets:
                target_unique_name = target["unique_name"]
                for effect_def in source["effects"]:
                    if len(effect_def) == 1:
                        effect_id = next(iter(effect_def))
                        simp_desc, failed = self.apply_simple_effect(effect_id, effect_def, target)
                        resp += simp_desc
                        if failed:
                            return (resp, True)
                    else:
                        mod_path = effect_def.get("path", None)
                        assert mod_path is not None
                        self.apply_effect_mod(target, mod_path, None, mod_list)
        return (resp, False)

    def remove_effect(self, effect: dict[str, any]) -> None:
        effect_uid = effect["uid"]
        for unique_target_name, effect_target in effect["targets"].items():
            target = self.get_object(unique_target_name)
            if target is None:
                continue
            # We remove the modifier for the given property path, and recaculate the value of 
            # the target path after it's removed
            for mod_path in effect_target.get("mod_paths", []):
                mod_list: list[any] = self.mods.get(unique_target_name, {}).get(mod_path, [])
                del_idx = None
                for idx, mod in enumerate(mod_list):
                    if mod["uid"] == effect_uid:
                        del_idx = idx
                        break
                if del_idx is not None:
                    del mod_list[del_idx]
                    self.apply_effect_mods(target, mod_path, mod_list)             
        self.effects.remove(effect)

    def update_effect(effect: dict[str, any]) -> None:
        pass

    def update_effect_list(self, effect_list: list) -> None:
        remove_list = []
        for effect_idx, effect in enumerate(self.effects):
            remove = False
            duration = effect.get("duration")
            if duration is not None:
                start_time = effect["start_time"]
                if isinstance(duration, str) and duration == "":
                    if duration == "encounter" and self.cur_encounter is None:
                        remove = True
                    else:
                        raise RuntimeError(f"duration value '{duration}' not recognized")
                elif isinstance(duration, int):
                    mins_elapsed = time_difference_mins(self.cur_time, start_time) 
                    if mins_elapsed >= duration:
                        remove = True
                else:
                    raise RuntimeError("duration is not a valid type (int|str)")
            turns = effect.get("turns")
            if turns is not None:
                turns -= 1
                effect["turns"] = turns
                if turns == 0:
                    remove = True
            check = effect.get("check")
            if check is not None:
                pass # TODO: implement check termination
            if remove:
                self.remove_effect(effect)
                remove_list.append(effect_idx)
            else:
                self.update_effect(effect)
        for idx in reversed(remove_list):
            del effect_list[idx]

    def update_all_effects(self) -> None:
        self.update_effects_list(self.game_state["effects"])
        if self.cur_encounter is not None:
            self.update_effect_list(self.cur_encounter["effects"])

    def check_requirements(self, being: dict[str, any], source: dict[str, any], targets: list[dict[str, any]]) -> tuple[str, bool]:
        require = source.get("require", [])
        resp = ""
        for req in require:
            if "check" in req:
                check = req["check"]
                if "ability1" in check:
                    for index, target in reversed(list(enumerate(targets))):
                        skill_ability1 = check.get("skill1") or check.get("ability1")
                        skill_ability2 = check.get("skill2") or check.get("ability2")
                        check_resp, success = Game.skill_ability_check_against(being, skill_ability1, target, skill_ability2)
                        resp = resp + check_resp + "\n"
                        if not success:
                            del targets[index]
                    if len(targets) == 0:
                        return (resp, True)
                else:
                    skill_ability = check.get("skill") or check.get("ability")
                    roll_against = check["roll"]
                    check_resp, success = Game.skill_ability_check(being, skill_ability, roll_against)
                    resp = resp + check_resp + "\n"
                    if not success:
                        return (resp, True)
        if resp == "":
            resp = "ok"
        return (resp, False)

    # GENERAL ACTIONS ----------------------------------------------------------

    def describe_location(self, image_if_first_time: bool = False) -> tuple[str, bool]:
        desc = "description: " + self.cur_location["description"].strip(" \n\t") + "\n\n"
        if self.cur_location_script is not None:
            desc += "\n" + self.cur_location_script["description"].strip(" \t\n") + "\n"
        changes = ""
        if "changes" in self.cur_location_state:
            changes = self.cur_location_state["changes"]
            if changes != "":
                changes = "changes:" + self.cur_location_state["changes"] + "\n"
        exits = self.describe_exits()
        if exits != "":
            exits = "exits: " + exits + "\n"
        items = ""
        if "items" in self.cur_location_state and len(self.cur_location_state["items"]) != 0:
            item_descs = Game.get_item_list_desc(self.cur_location_state["items"])
            items = "items: " + json.dumps(item_descs) + "\n"
        tasks = self.describe_tasks()
        if tasks != "":
            tasks = "tasks: " + tasks + "\n"
        topics = self.describe_topics()
        if topics != "":
            topics = "dialog topics: " + topics + "\n"
        # Set the current location image as the image the action will return
        self.action_image_path = self.location_image_path(if_first_time=image_if_first_time)
        all_npcs = self.cur_location.get("npcs", []) + \
            (self.cur_location_script.get("npcs", []) if self.cur_location_script is not None else [])
        npcs = ""
        if len(all_npcs) != 0:
            "npcs: " + ",".join(all_npcs) + " are here\n"
        # Make sure we've marked all NPCs as "known" by the players once they've seen them
        for npc_name in all_npcs:
            self.game_state["npcs"][npc_name]["has_player_met"] = True
        instr = self.cur_location.get("instructions", "").strip(" \n\t")
        if self.cur_location_script is not None and "instructions" in self.cur_location_script:
            if instr != "":
                instr += "\n\n"
            instr += self.cur_location_script["instructions"].strip(" \n\t") + "\n"
        if instr != "":
            instr = "\n" + self.prompts["instructions_prompt"].strip(" \n\t") + "\n\n" + instr + "\n"
        # If we're in encounter mode.. use an abbreviated location description with encounter insructions/rules
        encounter = self.describe_encounter()
        if encounter != "":
            resp = f"{desc}{npcs}{encounter}"
        else:
            resp = f"{desc}{changes}{exits}{items}{tasks}{npcs}{topics}{instr}"
        return (resp, False)

    def stats(self, being_name: str) -> tuple[str, bool]:
        if not being_name:
            responses = []
            for char_name in self.characters.keys():
                stats_resp, err = self.stats(char_name)
                if err:
                    return (stats_resp, err)
                responses.append(stats_resp)
            "\n\n".join(responses)
            return (responses, False)
        being = self.get_nearby_being(being_name)
        if being is None:
            {f"{being_name}' is not nearby", True}
        being_name = Game.get_encounter_or_normal_name(being)
        resp = ""
        resp += f"Character: '{being_name}'\n"
        resp += "  stats - " + json.dumps(being["stats"]["basic"]).strip("{}").replace("\"", "") + "\n"
        if "attributes" in being["stats"]:
            resp += "  attributes - " + json.dumps(being["stats"]["attributes"]).strip("{}").replace("\"", "") + "\n"
        if "skills" in being["stats"]:
            resp += "  skills - " + json.dumps(being["stats"]["skills"]).strip("{}").replace("\"", "") + "\n"
        if "abilities" in being["stats"]:
            resp += "  abilities - " + json.dumps(being["stats"]["abilities"]).strip("[]").replace("\"", "") + "\n"
        return (resp + "\n<INSTRUCTIONS>\nDescribe to player in a list the details of his character. Ignore the current storyline. Do not leave anything out.\n", False)

    def describe_party(self) -> tuple[str, bool]:
        resp = ""
        for char_name in self.game_state["characters"].keys():
            stats_resp, _ = self.stats(char_name)
            resp += stats_resp
        return (resp, False)

    def topic(self, npc: str, topic: str) -> tuple[str, bool]:
        if topic is None:
            topic = npc
            npc = "any"
        if not isinstance(topic, str):
            return ("please create your own response for this topic ", True)
        all_topics = self.get_merged_topics()
        if npc == "any":
            for npc_name, topics in all_topics.items():
                if topic in topics:
                    npc = npc_name
                    break
        npc_topics = all_topics.get(npc, {})
        _, topic_resp = find_case_insensitive(npc_topics, topic)
        if topic_resp is None:
            topics = json.dumps(list(npc_topics.keys()))
            return (f"no topic '{topic}' for npc '{npc}' - npc topics are {topics}\n" +\
                    "you can try again using one of these, or creatively improvise a response consistent with the story and rules\n", False)
        return ("TOPIC INFO:\n\n" + topic_resp + "\n\n" + self.prompts["topic_prompt"], False)

    def equip(self, char_name: str, weapon_name: str) -> tuple[str, bool]:
        if not isinstance(char_name, str) or not isinstance(weapon_name, str):
            return ("Unable to equip weapon", True)
        if not self.has_item(char_name, weapon_name):
            return (f"{char_name} does not have a {weapon_name} to equip", True)
        char = self.get_object(char_name)
        if char is None:
            return (f"{char_name} is not found", True)
        if not self.can_do_actions(char):
            return (f"{char_name} is unable to equip weapons right now", True)
        orig_weapon = self.find_item(char_name, weapon_name)
        weapon = self.get_merged_item(orig_weapon)
        weapon_type = weapon["type"]
        if weapon_type != "Melee Weapon" and weapon_type != "Ranged Weapon":
            return (f"You can't equip {weapon_name}", True)
        if weapon_type == "Melee Weapon":
            char["eqipped"]["melee_weapon"] = weapon_name
        else:
            char["eqipped"]["ranged_weapon"] = weapon_name
        return ("ok", False)

    def give(self, from_name: str, to_name: str, item_name: any, extra: any) -> tuple[str, bool]:
        if not isinstance(from_name, str) or not isinstance(to_name, str) or not isinstance(item_name, str):
            return ("invalid command", True)
        if from_name == to_name:
            return ("can not give to self", True)
        if not self.is_character_name(from_name) and not self.is_nearby_being_name(from_name):
            return (f"{from_name} is not a character or nearby monster or npc", True)
        if not self.is_character_name(to_name) and not self.is_nearby_being_name(to_name):
            return (f"{to_name} is not a character or nearby monster or npc", True)
        from_being = self.get_object(from_name)
        assert from_being is not None
        to_being = self.get_object(to_name)
        assert to_being is not None
        item_unique_name, item = find_case_insensitive(from_being["items"], item_name)
        if item is None:
            return (f"no item '{item_name}", True)
        item_qty = item.get("qty")        
        qty, err = any_to_int(extra)
        if err:
            qty = 1
        if qty > item_qty:
            return ("only has {item_qty}", False)
        give_item, err_str, err = self.remove_item(from_being, item_unique_name, qty)
        if err: 
            return (err_str, err)
        return self.add_item(to_being, give_item)

    def help(self, subject) -> tuple[str, bool]:
        if not subject:
            return ("AI Referee, help players.", False)
        self.skip_turn = True
        if subject.endswith(" spell"):
            subject = subject[:-6]
        elif subject.endswith(" spells"):
            subject = subject[:-7]
        elif subject.endswith(" equipemnt"):
            subject = subject[:-10]
        help = self.help_index.get(subject.lower())
        if help != None:
            match help["type"]:
                case "text":
                    resp, err = (help["help"], False)
                case "spell":
                    resp, err = self.describe_spell(help["name"])
                case "spells_list":
                    resp, err = str(help["list"])
                case "magic_categories":
                    resp, err = self.describe_magic(help["name"])
                case "equipment":
                    resp, err = self.describe_equipment(help["name"])
                case _:
                    raise RuntimeError("Invalid help index type")
        else:
            no_help_resp = self.prompts["no_help_response"].replace("{subject}", subject)
            resp, err = (no_help_resp, False)
        if err:
            return resp, err
        return "HELP RESPONSE:\n\n" + resp, err
            
    def look(self, subject, object) -> tuple[str, bool]:
        if subject is None or subject == self.cur_location_name or \
                subject == "location" or subject == "around":
            return self.describe_location()
        elif subject == "party":
            return self.describe_party()
        desc = None
        if subject in self.cur_location.get("poi", {}):
            poi = self.cur_location["poi"][subject]
            desc = poi["description"]
            self.action_image_path = check_for_image(poi.get("image", f"images/{subject}"))
        elif self.cur_location_script is not None and subject in self.cur_location_script.get("poi", {}):
            poi = self.cur_location_script["poi"][subject]
            desc = poi["description"]
            self.action_image_path = check_for_image(poi.get("image", f"images/{subject}"))
        elif subject in self.game_state["characters"]:
            desc = self.game_state["characters"][subject]["info"]["other"]["description"]
            self.action_image_path = self.other_image(subject, "characters")
        elif subject in self.game_state["npcs"] and \
                self.game_state["npcs"][subject].get("has_player_met", False) == True:
            desc = self.game_state["npcs"][subject]["description"]
            self.action_image_path = self.other_image(subject, "npcs")
        if desc is None:
            # Check topics (AI may be confused)
            npc_topics = self.get_merged_topics()
            for npc_name, topics in npc_topics.items():
                if subject in topics:
                    return self.topic(npc_name, subject)
        if desc is not None:
            return (f"please elaborate upon and creatively describe '{subject} with '{desc}'", False)
        return (f"if players can currently see '{subject}', provide a suitable description", False)      


    # EXPLORE ACTIONS ----------------------------------------------------------

    def describe_script_state(self) -> tuple[str, bool]:
        if self.cur_location_script is None:
            return ("no current script state", True)
        desc = "description: " + self.cur_location_script["description"].strip(" \t\n") + "\n\n"
        instr = ""
        if "instructions" in self.cur_location_script:
            instr = self.prompts["instructions_prompt"].strip(" \n\t") + "\n\n" + self.cur_location_script["instructions"].strip(" \n\t") + "\n"
        exits = "exits: " + self.describe_exits() + "\n"
        tasks = self.describe_tasks()
        resp = f"{desc}{instr}{exits}{tasks}"
        return (resp, False)

    def go(self, subject: str, object: str) -> tuple[str, bool]:
        exits = self.get_merged_exits()
        to = None
        if subject in exits:
            to = subject
        elif object in exits:
            to = object
        if to is None:
            exit_names = json.dumps(list(exits.keys()))
            return (f"can't go '{subject}'. You're location is '{self.cur_location_name}' and exits are {exit_names} - try again", True)
        new_loc_name = exits[to]["to"]
        self.set_location(new_loc_name)
        return self.describe_location(image_if_first_time=True)

    def change(self, changes: str) -> tuple[str, bool]:
        self.cur_location_state["changes"] = changes
        return ("ok", False)
    
    def invent(self, char_name) -> tuple[str, bool]:
        if not self.is_character_name(char_name):
            return (f"not a character '{char_name}'", True)
        invent_items = {}
        for item in self.characters[char_name]["items"].values():
            item_name = item["name"]
            if item_name not in invent_items:
                if "qty" in item:
                    invent_items[item_name] = { "qty": item["qty"] }
                else:
                    invent_items[item_name] = {}
            else:
                invent_items[item_name] = { "qty": invent_items[item_name].get("qty", 1) + item.get("qty", 1) }
        return (f"{char_name}'s inventory:\n\n" + json.dumps(invent_items) + "\n\n<INSTRUCTIONS>\nList and describe the inventory items for this character.\n", False)

    def pickup(self, being_name: str, item_name: str, extra: any) -> tuple[str, bool]:
        if not isinstance(being_name, str) or not isinstance(item_name, str):
            return ("invalid command", True)     
        if not self.is_nearby_being_name(being_name):
            return (f"'{being_name}' is not here", True)
        being = self.get_nearby_being(being_name)
        if not being:
            return (f"'no {being_name}' here", True)
        reason_cant_do, can_do = Game.can_do_actions(being)
        if not can_do:
            return (f"'{being_name}' {reason_cant_do}", True)      
        _, item = find_case_insensitive(self.cur_location_state.get("items", {}), item_name)
        if item is None:
            return (f"no item '{item_name}", True)
        qty, err = any_to_int(extra)
        if err:
            qty = 1
        pickup_item, resp, err = self.remove_item(self.cur_location_state, item, qty)
        if err:
            return (resp, err)
        return self.add_item(being, pickup_item)

    def drop(self, being_name: str, item_name: str, extra: any) -> tuple[str, bool]:
        if not isinstance(being_name, str) or not isinstance(item_name, str):
            return ("invalid command", True)     
        if not self.is_nearby_being_name(being_name):
            return (f"'{being_name}' is not here", True)
        being = self.get_nearby_being(being_name)
        if not being:
            return (f"'no {being_name}' here", True)
        reason_cant_do, can_do = Game.can_do_actions(being)
        if not can_do:
            return (f"'{being_name}' {reason_cant_do}", True)      
        _, item = find_case_insensitive(being["items"], item_name)
        if item is None:
            return (f"no item '{item_name}", True)
        item_qty = item.get("qty", 1)
        qty, err = any_to_int(extra)
        if err:
            qty = 1
        if qty > item_qty:
            return ("only has {item_qty}", False)
        drop_item, resp, err = self.remove_item(being, item, qty)
        if err:
            return (resp, err)
        return self.add_item(self.cur_location_state, drop_item)       

    def resume(self) -> tuple[str, bool]:
        resp = ""
        if self.cur_location_name == self.module["starting_location_name"]:
            if "overview" in self.module and len(self.module["overview"]) > 0:
                overview = self.module["overview"]
                if "description" in overview:
                    mod_overview = self.module["overview"]["description"]
                    resp += "MODULE OVERVIEW (for AI Referee only - DO NOT reveal to player!):\n" + mod_overview + "\n\n"
                self.action_image_path = self.module_path + "/" + overview["image"] \
                    if "image" in overview else None
        else:
            if "story_summary" in self.cur_area:
                resp = "THE STORY SO FAR:\n\n" + \
                    self.cur_area["story_summary"] + "\n\n"
        resp_party, error = self.describe_party()
        if error:
            return (resp_party, error)
        resp += "PLAYER PARTY:\n\n" + resp_party + "\n"
        resp_loc, error = self.describe_location()
        if error:
            return (resp_loc, error)
        resp += "FIRST LOCATION:\n\n" + resp_loc + "\n\n" 
        resp += self.prompts["overview_prompt"]       
        return (resp, False)

    async def restart(self) -> tuple[str, bool]:
#        if not self.game_over:
#            return ("current game is not over", True)
        await self.new_game()
        return await self.resume()

    def lobby(self) -> tuple[str, bool]:
        self.skip_turn = True
        self.exit_to_lobby = True
        return ("ok", False)

    async def complete(self, task_name: str) -> tuple[str, bool]:
        if task_name in self.game_state["tasks_completed"] and self.game_state["tasks_completed"][task_name]:
            (f"task '{task_name}' is already completed", True)
        self.game_state["tasks_completed"][task_name] = True
        task = self.cur_location.get("tasks", {}).get(task_name)
        if task is None:
            return ("ok", False)
        resp = ""
        rewards = task.get("rewards", {})
        for item_name, item in rewards.items():
            char = self.get_random_character()
            char_name = char["name"]
            qty = item.get("qty", 1)
            self.add_item(char_name, item_name, qty)
            resp = resp + f"{char_name} was rewarded with '{item_name}' " + json.dumps(item) + "\n"
        return (resp, False)

    def next_script_state(self, script_state) -> tuple[str, bool]:
        if script_state == "done" or script_state == "":
            self.script_state_since = self.cur_time
            self.cur_script_state = script_state
            self.cur_location_script = None
            return self.describe_location()
        else:
            if self.cur_location_script is None or \
                    "transitions" not in self.cur_location_script or \
                    script_state not in self.cur_location_script["transitions"]:
                return (f"no script state '{script_state}' - try again with the the script state provided in your instructions", True)
            self.script_state_since = self.cur_time
            self.cur_script_state = script_state
            self.cur_location_script = self.cur_location["script"][script_state]
            return self.describe_script_state()

    def skill_check(self, character: str, skill: str) -> tuple[str, bool]:
        if random.random() > 0.5:
            return ("succeded", False)
        else:
            return ("failed", False)

    def search(self, character_name: str, term: str) -> tuple[str, bool]:
        # Note, we don't use character skill rolls for searching (at least not yet)
        # so for now we skip the character.
        if character_name not in self.game_state["characters"]:
            term = character_name
            character = self.get_random_character()
            character_name = character["name"]
        if not term:
            term = "any"
        if "hidden" not in self.cur_location_state:
            return ("nothing found", False)
        hidden = self.cur_location_state["hidden"]
        found_state = None
        found_idx = None
        for idx, state in enumerate(hidden):
            terms = state.get("terms")
            if terms is None:
                found_state = state
                found_idx = idx
                break
            if term in terms:
                found_state = state
                found_idx = idx
                break
        if found_state is None:
            return ("nothing found", False)
        desc = found_state.get("description", "")
        if len(desc) > 0:
            desc += "\n"
        found_items = found_state.get("items")
        found_items_list = ""
        if found_items is not None:
            if "items" not in self.cur_location_state:
                self.cur_location_state["items"] = {}
            item_descs = Game.get_item_list_desc(found_items)
            found_items_list = "found items " + json.dumps(item_descs).strip("[]").replace("\"", "") + "\n"
            self.add_object_items(self.cur_location_state, found_items)
        found_exits = found_state.get("exits")
        found_exits_list = ""
        if found_exits is not None:
            if "exits" not in self.cur_location_state:
                self.cur_location_state["exits"] = copy.deepcopy(self.cur_location.get("exits", {}))
            found_exits_list = "found exits " + json.dumps(list(found_exits.keys())).strip("[]") + "\n"
            self.cur_location_state["exits"].update(found_exits)
        del self.cur_location_state["hidden"][found_idx]
        if "image" in found_state:
            self.action_image_path = check_for_image(self.module_path, found_state["image"])
        return (f"{desc}{found_items_list}{found_exits_list}", False)
    
    def use(self, args: list[str], use_verb: str) -> tuple[str, bool]:

        being_name = None
        being = None
        item = None
        target_item = None
        target_being = None  

        if len(args) < 1:
            return ("No item", True)

        # Check if first arg is character (if it isn't we guess the character)
        if isinstance(args[0], str) and self.is_nearby_being_name(args[0]):
            being = self.get_nearby_being(args[0])
            being_name = Game.get_encounter_or_normal_name(being)
            del args[0]

        if being == None and self.has_item(being_name or "any", args[0]):
            item_name = args[0]
            del args[0]
            being, item = self.find_item(being_name or "any", item_name)
        elif args[0] in self.cur_location_state.get("usables", {}):
            usable_name = args[0]
            del args[0]
            usable = self.cur_location_state["usables"][usable_name]
            if being is None:
                being = self.get_random_character()
        else:
            return (f"{args[0]} is not a usable item or thing", True)

        # If we're in an encounter, make sure we haven't moved yet
        can_move_msg, can_move = self.check_encounter_can_move("cast", being)
        if not can_move:
            return (can_move_msg, True)

        # Get the thing to use the item or usable with
        if len(args) > 0:
            if self.is_nearby_being_name(args[0]):
                target_being_name = args[0]
                del args[0]
                target_being = self.get_nearby_being(target_being_name)
            elif self.has_item(being_name or "any", args[0]):
                target_item_name = args[0]
                del args[0]
                _, target_item = self.find_item(being_name, target_item_name)
            elif args[0] in self.cur_location_state["usables"]:
                target_usable_name = args[0]
                del args[0]
                target_usable = self.cur_location_state["usables"][target_usable_name]

        item_or_usable_name = item_name or usable_name

        resp = f"{being_name} uses {item_or_usable_name}"
        if target_being or target_item or target_usable:
            target_name = target_being_name or target_item_name or target_usable_name
            resp += f" on {target_name}\n"
        else:
            resp += "\n"

        # If we're in an encounter, using something counts as a move
        self.mark_encounter_moved(being)

        # Can we use this?
        req_resp, failed = self.check_requirements(being, item or usable, [ target_item or target_being ])
        if failed:
            return (resp + req_resp, True)

        # Use it and apply the effects - note this counts as a move even if we fail to apply the effect
        use_effect_resp, failed = self.apply_effects("use", item_name or usable_name, item or usable, [ target_item or target_being ], use_verb)
        resp += use_effect_resp.strip("\n")

        return (resp, failed)

    def describe_spell(self, spell_name) -> tuple[str, bool]:
        if not spell_name or spell_name.lower() == "spells" or spell_name.lower() == "all":
            return ("SPELLS:\n" + json.dumps(list(self.rules["spells"].keys())) + "\n", False)
        spell_name, spell = find_case_insensitive(self.rules["spells"], spell_name)
        if spell is None:
            return (f"no spell {spell_name}", True)
        spell["category"] = spell["category"] + " Magic"
        image_path = check_for_image(self.rules_path + "/images", spell_name, "spells")
        if image_path:
            self.action_image_path = image_path
        return (f"SPELL DESCRIPTION: {spell_name}\n" + json.dumps(spell) + "\n", False)

    def describe_magic(self, magic_category) -> tuple[str, bool]:
        if not magic_category or magic_category.lower() == "magic categories" or magic_category.lower() == "all":
            categories = [name + " Magic" for name in self.rules["magic_categories"].keys()]
            categories.sort()
            return ("MAGIC CATEGORIES:\n" + json.dumps(categories) + "\n", False)
        magic_category_name, magic_category = find_case_insensitive(self.rules["magic_categories"], magic_category)
        if magic_category is None:
            return (f"no magic category {magic_category}", True)
        spell_names = [spell_name for spell_name, spell in self.rules["spells"].items() if spell["category"] == magic_category_name]
        image_path = check_for_image(self.rules_path + "/images", magic_category_name + " Magic", "magic_categories")
        if image_path:
            self.action_image_path = image_path
        return (f"MAGIC DESCRIPTION: Please elaborate on the following with a two paragraph descripton..\n\n{magic_category_name}\n" + 
                json.dumps(magic_category) + "\n\nSPELLS: list the names only without description\n" + json.dumps(spell_names) + "\n", False)

    def cast(self, args: list[str]) -> tuple[str, bool]:

        being_name = None
        being = None
        target_item = None
        target_being = None  

        if len(args) < 1:
            return ("No spell", True)

        # Check if first arg is character (if it isn't we guess the character)
        if isinstance(args[0], str) and self.is_nearby_being_name(args[0]):
            being = self.get_nearby_being(args[0])
            being_name = Game.get_encounter_or_normal_name(being)
            del args[0]

        if being is None:
            return ("no spell caster specified", True)

        # If we're in an encounter, make sure we haven't moved yet (aren't dead, etc.)
        can_move_msg, can_move = self.check_encounter_can_move("cast", being)
        if not can_move:
            return (can_move_msg, True)

        if len(args) > 0:
            spell_name, spell = find_case_insensitive(self.rules["spells"], args[0])
            if spell is None:
                return (f"'{spell_name}' is not a spell", True)
            magic_ability = spell["category"] + " Magic"
            if not Game.has_ability(being, magic_ability):
                return (f"Can't cast {spell_name}. {being_name} does have the {magic_ability} ability.", True)
            del args[0]
        else:
            return ("need spell name as second arg", True)

        # Get the thing to use the item or usable with
        if len(args) > 0:
            if self.is_nearby_being_name(args[0]):
                target_being_name = args[0]
                del args[0]
                target_being = self.get_nearby_being(target_being_name)
            elif self.has_item(being_name or "any", args[0]):
                target_item_name = args[0]
                del args[0]
                _, target_item = self.find_item(being_name, target_item_name)
            elif args[0] in self.cur_location_state.get("usables", {}):
                target_usable_name = args[0]
                del args[0]
                target_usable = self.cur_location_state["usables"][target_usable_name]
            else:
                return (f"cast target {args[0]} not found", True)

        resp = f"{being_name} \"cast\" {spell['category']} Magic spell {spell_name}"
        if target_being or target_item or target_usable:
            target_name = target_being_name or target_item_name or target_usable_name
            resp += f" on {target_name}\n"
        else:
            resp += "\n"
        if "description" in spell:
            resp += "  DESCRIPTION: " + spell["description"] + "\n"

        # If we're in an encounter, spell casting counts as a move
        self.mark_encounter_moved(being)

        # Can we cast this?
        req_resp, failed = self.check_requirements(being, spell, [ target_item or target_being ])
        if failed:
            return (resp + req_resp, True)

        # Apply the spell effects. Note we count this as a move even if the spell effect couldn't be applied.
        cast_effect_resp, failed = self.apply_effects("cast", spell_name, spell, [ target_item or target_being or target_usable ])
        resp += cast_effect_resp.strip("\n")

        # image?
        image_path = check_for_image(self.rules_path + "/images", spell_name, "spells")
        if image_path:
            self.action_image_path = image_path          

        return (resp, failed)

    def check_random_event(self) -> tuple[bool, dict[str, any] | None]:
        if self.cur_game_state_name != "exploration":
            return None
        area = self.cur_area
        if "random_events" not in area:
            return None
        events: list[dict[str, any]] = area["random_events"]
        index = int(len(events) * self.random_event_rand_sel_val)
        event = events[index]
        t = self.random_event_rand_time_val
        start_time = self.random_event_last_time + \
            timedelta(seconds=(1 - t) * event["min_freq_time"] + t * event["max_freq_time"])
        if datetime.now() >= start_time:
            self.random_event_rand_time_val = random.random()
            self.random_event_rand_sel_val = random.random()
            self.random_event_last_time = datetime.now()
            return event["event"]
        return None

    def handle_random_event(self, event: dict[str, any]) -> tuple[str, bool]:
        match event["type"]:
            case "say":
                resp = event["text"]
                return (resp, False)
        return ("", True)

    async def do_explore_action(self, action: any, subject: any, object: any, extra: any, extra2: any) -> tuple[str, bool]:
        resp = ""
        error = False

        if action in ("move", "goto"):
            action = "go"
        
        if subject == "party":
            subject = object
            object = extra

        match action:
            case "change":
                resp, error = self.change(subject)
            case "check":
                resp, error = self.skill_check(subject, object)
#            case "complete":
#                resp, error = self.complete(subject)
            case "drop":
                resp, error = self.drop(subject, object, extra)
            case "give":
                resp, error = self.give(subject, object, extra, extra2)
            case "go":
                resp, error = self.go(subject, object)
            case "invent":
                resp, error = self.invent(subject)
            case "party":
                resp, error = self.describe_party()
            case "pickup":
                resp, error = self.pickup(subject, object, extra)
            case "search":
                resp, error = self.search(subject, object)
            case "next":
                resp, error = self.next_script_state(subject)
            case _:
                resp = f"can't do action '{action}'"
                error = True
        return (resp, error)

    async def update_exploration(self) -> str:
        random_encounter = self.check_random_encounter()
        if random_encounter:
            resp, _ = self.start_encounter(random_encounter)
            resp = self.describe_encounter()
            return await self.referee_response("", resp, 1)
        random_event = self.check_random_event()
        if random_event is not None:
            resp, _ = self.handle_random_event(random_event)
            return await self.referee_response("", resp, 1)

    async def get_exploration_buttons(self, state: dict[str, any]) -> tuple[str|None, bool]:
        next_state = "chars_actions"
        if "action" not in state:
            state.update({ "action": None, 
                           "subject": None, 
                           "object": None, 
                           "extra": None, 
                           "extra2": None,
                           "choices": "",
                           "sentence": "" })
        clicked_index = state.get("clicked_index", -1)
        if clicked_index >= 0:
            choice: str = state["buttons"][clicked_index]["choice"]
            next_state = state["buttons"][clicked_index]["next_state"]
            if choice and not choice.startswith("@"):
                button: dict[str, any] = state["buttons"][clicked_index]
                choice_type = button["choice_type"]
                state[choice_type] = button["choice"]
                if button["phrase"]:
                    state["sentence"] += button["phrase"]
        state["buttons"] = []
        state["choices"] = ""
        match next_state:
            case "chars_actions":
                actions = [ ("move", "Go", "Go to ", "exits"), 
                            ("look", "Look", "Look ", "look_targets") ]
                for action, action_name, phrase, next_state in actions:
                    button = { "text": action_name,
                               "choice": action,
                               "choice_type": "action",
                               "phrase": phrase,
                               "next_state": next_state }
                    state["buttons"].append(button)
                for char_name in self.game_state["characters"].keys():
                    char = self.get_object(char_name)
                    # Check if paralyzed, dead, etc.
                    _, can_do = Game.can_do_actions(char)
                    if not can_do:
                        continue
                    button = { "text": char_name,
                               "choice": char_name, 
                               "choice_type": "subject",
                               "phrase": char_name + " ", 
                               "next_state": "actions" }
                    state["buttons"].append(button)
                if len(state["buttons"]) == 0:
                    return (None, False)
                return ("", True)
            case "exits":
                exits = self.get_merged_exits()
                for exit in exits.keys():
                    button = { "text": exit, 
                               "choice": exit, 
                               "choice_type": "subject", 
                               "phrase": exit, 
                               "next_state": "done" }
                    state["buttons"].append(button)
                return ("", True)
            case "look_targets":
                targets = []
                # targets += list(self.game_state["characters"].keys())
                targets += [ "around" ]
                targets += self.get_merged_npcs()
                targets += list(self.get_merged_poi().keys())
                targets = targets[:7]
                for target in targets:
                    button = { "text": target, 
                               "choice": target, 
                               "choice_type": "subject", 
                               "phrase": target,
                               "next_state": "done" }
                    state["buttons"].append(button)
                return ("", True)            
            case "actions":
                char = self.get_object(choice)
                if not char:
                    return ("", False)
                state["subject"] = choice
                state["char"] = char
                actions = [ ("say", "Say", "says ", "say_choices"), 
                            ("ask", "Ask", "asks ", "ask_choices"), 
                            ("use", "Use", "uses ", "usables"),
                            ("cast", "Cast", "casts ", "spell_abilities"),
                            ("@items", "Item", "", "item"),
                            ("@info", "Info", "", "imfo"),
                            ("@menu", "Menu", "", "menu") ]
                has_npcs = len(self.cur_location.get("npcs", [])) > 0
                has_usables = len(self.get_usable_items(char)) > 0 or \
                            len(self.get_merged_usables()) > 0
                has_spells = len(self.get_char_magic_abilities(char, spell_types="ability"))
                for action, action_name, phrase, next_state in actions:
                    button = {}
                    if (action == "say" or action == "ask") and not has_npcs:
                        continue
                    if action == "use" and not has_usables:
                        continue
                    if action == "cast" and not has_spells:
                        continue
                    button = { "text": action_name,
                               "choice": action,
                               "choice_type": "action",
                               "phrase": phrase,
                               "next_state": next_state }
                    state["buttons"].append(button)
                return ("", True)
            case "say_choices":
                char_name = state["subject"]
                prompt = self.prompts["say_choices_prompt"].replace("{char_name}", char_name)
                text = await self.generate("", prompt, "dialog_choices", "dialoger")
                choices = text.strip("\n").split("\n")[:6]
                fmt_choices = []
                for choice in choices:
                    choice = choice.lstrip("123456. -\"")
                    choice = choice.rstrip("\n\"")
                    if not choice:
                        continue
                    fmt_choices.append("🔸 " + choice)
                    first_three = " ".join(choice.split(" ")[:3]) + ".."
                    button = { "text": first_three, 
                               "choice": choice, 
                               "choice_type": "object", 
                               "phrase": "\"" + choice + "\"",
                               "next_state": "done" }
                    state["buttons"].append(button)
                state["choices"] = "\n".join(fmt_choices) + "\n"            
                return ("", True)
            case "ask_choices":
                char_name = state["subject"]
                prompt = self.prompts["ask_choices_prompt"].replace("{char_name}", char_name)
                text = await self.generate("", prompt, "dialog_choices", "dialoger")
                choices = text.strip("\n").split("\n")[:6]
                fmt_choices = []
                for choice in choices:
                    choice = choice.lstrip("123456. -\"")
                    choice = choice.rstrip("\n\"")
                    if not choice:
                        continue
                    fmt_choices.append("🔹 " + choice)
                    first_three = " ".join(choice.split(" ")[:3]) + ".."
                    button = { "text": first_three, 
                               "choice": choice, 
                               "choice_type": "object", 
                               "phrase": "\"" + choice + "\"",
                               "next_state": "done" }
                    state["buttons"].append(button)
                state["choices"] = "\n".join(fmt_choices) + "\n"            
                return ("", True)        
            case "usables":
                char_name: str = state["subject"]
                usables: list[str] = []      
                usables += list(self.get_usable_items(char_name))         
                usables += list(self.get_merged_usables().keys())
                usables = usables[:7]
                for usable in usables:
                    button = { "text": usable, 
                               "choice": usable, 
                               "choice_type": "object", 
                               "phrase": target,
                               "next_state": "done" }
                    state["buttons"].append(button)
                return ("", True)
            case "spell_abilities":
                state["action"] = "cast"
                char = state["char"]
                spell_abilities = self.get_char_magic_abilities(char, spell_types=["ability"])
                for spell_ability in spell_abilities:
                    button = { "text": spell_ability, 
                               "choice": spell_ability, 
                               "choice_type": "", 
                               "phrase": "",
                               "next_state": "spells" }
                    state["buttons"].append(button)
                return ("", True)             
            case "spells":
                state["action"] = "cast"
                char = state["char"]
                spells = self.get_spells_of_type(char, choice, spell_types="ability")
                for spell_name in spells:
                    spell = self.rules["spells"][spell_name]
                    next_state = "done"
                    if "target_type" in spell:
                        next_state = "spell_targets"
                    button = { "text": spell_name, 
                               "choice": spell_name, 
                               "choice_type": "object", 
                               "phrase": spell_name + (" on " if next_state == "spell_targets" else " "),
                               "next_state": next_state }
                    state["buttons"].append(button)
                return ("", True)                      
            case "spell_targets":
                spell_targets: list[str] = []
                spell_name = choice
                spell = self.rules["spells"][spell_name]
                # TODO: Do spells on usables later
                spell_targets += list(self.game_state["characters"].keys())
                spell_targets += self.get_merged_npcs()
                for spell_target in spell_targets:
                    button = { "text": spell_target,
                               "choice": spell_target,
                               "choice_type": "extra",
                               "phrase": spell_target,
                               "next_state": "done" }
                    state["buttons"].append(button)
                return ("", True)            
            case "done":
                if not state["sentence"].endswith('"'):
                    state["sentence"] += "."
                return ("done", False)
        return (None, False)

    # ENCOUNTER ACTIONS ----------------------------------------------------------

    def check_random_encounter(self) -> tuple[bool, dict[str, any] | None]:
        if self.cur_game_state_name != "exploration":
            return None        
        area = self.cur_area
        if "random_encounters" not in area:
            return None
        encounters: list[dict[str, any]] = area["random_encounters"]
        index = int(len(encounters) * self.random_encounter_rand_sel_val)
        encounter = encounters[index]
        t = self.random_encounter_rand_time_val
        start_time = self.random_encounter_last_time + \
            timedelta(seconds=(1 - t) * encounter["min_freq_time"] + t * encounter["max_freq_time"])
        if datetime.now() >= start_time:
            self.random_encounter_rand_time_val = random.random()
            self.random_encounter_rand_sel_val = random.random()
            self.random_encounter_last_time = datetime.now()
            return encounter["encounter"]
        return None

    def get_cur_location_encounter(self) -> dict[str, any]:
        encounter: dict[str, any] = None
        if self.cur_location_script is not None and "script_encounters" in self.cur_location_state:
            encounter = self.cur_location_state["script_encounters"].get(self.cur_script_state)
        if encounter is None:
            encounter = self.cur_location_state.get("encounter")
        return encounter
    
    def remove_cur_location_encounter(self) -> None:
        if self.cur_location_script is not None and \
                "script_encounters" in self.cur_location_state and \
                self.cur_script_state in self.cur_location_state["script_encounters"]:
            del self.cur_location_state["script_encounters"][self.cur_script_state]
        elif "encounter" in self.cur_location_state:
            del self.cur_location_state["encounter"]

    def start_encounter(self, random_encounter: dict[str, any] = None) -> tuple[str, bool]:
        assert self.cur_encounter is None and self.cur_game_state_name != "encounter"
        self.cur_game_state_name = "encounter"
        encounter: dict[str, any] = None
        if random_encounter is not None:
            if self.get_cur_location_encounter() is not None:
                return ("random encounter not possible, there are already monsters in area", True)
            encounter = copy.deepcopy(random_encounter)
            self.cur_location_state["encounter"] = encounter
        else:
            encounter = self.get_cur_location_encounter()
            if encounter is None:
                return ("there are no monsters at the current location", True)
        self.cur_game_state_name = "encounter"
        self.cur_encounter = encounter
        # Get the starting range for combatants in ft
        if "starting_range" not in encounter:
            loc_size = self.cur_location.get("size", "small")
            range_bands = 0
            match loc_size:
                case "medium":
                    range_bands = 1
                case "large":
                    range_bands = 2
                case "very_large":
                    range_bands = 3
                case "open":
                    range_bands = 4
                case "outside":
                    range_bands = 4
            encounter["starting_range"] = 15 * range_bands
            encounter["min_range"] = -(15 * range_bands)
            encounter["max_range"] = 15 * range_bands
        # Add temp encounter states to characters/monsters
        encounter["characters"] = {}
        for char_name, char in self.game_state["characters"].items():
            char["encounter"] = {}
            char["encounter"]["name"] = char["name"]
            char["encounter"]["moved_round"] = 0
            char["encounter"]["range"] = encounter["starting_range"]
            self.cur_encounter["characters"][char_name] = char_name
        for monster_name, monster_def in encounter["monsters"].items():
            # Note: a 'monster' can be an npc, we just use the name monster to mean any enemy
            monster = self.get_object(monster_def.get("unique_name") or monster_name)
            if monster is None:
                monster = self.merge_monster(monster_name, monster_def)
                self.game_state["monsters"][monster["unique_name"]] = monster
                self.add_to_object_map(monster)
            monster["encounter"] = {}
            monster["encounter"]["name"] = monster_name
            monster["encounter"]["moved_round"] = 0
            monster["encounter"]["range"] = 0
            self.cur_encounter["monsters"][monster_name] = monster["unique_name"]
        # Initiative? For now players first..
        self.cur_encounter["turn"] = "players" 
        self.cur_encounter["round"] = 1
        return ("ok", False)
    
    def describe_encounter(self) -> str:
        if self.cur_game_state_name != "encounter":
            return ""
        # An image if there is one
        if "image" in self.cur_encounter:
            self.action_image_path = check_for_image(self.module_path, self.cur_encounter["image"])
        resp = ""
        if "description" in self.cur_encounter:
            resp += "ENCOUNTER DESCRIPTION:\n\n" + self.cur_encounter["description"] + "\n\n"
        monster_types = {}
        monsters_desc = ""
        for monster_name, monster_unique_name in self.cur_encounter["monsters"].items():
            monster = self.get_object(monster_unique_name)
            if monster["type"] == "monster":
                monster_type = monster["monster_type"]
                monster_types[monster_type] = self.get_monster_type(monster_type)
            reason_cant_do, can_do = Game.can_do_actions(monster)
            if not can_do:
                monsters_desc += f"  '{monster_name}' {reason_cant_do}!\n"
                continue
            if Game.has_escaped(monster):
                monsters_desc += f"  '{monster_name}' has ESCAPED!\m"
                continue
            stats = json.dumps(monster['stats']['basic']).strip("{}").replace("\"", "")
            range_attack = ("YES" if Game.can_attack(monster, "range") else "NO")
            monsters_desc += f'  "{monster_name}" type: "{monster_type}", has_range_attack: {range_attack}, stats -- {stats}\n'
        resp += "MONSTER TYPES:\n\n" + json.dumps(monster_types) + "\n\n"
        resp += f"MONSTERS:\n\n{monsters_desc}\n"
        turn_desc = self.describe_encounter_turn()
        resp += turn_desc
        return resp

    def end_encounter(self) -> tuple[str, bool]:
        players_left, monsters_left = self.get_players_monsters_left()
        if self.cur_game_state_name != "encounter":
            return ("not in encounter", True)
        self.cur_game_state_name = "exploration"
        for char_unique_name in self.cur_encounter["characters"].values():
            char = self.get_object(char_unique_name)
            del char["encounter"]
        for monster_unique_name in self.cur_encounter["monsters"].values():
            monster = self.get_object(monster_unique_name)
            del monster["encounter"]
        for monster in self.game_state["monsters"].values():
            monster.pop("encounter", None)
            if Game.is_dead(monster):
                monster.pop("stats", None)
                monster.pop("info", None)
                monster.pop("melee_attack", None)
                monster.pop("ranged_attack", None)
        for char in self.game_state["characters"].values():
            char.pop("encounter", None)
        for npc in self.game_state["npcs"].values():
            npc.pop("encounter", None)
        self.game_state["encounter"] = None
        self.cur_encounter = None
        self.cur_game_state_name = "exploration"
        self.remove_cur_location_encounter()
        if players_left > 0:
            resp, _ = self.describe_location()
            return ("Player were victorious!\n\n" + resp, False)
        players_alive = self.get_players_alive()
        if players_alive == 0:
            self.game_over = True
            return ("All players were killed - game over", False)
        self.set_location(self.prev_location_name)
        resp, _ = self.describe_location()
        return ("Your party has escaped!\n\n" + resp, False)
    
    def get_encounter_being(self, attacker_name) -> dict[str, any]:
        player = self.get_object(self.cur_encounter["characters"].get(attacker_name))
        if player:
            return (player, "players")
        monster = self.get_object(self.cur_encounter["monsters"].get(attacker_name))
        if monster:
            return (monster, "monsters")
        return (None, None)

    def get_players_monsters_left(self) -> tuple[int, int]:
        chars_left = 0
        monsters_left = 0
        for char_unique_name in self.cur_encounter["characters"].values():
            char = self.get_object(char_unique_name)
            if Game.is_still_fighting(char):
                chars_left += 1
        for monster_unique_name in self.cur_encounter["monsters"].values():
            monster = self.get_object(monster_unique_name)
            if Game.is_still_fighting(monster):
                monsters_left += 1
        return (chars_left, monsters_left)

    def get_attackers_left_to_go(self) -> tuple[int, str]:
        if self.cur_encounter["turn"] != "players":
            return (0, "")       
        left_to_go = []
        for char_name, char_unique_name in self.game_state["characters"].items():
            char = self.get_object(char_unique_name)
            if Game.can_attack(char) and char["encounter"]["moved_round"] != self.cur_encounter["round"]:
                left_to_go.append(char_name)
        left_to_go.sort()
        resp = ""
        num_left_to_go = len(left_to_go)
        if len(left_to_go) > 0:
            left_to_go_str = json.dumps(left_to_go)
            resp += f"  {num_left_to_go} playes have not yet gone: {left_to_go_str}\n"
        return (num_left_to_go, resp)

    @staticmethod
    def get_encounter_or_normal_name(being: dict[str, any]) -> str:
        encounter_name = being.get("encounter", {}).get("name")
        if encounter_name is not None:
            return encounter_name
        return being["name"]

    @staticmethod
    def is_still_fighting(being: dict[str, any]) -> bool:
        return not Game.is_dead(being) and not Game.has_escaped(being)

    @staticmethod
    def get_range_dist(from_being: dict[str, any], to_being: dict[str, any]) -> int:
        return abs(from_being["encounter"]["range"] - to_being["encounter"]["range"])
    
    @staticmethod
    def get_range_str(range_ft) -> str:
        if range_ft == 0:
            return "close/melee"
        if range_ft >= 120:
            return "distant/120ft+"
        return f"{range_ft}ft"
    
    def get_closest_ranges(self) -> tuple[int, int]:
        closest_monster = -10000
        for monster_unique_name in self.cur_encounter["monsters"].values():
            monster = self.get_object(monster_unique_name)
            if Game.is_still_fighting(monster):
                closest_monster = max(monster["encounter"]["range"], closest_monster)
        closest_char = 10000
        for char_unique_name in self.cur_encounter["characters"].values():
            char = self.get_object(char_unique_name)
            if Game.is_still_fighting(char):
                closest_char = min(char["encounter"]["range"], closest_char)
        return (closest_monster, closest_char)

    def range_band_move(self, being_name: str, being: dict[str, any], range_band_delta: int) -> tuple[int, bool, str]:
        closest_monster, closest_character = self.get_closest_ranges()
        min_range = self.cur_encounter["min_range"]
        max_range = self.cur_encounter["max_range"]
        resp = ""
        if Game.is_character(being):
            old_range = being["encounter"]["range"]
            new_range = old_range - (range_band_delta * 30)
            if new_range < closest_monster:
                new_range = closest_monster
            escaped = False
            if new_range > max_range:
                Game.set_has_escaped(being, True)
                resp = f"'{being_name}' has escaped!"
                escaped = True
            else:
                if new_range < old_range:
                    resp = f"'{being_name}' advanced {abs(new_range - old_range)}ft"
                elif new_range > old_range:
                    resp = f"'{being_name}' retreated {abs(new_range - old_range)}ft"
                else:
                    resp = ""
            being["encounter"]["range"] = new_range
            return (-(new_range - old_range), escaped, resp)
        else:
            old_range = being["encounter"]["range"]
            new_range = old_range + (range_band_delta * 30)
            if new_range > closest_character:
                new_range = closest_character
            escaped = False
            if new_range < min_range:
                Game.set_has_escaped(being, True)
                resp = f"'{being_name}' has escaped!"
                escaped = True
            else:
                if new_range > old_range:
                    resp = f"'{being_name}' advanced {abs(new_range - old_range)}ft"
                elif new_range < old_range:
                    resp = f"'{being_name}' retreated {abs(new_range - old_range)}ft"
                else:
                    resp = ""
            being["encounter"]["range"] = new_range
            return (new_range - old_range, escaped, resp)
        
    def get_attacker_encounter_states(self) -> str:
        self.check_encounter_next_turn("")
        if self.cur_encounter["turn"] == "players":
            attackers = self.cur_encounter["characters"]
            targets = self.cur_encounter["monsters"]
            turn_name = "CHARACTER"
        else:
            attackers = self.cur_encounter["monsters"]
            targets = self.cur_encounter["characters"]
            turn_name = "MONSTER"
        resp = ""
        for attacker_name, attacker_unique_name in attackers.items():
            attacker = self.get_object(attacker_unique_name)
            if not Game.is_still_fighting(attacker):
                continue
            ranges = []
            for target_name, target_unique_name in targets.items():
                target = self.get_object(target_unique_name)
                if Game.is_still_fighting(target):
                    range = Game.get_range_str(Game.get_range_dist(attacker, target))
                    ranges.append(f"'{target_name}' - range: {range}")
            ranges_str = ", ".join(ranges)
            cur_health = Game.get_cur_health(attacker)
            cur_defense = Game.get_cur_defense(attacker)
            resp += f"'{attacker_name}' - health: {cur_health}, defense: {cur_defense} --- targets: {ranges_str}\n"
        return f"\n{turn_name} STATES:\n\n" + resp + "\n"

    def get_attackers_left_to_go(self) -> tuple[str, int]:
        left_to_go = []
        resp = ""
        players_left, monsters_left = self.get_players_monsters_left()
        if monsters_left == 0:
            return ("no monsters left", 0)
        if players_left == 0:
            return ("no players left", 0)
        if self.cur_encounter["turn"] == "players":
            for char_name, char_unique_name in self.cur_encounter["characters"].items():
                char = self.get_object(char_unique_name)
                if Game.is_still_fighting(char) and char["encounter"]["moved_round"] != self.cur_encounter["round"]:
                    left_to_go.append(char_name)
            if len(left_to_go) > 0:
                resp = "Players who haven't moved yet (AI Referee please tell players): " + ", ".join(left_to_go) + "\n"
            else:
                resp = "All players have moved\n"
        else:
            for monster_name, monster_unique_name in self.cur_encounter["monsters"].items():
                monster = self.get_object(monster_unique_name)
                if Game.is_still_fighting(monster) and monster["encounter"]["moved_round"] != self.cur_encounter["round"]:
                    left_to_go.append(monster_name)
            if len(left_to_go) > 0:
                resp = "Please choose moves for these monsters: " + ", ".join(left_to_go) + "\n"
            else:
                resp = "All monsters have moved\n"
        return (resp, len(left_to_go))

    def describe_encounter_turn(self) -> str:
        resp = self.get_attacker_encounter_states() + "\n"
        if self.cur_encounter["turn"] == "monsters":
            resp += self.prompts["monster_turn_prompt"].strip("\n")
        else:
            resp += self.prompts["player_turn_prompt"].strip("\n")
        return resp

    def next_encounter_turn(self) -> tuple[str, bool]:
        players_left, monsters_left = self.get_players_monsters_left()
        if players_left == 0 or monsters_left == 0:
            return self.end_encounter()
        if self.cur_encounter["turn"] == "players":
            self.cur_encounter["turn"] = "monsters"
            if self.agent.logging:
                print("\n\nNOW MONSTERS TURN...\n\n")
        else:
            self.cur_encounter["turn"] = "players"
            self.cur_encounter["round"] += 1
            if self.agent.logging:
                print("\n\nNOW PLAYERS TURN...\n\n")
        return (self.describe_encounter_turn(), False)

    def check_encounter_can_move(self, move: str, attacker: dict[str, any]) -> tuple[str, bool]:
        if self.cur_game_state_name == "encounter":
            attacker_name = attacker["name"]
            if self.has_attacker_moved(attacker):
                return (f"'{move}' FAILED - '{attacker_name} already moved this round", True)
            reason_cant_move, can_move = Game.can_do_actions(attacker)
            if not can_move:
                return (f"'{move}' FAILED - '{attacker_name}' {reason_cant_move}", True)
            if Game.has_escaped(attacker):
                return (f"'{move}' FAILED - '{attacker_name}' has escaped", True)
        return ( "ok", True )

    def has_attacker_moved(self,  attacker: dict[str, any]) -> bool:
        return attacker["encounter"]["moved_round"] == self.cur_encounter["round"]

    def mark_encounter_moved(self, attacker: dict[str, any]) -> None:
        if self.cur_game_state_name == "encounter":
            attacker["encounter"]["moved_round"] = self.cur_encounter["round"]

    def check_encounter_next_turn(self, resp: str) -> str:
        if self.cur_game_state_name == "encounter":
            _, left = self.get_attackers_left_to_go()
            if left == 0:
                turn_resp, err = self.next_encounter_turn()
                if err:
                    return (turn_resp, err)
                return (resp + "\n" + turn_resp + "\n", False)
            else:
                return (resp, False)
        else:
            return (resp, False)

    def attack_move(self, move: str, attacker_name: str, target_name: str) -> tuple[str, bool]:
        resp = ""

        if self.cur_game_state_name != "encounter" or self.cur_encounter is None:
            return ("'{move}' FAILED - not in 'encounter' game state", True)
        
        if move not in [ "attack", "press", "shoot", "advance", "retreat", "charge", "flee", "skip" ]:
            return (f"'{move}' FAILED - not a valid encounter action", True)

        # Get the attacker. Make sure it's the player's turn if the attacker is a player.
        attacker, attacker_side = self.get_encounter_being(attacker_name)
        if attacker is None:
            return (f"attacker '{attacker_name}' not found", True)
        if attacker_side != self.cur_encounter["turn"]:
            if attacker_side == "players":
                # A player attack FORCES the end of monsters turn (if if they haven't all gone)
                err_str, err = self.next_encounter_turn()
                if err:
                    return err_str
            else:
                return (f"{attacker_name} can't move because it is currently {self.cur_encounter['turn']} turn ", True)

        # Can the attacker do anything (paralyzed, asleep, dead?)
        can_move_msg, can_move = self.check_encounter_can_move(move, attacker)
        if not can_move:
            return (can_move_msg, True)

        # Get the attack target
        target = None        
        if move in [ "attack", "press", "shoot" ]:
            target, _ = self.get_encounter_being(target_name)
            if target is None:
                return (f"'{move}' FAILED - target '{target_name}' not found", True)
            if Game.is_dead(target):
                return (f"'{move}' FAILED - target '{target_name}' is dead", True)
            if Game.has_escaped(target):
                return (f"'{move}' FAILED - target '{target_name}' has escaped", True)

        # Do the move
        if move in [ "attack", "press", "shoot" ]:
            attack_type = ("ranged" if move == "shoot" else "melee")
            ability_name = ("Melee Combat" if attack_type == "melee" else "Ranged Combat")
            if not Game.can_attack(attacker, attack_type):
                return (f"'{move}' FAILED - '{attacker_name}' doesn't have a {attack_type} attack", True)
            range = Game.get_range_dist(attacker, target)
            if move == "press" and range != 0:
                return (f"'{move}' FAILED - {attacker_name}' is too far away for 'press' attack", True)
            if move == "attack":
                if range > 0 and range <= 30:
                    self.range_band_move(attacker_name, attacker, 1)
                elif range > 0:
                    return (f"'{move}' FAILED - '{attacker_name}' is {range}ft away from '{target_name}' - too far to 'attack'", True)
            weapon = self.get_merged_equipped_weapon(attacker, attack_type)
            if weapon is None:
                return (f"'{move}' FAILED - '{attacker_name}' does not have a {attack_type}", True)
            _, attack_mod_die, attack_adv_dis = Game.get_skill_ability_modifier(attacker, ability_name)
            roll = Game.die_roll("d20", attack_adv_dis)
            attack_mod_roll = Game.die_roll(attack_mod_die)
            defense = cur_value(target, "stats.basic", "defense")
            total_attack = roll + attack_mod_roll
            ability_mod_str = ""
            if not Game.is_monster(attacker):
                ability_mod_str = f", add {ability_name} roll of +{attack_mod_roll} gives attack {total_attack}"
            resp += f'{attacker_name} "{move}" - rolled {roll}{ability_mod_str} vs defense {defense}..'
            if total_attack >= defense:
                damage_die = weapon["damage"]
                damage = Game.die_roll(damage_die)
                cur_health = max(0, Game.get_cur_health(target) - damage)
                resp += f" HIT! - dealing damage -{damage} leaving health {cur_health}"
                if cur_health == 0:
                    resp += f" {target_name} DIES!"
                self.set_cur_health(target, cur_health)
            else:
                resp += " MISS!"
            if self.agent.logging:
                print("    " + resp)
            self.mark_encounter_moved(attacker)
            return (resp, False)

        elif move in [ "advance", "charge", "retreat", "flee" ]:
            err = False
            move_bands = 1
            if move in [ "charge", "flee" ]:
                move_bands = 2
            if move in [ "retreat", "flee" ]:
                move_bands = -move_bands
            _, _, moved_desc = self.range_band_move(attacker_name, attacker, move_bands)
            resp += "  " + moved_desc
            if self.agent.logging:
                print("  " + resp)
            self.mark_encounter_moved(attacker)
            return (resp, False)

        elif move == "skip":
            err = False
            resp = f"'{attacker_name}' skips this turn"
            self.mark_encounter_moved(attacker)
            return (resp, False)

    def get_response_encounter_end(self) -> str:
        players_left, monsters_left = self.get_players_monsters_left()
        if players_left == 0 or monsters_left == 0:
            resp, _ = self.check_encounter_next_turn("")
            return resp
        left_resp, left = self.get_attackers_left_to_go()
        if left != 0:
            return "\n  " + left_resp + "\n"
        if self.cur_encounter["turn"] == "monsters":
            resp, _ = self.check_encounter_next_turn("")
            return resp
        return "\nAll players have gone. NOW MONSTERS TURN!\n"

    def get_response_prefix_encounter(self) -> str:
        if self.skip_turn:
            return ""
        # Write results header if this is the first action in this response so AI can figure out
        # what's going on
        if self.cur_encounter["turn"] == "players":
            return "PLAYER TURN RESULTS:\n\n"
        if self.cur_encounter["turn"] == "monsters":
            return "MONSTER TURN RESULTS:\n\n"
        return ""
    
    def after_process_actions_encounter(self) -> str:
        if self.skip_turn:
            return ""
        # Maker sure we go to the next turn for players after monsters turn
        if self.cur_encounter["turn"] == "monsters":
            resp, _ = self.next_encounter_turn()
            return resp
        return ""    
        
    def get_addl_response_encounter(self) -> str:
        if self.skip_turn:
            return ""
        if self.cur_encounter["turn"] == "players":
            resp, _ = self.check_encounter_next_turn("")
            return resp
        return ""

    async def do_encounter_action(self, action: any, subject: any, object: any, extra: any, extra2: any) -> tuple[str, bool]:
        resp = ""
        error = False

        if action in [ "charge", "flee", "advance", "retreat", "attack", "press", "block", "shoot", "skip" ]:
            resp, error = self.attack_move(action, subject, object)
        else:   
            match action:
                case _:
                    resp = f"can't do action '{action}'"
                    error = True
        return (resp, error)

    def check_for_buttons_encounter(self) -> str:
        if self.cur_encounter["turn"] == "players":
            return "encounter_buttons"
        return None

    async def get_encounter_buttons(self, state: dict[str, any]) -> tuple[str|None, bool]:
        next_state = "chars"
        if "action" not in state:
            state.update({ "action": None, 
                           "subject": None, 
                           "object": None, 
                           "extra": None, 
                           "extra2": None, 
                           "choices": "",
                           "sentence": "" })
        clicked_index = state.get("clicked_index", -1)
        if clicked_index >= 0:
            choice: str = state["buttons"][clicked_index]["choice"]
            next_state = state["buttons"][clicked_index]["next_state"]
            if choice and not choice.startswith("@"):
                button: dict[str, any] = state["buttons"][clicked_index]
                choice_type = button["choice_type"]
                state[choice_type] = button["choice"]
                if button["phrase"]:
                    state["sentence"] += button["phrase"]
        state["buttons"] = []
        match next_state:
            case "chars":
                for char_name in self.cur_encounter["characters"].keys():
                    char = self.get_object(char_name)
                    # Check if paralyzed, dead, etc.
                    _, can_do = Game.can_do_actions(char)
                    if not can_do:
                        continue
                    if Game.has_escaped(char):
                        continue
                    if self.has_attacker_moved(char):
                        continue
                    button = { "text": char_name,
                               "choice": char_name, 
                               "choice_type": "subject",
                               "phrase": char_name + " ", 
                               "next_state": "actions" }
                    state["buttons"].append(button)
                if len(state["buttons"]) == 0:
                    return (None, False)
                return ("", True)
            case "actions":
                char = self.get_object(choice)
                if not char:
                    return ("", False)
                state["subject"] = choice
                state["char"] = char
                actions = [ ("attack", "Attack", "attack ", "monster_targets"), 
                            ("shoot", "Shoot", "shoot ", "monster_targets"), 
                            ("cast", "Cast", "casts ", "spells"), 
                            ("@move", "Move", "", "move_actions") ]
                for action, action_name, phrase, next_state in actions:
                    button = {}
                    if action == "attack" and not Game.can_attack(char, "melee"):
                        continue
                    if action == "shoot" and not Game.can_attack(char, "ranged"):
                        continue
                    if action == "cast" and len(char["equipped"].get("spells", [])) == 0:
                        continue
                    weapon_name = ""
                    if action == "attack":
                        weapon_name = char["equipped"]["melee_weapon"]
                    if action == "shoot":
                        weapon_name = char["equipped"]["ranged_weapon"]
                    button_text = (action_name if weapon_name == "" else f"{action_name} {weapon_name}")
                    button_phrase = (phrase if weapon_name == "" else f"uses {weapon_name} to {phrase}")
                    button = { "text": button_text,
                               "choice": action,
                               "choice_type": "action",
                               "phrase": button_phrase,
                               "next_state": next_state }
                    state["buttons"].append(button)
                return ("", True)
            case "move_actions":
                actions = [ ("advance", "Advance", "advances", "done"), 
                            ("retreat", "Retreat", "retreats", "done"), 
                            ("charge", "Charge", "charges", "done"), 
                            ("flee", "Flee", "flees", "done") ]
                for action, action_name, phrase, next_state in actions:
                    button = { "text": action_name, 
                               "choice": action, 
                               "choice_type": "action", 
                               "phrase": phrase, 
                               "next_state": next_state }
                    state["buttons"].append(button)
                return ("", True)
            case "spells":
                state["action"] = "cast"
                char = state["char"]
                for spell_name in char["equipped"].get("spells", []):
                    spell = self.rules["spells"][spell_name]
                    button_next_state = ""
                    if spell["type"] == "offensive":
                        button_next_state = "monster_targets"
                    elif spell["type"] == "defensive":
                        button_next_state = "player_targets"
                    else:
                        button_next_state = "done"
                    button = { "text": spell_name, 
                               "choice": spell_name, 
                               "choice_type": "object", 
                               "phrase": spell_name + " on ",
                               "next_state": button_next_state }
                    state["buttons"].append(button)
                return ("", True)
            case "monster_targets":
                choice_type = ("object" if state["object"] is None else "extra")
                for monster_name, monster_unique_name in self.cur_encounter["monsters"].items():
                    monster = self.get_object(monster_unique_name)
                    # Check if escaped, dead.
                    if Game.is_dead(monster) or Game.has_escaped(monster):
                        continue
                    button = { "text": monster_name,
                               "choice": monster_name,
                               "choice_type": choice_type,
                               "phrase": monster_name,
                               "next_state": "done" }
                    state["buttons"].append(button)
                return ("", True)
            case "player_targets":
                choice_type = ("object" if state["object"] is None else "extra")
                for char_name in self.cur_encounter["characters"].keys():
                    char = self.get_object(char_name)
                    # Check if escaped, dead.
                    if Game.is_dead(char) or Game.has_escaped(char):
                        continue
                    button = { "text": char_name,
                               "choice": char_name,
                               "choice_type": choice_type,
                               "phrase": char_name,
                               "next_state": "done" }
                    state["buttons"].append(button)
                return ("", True)            
            case "done":
                state["sentence"] += "."
                return ("done", False)
        return (None, False)

    # NEXT TURN ----------------------------------------------------------

    def get_response_start(self) -> str:
        return ""
    
    def get_response_end(self) -> str:
        match self.cur_game_state_name:
            case "encounter":
                return self.get_response_encounter_end()
            case _:
                return ""

    # Called to add any prefix to action results response
    def get_response_prefix(self) -> str:
        match self.cur_game_state_name:
            case "encounter":
                resp = self.get_response_prefix_encounter()
            case _:
                resp = ""
        return resp
    
    # Called after all actions have been called to update game state
    def after_process_actions(self) -> str:
        match self.cur_game_state_name:
            case "encounter":
                resp = self.after_process_actions_encounter()
            case _:
                resp = ""
        return resp    

    # Response that causes AI to do additional do_action() processing
    def get_addl_response(self) -> str:
        match self.cur_game_state_name:
            case "encounter":
                resp = self.get_addl_response_encounter()
            case _:
                resp = ""
        return resp
    
    def check_for_buttons(self) -> str:
        match self.cur_game_state_name:
            case "encounter":
                resp = self.check_for_buttons_encounter()
            case _:
                resp = "exploration_buttons"
        return resp
    
    async def get_buttons(self, button_tag: str, state: dict[str, any]) -> tuple[str|None, bool]:
        if button_tag == "encounter_buttons":
            return await self.get_encounter_buttons(state)
        elif button_tag == "exploration_buttons":
            return await self.get_exploration_buttons(state)
        return ("", False)

    async def do_action(self, action: any, 
                  subject: any = None, 
                  object: any = None, 
                  extra: any = None, 
                  extra2: any = None) -> tuple[str, bool]:
        
        resp = ""
        error = False

        if self.agent.logging:
            print(f"  ACTION: {action} {subject} {object} {extra}")
        
        if self.game_over and action != "restart":
            return "Players lost and game is over - players must ask AI to \"restart\" the game or return to lobby."

        if action == "exit" or action == "quit":
            action = "lobby"

        use_verb = None
        if self.cur_game_state_name != "encounter" and action == "press":
            use_verb = "press"  # Press is also an encounter action!
            action = "use"
        elif action in ("light", "extinguish", "eat", "drink", "open", "close", "push", "pull", 
                        "activate", "lock", "unlock"):
            use_verb = action
            action = "use"

        match action:
            case "cast":
                args = [ subject, object, extra, extra2 ]
                resp, error = self.cast(args)
            case "describe":
                resp, error = self.look(subject, object)
            case "equip":
                resp, error = self.equip(subject, object)
            case "help":
                resp, error = self.help(subject)
            case "lobby":
                resp, error = self.lobby()
            case "party":
                resp, error = self.describe_party()
            case "topic":
                resp, error = self.topic(subject, object)
            case "give":
                resp, error = self.give(subject, object, extra, extra2)
            case "look":
                resp, error = self.look(subject, object)
            case "resume":
                resp, error = self.resume()
            case "stats":
                resp, error = self.stats(subject)
            case "use":
                args = [ subject, object, extra, extra2 ]
                resp, error = self.use(args, use_verb)
            case _:
                match self.cur_game_state_name:
                    case "exploration":
                        resp, error = await self.do_explore_action(action, subject, object, extra, extra2)
                    case "encounter":
                        resp, error = await self.do_encounter_action(action, subject, object, extra, extra2)
                    case _:
                        resp = f"unknown game state {self.cur_game_state_name}'"
                        self.cur_game_state_name = "exploration"
                        error = True    

        if error:
            if self.agent.logging:
                print(f"  ERROR: {resp}")
            return (resp, True)
        
        # Note, we don't wait for it to finish saving
        if not self.skip_turn:
            await self.save_game()

        return (resp, False)

    async def timer_update(self) -> str:
        match self.cur_game_state_name:
            case "exploration":
                resp = await self.update_exploration()
            case "encounter":
                # resp = await self.update_encounter()
                resp = ""
            case _:
                resp = ""
        return resp
        
    # SPLIT DIALOG ----------------------------------------------------------

    def split_dialog(self, resp) -> list[str]:
        paras = resp.split("\n\n")
        prev_para = ""
        char_names: list[str|bytes] = []
        char_names += self.get_merged_npcs()
        char_names += list(self.game_state["characters"].keys())        
        out_paras: list[str] = []
        last_char = ""
        for para in paras:
            found_image = False
            if '"' in para:
                if prev_para:
                    out_paras.append(prev_para)
                    prev_para = ""
                noquote_para = re.sub(r'("[^"]*")', "", para)
                found_char = ""
                min_pos = 1000
                for char_name in char_names:
                    pos = noquote_para.find(char_name)
                    if pos >= 0 and pos < min_pos:
                        min_pos = pos
                        found_char = char_name
                if not found_char:
                    found_char = last_char
                if found_char:
                    last_char = found_char
                    if found_char in self.game_state["characters"]:
                        char = self.game_state["characters"][found_char]
                        full_name = char["info"]["basic"]["full_name"]
                        image_path = check_for_image("data/characters/images", full_name)
                    else:
                        image_path = check_for_image(self.module_path + "/images", found_char)
                    if image_path:
                        found_image = True
                        is_quote = para.startswith('"')
                        parts = para.split('"')
                        for part in parts:
                            part = part.strip(" ")
                            if not part:
                                continue
                            if is_quote:
                                out_paras.append(draw_dialog_image(image_path, found_char, part))
                                is_quote = False
                            else:
                                out_paras.append(part)
                                is_quote = True
            if not found_image:
                if prev_para:
                    prev_para += "\n\n" + para
                else:
                    prev_para = para
        if prev_para:
            out_paras.append(prev_para)
        return out_paras

