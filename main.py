import sys
sys.path.append("src")

# Load default environment variables (.env)
from dotenv import load_dotenv
load_dotenv()

# Init the config
import config
config.init_config()

# Register the games available
import games.hoa as hoa #type: ignore
hoa.register_engine_hoa()
hoa.register_chatbot_hoa_openai_v1()

# Run the discord bot
import discord_chatbot as discord_chatbot #type: ignore
discord_chatbot.run_discord_chatbot()
