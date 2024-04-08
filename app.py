# The game as a web service (for the OpenAI GPT environment)
import sys
sys.path.append("src")

# Define main event loop
import asyncio
event_loop = asyncio.new_event_loop()
asyncio.set_event_loop(event_loop)

# Load default environment variables (.env)
from dotenv import load_dotenv
load_dotenv()

# Init the config
import config
config.init_config()

# Register the games available
from games.hoa import register_game_hoa #type: ignore
register_game_hoa()

# Run the web service
from  web_service import app
if __name__ == "__main__":
    print("Running debug webserver")
    app.run(host="localhost", port=8000)
