from .engine_hoa import EngineHoa, ChatbotDef
from .game_hoa_openai_v1 import GameHoaOpenAIV1
from .lobby_hoa_openai_v1  import LobbyHoaOpenAIV1

from games.hoa.game_hoa import GameHoa
from games.hoa.lobby_hoa import LobbyHoa

from agents.agent_openai_v1 import OpenAIAgentV1

from engine import EngineManager

def register_engine_hoa() -> None:
    EngineHoa.game_type = GameHoa
    EngineHoa.lobby_type = LobbyHoa
    EngineManager.register_engine("hoa", EngineHoa)

def register_chatbot_hoa_openai_v1() -> None:

    chatbot_def: ChatbotDef = {
        "create_chatbot_agent": OpenAIAgentV1,
        "create_chatbot_game": GameHoaOpenAIV1,
        "create_chatbot_lobby": LobbyHoaOpenAIV1
    }

    EngineHoa.register_chatbot("openai_v1", chatbot_def)

