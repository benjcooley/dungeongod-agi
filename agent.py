import openai
import os
import pinecone
import tiktoken
import yaml
from dotenv import load_dotenv
from datetime import datetime
import json
import textwrap
import time

# Load default environment variables (.env)
load_dotenv()

RESPONSE_RESERVE=500

AGENT_LOGGING = ((os.getenv('AGENT_LOGGING') or "true") == "true")

OPENAI_MODEL = os.getenv("OPENAI_MODEL") or "gpt-3.5-turbo-16k"
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS") or 16384) - RESPONSE_RESERVE
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE") or 0.3)
OPENAI_PROMPT_COST = float(os.getenv("OPENAI_PROMPT_COST") or 0.003)
OPENAI_GEN_COST = float(os.getenv("OPENAI_GEN_COST") or 0.004)

OPENAI_SECONDARY_MODEL = os.getenv("OPENAI_SECONDARY_MODEL") or OPENAI_MODEL
OPENAI_SECONDARY_MAX_TOKENS = int(os.getenv("OPENAI_SECONDARY_MAX_TOKENS") or OPENAI_MAX_TOKENS)
OPENAI_SECONDARY_TEMPERATURE = float(os.getenv("OPENAI_SECONDARY_TEMPERATURE") or OPENAI_TEMPERATURE)
OPENAI_SECONDARY_PROMPT_COST = float(os.getenv("OPENAI_SECONDARY_PROMPT_COST") or 0.0015)
OPENAI_SECONDARY_GEN_COST = float(os.getenv("OPENAI_SECONDARY_GEN_COST") or 0.002)

PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PINECONE_API_ENV = os.getenv("PINECONE_API_ENV")

token_enc = tiktoken.encoding_for_model(OPENAI_MODEL)

# Counter Initialization
counter={ "count": 1 }
if os.path.exists('memory_count.yaml'):
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
        self.previous_memories = ""
        self.previous_response = ""
        self.agent_name = agent_name
        self.user_name = user_name
        self.memory = None
        self.thought_id_count = int(counter['count'])
        self.last_message = ""
        self.logging = AGENT_LOGGING
        self.first_query = True
        self.prompt_tokens = 0
        self.gen_tokens = 0
        self.secondary_prompt_tokens = 0
        self.secondary_gen_tokens = 0
        self.log_file_name = f"logs/log_{agent_name}_" + datetime.now().strftime('%Y-%m-%d_%H-%M-%S') + ".txt"
        # Prompt Initialization
        with open('data/prompts/prompts.yaml', 'r') as f:
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

    def make_prefix(self, messages: list[str]) -> list[dict]:
        out_prefix = []
        for msg_index, msg in enumerate(messages):
            role = ("user" if msg_index % 2 == 0 else "assistant")
            out_prefix.append({ "role": role, "content": msg })
        return out_prefix

    @staticmethod
    def make_message(role: str, content: str, source: str, keep: bool = False) -> dict[str, any]:
        tokens = len(token_enc.encode(content))
        return { "role": role, "content": content, "source": source, "tokens": tokens, "keep": keep }

    async def chunk_acreate(self, model, messages, temperature=1.0, chunk_handler=None):
        # send a ChatCompletion request
        response = await openai.ChatCompletion.acreate(
            model=model,
            messages=messages,
            temperature=temperature,
            stream=True  # using stream=True
        )

        # create variables to collect the stream of chunks
        collected_chunks = []
        collected_messages = []

        # iterate through the stream of events
        async for chunk in response:
            collected_chunks.append(chunk)  # save the event response
            chunk_message = chunk['choices'][0]['delta']  # extract the message
            collected_messages.append(chunk_message)  # save the message

            if chunk_handler:
                await chunk_handler(chunk_message.get('content', ''), time.time())  # process the chunk

        # combine the messages to form the full response
        full_reply_content = ''.join([m.get('content', '') for m in collected_messages])

        return {
            "full_reply_content": full_reply_content,
            "individual_chunks": collected_chunks
        }

    async def generate(self, messages: list[dict], primary: bool = True, keep: bool = False, maxlen: int = -1, chunk_handler: any = None) -> dict[str, any]:

        # If both models are the same, we're using primary
        if OPENAI_MODEL == OPENAI_SECONDARY_MODEL:
            primary = True

        max_tokens = OPENAI_MAX_TOKENS if primary else OPENAI_SECONDARY_MAX_TOKENS
        if maxlen == -1:
            maxlen = max_tokens
        else:
            maxlen = min(max_tokens, maxlen)

        query = messages[-1]["content"]

        if self.logging:
            print("\n----------------------------------------------  QUERY  -------------------------------------------------------\n\n" +
                  f"{query}\n")
        
        size = 0
        for msg in messages:
            size += msg["tokens"]

        # shallow copy so we can modify this list
        messages = messages.copy()

        # Make sure message fits in max context size
        while size > maxlen:
            for idx, msg in enumerate(messages):
                if not msg["keep"]:
                    size -= msg["tokens"]
                    del messages[idx]
                    break

        send_messages = []
        for msg in messages:
            send_messages.append({ "role": msg["role"], "content": msg["content"] })

        if primary:
            self.prompt_tokens += size
        else:
            self.secondary_prompt_tokens += size

        model = OPENAI_MODEL if primary else OPENAI_SECONDARY_MODEL
        temp = OPENAI_TEMPERATURE if primary else OPENAI_SECONDARY_TEMPERATURE

        if not chunk_handler:
            completion = await openai.ChatCompletion.acreate(
                model=model,
                temperature=temp,
                messages=send_messages
            )
            response = completion.choices[0].message["content"]
        else:
            completion_pair = await self.chunk_acreate(
                model=model,
                temperature=temp,
                messages=send_messages,
                chunk_handler=chunk_handler
            )
            response = completion_pair["full_reply_content"]

        resp_size = len(token_enc.encode(response))

        if primary:
            self.gen_tokens += resp_size
        else:
            self.secondary_gen_tokens += resp_size

        if primary:
            prompt_tokens = self.prompt_tokens
            prompt_cost = prompt_tokens * OPENAI_PROMPT_COST * 0.001
            gen_tokens = self.gen_tokens
            gen_cost = gen_tokens * OPENAI_GEN_COST * 0.001
        else:
            prompt_tokens = self.secondary_prompt_tokens
            prompt_cost = prompt_tokens * OPENAI_SECONDARY_PROMPT_COST * 0.001
            gen_tokens = self.secondary_gen_tokens
            gen_cost = gen_tokens * OPENAI_SECONDARY_GEN_COST * 0.001

        if self.logging:
            print("\n---------------------------------------------  RESPONSE  -----------------------------------------------------\n\n" +\
                  f"{response}\n\n" + \
                  f"    {model} - new_tokens: {size+resp_size} prompt_tokens: {prompt_tokens} prompt_cost: {prompt_cost:.2f} gen_tokens: {gen_tokens} gen_cost: {gen_cost:.2f}\n")
            print("\n--------------------------------------------------------------------------------------------------------------\n\n")

        # Write out log
        if not os.path.exists("logs"):
            os.makedirs("logs")
        with open(self.log_file_name, "a") as f:
            indent_query = textwrap.indent(query, prefix="    ")
            resp_lines = str.splitlines(response)
            indent_response = ""
            for line in resp_lines:
                indent_response += textwrap.fill(line, width=100) + "\n"
            indent_response = textwrap.indent(indent_response, prefix="    ")
            f.write(f"USER:\n\n{indent_query}\n\nASSISTANT:\n\n{indent_response}\n\n")

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


