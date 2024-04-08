# Chatbot implementation for 

import re

from agent import Agent
from agents.agent_openai_v1 import OpenAIAgentV1
from game import Game, ChatGameDriver
from games.hoa.game_hoa import GameHoa, Obj
from db_access import Db
from games.hoa.engine_hoa import EngineHoa
from engine import Engine, EngineManager
from user import User
from datetime import datetime, timedelta
import os.path as path
from utils import check_for_image, extract_arguments
from drawing import draw_dialog_image, draw_card_image
from typing import Any, cast

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

class GameHoaOpenAIV1(ChatGameDriver):

    def __init__(self, 
                 engine: Engine, 
                 user: User, 
                 agent: Agent,
                 start_game_action: str = "new_game",
                 module_name: str = "", 
                 party_name: str = "",
                 save_game_name: str = "") -> None:
        self._engine: EngineHoa = cast(EngineHoa, engine)
        self.user: User = user
        self._agent: OpenAIAgentV1 = cast(OpenAIAgentV1, agent)
        self.response_id: int = 1
        self._game: GameHoa = GameHoa(cast(EngineHoa, engine), 
                                      user, 
                                      start_game_action=start_game_action, 
                                      module_name=module_name, 
                                      party_name = party_name, 
                                      save_game_name = save_game_name)
        self.prompts: dict[str, Any] = {}
        self.exploration_prefix: list[Any] = []
        self.encounter_prefix: list[Any] = []
        self.response_prefix: list[Any] = []
        self._button_tag: str | None = None
        self.messages = [ self._agent.make_message("assistant", "I'm Ready!", "referee", True) ]

    @property
    def agent(self) -> Agent:
        return self._agent

    @property
    def game(self) -> Game:
        return self._game
    
    @property
    def action_image_path(self) -> str | None:
        return self._game.action_image_path
    
    @action_image_path.setter
    def action_image_path(self, v: str | None) -> None:
        self._game.action_image_path = v

    @property
    def button_tag(self) -> str | None:
        return self._button_tag
    
    @button_tag.setter
    def button_tag(self, v: str | None) -> None:
        self._button_tag = v


    @property
    def exit_to_lobby(self) -> bool:
        return self._game.exit_to_lobby

    @exit_to_lobby.setter
    def exit_to_lobby(self, value: bool) -> None:
        self._game.exit_to_lobby = value

    @property
    def is_started(self) -> bool:
        return self._game.is_started

    @property
    def game_over(self) -> bool:
        return self._game.game_over

    @property
    def characters(self) -> dict[str, Obj]:
        return self.game.characters 

    @property
    def areas(self) -> dict[str, Obj]:
        return self.game.areas

    @property
    def locations(self) -> dict[str, Obj]:
        return self.game.locations

    @property
    def monster_types(self) -> dict[str, Obj]:
        return self.game.monsters

    @property
    def module_monster_types(self) -> dict[str, Obj]:
        return self.game.module_monster_types

    @property
    def monsters(self) -> dict[str, Obj]:
        return self.game.monsters

    @property
    def player_map(self) -> dict[str, list[str]]:
        return self.game.player_map

    async def start_game(self) -> str:
        if self._game.is_started:
            return "This game has already been started."
        await self._game.start_game()
        if len(self.prompts) == 0:
            self.prompts = self._engine.game_prompts
            # Prefix messages we prepend to our message stream which has the rules/instructions for various modes
            self.exploration_prefix = [self._agent.make_message("user", self.prompts["exploration_prompt"], "prefix", keep=True)]
            self.encounter_prefix = [self._agent.make_message("user", self.prompts["encounter_prompt"], "prefix", keep=True)]
            self.response_prefix = [self._agent.make_message("user", self.prompts["response_prompt"], "prefix", keep=True)]
        resume_prompt = self._agent.make_prompt(self.prompts["resume_game_prompt"], self._game.module["info"])
        return await self.system_action(resume_prompt, \
                           'call do_action("resume")', \
                           'AI Referee, you must use [call do_action("resume")] to start the game. Please try again.\n')

    # A per user response hint to focus the AI (not usually used unless AI has trouble)
    def append_query_hint(self, query) -> str:
        hint = self._game.get_query_hint()
        if hint is None:
            return query
        return query + "\n\n" + hint

   # Hand parse some simple actions so they don't round trip to the AI.
    async def parse_simple_action(self, query: str) -> str:
        result = self._game.parse_simple_action(query)
        if result is None:
            return ""
        return await self.call_actions(query, result)

    def filter_messages(self, sources: list[str], max_msgs: int = -1) -> list[dict[str, Any]]:
        filtered_msgs: list[dict[str, Any]] = []
        for msg in self.messages:
            if msg["source"] in sources:
                filtered_msgs.append(msg)
        if max_msgs != -1:
            filtered_msgs = filtered_msgs[-max_msgs:]
        return filtered_msgs

    def is_redundant_phrase(self, resp: str) -> bool:
        # These are so annoying we look for them and just cut them from the conversation.
        r_phrases = [ "So, what will you do",
                      "How do you wish to proceed", 
                      "What would you like to say",
                      "What would you like to share",
                      "What do you want to say",
                      "What do you want to share",
                      "What will you do",
                      "What do you want to do",
                      "What do you do next",
                      "What do you wish to do next", 
                      "How do you respond", 
                      "How do you choose to",
                      "As the AI Referee I will",
                      "As the AI Referee, I will",
                      "Will you be able to",
                      "Remember, you can say",
                      "Now it's your turn to respond",
                      "Now, I leave you to ponder your next move",
                      "Now, let the adventure begin" ]
        for r_phrase in r_phrases:
            if resp.startswith(r_phrase):
                return True
        return False

    def cut_max_paras(self, resp: str) -> str:
        if self._game.cur_location is None:
            return resp
        max_paras = self._game.cur_response_max_para
        if max_paras == 0:
            return resp
        return "\n\n".join(resp.split("\n\n")[0:max_paras])

    async def generate(self, instr_query: str, query: str, mode: str, source: str, 
                       primary: bool = True, keep: bool = False, chunk_handler: Any = None) -> str:
        query_msg = self._agent.make_message("user", query, source, keep=keep)
        if not instr_query:
            instr_query = query
        if instr_query != query:
            instr_msg = self._agent.make_message("user", instr_query, source, keep=keep)
        else:
            instr_msg = query_msg
        resp_msg: dict[str, Any] = {}
        match mode:
            case "exploration_action" | "encounter_action":
                prefix = (self.exploration_prefix if mode == "exploration_action" else self.encounter_prefix)
                # We get the whole msg stack for the "actioner" query (actioner and engine responses).
                msgs = prefix + \
                    self.filter_messages(["player", "actioner", "engine", "referee"]) + \
                    [ instr_msg ]
                resp = await self._agent.generate(msgs, primary, chunk_handler=chunk_handler)
                resp_msg = self._agent.make_message("assistant", resp, "actioner", keep=False)
            case "engine_response" | "referee_response":
                # For the user friendly "referee" response we only need player/referee msgs.
                msgs = self.response_prefix + \
                    self.filter_messages(["player", "referee"]) + \
                    [ instr_msg ]
                # Don't use GPT-4 for big responses
                if instr_msg["tokens"] > 200:
                    primary = True
                resp = await self._agent.generate(msgs, primary, chunk_handler=chunk_handler)
                resp = self.cut_max_paras(resp)
                resp_msg = self._agent.make_message("assistant", resp, "referee", keep=False)
            case "dialog_choices":
                prefix = self.response_prefix
                # For dialog choices we only need player/referee messages.
                msgs = prefix + \
                    self.filter_messages(["player", "referee"]) + \
                    [ instr_msg ]
                resp = await self._agent.generate(msgs, primary, chunk_handler=chunk_handler)
                resp_msg = self._agent.make_message("assistant", resp, "dialogee", keep=False)
        self.messages.append(query_msg)
        self.messages.append(resp_msg)
        return resp_msg["content"]
 
    async def system_action(self, 
                            query: str, 
                            expected_action: str|None = None, 
                            retry_msg: str|None = None, 
                            chunk_handler: Any = None) -> str:
        # Handle case where we check for an expected action, but AI is sending the slash
        # action with the underscore.
        if expected_action:
            query = query.replace("do\\_action(", "do_action(")
        return await self.process_action("system", query, 
                                         expected_action, retry_msg, 
                                         chunk_handler=chunk_handler)

    async def player_action(self, query: str, chunk_handler: Any = None) -> str:
        return await self.process_action("player", query, chunk_handler=chunk_handler)

    async def call_actions(self, query: str, actions: list[tuple[str, list[str]]]) -> str:
        response = "<HIDDEN>"
        for action_args in actions:
            (action, args) = action_args
            arg_str = make_arg_str(action, args)
            response = f"{response}\ncall do_action({arg_str})\n"
        cmd_msg = self._agent.make_message("assistant", response, "actioner", keep=False)
        self.messages.append(cmd_msg)
        if EngineManager.logging:
            print(cmd_msg["content"])
        resp = await self.process_response(query, response, level=1)
        return self.post_action_update("", resp)

    async def process_action(self, 
                             source: str, 
                             query: str, 
                             expected_action: str|None = None, 
                             retry_msg: str|None = None, 
                             chunk_handler: Any = None) -> str:
        self._game.action_image_path = None
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
                if self._game.cur_location_script and "hint" in self._game.cur_location_script:
                    query = self.append_query_hint(query)
                action_instr = "<PLAYER>\n" + query + "\n\n" + self.prompts["action_instr_prompt"] + "\n"
            action_mode = self._game.cur_game_state_name + "_action"
            source = "engine" if is_system else "player"
            resp = await self.generate(action_instr, query, action_mode, source, primary=True, keep=False)
            if expected_action is not None and expected_action not in resp:
                assert retry_msg is not None
                while expected_action not in resp:
                    resp = await self.generate("", retry_msg, action_mode, "system", primary=True, keep=False)
            processed_resp = await self.process_response(query, resp, level=1, chunk_handler=chunk_handler)
            resp = self.post_action_update(query, processed_resp, is_system=is_system)
        # Always set the next max para after we generate the respone. This allows the first response for a location
        # to be longer than the limit.
        self._game.cur_response_max_para = self._game.next_response_max_para
        return resp

    def post_action_update(self, query: str, resp: str, is_system: bool = False) -> str:
        # Check to see if we have any contextual images
        if self._game.action_image_path is None and not is_system:
            self._game.action_image_path = self._game.get_contextual_image(query, resp)
        # Process the final response
        if self._game.action_image_path is not None:
            resp = resp + "\n" + "@image: " + self._game.action_image_path
            self._game.action_image_path = None
        # Check for Any buttons to show
        self._button_tag = self._game.check_for_buttons()
        return resp

    async def process_response(self, query: str, response: str, level: int, chunk_handler: Any = None) -> str:
        if level == 4:
            return response.strip(" \n\t")
        query, results, num_calls = await self.process_game_actions(query, response)
        # Do we need to pass this to the AI for further processing? If not just return it.
        if num_calls == 1 and results.startswith("<RESULTS>\n"):
            results = results[10:]
        # Check for additional responses.
        if results != "" and not self._game.skip_turn:
            addl_resp = self._game.get_addl_response()
            if addl_resp:
                addl_resp_instr = results.strip("\n") + "\n\n" + addl_resp
                resp_list = addl_resp_instr.split("<INSTRUCTIONS>\n")
                has_instr = len(resp_list) > 1
                mode = (self._game.cur_game_state_name + "_action" if has_instr else "engine_response")
                addl_query = resp_list[0]
                ai_addl_resp = await self.generate(addl_resp_instr, addl_query, mode, "engine", primary=False)
                _, addl_results, _ = await self.process_game_actions("", ai_addl_resp)
                results = results.strip("\n") + "\n\n" + addl_results
        # Pass it to the AI.
        return await self.referee_response(query, results, level, chunk_handler=chunk_handler)

    async def process_game_actions(self, query: str, response: str) -> tuple[str, str, int]:
        lines = response.split("\n")
        prefix = self._game.get_response_prefix()
        results = ""
        num_calls = 0
        for line in lines:
            line = line.strip()
            line = line.replace("do\\_action", "do_action") # Some AI's have trouble with the _
            if "do_action(\"" in line:
                args = extract_arguments(line, 5)
                game_resp, _ = await self._game.do_action(args[0], args[1], args[2], args[3], args[4])
                results += game_resp + "\n"
                num_calls += 1
        if not self._game.skip_turn or num_calls > 1:
            results = prefix + results + self._game.after_process_actions()
        return (query, results, num_calls)

    async def referee_response(self, query: str, results: str, level: int, chunk_handler: Any = None) -> str: 
        if results.startswith("<RESULTS>\n"):
            return results[10:]
        if results != "":
            instr_query = "<RESPONSE>\n" + \
                        (self._game.get_current_game_state_str() + "\n\n" if not self._game.skip_turn else "") + \
                        self._game.get_response_start() + \
                        results + \
                        self._game.get_response_end()
            resp_list = instr_query.split("<INSTRUCTIONS>\n")
            # If there are Any instructions we use the action prefix for the game state so we get the state
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

    async def timer_update(self, dt: float) -> str:
        match self._game.cur_game_state_name:
            case "exploration":
                # resp = await self.update_exploration(dt)
                resp = ""
            case "encounter":
                # resp = await self.update_encounter(dt)
                resp = ""
            case _:
                resp = ""
        return resp
    
    # async def update_exploration(self, dt: float) -> str:
    #    resp = self._game.update_exploration()
    #    if resp is not None:
    #        return await self.referee_response("", resp, 1)
    #    return ""

    async def get_buttons(self, button_tag: str, state: dict[str, Any]) -> tuple[str|None, bool]:
        
        async def generate(prompt: str) -> str:
            return await self.generate("", prompt, "dialog_choices", "dialoger")

        return await self._game.get_buttons(button_tag, state, generate)

    # SPLIT DIALOG ----------------------------------------------------------

    def split_dialog(self, resp) -> list[str|bytes]:
        resp = resp.replace("\n\n", "\n")
        paras: list[str] = resp.split("\n")
        char_names: list[str|bytes] = []
        char_names += self._game.get_merged_npcs()
        char_names += list(self._game.game_state["characters"].keys())        
        out_paras: list[str|bytes] = []
        found_char = ""

        # Process all paras to add quote images. Note: empty paras will be added in to 
        # make sure para spacings are preserved.
        for para in paras:

            para = para.rstrip()
            if len(para) == 0:
                out_paras.append(para)
                continue

            # Let AI hide subsequent lines if it wants
            if para == "<HIDDEN>" or para == "<INSTRUCTIONS>":
                break

            # Ignore when AI gets confused and repeats internal tags
            if para.startswith("state: exploration ") or \
                    para.startswith("state: combat") or \
                    para == "<RESPONSE>":
                continue

            if para.startswith("@card"):
                card_type, card_subject = \
                    (match.groups() if (match := re.search(r'@card\("([^"]*)", "([^"]*)"\)', para)) else ("", ""))
                out_paras.append(draw_card_image(card_type, card_subject))

            # Some AI's add useless prefixes to lines
            if para.startswith("Description: ") or para.startswith("description: "):
                para = para[13:]

            # Find out who the speaking character is
            min_pos = 10000
            noquote_para = re.sub(r'("[^"]*")', "", para)
            for char_name in char_names:
                pos = noquote_para.find(str(char_name))
                if pos >= 0 and pos < min_pos:
                    prefix = ""
                    if pos > 4:
                        prefix = noquote_para[pos - 4: pos]
                    if prefix != " at " and prefix != " to ":
                        min_pos = pos
                        found_char = char_name

            # If no quotes in para.. continue
            if not found_char or '"' not in para:
                out_paras.append(para)
                continue

            # Find char and char portrait for quote
            if found_char in self._game.game_state["characters"]:
                char = self._game.game_state["characters"][found_char]
                full_name = char["info"]["basic"]["full_name"]
                image_path = check_for_image(f"{self._game.base_path}/characters/images", full_name)
            else:
                image_path = check_for_image(self._game.module_path + "/images", str(found_char)+"Small")
                if image_path is None:
                    image_path = check_for_image(self._game.module_path + "/images", str(found_char))

            # If we didn't find char image or char, continue
            if not image_path:
                out_paras.append(para)
                continue

            # Split the string and add quote portrait images for each quote
            parts = re.split(r'(\")', para)
            is_quote = False
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                if part == '"':
                    is_quote = not is_quote
                elif is_quote:
                    out_paras.append(draw_dialog_image(image_path, str(found_char), part))
                else:
                    out_paras.append(part)

        # Trim empty paras from beginning of list
        while len(out_paras) > 0 and not out_paras[0]:
            out_paras.pop(0)

        # Trim empty paras from end of list
        while len(out_paras) > 0 and not out_paras[-1]:
            out_paras.pop(0)

        # Concatenate string paraas back together in to final results
        final_paras: list[str|bytes] = []
        prev_para = ""
        cur_para = ""
        for para in out_paras:
            if isinstance(para, str):
                if cur_para:
                    # If previous line is reasonably short, and it's a :, bullet, number list, only one return.
                    if prev_para.endswith(":") or prev_para.startswith("-") or \
                            (len(prev_para) > 0 and prev_para[0].isnumeric()):
                        cur_para = cur_para + "\n" + para
                    else:
                        # Otherwise two returns
                        cur_para = cur_para + "\n\n" + para
                else:
                    cur_para = para
                prev_para = cur_para
            elif isinstance(para, bytes):
                if cur_para:
                    final_paras.append(cur_para)
                final_paras.append(para)
                cur_para = ""
                prev_para = ""
        if cur_para:
            final_paras.append(cur_para)

        return final_paras

    @property
    def action_list(self) -> list[dict[str, Any]]:
        return self._game.action_list

    def clear_action_list(self) -> None:
        self._game.clear_action_list()
