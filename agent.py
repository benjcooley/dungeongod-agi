import openai
import os
import pinecone
import tiktoken
import yaml
from dotenv import load_dotenv
from datetime import datetime
import json

# Load default environment variables (.env)
load_dotenv()

RESPONSE_RESERVE=500

OPENAI_MODEL = os.getenv("OPENAI_MODEL") or "gpt-4"
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS") or 8192) - RESPONSE_RESERVE
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE") or 0.3)
OPENAI_SECONDARY_MODEL = os.getenv("OPENAI_SECONDARY_MODEL") or OPENAI_MODEL
OPENAI_SECONDARY_TEMPERATURE = float(os.getenv("OPENAI_SECONDARY_TEMPERATURE") or OPENAI_TEMPERATURE)

PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PINECONE_API_ENV = os.getenv("PINECONE_API_ENV")

token_enc = tiktoken.encoding_for_model(OPENAI_MODEL)

# Counter Initialization
with open('memory_count.yaml', 'r') as f:
    counter = yaml.load(f, Loader=yaml.FullLoader)

# Thought types, used in Pinecone Namespace
THOUGHTS = "Thoughts"
QUERIES = "Queries"
INFORMATION = "Information"
ACTIONS = "Actions"
FACTS = "Facts"

# Top matches length
k_n = 3

# initialize pinecone
#pinecone.init(api_key=PINECONE_API_KEY, environment=PINECONE_API_ENV)

# initialize openAI
openai.api_key = OPENAI_API_KEY

def get_ada_embedding(text):
        text = text.replace("\n", " ")
        return openai.Embedding.create(input=[text], model="text-embedding-ada-002")[
            "data"
        ][0]["embedding"]

class Agent():
    def __init__(self, agent_name, user_name) -> None:
        self.messages = []
        self.previous_memories = ""
        self.previous_response = ""
        self.agent_name = agent_name
        self.user_name = user_name
        self.memory = None
        self.thought_id_count = int(counter['count'])
        self.last_message = ""
        self.logging = True
        self.first_query = True        
        # Prompt Initialization
        with open('prompts/prompts.yaml', 'r') as f:
            self.prompts = yaml.load(f, Loader=yaml.FullLoader)        
        # Creates Pinecone Index
        #self.table_name = table_name
        #self.createIndex(table_name)

    # Keep Remebering!
    # def __del__(self) -> None:
    #     with open('memory_count.yaml', 'w') as f:
    #         yaml.dump({'count': str(self.thought_id_count)}, f)
    

    def createIndex(self, table_name=None):
        # Create Pinecone index
        if(table_name):
            self.table_name = table_name

        if(self.table_name == None):
            return

        dimension = 1536
        metric = "cosine"
        pod_type = "p1"
        if self.table_name not in pinecone.list_indexes():
            pinecone.create_index(
                self.table_name, dimension=dimension, metric=metric, pod_type=pod_type
            )

        # Give memory
        self.memory = pinecone.Index(self.table_name)

    def generate(self, query: str, primary: bool = True, keep: bool = False) -> None:

        query_size = len(token_enc.encode(query))

        if self.logging:
            print("\n----------------------------------------------  QUERY  -------------------------------------------------------\n\n" +
                  f"{query}\n")

        size = query_size
        for msg in self.messages:
            size += msg["tokens"]

        while size > OPENAI_MAX_TOKENS:
            for idx, msg in enumerate(self.messages):
                if not msg["keep"]:
                    size -= msg["tokens"]
                    del self.messages[idx]
                    break

        send_messages = []
        for msg in self.messages:
            send_messages.append({ "role": msg["role"], "content": msg["content"] })

        if query != "":
            query_message = [{ "role": "user", "content": query }]
            save_query_message = [{ "role": "user", "content": query, "keep": keep, "tokens": query_size }]
        else:
            query_message = []
            save_query_message = []

        completion = openai.ChatCompletion.create(
            model=(OPENAI_MODEL if primary else OPENAI_SECONDARY_MODEL),
            temperature=(OPENAI_TEMPERATURE if primary else OPENAI_SECONDARY_TEMPERATURE),
            messages=send_messages + query_message
        )

        response = completion.choices[0].message["content"]

        if self.logging:
            print("\n---------------------------------------------  RESPONSE  -----------------------------------------------------\n\n" +\
                  f"{response}\n")
            print("\n--------------------------------------------------------------------------------------------------------------\n\n")

        self.messages += save_query_message + \
            [{"role": "assistant", "content": response, "keep": False, "tokens": len(token_enc.encode(response))}]

        return response

    def make_prompt(self, prompt_template, args: dict[str, any]=None) -> str:
        exp_prompt = prompt_template
        if args is not None:
            for key, value in args.items():
                exp_prompt = exp_prompt.replace("{" + key + "}", value)
        return exp_prompt

    def make_prompt_from_id(self, prompt_name: str, args: dict[str, any]=None) -> str:
        prompt_template = self.prompts.get(prompt_name)
        if prompt_template is None:
            with open(f"prompts/{prompt_name}.txt", "r") as f:
                prompt_template = f.read()
            self.prompts[prompt_name] = prompt_template

    def remove_hidden(self, text: str) -> str:
        lines = text.split("\n")
        last = len(lines)
        for i in range(len(lines)):
            line = lines[i].strip()
            if "<HIDDEN>" in line or "[HIDDEN]" in line or "<RESPONSE>" in line or "call next_turn(" in line:
                last = i - 1
                break
        if last < 0:
            return ""
        else:
            return "\n".join(lines[:last]).strip(" \n\t")

    # Adds new Memory to agent, types are: THOUGHTS, ACTIONS, QUERIES, INFORMATION
    def updateMemory(self, new_thought, thought_type):
        with open('memory_count.yaml', 'w') as f:
             yaml.dump({'count': str(self.thought_id_count)}, f)

        now = str(datetime.now())

        if thought_type==ACTIONS:
            # Not needed since already in prompts.yaml as external thought memory
            return

        vector = get_ada_embedding(new_thought)
        upsert_response = self.memory.upsert(
        vectors=[
            {
            'id':f"thought-{self.thought_id_count}", 
            'values':vector, 
            'metadata':
                {"thought_string": new_thought,
                 "time": now
                }
            }],
	    namespace=thought_type,
        )

        self.thought_id_count += 1

    def updateFacts(self, facts):
        with open('memory_count.yaml', 'w') as f:
             yaml.dump({'count': str(self.thought_id_count)}, f)

        now = str(datetime.now())

        for fact in facts:
            if len(fact) == 2:
                if "Unknown" in fact[1] or "No specific" in fact[1] or "was not provided" in fact[1]:
                    continue
                vector = get_ada_embedding(fact[0])
                upsert_response = self.memory.upsert(
                vectors=[
                    {
                    'id':f"thought-{self.thought_id_count}", 
                    'values':vector, 
                    'metadata':
                        {"thought_string": fact[0] + " ANSWER: " + fact[1],
                        "time": now
                        }
                    }],
                namespace=FACTS,
                )
                self.thought_id_count += 1

    def queryFacts(self, questions, top_k=5):
        results = []
        for question in questions:
            query_embedding = get_ada_embedding(question)
            query_results = self.memory.query(query_embedding, top_k=2, include_metadata=True, namespace=FACTS)
            results += query_results["matches"]
        sorted_results = sorted(results, key=lambda x: x.score, reverse=True)
        del sorted_results[top_k:]
        return "\nANSWERS:\n\n" + "\n".join([(str(item.metadata["thought_string"])) for item in sorted_results])

    # Agent thinks about given query based on top k related memories. Internal thought is passed to external thought
    def internalThought(self, query) -> str:

        results = []

        if self.first_query:
            user_embedding = get_ada_embedding("Who is {user}?".replace("{user}", self.user_name))
            user_results = self.memory.query(user_embedding, top_k=2, include_metadata=True, namespace=THOUGHTS)
            subject_embedding = get_ada_embedding("What was the previous conversaton with this user about?")
            subject_results = self.memory.query(subject_embedding, top_k=2, include_metadata=True, namespace=THOUGHTS)
            results = results + user_results.matches + subject_results.matches

        query_embedding = get_ada_embedding(query)
        query_results = self.memory.query(query_embedding, top_k=2, include_metadata=True, namespace=QUERIES)
        thought_results = self.memory.query(query_embedding, top_k=2, include_metadata=True, namespace=THOUGHTS)
        results = results + query_results.matches + thought_results.matches

        sorted_results = sorted(results, key=lambda x: x.score, reverse=True)
        top_matches = "\n\n".join([(str(item.metadata["thought_string"])) for item in sorted_results])
        if self.logging:
            print(top_matches)
        
        internalThoughtPrompt = self.prompts['internal_thought']
        internalThoughtPrompt = internalThoughtPrompt \
            .replace("{query}", query) \
            .replace("{previous_memories}", self.previous_memories) \
            .replace("{top_matches}", top_matches) \
            .replace("{previous_response}", self.previous_response)
        #if self.logging:
        #    print("------------INTERNAL THOUGHT PROMPT------------")
        #    print(internalThoughtPrompt)
        internal_thought = self.generate(internalThoughtPrompt) # OPENAI CALL: top_matches and query text is used here
        
        # Debugging purposes
        if self.logging:
            print("\033[32m------------INTERNAL THOUGHT ------------\033[0m")
            print(internal_thought)

        memories = self.previous_memories
        answers = []
        google_search = ""

        try:
            thought_json = json.loads(internal_thought)
            memories = thought_json["memories"]
            remember_list = [[q.strip(" ") for q in v.replace("-->", "^").split("^")] for v in thought_json["to_remember"]]
            if len(remember_list) > 0:
                self.updateFacts(remember_list)
            if len(thought_json["questions"]) > 0:
                answers = self.queryFacts(thought_json["questions"])
            google_search = thought_json["google_search"]
        except:
            pass

        internalMemoryPrompt = self.prompts['internal_thought_memory']
        internalMemoryPrompt = internalMemoryPrompt \
            .replace("{now}", str(datetime.now())) \
            .replace("{query}", query) \
            .replace("{memories}", memories)
        self.updateMemory(internalMemoryPrompt, THOUGHTS)

        return memories, answers, google_search

    def old_action(self, query) -> str:
        if query == "logging: on":
            self.logging = True
            print("Logging enabled.")
            return
        elif query == "logging: off":
            self.logging = False
            print("Logging disabled.")
            return
        
        memories, answers, google_search = self.internalThought(query)
        
        if google_search and not self.first_query:
            return self.search(google_search, user_query=query)

        contextPrompt = self.prompts['context_prompt']
        contextPrompt = contextPrompt \
            .replace("{memories}", memories) \
            .replace("{answers}", "\n".join(answers)) \
            .replace("{now}", str(datetime.now()))
        if self.logging:
            print("\033[32m------------EXTERNAL THOUGHT PROMPT------------\033[0m")
            print(contextPrompt)
        external_thought = self.generate(query) # OPENAI CALL: top_matches and query text is used here

        if self.logging:
            print("\033[32m------------EXTERNAL THOUGHT------------\033[0m")
            print(external_thought)

        processed_thoughts_map = {}

        if "RESPONSE:" in external_thought:
            processed_thoughts = external_thought \
                .replace("REMEMBER THIS:", "~memory^") \
                .replace("RESPONSE:", "~response^") \
                .strip("\n ~^") \
                .split("~")
            try:
                for thought in processed_thoughts:
                    pair = thought.split("^")
                    processed_thoughts_map[pair[0].strip(" \n")] = pair[1].strip(" \n")
            except:
                processed_thoughts_map["response"] = external_thought
        else:
            processed_thoughts_map["response"] = external_thought

        external_memories = ""

        if len(processed_thoughts_map) == 2:
            external_thought = processed_thoughts_map["response"]
            external_memories = processed_thoughts_map["memory"]
        else:
            external_thought = processed_thoughts_map["response"]

        externalMemoryPrompt = self.prompts['external_thought_memory']
        externalMemoryPrompt = externalMemoryPrompt \
            .replace("{now}", str(datetime.now())) \
            .replace("{query}", query) \
            .replace("{external_thought}", external_thought)
        self.updateMemory(externalMemoryPrompt, THOUGHTS)
        
        requestMemoryPrompt = self.prompts["request_memory"]
        requestMemoryPrompt = requestMemoryPrompt \
            .replace("{now}", str(datetime.now())) \
            .replace("{query}", query) \
            .replace("{response}", external_thought)
        self.updateMemory(requestMemoryPrompt, QUERIES)
        
        self.previous_memories = memories + "\n" + external_memories
        self.previous_response = external_thought
        self.first_query = False

        if self.logging:
            print("\033[32m------------ RESPONSE ------------\033[0m")
        
        return external_thought

    # Make agent think some information
    def think(self, text) -> str:
        self.updateMemory(text, THOUGHTS)

    # Make agent read some information
    def read(self, url, user_query="") -> str:

        print(f"Summarizing {url}\n")

        try:
            text = getHtmlText(url)
        except:
            return f"Couldn't read {url}."

        summaryRequest = self.prompts['wepbage_summary_request']
        summaryRequest = summaryRequest \
            .replace("{query}", user_query) \
            .replace("{text}", text[0:5000])
        
        try:
            summary = self.generate(summaryRequest) 
        except:
            return f"Couldn't summarize {url}."

        self.updateMemory(summary, THOUGHTS)

        response = f"I found this on the web:\n\n\033[35m{summary}\033[0m\n"
        self.messages += [{"role": "assistant", "content": response}]
        return response


