from agent import Agent
import copy
from datetime import datetime, timedelta
import json
import os
import os.path as path
import random
import yaml
import pydash

def find_case_insensitive(dic: dict, key: str) -> tuple[str, any]:
    # Unique name will always be the right case (i.e "Dagger#1001")
    value = dic.get(key)
    if value is not None:
        return (key, value)
    # Search the dictionary linearly for non unique name. Sometimes this 
    # may not match case as the AI sometimes doesn't get casing right.
    lower_key = key.casefold()
    for k, v in dic.items():
        # We use the "name" prop if it has one, otherwise use k
        if isinstance(v, dict):
            name = v.get("name") or k
        else:
            name = k
        if name.casefold() == lower_key:
            # Always return the key as the name
            return (k, v)
    return (None, None)

def any_to_int(val: any) -> tuple[int, bool]:
    if isinstance(val, int):
        return (val, False)
    if not isinstance(val, str):
        return (0, True)
    s = val.strip()
    p = len(s) - 1
    while p >= 0:
        if s[p].isdigit():
            return (int(s[:p + 1]), False)
    return (0, True)

def parse_date_time(time_str: str) -> datetime:
    return datetime.strptime(time_str, "%b %d %Y %H:%M")   

def time_difference_mins(time1_str, time2_str) -> int:
    delta = parse_date_time(time1_str) - parse_date_time(time2_str)
    return int(delta.total_seconds() / 60)

def escape_path_key(key: str) -> str:
    key = key.replace("\\", r"\\")
    key = key.replace(".", r"\.")
    return key

def cur_value(obj: dict[str, any], path: str, value: str) -> any:
    return pydash.get(obj, path + ".cur_" + value) or pydash.get(obj, path + "." + value)

class Game():
    def __init__(self, agent: Agent, module_name: str, party_name: str, save_game_name: str) -> None:
        self.module_name = module_name
        self.agent: Agent = agent
        self.game_started = False
        self.cur_location: dict[str, any] = {}
        self.cur_location_state: dict[str, any] = {}
        self.cur_location_script: dict[str, any] | None = None
        self.cur_encounter: dict[str, any] | None = None
        self.party_name = party_name
        with open(f"{self.parties_path}/party.yaml", "r") as f:
            self.party = yaml.load(f, Loader=yaml.FullLoader)
        with open(f"{self.module_path}/module.yaml", "r") as f:
            self.module = yaml.load(f, Loader=yaml.FullLoader)
        with open(f"{self.rules_path}/rules.yaml", "r") as f:
            self.rules = yaml.load(f, Loader=yaml.FullLoader)
        self.help_index = {}
        self.init_help_index()
        self.save_game_name = save_game_name
        self.game_state: dict[str, any] = {}
        self.action_image_path: str | None = None
        self.response_id = 1
        self.player_results_id = 0
        self.monster_results_id = 0
        self.load_game()

    @property
    def is_started(self) -> bool:
        return self.game_started

    def start_game(self) -> str:
        self.game_started = True        
        resp = self.agent.generate(self.rules["starting_prompt"], primary=True, keep=True)
        resume_prompt = self.agent.make_prompt(self.rules["resume_game_prompt"], self.module["info"])
        return self.system_action(resume_prompt, \
                           'call next_turn("resume")', \
                           'AI Referee, you must use [call next_turn("resume")] to start the game. Please try again.\n')

    # A per user response hint to focus the AI (not usually used unless AI has trouble)
    def add_query_hint(self, query) -> str:
        return "PLAYERS RESPONSE:\n\n" + \
            query + "\n\n" + \
            self.rules["instructions_prompt"] + "\n\n" + \
            self.cur_location_script["hint"] + "\n\n"

    def system_action(self, query: str, expected_action: str = None, retry_msg: str = None) -> str:
        return self.process_action(True, query, expected_action, retry_msg)

    def user_action(self, query: str) -> str:
        return self.process_action(False, query)
            
    def process_action(self, is_system: bool, query: str, expected_action: str = None, retry_msg: str = None) -> str:
        self.action_image_path = None
        if not is_system and self.cur_location_script and "hint" in self.cur_location_script:
            query = self.add_query_hint(query)
        resp = self.agent.generate(query, primary=True)
        if not is_system:
            trans_action = self.evaluate_transitions()
            if trans_action is not None:
                resp = resp + trans_action
        if expected_action is not None and expected_action not in resp:
            while expected_action not in resp:
                resp = self.agent.generate(retry_msg, primary=True)
        processed_resp = self.process_response(resp, 1)
        if not is_system:
            self.inc_cur_time(self.turn_period)
        if self.action_image_path is not None:
            processed_resp = processed_resp + "\n" + "@image: " + self.action_image_path
            self.action_image_path = None
        return processed_resp

    def process_response(self, response: str, level: int) -> str:
        if level == 3:
            return response.strip(" \n\t")
        lines = response.split("\n")
        ai_next_turn_resp = ""
        results = ""
        for line in lines:
            line = line.replace("<HIDDEN>", "").strip()
            if line.startswith("call next_turn("):
                exec_text = line.replace("call next_turn(", "game_resp = self.next_turn(")
                loc = { 'self': self, 'game_resp': "" }
                exec(exec_text, globals(), loc)
                game_resp = loc["game_resp"]
                if "call next_turn(" in game_resp:
                    game_resp = self.process_response(game_resp, level + 1)
                results += game_resp + "\n"
        if results != "":
            ai_next_turn_resp = self.agent.generate("<RESPONSE>\n" + \
                                                    self.get_current_game_state_str() + "\n\n" + \
                                                    self.get_response_start() + \
                                                    results + \
                                                    self.get_response_end(), primary=False)
            self.response_id += 1
        if "call next_turn(" in ai_next_turn_resp:
            ai_next_turn_resp = self.process_response(ai_next_turn_resp, level + 1)
        vis_resp = self.agent.remove_hidden(response)
        turn_vis_resp = self.agent.remove_hidden(ai_next_turn_resp)
        if turn_vis_resp != "":
            resp = vis_resp + "\n\n" + turn_vis_resp
        else:
            resp = vis_resp
        resp = resp.strip(" \t\n")
        return resp

    def init_help_index(self) -> None:
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
            self.help_index[(magic_category_name + " Magic").lower()] = { "name": magic_category_name, "type": "magic_categories" }
        for equipment_name in self.rules["equipment"].keys():
            self.help_index[equipment_name.lower()] = { "name": equipment_name, "type": "equipment" }

    def init_game(self) -> None:
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

    def new_game(self) -> None:
        game_path = 'data/new_game_state.yaml'
        with open(game_path, 'r') as f:
            self.game_state = yaml.load(f, Loader=yaml.FullLoader)
            self.game_state["characters"] = copy.deepcopy(self.party["characters"])
            self.game_state["npcs"] = copy.deepcopy(self.module["npcs"])
            self.game_state["monsters"] = copy.deepcopy(self.module["monsters"])
            self.game_state["state"]["last_effect_uid"] = 1000
            self.game_state["state"]["last_object_uid"] = 1000
            self.cur_game_state_name = self.module["starting_game_state"]
            self.cur_location_name = None
            self.cur_time = self.module["starting_time"]
        for loc_name, loc in self.module["locations"].items():
            self.location_states[loc_name] = copy.deepcopy(loc.get("state", {}))
        self.init_game()
        self.save_game()

    def load_game(self) -> None:
        game_path = f'save_games/{self.module_name}/{self.save_game_name}.yaml'
        if path.exists(game_path):
            with open(game_path, 'r') as f:
                self.game_state = yaml.load(f, Loader=yaml.FullLoader)
            self.init_game()
        else:
            self.new_game()

    def save_game(self) -> None:
        module_save_path = f'save_games/{self.module_name}'
        game_save_path = f'save_games/{self.module_name}/{self.save_game_name}.yaml'       
        if not path.exists(module_save_path):
            os.makedirs(module_save_path)
        with open(game_save_path, 'w') as f:
            yaml.dump(self.game_state, f)

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
        return f"modules/{self.module_name}"

    @property
    def rules_path(self) -> str:
        return f"rules/{self.module['info']['game']}/{self.module['info']['game_version']}"

    @property
    def parties_path(self) -> str:
        return f"parties/{self.party_name}"

    @property
    def locations(self) -> dict[str, dict[str, any]]:
        return self.module["locations"]

    @property
    def location_states(self) -> dict[str, dict[str, any]]:
        return self.game_state["location_states"]

    @property
    def game_over(self) -> bool:
        return self.game_state.get("game_over", False)
    
    @game_over.setter
    def game_over(self, value: bool) -> None:
        self.game_state["state"]["game_over"] = value    

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
        image_path = self.check_for_image(self.rules_path + "/images", equipment_name, "equipment")
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
        self.cur_location_name = new_loc_name
        self.cur_location = new_loc
        self.cur_location_state = new_loc_state
        self.location_since = self.cur_time
        if self.cur_script_state != "":
            self.cur_location_script = self.cur_location["script"][self.cur_script_state]
        else:
            self.cur_location_script = None
        self.script_state_since = self.cur_time
        # Check for encounter at the new location
        if self.get_cur_location_encounter() is not None:
            self.start_encounter()
        else:
            random_encounter = self.check_random_encounter()
            if random_encounter is not None:
                self.start_encounter(random_encounter)

    def get_current_game_state_str(self) -> str:
        return f"state: {self.cur_game_state_name}, location: {self.cur_location_name}, time: {self.cur_date_time_12hr}" 

    @staticmethod
    def merge_topics(target: dict[str,str], from_topics: dict[str, str]) -> None:
         for npc_name, npc_topics in from_topics.items():
            target[npc_name] = target.get(npc_name, {})
            target[npc_name].update(npc_topics)

    def get_current_topics(self) -> dict[str, str]:
        all_topics = {}
        npcs = self.cur_location.get("npcs", [])
        npc_topics = {}
        for npc_name in npcs:
            npc = self.module["npcs"][npc_name]
            if "topics" in npc:
                npc_topics[npc_name] = npc["topics"]
        Game.merge_topics(all_topics, npc_topics)
        Game.merge_topics(all_topics, self.cur_location.get("topics", {}))
        if self.cur_location_script:
            Game.merge_topics(all_topics, self.cur_location_script.get("topics", {}))
        return all_topics

    def describe_topics(self) -> str:
        all_topics = self.get_current_topics()
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
        num_chars = len(self.characters)
        char_idx = random.randint(0, num_chars - 1)
        for char in self.characters.values():
            if char_idx == 0:
                return char
            char_idx -= 1
        return None

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
            return f'<HIDDEN>\ncall next_turn("next", "{next_state}")\n'
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
            image_path = self.module_path + "/" + self.cur_location_script["image"]
        elif "image" in self.cur_location:
            session_loc = self.session_state["locations"][self.cur_location_name] = self.session_state["locations"].get(self.cur_location_name, {})
            if if_first_time and session_loc.get("players_have_seen", False):
                return None
            session_loc["players_have_seen"] = True
            image_path = self.module_path + "/" + self.cur_location["image"]
        return image_path
    
    @staticmethod
    def check_for_image(base_path: str, name: str, type_name: str) -> str:
        exts = [ ".jpg", ".png" ]
        paths = [ f"/{type_name}", "" ]
        for ext in exts:
            for path in paths:
                image_path = base_path + path + f"/{name}{ext}"
                if os.path.exists(image_path):
                    return image_path
        return None

    def other_image(self, name, type_name) -> str:
        image_path = Game.check_for_image(self.module_path + "/images", name, type_name)
        if image_path is not None:
            return image_path
        image_path = Game.check_for_image(self.rules_path + "/images", name, type_name)
        if image_path is not None:
            return image_path
        image_path = Game.check_for_image(self.parties_path + "/images", name, type_name)
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
    def set_has_escaped(being_name: str, being: dict[str, any], escaped: bool) -> None:
        if Game.has_escaped(being) == escaped:
            return
        being["encounter"]["escaped"] = escaped

    @staticmethod
    def get_cur_health(being: dict[str, any]) -> int:
        basic_stats = being["stats"]["basic"]
        if "cur_health" not in basic_stats:
            basic_stats["cur_health"] = basic_stats["health"]
        return basic_stats["cur_health"]
    
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
    def can_do_actions(being: dict[str, any]) -> None:
        return not Game.is_dead(being)

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
        weapon = copy.deepcopy(attacker["items"][weapon_name])
        if "rules_item" in weapon:
            rules_weapon_name = weapon["rules_item"]
        else:
            rules_weapon_name = weapon_name
        rules_weapon = self.rules["equipment"][rules_weapon_name]
        weapon.update(rules_weapon)
        return weapon
    
    def get_merged_exits(self) -> dict[str, any]:
        # add base exits
        exits = self.cur_location.get("exits", {})
        # add any additional exits revealed in the current script state
        if self.cur_location_script and "exits" in self.cur_location_script:
            exits.update(self.cur_location_script["exits"])
        # add any additional exits added to the current location state (say by a search)
        if "exits" in self.cur_location_state:
            exits.update(self.cur_location_state["exits"])
        return exits
    
    # OBJECT MAP ----------------------------------------------------------
    
    @property
    def object_map(self) -> dict[str, any]:
        return self.game_state["object_map"]
    
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
        self.game_state["object_map"] = {}
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
        match effect_id:
            case "heal":
                die = effect_def["heal"]["die"]
                value = Game.die_roll(die)
                new_health = self.set_cur_health(target, self.get_cur_health(target) + value)
                max_health = target["stats"]["basic"]["health"]
                return (f"Heal {die} {value}, new health: {new_health} of {max_health}\n", False)
            case "damage":
                die = effect_def["damage"]["die"]
                value = Game.die_roll(die)
                new_health = self.set_cur_health(target, self.get_cur_health(target) - value)
                max_health = target["stats"]["basic"]["health"]
                return (f"Damage {die} {value}, new health: {new_health} of {max_health}\n", False)
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
        desc = f"{verb} {name}"
        if len(targets) > 0:
            desc += " on/with " + ", ".join([target.get("name", "") for target in targets])
        # Apply the action
        duration = effect_src.get("duration")       
        turns = effect_src.get("turns")
        check = effect_src.get("check")
        resp = desc + "\n"
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

    def describe_location(self) -> tuple[str, bool]:
        desc = "description: " + self.cur_location["description"].strip(" \n\t") + "\n\n"
        if self.cur_location_script is not None:
            desc += "\n" + self.cur_location_script["description"].strip(" \t\n") + "\n"
        instr = self.cur_location.get("instructions", "").strip(" \n\t")
        if self.cur_location_script is not None and "instructions" in self.cur_location_script:
            if instr != "":
                instr += "\n\n"
            instr += self.cur_location_script["instructions"].strip(" \n\t") + "\n"
        if instr != "":
            instr = self.rules["instructions_prompt"].strip(" \n\t") + "\n\n" + instr + "\n"
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
        self.action_image_path = self.location_image_path(if_first_time=True)
        all_npcs = self.cur_location.get("npcs", []) + \
            (self.cur_location_script.get("npcs", []) if self.cur_location_script is not None else [])
        npcs = ""
        if len(all_npcs) != 0:
            "npcs: " + ",".join(all_npcs) + " are here\n"
        # Make sure we've marked all NPCs as "known" by the players once they've seen them
        for npc_name in all_npcs:
            self.game_state["npcs"][npc_name]["has_player_met"] = True
        # If we're in encounter mode.. use an abbreviated location description with encounter insructions/rules
        encounter = self.describe_encounter()
        if encounter != "":
            resp = f"{desc}{npcs}{encounter}"
        else:
            resp = f"{desc}{changes}{instr}{exits}{items}{tasks}{npcs}{topics}"
        return (resp, False)

    def stats(self, being_name: str) -> tuple[str, bool]:
        being = self.get_nearby_being(being_name)
        if being is None:
            {f"{being_name}' is not nearby", True}
        being_name = Game.get_encounter_or_normal_name(being)
        resp = ""
        resp += f"Character: '{being_name}'\n"
        resp += "  stats - " + json.dumps(being["stats"]["basic"]).strip("{}").replace("\"", "") + "\n"
        resp += "  attributes - " + json.dumps(being["stats"]["attributes"]).strip("{}").replace("\"", "") + "\n"
        resp += "  skills - " + json.dumps(being["stats"]["skills"]).strip("{}").replace("\"", "") + "\n"
        resp += "  abilities - " + json.dumps(being["stats"]["abilities"]).strip("[]").replace("\"", "") + "\n"
        item_descs = Game.get_item_list_desc(being["items"])
        resp += "  inventory - " + json.dumps(item_descs).strip("[]").replace("\"", "") + "\n\n"
        return (resp, False)

    def describe_party(self) -> tuple[str, bool]:
        resp = ""
        for char_name in self.game_state["characters"].keys():
            stats_resp, _ = self.stats(char_name)
            resp += stats_resp
        return (resp, False)

    def topic(self, npc: str, topic: str) -> tuple[str, bool]:
        if not isinstance(npc, str) or not isinstance(topic, str):
            return ("please create your own response for this topic ", True)
        all_topics = self.get_current_topics()
        npc_topics = all_topics.get(npc, {})
        _, topic_resp = find_case_insensitive(npc_topics, topic)
        if topic_resp is None:
            topics = json.dumps(list(npc_topics.keys()))
            return (f"no topic '{topic}' for npc '{npc}' - npc topics are {topics}\n" +\
                    "you can try again using one of these, or creatively improvise a response consistent with the story and rules\n", False)
        return (topic_resp, False)

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
        resp, err = self.remove_item(from_being, item_unique_name, qty)
        if err: 
            return (resp, err)
        return self.add_item(to_being, item_unique_name, qty)

    def help(self, subject) -> tuple[str, bool]:
        if subject.endswith(" spell"):
            subject = subject[:-6]
        elif subject.endswith(" equipemnt"):
            subject = subject[:-10]
        look_info = self.help_index.get(subject.lower())
        if look_info != None:
            match look_info["type"]:
                case "spell":
                    return self.describe_spell(look_info["name"])
                case "magic_categories":
                    return self.describe_magic(look_info["name"])
                case "equipment":
                    return self.describe_equipment(look_info["name"])
                case _:
                    raise RuntimeError("Invalid help index type")
        return (f"no help subject {subject} found", False)
            
    def look(self, subject, object) -> tuple[str, bool]:
        if subject is None or subject == self.cur_location_name or subject == "location":
            return self.describe_location()
        elif subject == "party":
            return self.describe_party()
        desc = None
        if subject in self.cur_location.get("poi", {}):
            desc = self.cur_location["poi"]["description"]
            self.action_image_path = self.other_image(subject, "poi")
        elif self.cur_location_script is not None and subject in self.cur_location_script.get("poi", {}):
            desc = self.cur_location_script["poi"]["description"]
            self.action_image_path = self.other_image(subject, "poi")
        elif subject in self.game_state["characters"]:
            desc = self.game_state["characters"][subject]["info"]["other"]["description"]
            self.action_image_path = self.other_image(subject, "characters")
        elif subject in self.game_state["npcs"] and \
                self.game_state["npcs"][subject].get("has_player_met", False) == True:
            desc = self.game_state["npcs"][subject]["description"]
            self.action_image_path = self.other_image(subject, "npcs")
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
            instr = self.rules["instructions_prompt"].strip(" \n\t") + "\n\n" + self.cur_location_script["instructions"].strip(" \n\t") + "\n"
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
        return self.describe_location()

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
        return (json.dumps(invent_items) + "\n", False)

    def pickup(self, being_name: str, item_name: str, extra: any) -> tuple[str, bool]:
        if not isinstance(being_name, str) or not isinstance(item_name, str):
            return ("invalid command", True)     
        if not self.is_nearby_being_name(being_name):
            return (f"'{being_name}' is not here", True)
        being = self.get_nearby_being(being_name)
        if not Game.can_do_actions(being):
            return (f"'{being_name}' is not here", True)        
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
        if not Game.can_do_actions(being):
            return (f"'{being_name}' is not here", True)  
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
        if "overview" in self.module and len(self.module["overview"]) > 0:
            overview = self.module["overview"]
            if "description" in overview:
                mod_overview = self.module["overview"]["description"]
                resp += "MODULE OVERVIEW (for AI do NOT show to player!):\n" + mod_overview + "\n\n"
            self.action_image_path = self.module_path + "/" + overview["image"] \
                if "image" in overview else None
        resp_party, error = self.describe_party()
        if error:
            return (resp_party, error)
        resp += "PLAYER PARTY:\n\n" + resp_party + "\n"
        resp_loc, error = self.describe_location()
        if error:
            return (resp_loc, error)
        resp += "FIRST LOCATION:\n\n" + resp_loc + "\n\n"
        resp += self.rules["overview_prompt"]
        return (resp, False)

    def restart(self) -> tuple[str, bool]:
#        if not self.game_over:
#            return ("current game is not over", True)
        self.new_game()
        return self.resume()

    def complete(self, task_name: str) -> tuple[str, bool]:
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
        if self.cur_location_script is None or \
                "transitions" not in self.cur_location_script or \
                script_state not in self.cur_location_script["transitions"]:
            return (f"no script state '{script_state}' - try again with the the script state provided in your instructions", True)
        self.cur_script_state = script_state
        self.cur_location_script = self.cur_location["script"][script_state]
        self.script_state_since = self.cur_time
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
        elif args[0] in self.cur_location_state["usables"]:
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
        resp += use_effect_resp + "\n"

        return (resp, failed)

    def describe_spell(self, spell_name) -> tuple[str, bool]:
        if not spell_name or spell_name.lower() == "spells" or spell_name.lower() == "all":
            return ("SPELLS:\n" + json.dumps(list(self.rules["spells"].keys())) + "\n", False)
        spell_name, spell = find_case_insensitive(self.rules["spells"], spell_name)
        if spell is None:
            return (f"no spell {spell_name}", True)
        spell["category"] = spell["category"] + " Magic"
        image_path = self.check_for_image(self.rules_path + "/images", spell_name, "spells")
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
        image_path = self.check_for_image(self.rules_path + "/images", magic_category_name + " Magic", "magic_categories")
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
            elif args[0] in self.cur_location_state["usables"]:
                target_usable_name = args[0]
                del args[0]
                target_usable = self.cur_location_state["usables"][target_usable_name]

        resp = f"{being_name} casts {spell['category']} Magic spell {spell_name}"
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
        resp += cast_effect_resp + "\n"

        # image?
        image_path = self.check_for_image(self.rules_path + "/images", spell_name, "spells")
        if image_path:
            self.action_image_path = image_path          

        return (resp, failed)

    def do_explore_action(self, action: any, subject: any, object: any, extra: any, extra2: any) -> tuple[str, bool]:
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
            case "complete":
                resp, error = self.complete(subject)
            case "drop":
                resp, error = self.drop(subject, object, extra)
            case "give":
                resp, error = ("ok", True)
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
            case "use":
                resp, error = self.search(subject)
            case "next":
                resp, error = self.next_script_state(subject)
            case _:
                resp = f"can't do action '{action}'"
                error = True
        return (resp, error)

    # ENCOUNTER ACTIONS ----------------------------------------------------------

    def check_random_encounter(self) -> tuple[bool, dict[str, any] | None]:
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
            self.action_image_path = self.module_path + "/" + self.cur_encounter["image"]
        resp = ""
        if "description" in self.cur_encounter:
            resp += "ENCOUNTER DESCRIPTION:\n\n" + self.cur_encounter["description"] + "\n\n"
        resp += self.rules["encounter_prompt"] + "\n\n"
        monster_types = {}
        monsters_desc = ""
        for monster_name, monster_unique_name in self.cur_encounter["monsters"].items():
            monster = self.get_object(monster_unique_name)
            if monster["type"] == "monster":
                monster_type = monster["monster_type"]
                monster_types[monster_type] = self.get_monster_type(monster_type)
            if Game.is_dead(monster):
                monsters_desc += f"  '{monster_name}' is DEAD!\n"
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
        self.game_state["encounter"] = None
        self.cur_encounter = None
        self.remove_cur_location_encounter()
        if players_left > 0:
            return ("Player were victorious!", False)
        players_alive = self.get_players_alive()
        if players_alive == 0:
            self.game_over = True
            return ("All players were killed - game over", False)
        self.set_location(self.prev_location_name)
        resp, err = self.describe_location()
        return ("Your party has escaped!\n\n" + resp, False)
    
    def get_attacker(self, attacker_name) -> dict[str, any]:
        if self.cur_encounter["turn"] == "players":
            return self.get_object(self.cur_encounter["characters"].get(attacker_name))
        else:
            return self.get_object(self.cur_encounter["monsters"].get(attacker_name))

    def get_attack_target(self, target_name) -> dict[str, any]:
        if self.cur_encounter["turn"] == "monsters":
            return self.get_object(self.cur_encounter["characters"].get(target_name))
        else:
            return self.get_object(self.cur_encounter["monsters"].get(target_name))

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

    def get_players_left_to_go(self) -> tuple[int, str]:
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
                Game.set_has_escaped(being_name, being, True)
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
        if self.cur_encounter["turn"] == "players":
            _, monsters_left = self.get_players_monsters_left()
            if monsters_left == 0:
                return ("no monsters left", 0)
            for char_name, char_unique_name in self.cur_encounter["characters"].items():
                char = self.get_object(char_unique_name)
                if Game.is_still_fighting(char) and char["encounter"]["moved_round"] != self.cur_encounter["round"]:
                    left_to_go.append(char_name)
            resp += "Characters who haven't moved yet (AI Referee please tell players): " + ", ".join(left_to_go) + "\n"
        else:
            _, monsters_left = self.get_players_monsters_left()
            if monsters_left == 0:
                return ("all players", 0)
            for monster_name, monster_unique_name in self.cur_encounter["monsters"].items():
                monster = self.get_object(monster_unique_name)
                if Game.is_still_fighting(monster) and monster["encounter"]["moved_round"] != self.cur_encounter["round"]:
                    left_to_go.append(monster_name)
        return (resp, len(left_to_go))

    def describe_encounter_turn(self) -> str:
        if self.cur_encounter["turn"] == "monsters":
            resp = self.get_attacker_encounter_states() + "\n" + self.rules["monster_turn_prompt"].strip("\n")
        else:
            resp = self.get_attacker_encounter_states() + "\n" + self.rules["player_turn_prompt"].strip("\n")
        return resp

    def next_encounter_turn(self) -> tuple[str, bool]:
        if self.cur_encounter["turn"] == "players":
            _, monsters_left = self.get_players_monsters_left()
            if monsters_left == 0:
                return self.end_encounter()
            self.cur_encounter["turn"] = "monsters"
            if self.agent.logging:
                print("\n\nNOW MONSTERS TURN...\n\n")
        else:
            players_left, _ = self.get_players_monsters_left()
            if players_left == 0:
                return self.end_encounter()
            self.cur_encounter["turn"] = "players"
            self.cur_encounter["round"] += 1
            left_to_go_str, _ = self.get_attackers_left_to_go()
            if self.agent.logging:
                print("\n\nNOW PLAYERS TURN...\n\n")
        return (self.describe_encounter_turn(), False)

    def check_encounter_can_move(self, move: str, attacker: dict[str, any]) -> tuple[str, bool]:
        if self.cur_game_state_name == "encounter":
            attacker_name = attacker["name"]
            if attacker["encounter"]["moved_round"] == self.cur_encounter["round"]:
                return (f"'{move}' FAILED - '{attacker_name} already moved this round", True)
            if Game.is_dead(attacker):
                return (f"'{move}' FAILED - '{attacker_name}' is dead", True)
            if Game.has_escaped(attacker):
                return (f"'{move}' FAILED - '{attacker_name}' has escaped", True)
        return ( "ok", True )

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

        # Write results header if this is the first action in this response so AI can figure out
        # what's going on
        if self.cur_encounter["turn"] == "players" and self.player_results_id != self.response_id:
            self.player_results_id = self.response_id
            resp = "\nPLAYER TURN RESULTS:\n\n"
        elif self.cur_encounter["turn"] == "monsters" and self.monster_results_id != self.response_id:
            self.monster_results_id = self.response_id
            resp = "\nMONSTER TURN RESULTS:\n\n"

        if self.cur_game_state_name != "encounter" or self.cur_encounter is None:
            return ("'{move}' FAILED - not in 'encounter' game state", True)
        
        if move not in [ "attack", "press", "shoot", "advance", "retreat", "charge", "flee", "pass" ]:
            return (f"'{move}' FAILED - not a valid encounter action", True)

        attacker = self.get_attacker(attacker_name)
        if attacker is None:
            if self.get_attack_target(attacker_name) is not None:
                return (f"{attacker_name} can't move because it is currently {self.cur_encounter['turn']} turn ", True)
            else:
                return (f"attacker '{attacker_name}' not found", True)
        
        can_move_msg, can_move = self.check_encounter_can_move(move, attacker)
        if not can_move:
            return (can_move_msg, True)

        target = None        
        if move in [ "attack", "press", "shoot" ]:
            target = self.get_attack_target(target_name)
            if target is None:
                return (f"'{move}' FAILED - target '{target_name}' not found", True)
            if Game.is_dead(target):
                return (f"'{move}' FAILED - target '{target_name}' is dead", True)
            if Game.has_escaped(target):
                return (f"'{move}' FAILED - target '{target_name}' has escaped", True)

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
                cur_health = Game.get_cur_health(target) - damage
                resp += f" HIT! - dealing damage -{damage} leaving health {cur_health}"
                if cur_health < 0:
                    cur_health = 0
                    resp += " DEAD"
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

        elif move == "pass":
            err = False
            resp = f"'{attacker_name}' passes this turn"
            self.mark_encounter_moved(attacker)
            return (resp, False)

    def get_encounter_response_end(self) -> str:
        left_resp, left = self.get_attackers_left_to_go()
        if left != 0:
            return "\n\n  " + left_resp + "\n"
        return ""       

    def do_encounter_action(self, action: any, subject: any, object: any, extra: any, extra2: any) -> tuple[str, bool]:
        resp = ""
        error = False

        if action in [ "wait", "hold", "stop", "defend", "no_action" ]:
            action = "pass"

        if action in [ "charge", "flee", "advance", "retreat", "attack", "press", "block", "shoot", "pass" ]:
            resp, error = self.attack_move(action, subject, object)
        else:   
            match action:
                case _:
                    resp = f"can't do action '{action}'"
                    error = True
        return (resp, error)

    # DIALOG ACTIONS ----------------------------------------------------------

    def do_dialog_action(self, action: any, subject: any, object: any, extra: any, extra2: any) -> tuple[str, bool]:
        resp = ""
        error = False
        match action:
            case _:
                resp = f"can't do action '{action}'"
                error = True
        return (resp, error)
    
    # STORE ACTIONS ----------------------------------------------------------

    def do_store_action(self, action: any, subject: any, object: any, extra: any, extra2: any) -> tuple[str, bool]:
        resp = ""
        error = False
        match action:
            case _:
                resp = f"can't do action '{action}'"
                error = True
        return (resp, error)

    # NEXT TURN ----------------------------------------------------------

    def get_response_start(self) -> str:
        return ""
    
    def get_response_end(self) -> str:
        match self.cur_game_state_name:
            case "encounter":
                return self.get_encounter_response_end()
            case _:
                return ""

    def next_turn(self, action: any, 
                  subject: any = None, 
                  object: any = None, 
                  extra: any = None, 
                  extra2: any = None) -> str:
        
        resp = ""
        error = False

        if self.agent.logging:
            print(f"  ACTION: {action} {subject} {object} {extra}")
        
        if self.game_over and action != "restart":
            return "Players lost and game is over - players must ask AI to \"restart\" the game"

        use_verb = None
        if action in ("light", "extinguish", "eat", "drink", "open", "close", "push", "pull", 
                      "activate", "press", "lock", "unlock"):
            use_verb = action
            action = "use"

        match action:
            case "cast":
                args = [ subject, object, extra, extra2 ]
                resp, error = self.cast(args)
            case "describe":
                resp, error = self.look(subject, object)
            case "help":
                resp, error = self.help(subject)
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
            case "restart":
                resp, error = self.restart()
            case "stats":
                resp, error = self.stats(subject)
            case "use":
                args = [ subject, object, extra, extra2 ]
                resp, error = self.use(args, use_verb)
            case _:
                match self.cur_game_state_name:
                    case "exploration":
                        resp, error = self.do_explore_action(action, subject, object, extra, extra2)
                    case "encounter":
                        resp, error = self.do_encounter_action(action, subject, object, extra, extra2)
                    case "dialog":
                        resp, error = self.do_dialog_action(action, subject, object, extra, extra2)
                    case "store":
                        resp, error = self.do_store_action(action, subject, object, extra, extra2)
                    case _:
                        resp = "unknown game state {self.cur_game_state_name}'"
                        self.cur_game_state_name = "exploration"
                        error = True

        if self.cur_game_state_name == "encounter":
            resp, next_turn_error = self.check_encounter_next_turn(resp)
            error = error or next_turn_error        

        if error:
            if self.agent.logging:
                print(f"  ERROR: {resp}")
            return resp
        
        self.save_game()

        return resp
    
    # USER COMMANDS ----------------------------------------------------------

    def restart_command(self) -> str:
        resp, err = self.restart()
        if err:
            return resp
        return self.process_response(resp, 1)

