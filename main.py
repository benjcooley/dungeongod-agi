import agent
import os
import traceback
from dotenv import load_dotenv

from agent import Agent
from game import Game

import discord

# Load default environment variables (.env)
load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
DISCORD_GUILD = os.getenv('DISCORD_GUILD')
DISCORD_BOT_CHANNEL = os.getenv('DISCORD_BOT_CHANNEL') or "general"

AGENT_NAME = os.getenv("AGENT_NAME") or "my-agent"
USER_NAME = os.getenv("USER_NAME") or "Anonymous"

agent = Agent(AGENT_NAME)
game = Game(agent, "Encounter Test", "Band of Heroes")

intents = discord.Intents.default()
intents.message_content = True
discord_client = discord.Client(intents=intents)

async def send_to_channel(channel: any, msg: str) -> None:
    lines = msg.splitlines()
    image_path = None
    out_lines = []
    for line in lines:
        if line.startswith("@image: "):
            image_path = line[8:]
            if not os.path.exists(image_path):
                image_path = None
        else:
            out_lines.append(line)
    msg = "\n".join(out_lines)
    if image_path is not None:
        await channel.send(msg, file=discord.File(image_path))
    else:
        await channel.send(msg)

@discord_client.event
async def on_ready():

    guild = discord.utils.get(discord_client.guilds, name=DISCORD_GUILD)
    if guild is None:
        return

    channel = discord.utils.get(guild.channels, name=DISCORD_BOT_CHANNEL)
    if channel is None:
        return

    print(
        f'{discord_client.user} is connected to the following guild:\n'
        f'{guild.name} (id: {guild.id})'
        f'{channel.name} (id: {channel.id})'
    )

    if game.is_started:
        return

    try:
        result = game.start_game()
    except:
        result = traceback.format_exc()
    await send_to_channel(channel, result)

@discord_client.event
async def on_message(message):
    if message.author == discord_client.user:
        return

    if message.channel.name != DISCORD_BOT_CHANNEL:
        return

    content = message.content
    if content.startswith("!"):
        return

    try:
        result = game.action(content)
    except:
        result = traceback.format_exc()
    await send_to_channel(message.channel, result)   

if DISCORD_TOKEN:
    # Run as a bot on discord
    discord_client.run(DISCORD_TOKEN)
else:
    # Run locally using console
    result = game.start_game()
    print(f"\n\033[34mAgent:\033[0m\n{result}")    

    while True:
        print("\n\033[33mUser:\033[0m")
        userInput = input()
        result = game.action(userInput)
        if result != "":
            print(f"\n\033[34mAgent:\033[0m\n{result}")
