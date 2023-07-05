from agent import Agent
import copy
from datetime import datetime, timedelta
import json
import os
import os.path as path
import random
import yaml

def find_case_insensitive(dic: dict, key: str) -> tuple[str, any]:
    value = dic.get(key)
    if value is not None:
        return (key, value)
    lower_key = key.lower()
    for k, v in dic.items():
        if k.lower() == lower_key:
            return (k, v)
    return (key, None)

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

class Game():
    def __init__(self, agent: Agent, module_name: str, party_name: str) -> None:
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
        self.save_game_name = ""
        self.game_state: dict[str, any] = {}
        self.action_image_path: str | None = None
        self.response_id = 1
        self.player_results_id = 0
        self.monster_results_id = 0
        self.load_game()
        self.init_session_state()

    @property
    def is_started(self) -> bool:
        return self.game_started

    def start_game(self) -> str:
        self.game_started = True        
        resp = self.agent.generate(self.rules["starting_prompt"], primary=True, keep=True)
        resume_prompt = self.agent.make_prompt(self.rules["resume_game_prompt"], self.module["info"])
        return self.action(resume_prompt, \
                           'call next_turn("resume")', \
                           'AI Referee, you must use [call next_turn("resume")] to start the game. Please try again.\n')

    # A per user response hint to focus the AI (not usually used unless AI has trouble)
    def add_query_hint(self, query) -> str:
        return "PLAYERS RESPONSE:\n\n" + \
            query + "\n\n" + \
            self.rules["instructions_prompt"] + "\n\n" + \
            self.cur_location_script["hint"] + "\n\n"

    def action(self, query: str, expected_action: str = None, retry_msg: str = None) -> str:
        self.action_image_path = None
        is_system = query.startswith("SYSTEM:")
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

    def init_game(self) -> None:
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
            self.cur_game_state_name = self.module["starting_game_state"]
            self.cur_location_name = None
            self.cur_time = self.module["starting_time"]
        self.init_game()
        self.save_game()

    def load_game(self) -> None:
        game_path = f'save_games/{self.module_name}/{self.party_name}.yaml'
        if path.exists(game_path):
            with open(game_path, 'r') as f:
                self.game_state = yaml.load(f, Loader=yaml.FullLoader)
            self.init_game()
        else:
            self.new_game()

    def save_game(self) -> None:
        module_save_path = f'save_games/{self.module_name}'
        game_save_path = f'save_games/{self.module_name}/{self.party_name}.yaml'       
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

    def is_character(self, char_name: str) -> bool:
        return char_name in self.characters
    
    def add_item(self, char_name: str, item_name: str, qty: int) -> tuple[str, bool]:
        if not self.is_character(char_name):
            return (f"is not a character '{char_name}'", True)
        char = self.characters[char_name]
        cur_item = char["inventory"].get(item_name)
        if cur_item is None:
            if qty > 1:
                char["inventory"][item_name] = { "qty": qty }
            else:
                char["inventory"][item_name] = {}
        else:
            cur_item["qty"] = qty + cur_item.get("qty", 1)
        return ("ok", False)

    def remove_item(self, char_name: str, item_name: str, qty: int) -> tuple[str, bool]:
        if not self.is_character(char_name):
            return (f"is not a character '{char_name}'", True)
        char = self.characters[char_name]
        cur_item = char["inventory"].get(item_name)
        if cur_item is None:
            return (f"item '{item_name}' not in inventory", True)
        cur_qty = cur_item.get("qty", 1)
        if qty > cur_qty:
            return (f"only {cur_qty} of '{item_name}' in inventory", True)
        if qty == cur_qty:
            del char["inventory"][item_name]
        else:
            cur_item["qty"] = cur_qty - qty
        return ("ok", False)
    
    def place_item(self, location: dict[str, any], item_name: str, item: dict[str, any]) -> None:
        if "items" not in location:
            location["items"] = {}
        location["items"][item_name] = item

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
        exits = self.cur_location.get("exits", {})
        if self.cur_location_script is not None:
            exits.update(self.cur_location_script.get("exits", {}))
        exit_names = []
        for exit_name, _ in exits.items():
            if not self.exit_blocked(self.cur_location_name, exit_name):
                exit_names.append(f'"{exit_name}"')
        return ", ".join(exit_names)

    def get_random_character(self) -> tuple[str, dict]:
        num_chars = len(self.characters)
        char_idx = random.randint(0, num_chars - 1)
        for char_name, char in self.characters.items():
            if char_idx == 0:
                return (char_name, char)
            char_idx -= 1
        return ("", None)

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
        exts = [ ".png", ".jpg" ]
        paths = [ "", f"/{type_name}"]
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
    def die_roll(dice: str) -> int:
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

    @staticmethod
    def is_character(maybe_char: dict[str, any]) -> bool:
        return maybe_char.get("type", "character") == "character"

    @staticmethod
    def is_monster(maybe_monster: dict[str, any]) -> bool:
        return maybe_monster.get("type", "character") == "monster"

    @staticmethod
    def is_npc(maybe_npc: dict[str, any]) -> bool:
        return maybe_npc.get("type", "character") == "npc"

    @staticmethod
    def get_skill_ability_modifier(being: dict[str, any], skill_ability: str) -> str:
        if skill_ability in being.get("stats", {}).get("skills", {}):
            return being["stats"]["skills"].get(skill_ability, "")
        if skill_ability in being.get("stats", {}).get("abilities", {}):
            return being["stats"]["abilities"].get(skill_ability, "")
        return 0
    
    @staticmethod
    def get_equipped_weapon(being: dict[str, any]) -> dict[str, any]:
        if "equpped" in being:
            equipped_weapon_name = being["equipped"]
            if "inventory" in being:
                return being["inventory"].get(equipped_weapon_name)
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
    def get_is_dead(being: dict[str, any]) -> bool:
        return being.get("dead", False)

    def set_is_dead(self, being_name, being: dict[str, any], dead: bool) -> None:
        if Game.get_is_dead(being):
            return
        being["dead"] = dead
        # Add the npc, char, monster's corpse to the items in the room. Make sure their inventory is still
        # accessible
        if dead:
            if Game.is_character(being):
                items = copy.deepcopy(being.get("inventory", {}))
                self.place_item(self.cur_location, f"{being_name}'s Corpse", { "type": "corpse", "character": being_name, "items": items })
            elif Game.is_npc(being):
                items = copy.deepcopy(being.get("inventory", {}))
                self.place_item(self.cur_location, f"{being_name}'s Corpse", { "type": "corpse", "npc": being_name, "items": items })
            else:
                # TODO: Get or generate monster treasure
                items = copy.deepcopy(being.get("items", {}))
                self.place_item(self.cur_location, f"{being_name}'s Corpse", { "type": "corpse", "monster_type": being["monster_type"], "items": items })

    @staticmethod
    def get_has_escaped(being: dict[str, any]) -> bool:
        return being["encounter"].get("escaped", False)

    @staticmethod
    def set_has_escaped(being_name: str, being: dict[str, any], escaped: bool) -> None:
        if Game.get_has_escaped(being) == escaped:
            return
        being["encounter"]["escaped"] = escaped

    @staticmethod
    def get_cur_health(being: dict[str, any]) -> str:
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

    def set_cur_health(self, being_name: str, being: dict[str, any], value: int) -> str:
        basic_stats = being["stats"]["basic"]
        if "cur_health" not in basic_stats:
            basic_stats["cur_health"] = basic_stats["health"]
        basic_stats["cur_health"] = value
        if basic_stats["cur_health"] < 0:
            basic_stats["cur_health"] = 0
        if basic_stats["cur_health"] > basic_stats["health"]:
            basic_stats["cur_health"] = basic_stats["health"]
        if basic_stats["cur_health"] == 0 and not Game.get_is_dead(being):
            self.set_is_dead(being_name, being, True)
        return basic_stats["cur_health"]

    def get_players_alive(self) -> int:
        chars_alive = 0
        for char in self.game_state["characters"].values():
            if not Game.get_is_dead(char):
                chars_alive += 1
        return chars_alive
    
    # Used when beginning combat to get a full inflated copy of target monster or npc.
    # NPC stats are copied back to npc when encounter is over.
    def merge_monster_type_or_npc_to_encounter(self, being_name: str, being: dict[str, any]) -> None:
        if "monster_type" in being:
            monster_type = copy.deepcopy(self.module["monsters"][being["monster_type"]])
            being.update(monster_type)
            being["type"] = "monster"
        elif being_name in self.game_state["npcs"]:
            npc_info = copy.deepcopy(self.game_state["npcs"][being_name])
            being.update(npc_info)
            being["type"] = "npc"
            del being["description"] # don't need
            del being["topics"] # don't need
        else:
            raise RuntimeError(f"encounter monter 'being_name' not a valid monster or npc")

    def merge_encounter_npc_back_to_game_state(self, npc_name: str, npc: dict[str, any]) -> None:
        self.game_state["npcs"][npc_name].upate(npc)

    @staticmethod
    def get_can_attack(attacker: dict[str, any], attack_type: str) -> None:
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
        weapon = copy.deepcopy(attacker["inventory"][weapon_name])
        if "rules_item" in weapon:
            rules_weapon_name = weapon["rules_item"]
        else:
            rules_weapon_name = weapon_name
        rules_weapon = self.rules["equipment"][rules_weapon_name]
        weapon.update(rules_weapon)
        return weapon

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
            items = "items: " + json.dumps(self.cur_location_state["items"]) + "\n"
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

    def describe_party(self) -> tuple[str, bool]:
        resp = ""
        for char_name, char in self.game_state["characters"].items():
            resp += f"Character: '{char_name}'\n"
            resp += "  stats - " + json.dumps(char["stats"]["basic"]).strip("{}").replace("\"", "") + "\n"
            resp += "  attributes - " + json.dumps(char["stats"]["attributes"]).strip("{}").replace("\"", "") + "\n"
            resp += "  skills - " + json.dumps(char["stats"]["skills"]).strip("{}").replace("\"", "") + "\n"
            resp += "  abilities - " + json.dumps(char["stats"]["abilities"]).strip("[]").replace("\"", "") + "\n"
            resp += "  inventory - " + json.dumps(list(char["inventory"].keys())).strip("[]").replace("\"", "") + "\n\n"
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
                    "you can try again using one of these, or choose to make up your own response\n", True)
        return (topic_resp, False)

    def give(self, char_name: str, to_name: str, item_name: any, extra: any) -> tuple[str, bool]:
        if not isinstance(char_name, str) or not isinstance(to_name, str) or not isinstance(item_name, str):
            return ("invalid command", True)
        if not self.is_character(char_name):
            return (f"not a character '{char_name}'", True)
        if not self.is_character(to_name):
            return (f"not a character '{to_name}'", True)        
        char = self.characters[char_name]
        (item_name, item) = find_case_insensitive(char["inventory"], item_name)
        if item is None:
            return (f"no item '{item_name}", True)
        item_qty = item.get("qty", 1)
        qty, err = any_to_int(extra)
        if err:
            qty = 1
        if qty > item_qty:
            return ("only has {item_qty}", False)
        resp, err = self.remove_item(char_name, item_name, qty)
        if err:
            return (resp, err)
        return self.add_item(to_name, item_name, qty)

    def look(self, subject) -> tuple[str, bool]:
        if subject is None or subject == self.cur_location_name:
            return self.describe_location()
        desc = None
        if subject in self.cur_location["poi"]:
            desc = self.cur_location["poi"]["description"]
            self.action_image_path = self.other_image(subject, "poi")
        elif self.cur_location_script is not None and subject in self.cur_location_script["poi"]:
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
        exits = self.cur_location.get("exits", {})
        if self.cur_location_script and "exits" in self.cur_location_script:
            exits.update(self.cur_location_script["exits"])
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
        if not self.is_character(char_name):
            return (f"not a character '{char_name}'", True)
        return (json.dumps(self.characters[char_name]["inventory"]) + "\n", False)

    def pickup(self, char_name: str, item_name: str, extra: any) -> tuple[str, bool]:
        if not isinstance(char_name, str) or not isinstance(item_name, str):
            return ("invalid command", True)     
        if not self.is_character(char_name):
            return (f"not a character '{char_name}'", True)
        (item_name, item) = find_case_insensitive(self.cur_location_state.get("items", {}), item_name)
        if item is None:
            return (f"no item '{item_name}", True)
        qty, err = any_to_int(extra)
        if err:
            qty = 1
        item_qty = item.get("qty", 1)
        if qty > item_qty:
            return ("only {item_qty} available", False)
        elif qty == item_qty:
            del self.cur_location_state["items"][item_name]
        else:
            new_qty = item_qty - qty
            if new_qty == 1 and "qty" in item:
                del item["qty"]
            else:
                item["qty"] = new_qty
        return self.add_item(char_name, item_name, qty)

    def drop(self, char_name: str, item_name: str, extra: any) -> tuple[str, bool]:
        if not isinstance(char_name, str) or not isinstance(item_name, str):
            return ("invalid command", True)
        if not self.is_character(char_name):
            return (f"not a character '{char_name}'", True)
        char = self.characters[char_name]
        (item_name, item) = find_case_insensitive(char["inventory"], item_name)
        if item is None:
            return (f"no item '{item_name}", True)
        item_qty = item.get("qty", 1)
        qty, err = any_to_int(extra)
        if err:
            qty = 1
        if qty > item_qty:
            return ("only has {item_qty}", False)
        loc_items = self.cur_location_state.get("items")
        if loc_items is None:
            loc_items = {}
            self.cur_location_state["items"] = loc_items
        loc_item = loc_items.get(item_name)
        if loc_item is None:
            loc_item = {}
            if qty > 1:
                loc_item["qty"] = qty
            self.cur_location_state["items"][item_name] = loc_item
        else:
            loc_item_qty = loc_item.get("qty", 1)
            loc_item["qty"] = qty + loc_item_qty
        return self.remove_item(char_name, item_name, qty)

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
        if not self.game_over:
            return ("current game is not over", True)
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
            char_name, char = self.get_random_character()
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

    def search(self, character: str) -> tuple[str, bool]:
        if "hidden" not in self.cur_location_state:
            return ("nothing found", False)
        if "items" not in self.cur_location_state:
            self.cur_location_state["items"] = {}
        hidden_items = json.dumps(self.cur_location_state["hidden"])
        self.cur_location_state["items"].update(self.cur_location_state["hidden"])
        del self.cur_location_state["hidden"]
        return (f"found items {hidden_items}", False)
    
    def do_expore_action(self, action: any, subject: any, object: any, extra: any) -> tuple[str, bool]:
        resp = ""
        error = False

        use_synonym = ""
        if action in ("eat", "drink", "open", "close", "push", "pull", "activate", "press", "lock"):
            use_synonym = action
            action = "use"

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
            case "move":
                resp, error = self.go(subject, object)
            case "party":
                resp, error = self.describe_party()
            case "pickup":
                resp, error = self.pickup(subject, object, extra)
            case "search":
                resp, error = self.search(subject)
            case "next":
                resp, error = self.next_script_state(subject)
            case _:
                resp = f"can't do action '{action}'"
                error = True
        return (resp, error)

    # ENCOUNTER ACTIONS ----------------------------------------------------------

    def check_random_encounter(self) -> tuple[bool, dict[str, any] | None]:
        if "encounter" in self.cur_location_state:
            return (True, None)
        # Implement random encounters here!
        return ()
    
    def get_cur_location_encounter(self) -> dict[str, any]:
        if self.cur_location_script is not None and "encounter" in self.cur_location_script:
            return self.cur_location_script["encounter"]
        return self.cur_location_state.get("encounter", None)
    
    def remove_cur_location_encounter(self) -> None:
        if self.cur_location_script is not None and "encounter" in self.cur_location_script:
            del self.cur_location_script["encounter"]
        else:
            del self.cur_location["encounter"]

    def start_encounter(self, random_encounter: dict[str, any] = None) -> tuple[str, bool]:
        encounter: dict[str, any] = None
        if random_encounter is not None:
            if self.get_cur_location_encounter() is not None:
                return ("random encounter not possible, there are already monsters in area", True)
            encounter = random_encounter
            self.cur_location_state["encounter"] = copy.deepcopy(random_encounter)
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
        for char in self.game_state["characters"].values():
            char["encounter"] = {}
            char["encounter"]["moved_round"] = 0
            char["encounter"]["range"] = encounter["starting_range"]
        for monster_name, monster in encounter["monsters"].items():
            # Note: a 'monster' can be an npc, we just use monster for enemies
            self.merge_monster_type_or_npc_to_encounter(monster_name, monster)
            monster["encounter"] = {}
            monster["encounter"]["moved_round"] = 0
            monster["encounter"]["range"] = 0
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
        for monster_name, monster in self.cur_encounter["monsters"].items():
            monster_type = monster['monster_type']
            if monster["type"] == "monster":
                monster_type = monster["monster_type"]
                monster_types[monster_type] = self.module["monsters"][monster_type]
            if Game.get_is_dead(monster):
                monsters_desc += f"  '{monster_name}' is DEAD!\n"
                continue
            if Game.get_has_escaped(monster):
                monsters_desc += f"  '{monster_name}' has ESCAPED!\m"
                continue
            stats = json.dumps(monster['stats']['basic']).strip("{}").replace("\"", "")
            range_attack = ("YES" if Game.get_can_attack(monster, "range") else "NO")
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
        for char in self.game_state["characters"].values():
            del char["encounter"]
        for monster_npc_name, monster_npc in self.cur_encounter["monsters"].items():
            del monster_npc["encounter"]
            if monster_npc["type"] == "npc":
                self.merge_encounter_npc_back_to_game_state(monster_npc_name, monster_npc)
        self.game_state["encounter"] = None
        self.cur_encounter = None
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
            return self.game_state["characters"].get(attacker_name)
        else:
            return self.cur_encounter["monsters"].get(attacker_name)

    def get_attack_target(self, target_name) -> dict[str, any]:
        if self.cur_encounter["turn"] == "monsters":
            return self.game_state["characters"].get(target_name)
        else:
            return self.cur_encounter["monsters"].get(target_name)

    def get_players_monsters_left(self) -> tuple[int, int]:
        chars_left = 0
        monsters_left = 0
        for char in self.game_state["characters"].values():
            if Game.get_is_still_fighting(char):
                chars_left += 1
        for monster in self.cur_encounter["monsters"].values():
            if Game.get_is_still_fighting(monster):
                monsters_left += 1
        return (chars_left, monsters_left)

    def get_players_left_to_go(self) -> tuple[int, str]:
        if self.cur_encounter["turn"] != "players":
            return (0, "")       
        left_to_go = []
        for char_name, char in self.game_state["characters"].items():
            if not Game.get_is_dead(char) and char["encounter"]["moved_round"] != self.cur_encounter["round"]:
                left_to_go.append(char_name)
        left_to_go.sort()
        resp = ""
        num_left_to_go = len(left_to_go)
        if len(left_to_go) > 0:
            left_to_go_str = json.dumps(left_to_go)
            resp += f"  {num_left_to_go} playes have not yet gone: {left_to_go_str}\n"
        return (num_left_to_go, resp)

    @staticmethod
    def get_is_still_fighting(being: dict[str, any]) -> bool:
        return not Game.get_is_dead(being) and not Game.get_has_escaped(being)

    @staticmethod
    def get_range_dist(from_entity: dict[str, any], to_entity: dict[str, any]) -> int:
        return abs(from_entity["encounter"]["range"] - to_entity["encounter"]["range"])
    
    @staticmethod
    def get_range_str(range_ft) -> str:
        if range_ft == 0:
            return "close/melee"
        if range_ft >= 120:
            return "distant/120ft+"
        return f"{range_ft}ft"
    
    def get_closest_ranges(self) -> tuple[int, int]:
        closest_monster = -10000
        for monster in self.cur_encounter["monsters"].values():
            if Game.get_is_still_fighting(monster):
                closest_monster = max(monster["encounter"]["range"], closest_monster)
        closest_char = 10000
        for char in self.game_state["characters"].values():
            if Game.get_is_still_fighting(char):
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
            attackers = self.game_state["characters"]
            targets = self.cur_encounter["monsters"]
            turn_name = "CHARACTER"
        else:
            attackers = self.cur_encounter["monsters"]
            targets = self.game_state["characters"]
            turn_name = "MONSTER"
        resp = ""
        for attacker_name, attacker in attackers.items():
            if not Game.get_is_still_fighting(attacker):
                continue
            ranges = []
            for target_name, target in targets.items():
                if Game.get_is_still_fighting(target):
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
            for char_name, char in self.game_state["characters"].items():
                if Game.get_is_still_fighting(char) and char["encounter"]["moved_round"] != self.cur_encounter["round"]:
                    left_to_go.append(char_name)
            resp += "Characters who haven't moved yet (AI Referee please tell players): " + ", ".join(left_to_go) + "\n"
        else:
            _, monsters_left = self.get_players_monsters_left()
            if monsters_left == 0:
                return ("all players", 0)
            for monster_name, monster in self.cur_encounter["monsters"].items():
                if Game.get_is_still_fighting(monster) and monster["encounter"]["moved_round"] != self.cur_encounter["round"]:
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
        
        if move not in [ "attack", "press", "shoot", "advance", "retreat", "charge", "flee" ]:
            return (f"'{move}' FAILED - not a valid encounter action", True)

        attacker = self.get_attacker(attacker_name)
        if attacker is None:
            if self.get_attack_target(attacker_name) is not None:
                return (f"{attacker_name} can't move because it is currently {self.cur_encounter['turn']} turn ", True)
            else:
                return (f"attacker '{attacker_name}' not found", True)
        if attacker["encounter"]["moved_round"] == self.cur_encounter["round"]:
            return (f"'{move}' FAILED - '{attacker_name} already moved this round", True)
        if Game.get_is_dead(attacker):
            return (f"'{move}' FAILED - '{attacker_name}' is dead", True)
        if Game.get_has_escaped(attacker):
            return (f"'{move}' FAILED - '{attacker_name}' has escaped", True)

        target = None        
        if move in [ "attack", "press", "shoot" ]:
            target = self.get_attack_target(target_name)
            if target is None:
                return (f"'{move}' FAILED - target '{target_name}' not found", True)
            if Game.get_is_dead(target):
                return (f"'{move}' FAILED - target '{target_name}' is dead", True)
            if Game.get_has_escaped(target):
                return (f"'{move}' FAILED - target '{target_name}' has escaped", True)

        if move in [ "attack", "press", "shoot" ]:
            attack_type = ("ranged" if move == "shoot" else "melee")
            ability_name = ("Melee Combat" if attack_type == "melee" else "Ranged Combat")
            if not Game.get_can_attack(attacker, attack_type):
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
            roll = random.randint(1, 20)
            attack_mod_die = Game.get_skill_ability_modifier(attacker, ability_name) or ""
            attack_mod_roll = Game.die_roll(attack_mod_die)
            defense = target["stats"]["basic"]["defense"]
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
                self.set_cur_health(target_name, target, cur_health)
            else:
                resp += " MISS!"
            if self.agent.logging:
                print("    " + resp)
            attacker["encounter"]["moved_round"] = self.cur_encounter["round"]

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
            attacker["encounter"]["moved_round"] = self.cur_encounter["round"]

        _, left = self.get_attackers_left_to_go()
        if left == 0:
            turn_resp, err = self.next_encounter_turn()
            if err:
                return (turn_resp, err)
            return (resp + "\n" + turn_resp + "\n", False)
        else:
            return (resp, False)

    def get_encounter_response_end(self) -> str:
        left_resp, left = self.get_attackers_left_to_go()
        if left != 0:
            return "\n\n  " + left_resp + "\n"
        return ""       

    def do_encounter_action(self, action: any, subject: any, object: any, extra: any) -> tuple[str, bool]:
        resp = ""
        error = False

        if action in [ "charge", "flee", "advance", "retreat", "attack", "press", "block", "shoot" ]:
            resp, error = self.attack_move(action, subject, object)
        else:   
            match action:
                case _:
                    resp = f"can't do action '{action}'"
                    error = True
        return (resp, error)

    # DIALOG ACTIONS ----------------------------------------------------------

    def do_dialog_action(self, action: any, subject: any, object: any, extra: any) -> tuple[str, bool]:
        resp = ""
        error = False
        match action:
            case _:
                resp = f"can't do action '{action}'"
                error = True
        return (resp, error)
    
    # STORE ACTIONS ----------------------------------------------------------

    def do_store_action(self, action: any, subject: any, object: any, extra: any) -> tuple[str, bool]:
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
                ""

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

        match action:
            case "describe":
                resp, error = self.describe_location()
            case "party":
                resp, error = self.describe_party()
            case "topic":
                resp, error = self.topic(subject, object)
            case "give":
                resp, error = self.give(subject, object, extra, extra2)
            case "look":
                resp, error = self.look(subject)
            case "resume":
                resp, error = self.resume()
            case "restart":
                resp, error = self.restart()
            case _:
                match self.cur_game_state_name:
                    case "exploration":
                        resp, error = self.do_expore_action(action, subject, object, extra)
                    case "encounter":
                        resp, error = self.do_encounter_action(action, subject, object, extra)
                    case "dialog":
                        resp, error = self.do_dialog_action(action, subject, object, extra)
                    case "store":
                        resp, error = self.do_store_action(action, subject, object, extra)
                    case _:
                        resp = "unknown game state {self.cur_game_state_name}'"
                        self.cur_game_state_name = "exploration"
                        error = True
        
        if error:
            if self.agent.logging:
                print(f"  ERROR: {resp}")
            return resp
        
        self.save_game()

        return resp
