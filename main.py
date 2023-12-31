import asyncio
import io
import os
import traceback
from dotenv import load_dotenv
import yaml

from agent import Agent
from engine import Engine
from lobby import Lobby
from game import Game
from timer import Timer
from filedb import FileDb
from user import User, get_user

import discord

# Load default environment variables (.env)
load_dotenv()

DEVELOPER_MODE = (os.getenv('DEVELOPER_MODE') == "true")
ERROR_LOGGING = ((os.getenv('ERROR_LOGGING') or "true") == "true")
CONFIG_TAG = ("dev" if DEVELOPER_MODE else "prod")

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

config: dict[str, any] = {}

active_sessions: dict[str, any] = {}
channel_states: dict[str, any] = {}

timer_updater: Timer = None

with open('config.yaml', 'r') as f:
    config_all = yaml.load(f, Loader=yaml.FullLoader)
    config = config_all[CONFIG_TAG]

CHANNEL_PREFIX = config.get("channel_prefix")
BOT_NAME = config.get("bot_name")
BOT_ID = config.get("bot_id")
DEV_DISCORD_GUILD = config.get("dev_guild")
DEV_DISCORD_GUILD_ID = config.get("dev_guild_id")

intents = discord.Intents.none()
intents.guilds = True
intents.members = True
intents.guild_messages = True
intents.message_content = True
discord_client = discord.Client(intents=intents)
discord_tree = discord.app_commands.CommandTree(discord_client)

engine = Engine(FileDb())
engine.set_defaults(config["default_party_name"], config["default_module_name"])

# ------------------
# Start Game Session
# ------------------

async def start_session(user: discord.User, 
                        guild: discord.Guild, 
                        action: str = "lobby",
                        channel: discord.TextChannel = None,
                        create_thread: bool = False,
                        thread: discord.Thread = None,
                        party_name: str = None,
                        module_name: str = None,
                        save_game_name: str = None) -> (str, bool, dict[str, any]):
    
    user_name = user.name
    user_id = user.id

    # Get or create user
    game_user: User = await get_user(engine.db, user_name, user_id)

    # Make sure user has at least one party
    err_str, err, _ = await engine.load_default_party(game_user)
    if err:
        return (err_str, err, None)

    if thread:
        channel_name = thread.name
        session_id = str(thread.id)
        session = active_sessions.get(session_id)
    elif create_thread:
        channel_name = f"{user_name}'s Game"
        session_id = None
        session = None
    elif channel:
        channel_name = channel.name
        session_id = str(channel.id)
        session = active_sessions.get(session_id)
    else:
        assert False

    if action == "new_game":
        err_str, err = await engine.can_play_game(game_user,
                                            module_name=module_name, 
                                            party_name=party_name)
        if err:
            return (err_str, err, None)
    elif action == "resume_game":
        err_str, err = await engine.can_resume_game(game_user, 
                                              module_name=module_name,
                                              party_name=party_name,
                                              save_game_name=save_game_name)
        if err:
            return (err_str, err, None)
    elif action == "lobby":
        pass
    else:
        assert False

    if session is None:
        agent: Agent = Agent(channel_name, user_name)
        lobby: Lobby = Lobby(engine, game_user, agent)
    else:
        agent: Agent = session["agent"]
        lobby: Lobby = session["lobby"]

    if action == "new_game" or action == "resume_game":
        game: Game = Game(engine, 
                        game_user, 
                        agent,
                        start_game_action=action,
                        module_name=module_name, 
                        party_name=party_name,
                        save_game_name=save_game_name)
    elif session is not None:
        game: Game = session["game"]
    else:
        game: Game = None

    new_thread = False
    if create_thread and not thread:        
        thread = discord.utils.get(channel.threads, name=channel_name)
        if not thread:
            assert channel is not None
            # Create a new thread for this user if needed
            thread = await channel.create_thread(
                name=channel_name,
                type=discord.ChannelType.private_thread
            )
            # Add the bot/user to the thread
            await thread.add_user(discord_client.user)
            await thread.add_user(user)
            new_thread = True
        thread.join()
        session_id = str(thread.id)

    # Remove all other sessions for this user. Delete any private thraed.
    remove_sessions = []
    for rem_session_id, rem_session in active_sessions.items():
        if rem_session["user"].id == user.id and \
                rem_session["is_thread"] and \
                rem_session_id != session_id:
            remove_sessions.append(rem_session_id)
    for rem_session_id in remove_sessions:
        rem_session = active_sessions[rem_session_id]
        rem_thread: discord.Thread = rem_session["channel"]
        if rem_thread and rem_thread.type == discord.ChannelType.private_thread:
            try:
                await rem_thread.delete()
            except:
                pass
        del active_sessions[rem_session_id]

    # Keep track of the session.
    session = {
        "agent": agent,
        "engine": engine,
        "game": game,
        "lobby": lobby,
        "user": game_user,
        "channel": (thread or channel),
        "is_thread": (thread is not None),
        "channel_id": (thread.id if thread else channel.id)
    }
    active_sessions[session_id] = session

    # Remember what state this session is in to recover it on restart.
    channel_state = {
        "mode": ("lobby" if action == "lobby" else "game"),
        "user": user_name,
        'user_id': user_id,
        "is_thread": (thread is not None),
        "channel_id": (thread.id if thread else channel.id)
    }
    await engine.set_channel_state(guild.id, (thread.id if thread else channel.id), channel_state)

    result = { "new_thread": new_thread, "thread": thread, "session": session }
    return ("ok", False, result)

# ------------------
# Dev Channels
# ------------------

async def start_dev_channels(guild: discord.Guild) -> None:

    user = discord.utils.get(guild.members, name=config["dev_user"])

    for channel in guild.channels:
        channel_name = channel.name
        if channel_name in config["dev_channels"]:

            module_name = config["dev_channels"][channel_name]["module_name"]
            party_name = config["dev_channels"][channel_name]["party_name"]
            
            print(f"{channel.name} (id: {channel.id}) (module: {module_name}) (party: {party_name})")

            err_str, err, info = await start_session(user, 
                                                        guild, 
                                                        action="resume_game", 
                                                        channel=channel,
                                                        module_name=module_name,
                                                        party_name=party_name,
                                                        save_game_name=channel_name)
            if err:
                await channel.send(err_str)
                return
            
            channel_session = info["session"]
            game: Game = channel_session["game"]
            
            if not game.is_started:
                try:
                    result = await game.start_game()
                except:
                    result = traceback.format_exc()
            
                await send_to_channel(channel, result, game)

# ------------------
# Message Sender
# ------------------

async def send_to_channel_msg(channel: discord.TextChannel, msg: str) -> discord.Message:
    if not msg:
        return
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
    if len(msg) > 2000:
        msg = msg[:2000]
    if image_path is not None:
        return await channel.send(msg, file=discord.File(image_path))
    else:
        return await channel.send(msg)

async def send_to_channel(channel: discord.TextChannel, msg: str, game: Game = None, split_lines: bool = True) -> None:
    if not msg:
        return
    sent_message: bool = False

    # Should we split this message into multiple messages with dialog portraits?
    if game and split_lines:
        split_msgs = game.split_dialog(msg)
        if len(split_msgs) > 1:
            for index, split_msg in enumerate(split_msgs):
                last_para = index == len(split_msgs) - 1
                if isinstance(split_msg, bytes):
                    await channel.send(file=discord.File(io.BytesIO(split_msg), filename="dialog.png"))
                else:
                    await send_to_channel_msg(channel, split_msg)
                if not last_para:
                    await asyncio.sleep(1)
            sent_message = True

    # Send message if we haven't split above
    if not sent_message:
        await send_to_channel_msg(channel, msg)
    
    # Show button menu?
    if game and game.button_tag is not None:
        button_tag = game.button_tag
        game.button_tag = None
        await show_button_menu(game, channel, button_tag)

async def show_button_menu(game: Game, channel: discord.TextChannel, button_tag: str) -> None:
    state = {}
    view = discord.ui.View()
    state["view"] = view
    state["channel"] = channel

    async def clicked(interaction: discord.Interaction, index: int) -> None:
        state["clicked_index"] = index
        await interaction.response.defer()
        message: discord.Message = state["message"]
        channel: discord.TextChannel = state["channel"]
        view: discord.ui.View = state["view"]
        view.clear_items()
        resp, has_buttons = await game.get_buttons(button_tag, state)
        if not has_buttons:
            await message.edit(content=state["sentence"], view=view)
            if state["action"] == "say" or state["action"] == "ask":
                await send_to_channel(channel, state["sentence"], game)
                resp = await game.player_action(state["sentence"])
            else:
                args = [ state["subject"], state["object"], state["extra"], state["extra2"] ]
                resp = await game.call_action(state["action"], args)
            channel: discord.TextChannel = state["channel"]
            await send_to_channel(channel, resp, game)
        else:
            for index, button_info in enumerate(state["buttons"]):
                button = discord.ui.Button(label=button_info["text"])
                callback: callable[discord.Interaction, int] = state["callback"]
                button.callback = lambda interaction, index=index: callback(interaction, index)
                view.add_item(button)
            await message.edit(content=state["choices"] + state["sentence"], view=view)

    state["callback"] = clicked
    resp, has_buttons = await game.get_buttons(button_tag, state)
    if not has_buttons:
        return resp
    for index, button_info in enumerate(state["buttons"]):
        button = discord.ui.Button(label=button_info["text"])
        button.callback = lambda interaction, index=index: clicked(interaction, index)
        view.add_item(button)
    state["message"] = await channel.send(content=state["choices"] + state["sentence"], view=view)

@discord_client.event
async def on_ready():

    # Start the timer 
    global timer_updater
    timer_updater = Timer(1.0, timer_update_func)

    # Sync our command tree here
    await discord_tree.sync()

    # Start our dev channels
    if DEVELOPER_MODE:
        dev_guild = discord.utils.get(discord_client.guilds, name=DEV_DISCORD_GUILD)
        if dev_guild is not None:
            await start_dev_channels(dev_guild)

# ----------------------
# Meain Message Handler
# ----------------------

@discord_client.event
async def on_message(message: discord.Message):

    user = message.author
    guild = message.guild
    channel = message.channel
    game: Game = None

    # Ignore our own msgs
    if message.author != discord_client.user:

        # Check to see if we should respond to this msg
        session_id = str(channel.id)
        if session_id in active_sessions:
            # We have a session for this channel.. go
            channel_session = active_sessions[session_id]
        else:
            # No session, check if we restart a saved session?
            channel_state = await engine.get_channel_state(guild.id, channel.id)
            if channel_state is None:
                # No saved sessions either.. ignore this msg
                return
            else:
                # Restart a session if it's in the saved channel state (so we can warm load sessions on
                # reload and players don't lose their state.)
                if channel_state["mode"] == "game":
                    err_str, err, info = await start_session(user, guild, action="resume_game", thread=channel)
                else:
                    err_str, err, info = await start_session(user, guild, action="lobby", thread=channel)
                if err:
                    channel.send(err_str)
                    return
                channel_session = info["session"]
                try:
                    if channel_session["game"] is not None:
                        game: Game = channel_session["game"]
                        _ = await game.start_game()
                    else:
                        lobby: Lobby = channel_session["lobby"]
                        _ = await lobby.start_lobby()
                except:
                    channel.send(traceback.format_exc())
                    return

        channel_state = await engine.get_channel_state(guild.id, channel.id)

        lobby: Lobby = channel_session["lobby"]
        game: Game|None = channel_session["game"]

        content = message.content

        # Ignore users messages among themselves
        if content.startswith("!"):
            return

        try:
            # If game is running, call game, otherwise call the lobby
            if game is not None and game.is_started and not game.game_over:
                result = await game.player_action(content)
                # Handle returning to lobby from game
                if game.exit_to_lobby:
                    game.exit_to_lobby = False
                    channel_session["game"] = None
                    # Save the current state for this channel
                    channel_state["mode"] = "lobby"
                    await engine.set_channel_state(guild.id, channel.id, channel_state)               
                    # Restart the lobby   
                    result = await lobby.start_lobby()
            else:
                result = await lobby.player_action(content)
                # Handle starting a game from lobby
                if lobby.start_the_game:
                    start_game_action = lobby.start_game_action
                    module_name = lobby.start_game_module_name
                    party_name = lobby.start_game_party_name
                    save_game_name = lobby.start_game_save_game_name
                    lobby.start_the_game = False
                    lobby.start_game_module_name = lobby.start_game_party_name = None
                    game: Game = Game(engine, 
                                    channel_session["user"], 
                                    channel_session["agent"], 
                                    start_game_action=start_game_action,
                                    module_name=module_name, 
                                    party_name=party_name,
                                    save_game_name=save_game_name)
                    channel_session["game"] = game
                    # Save the current state for this channel
                    channel_state["mode"] = "game"
                    await engine.set_channel_state(guild.id, channel.id, channel_state)               
                    result = await game.start_game()
        except:
            result = traceback.format_exc()
            if ERROR_LOGGING:
                print(result)

        if result != "":
            await send_to_channel(message.channel, result, game)

# ----------------------
# Commands
# ----------------------

# make the slash command
#@discord_tree.command(name="dgod_new_game", description="Creates a channel where you can start a new game.", 
#                      guild=discord.Object(id=DEV_DISCORD_GUILD_ID))
@discord_tree.command(name="dgod_new_game", description="Start a new DungeonGod game module with an existing party.")
async def dgod_new_game(interaction: discord.Interaction, party_name: str = engine.default_party_name, module_name: str = engine.default_module_name): 

    if party_name == "":
        party_name = engine.default_party_name
    if module_name == "":
        module_name = engine.default_module_name

    channel = interaction.channel

    root_channel_name = (getattr(channel, "parent", None) or channel).name
    if not root_channel_name.startswith(CHANNEL_PREFIX):
        await interaction.response.send_message(f"DungeonGod commands are only available in channels or threads of channels with a \"{CHANNEL_PREFIX}\" prefix.")
        return

    # We create a private thread if this command is called in a channel
    is_thread: bool = (channel.type == discord.ChannelType.private_thread or 
                       channel.type == discord.ChannelType.public_thread)
    thread: discord.Thread = (channel if is_thread else None)

    err_str, err, result = await start_session(
                                                interaction.user, 
                                                interaction.guild, 
                                                action="new_game",
                                                channel=channel,
                                                create_thread=not is_thread,
                                                thread=thread,
                                                module_name=module_name, 
                                                party_name=party_name 
                                              )

    if err:
        await channel.send(err_str)
        return

    session = result["session"]
    game: Game = session["game"]
    is_thread = session["is_thread"]
    thread = (session["channel"] if is_thread else None)

    try:
        if thread is not None and thread.id != channel.id:
            await interaction.response.send_message(f"Your game is started in thread <#{thread.id}>.", ephemeral=True, delete_after=30)
        else:
            await interaction.response.send_message(f"Starting game..")
        result = await game.start_game()
    except:
        result = traceback.format_exc()
        if ERROR_LOGGING:
            print(result)

    if result != "":
        await send_to_channel(thread or channel, result, game)

# make the slash command
#@discord_tree.command(name="dgod_resume_game", description="Creates a thread and resumes the last game.", 
#                      guild=discord.Object(id=DEV_DISCORD_GUILD_ID))
@discord_tree.command(name="dgod_resume_game", description="Resume your last DungeonGod game in progress.")
async def dgod_resume_game(interaction: discord.Interaction): 

    channel = interaction.channel

    root_channel_name = (getattr(channel, "parent", None) or channel).name
    if not root_channel_name.startswith(CHANNEL_PREFIX):
        await interaction.response.send_message(f"DungeonGod commands are only available in channels or threads of channels with a \"{CHANNEL_PREFIX}\" prefix.")
        return   

    # We create a private thread if this command is called in a channel
    is_thread: bool = (channel.type == discord.ChannelType.private_thread or 
                       channel.type == discord.ChannelType.public_thread)
    thread: discord.Thread = (channel if is_thread else None)

    err_str, err, result = await start_session(
                                                interaction.user, 
                                                interaction.guild, 
                                                action="resume_game",
                                                channel=channel, 
                                                create_thread=not is_thread,
                                                thread=thread
                                              )


    if err:
        await channel.send(err_str)
        return

    session = result["session"]
    game: Game = session["game"]
    is_thread = session["is_thread"]
    thread = (session["channel"] if is_thread else None)

    try:
        if thread is not None and thread.id != channel.id:
            await interaction.response.send_message(f"Your game has been resumed in thread <#{thread.id}>.", ephemeral=True, delete_after=30)
        else:
            await interaction.response.send_message(f"Resuming game..")
        result = await game.start_game()
    except:
        result = traceback.format_exc()
        if ERROR_LOGGING:
            print(result)

    if result != "":
        await send_to_channel(thread or channel, result, game)

# make the slash command
#@discord_tree.command(name="dgod_lobby", description="Creates a lobby thread to build a party and start a game.", 
#                      guild=discord.Object(id=DEV_DISCORD_GUILD_ID))
@discord_tree.command(name="dgod_lobby", description="Opens a DungeonGod lobby to manage your parties and start a game.")
async def dgod_lobby(interaction: discord.Interaction): 

    channel = interaction.channel

    root_channel_name = (getattr(channel, "parent", None) or channel).name
    if not root_channel_name.startswith(CHANNEL_PREFIX):
        await interaction.response.send_message(f"DungeonGod commands are only available in channels or threads of channels with a \"{CHANNEL_PREFIX}\" prefix.")
        return

    # We create a private thread if this command is called in a channel
    is_thread: bool = (channel.type == discord.ChannelType.private_thread or 
                       channel.type == discord.ChannelType.public_thread)
    thread: discord.Thread = (channel if is_thread else None)

    err_str, err, result = await start_session(
                                                interaction.user, 
                                                interaction.guild, 
                                                action="lobby",
                                                channel=channel,
                                                create_thread=not is_thread,
                                                thread=thread,
                                              )

    if err:
        await channel.send(err_str)
        return

    session = result["session"]
    lobby: Lobby = session["lobby"]
    is_thread = session["is_thread"]
    thread = (session["channel"] if is_thread else None)

    try:
        if thread is not None and thread.id != channel.id:
            await interaction.response.send_message(f"Your lobby has been started in thread <#{thread.id}>.", ephemeral=True, delete_after=30)
        else:
            await interaction.response.send_message(f"Starting lobby..")
        result = await lobby.start_lobby()

    except:
        result = traceback.format_exc()
        if ERROR_LOGGING:
            print(result)

    if result != "":
        await send_to_channel(thread or channel, result)

async def timer_update_func() -> None:
    for session in active_sessions.values():
        game: Game = session["game"]
        channel: discord.TextChannel = session["channel"]
        if game.game_started and not game.game_over and not game.exit_to_lobby:
            resp = await game.timer_update()
            if resp:
                await send_to_channel(channel, resp, game)

# Run as a bot on discord
discord_client.run(DISCORD_TOKEN)
