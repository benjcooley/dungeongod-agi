import agent
import os
import traceback
from dotenv import load_dotenv
import yaml

from agent import Agent
from game import Game

import discord

# Load default environment variables (.env)
load_dotenv()

DEVELOPER_MODE = (os.getenv('DEVELOPER_MODE') == "true")

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
DISCORD_GUILD = os.getenv('DISCORD_GUILD')

AGENT_NAME = os.getenv("AGENT_NAME") or "dungeon-god-agi"
USER_NAME = os.getenv("USER_NAME") or "dungeon-god-agi"

config: dict[str, any] = {}
channel_games: dict[str, any] = {}

with open('config.yaml', 'r') as f:
    config = yaml.load(f, Loader=yaml.FullLoader)

# Create a module per channel
for channel_name, channel_config in config["channels"].items():
    agent = Agent(AGENT_NAME, USER_NAME)
    game = Game(agent, channel_config["module"], channel_config["party"], channel_name)
    actual_channel_name = (f"dev-{channel_name}" if DEVELOPER_MODE else channel_name)
    channel_games[actual_channel_name] = { "agent": agent, "game": game }

intents = discord.Intents.default()
intents.message_content = True
discord_client = discord.Client(intents=intents)
discord_tree = discord.app_commands.CommandTree(discord_client)

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

    channels_to_start = []
    for channel in guild.channels:
        channel_name = channel.name
        if not channel_name in channel_games:
            continue
        channels_to_start.append({ "channel": channel, "game": channel_games[channel_name]["game"] })

    if len(channels_to_start) == 0:
        return

    print(
        f'{discord_client.user} is connected to the following guild:\n'
        f'{guild.name} (id: {guild.id})\n'
    )

    for start_channel in channels_to_start:
        channel: discord.ChannelType = start_channel["channel"]
        game: Game = start_channel["game"]
        if game.is_started:
            continue
        print(f"{channel.name} (id: {channel.id}) (module: {game.module_name}) (party: {game.party_name})")
        try:
            result = game.start_game()
        except:
            result = traceback.format_exc()
        await send_to_channel(channel, result)

@discord_client.event
async def on_message(message):
    if message.author == discord_client.user:
        return

    channel_name = message.channel.name
    if not channel_name in channel_games:
        return

    game: Game = channel_games[channel_name]["game"]
    content = message.content

    # Ignore users messages among themselves
    if content.startswith("!"):
        return

    try:
        result = game.user_action(content)
    except:
        result = traceback.format_exc()
        if agent.logging:
            print(result)

    if result != "":
        await send_to_channel(message.channel, result)   

# make the slash command
#@discord_tree.command(name="restart", description="Restarts game")
#async def slash_command(interaction: discord.Interaction): 
#    if interaction.channel.name != DISCORD_BOT_CHANNEL:
#        return
# 
#    resp = game.restart_command()
#    await interaction.response.send_message(resp)

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
        result = game.user_action(userInput)
        if result != "":
            print(f"\n\033[34mAgent:\033[0m\n{result}")
