import openai
import os

from agents.agent_openai_v1_base import OpenAIModule
from .agent_openai_v1_base import AgentOpenAIV1Base

from typing import Any, cast

class OpenAIAgentV1(AgentOpenAIV1Base):

    def init_openai(self) -> OpenAIModule:
        OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
        assert OPENAI_API_KEY is not None
        openai.api_key = OPENAI_API_KEY
        OPENAI_API_BASE = os.getenv("OPENAI_API_BASE")
        if OPENAI_API_BASE:
            openai.api_base = OPENAI_API_BASE
        return cast(OpenAIModule, openai)

