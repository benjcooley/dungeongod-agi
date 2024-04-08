import copy
import openai
import os
import textwrap
import tiktoken
import time

from abc import abstractmethod
from agent import Agent
from config import AGENT_LOGGING, config_all
from datetime import datetime
from typing import Any
from engine import EngineManager

RESPONSE_RESERVE=500

class OpenAIModule:
    
    @property
    @abstractmethod
    def ChatCompletion(self) -> openai.ChatCompletion:
        pass

class ModelState:

    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.gen_tokens = 0

class AgentOpenAIV1Base(Agent):

    def __init__(self, agent_tag: str, model_endpoint: str) -> None:
        self._agent_tag = agent_tag
        self.use_async = True
        self.include_model_in_call = True
        endpoint_cfg: dict[str, Any] = config_all["model_endpoints"][model_endpoint]
        self._primary_model_id = endpoint_cfg["primary"]["model"]
        self._secondary_model_id = endpoint_cfg.get("secondary", {}).get("model")
        # Primary model
        self.primary_model_config: dict[str, Any] = copy.deepcopy(config_all["model_info"].get(self._primary_model_id, {}))
        self.primary_model_config.update(endpoint_cfg["primary"])
        self.primary_token_enc = tiktoken.encoding_for_model(self.primary_model_config.get("tokenizer_model", "gpt-4"))
        self.primary_model_state = ModelState()
        # Secondary model
        self.secondary_model_config: dict[str, Any]
        if self._secondary_model_id is not None and self._secondary_model_id != self._primary_model_id:
            self.secondary_model_config: dict[str, Any] = copy.deepcopy(config_all["model_info"].get(self._secondary_model_id, {}))
            self.secondary_model_config.update(endpoint_cfg["secondary"])
            self.secondary_token_enc = tiktoken.encoding_for_model(self.secondary_model_config.get("tokenizer_model", "gpt-4"))
        else:
            self._secondary_model_id = self._primary_model_id
            self.secondary_model_config = self.primary_model_config
            self.secondary_token_enc = self.primary_token_enc
        self.secondary_model_state = ModelState()
        # Other
        self.openai: OpenAIModule = self.init_openai()
        self.logging = AGENT_LOGGING
        self.log_file_name = f"logs/log_{self._agent_tag}_" + datetime.now().strftime('%Y-%m-%d_%H-%M-%S') + ".txt"

    @abstractmethod
    def init_openai() -> OpenAIModule:
        pass

    @property
    def agent_id(self) -> str:
        return "openai_v1"

    @property
    def agent_tag(self) -> str:
        return self._agent_tag

    @property
    def primary_model_id(self) -> str:
        return self._primary_model_id

    @property
    def secondary_model_id(self) -> str:
        return self._secondary_model_id

    def make_prefix(self, messages: list[str]) -> list[dict]:
        out_prefix = []
        for msg_index, msg in enumerate(messages):
            role = ("user" if msg_index % 2 == 0 else "assistant")
            out_prefix.append({ "role": role, "content": msg })
        return out_prefix

    def make_message(self, role: str, content: str, source: str, keep: bool = False, primary: bool = False) -> dict[str, Any]:
        token_enc = self.primary_token_enc if primary else self.secondary_token_enc
        tokens = len(token_enc.encode(content))
        return { "role": role, "content": content, "source": source, "tokens": tokens, "keep": keep }

    def make_prompt(self, prompt_template, args: dict[str, Any]|None=None) -> str:
        exp_prompt = prompt_template
        if args is not None:
            for key, value in args.items():
                exp_prompt = exp_prompt.replace("{" + key + "}", value)
        return exp_prompt

    def remove_hidden(self, text: str) -> str:
        lines = text.split("\n")
        last = len(lines)
        for i in range(len(lines)):
            line = lines[i].strip()
            if "<HIDDEN>" in line or "[HIDDEN]" in line or "<RESPONSE>" in line or "call do_action(" in line:
                last = i - 1
                break
        if last < 0:
            return ""
        else:
            return "\n".join(lines[:last]).strip(" \n\t")
        
    async def chunk_acreate(self, model, messages, temperature=1.0, chunk_handler=None) -> dict[str, Any]:
        # send a ChatCompletion request
        response: Any = await self.openai.ChatCompletion.acreate(
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

    async def generate(self, 
                       messages: list[dict], 
                       primary: bool = True, 
                       maxlen: int = -1, 
                       chunk_handler: Any = None) -> str:

        # If both models are the same, we're using primary
        if self.primary_model_config == self.secondary_model_config:
            primary = True

        model_config = self.primary_model_config if primary else self.secondary_model_config
        model_state = self.primary_model_state if primary else self.secondary_model_state
        token_enc = self.primary_token_enc if primary else self.secondary_token_enc

        max_tokens = model_config.get("max_tokens", 2048) 
        if maxlen == -1:
            maxlen = max_tokens
        else:
            maxlen = min(max_tokens, maxlen)

        query = messages[-1]["content"]

        if EngineManager.logging:
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

        model_state.prompt_tokens += size

        if self.include_model_in_call:
            model = model_config.get("model", "gpt-3.5-turbo")
        else:
            model = ""
        temp = model_config.get("temperature", 0.3)

        try:
            if self.use_async:
                if not chunk_handler:
                    completion: Any = await self.openai.ChatCompletion.acreate(
                        model=model,
                        temperature=temp,
                        messages=send_messages
                    )
                    response = completion.choices[0].message["content"]
                else:
                    completion_pair: Any = await self.chunk_acreate(
                        model=model,
                        temperature=temp,
                        messages=send_messages,
                        chunk_handler=chunk_handler
                    )
                    response = completion_pair["full_reply_content"]
            else:
                # Ignore streaming handler
                completion: Any = self.openai.ChatCompletion.create(
                    temperature=temp,
                    messages=send_messages
                )
                response = completion["choices"][0]["message"]["content"]
        except Exception as error:
            print(error)
            response = ""

        resp_size = len(token_enc.encode(response))

        model_state.gen_tokens += resp_size

        prompt_tokens = model_state.prompt_tokens
        prompt_cost = prompt_tokens * model_config.get("prompt_cost", 0.0) * 0.001
        gen_tokens = model_state.gen_tokens
        gen_cost = gen_tokens * model_config.get("gen_cost", 0.0) * 0.001

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



