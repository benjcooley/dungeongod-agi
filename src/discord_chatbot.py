import asyncio
from datetime import datetime
import io
import os
import re
import traceback

from agent import Agent
from config import ERROR_LOGGING, DEVELOPER_MODE, config, config_all
from engine import Engine, EngineManager
from filedb import FileDb
from game import ChatGameDriver
from lobby import ChatLobbyDriver
from user import User, get_user
from typing import Any, cast, Callable, Type, TypedDict

import discord

MESSAGE_RATE_LIMIT = int(os.getenv('MESSAGE_RATE_LIMIT') or 5)

# Get the discord token
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
assert DISCORD_TOKEN is not None

# Get agent, primary/secondary model to use
MODEL_ENDPOINT = os.getenv('MODEL_ENDPOINT')
assert MODEL_ENDPOINT is not None

class ChatbotGameSession:

    def __init__(self, 
             agent: Agent,
             engine: Engine,
             game: ChatGameDriver|None,
             lobby: ChatLobbyDriver|None,
             game_user: User,
             channel: discord.TextChannel,
             is_thread: bool,
             channel_id: int):
        self.agent = agent
        self.engine = engine
        self.game: ChatGameDriver|None = game
        self.lobby: ChatLobbyDriver|None = lobby
        self.game_user = game_user
        self.channel = channel
        self.is_thread = is_thread
        self.channel_id = channel_id
        self.messages: list[tuple[str, str]] = [] # User messages (user_name, msg) in post order since last response
        self.last_message_time = datetime.min
        self.last_response_time = datetime.min   # Last resposne time

active_sessions: dict[str, ChatbotGameSession] = {}

CHANNEL_PREFIX: str = config.get("channel_prefix", "")
BOT_NAME: str = config.get("bot_name", "")
BOT_ID: str = config.get("bot_id", "")
GAME: str = config.get("game", "")
DEV_DISCORD_GUILD: str = config.get("dev_guild", "")
DEV_DISCORD_GUILD_ID: str = config.get("dev_guild_id", "")

intents = discord.Intents.none()
intents.guilds = True
intents.members = True
intents.guild_messages = True
intents.message_content = True
discord_client = discord.Client(intents=intents)
discord_tree = discord.app_commands.CommandTree(discord_client)

engine_class: Type[Engine] = EngineManager.get_engine(GAME)
engine: Engine = engine_class(FileDb(), logging=ERROR_LOGGING)
engine.set_defaults(config["default_party_name"], config["default_module_name"])

# ------------------
# Start Game Session
# ------------------

class StartSessionResult(TypedDict):
    new_thread: bool
    thread: discord.Thread|None
    session: ChatbotGameSession

async def start_session(user: discord.User, 
                        guild: discord.Guild, 
                        action: str = "lobby",
                        channel: discord.TextChannel|None = None,
                        create_thread: bool = False,
                        thread: discord.Thread|None = None,
                        party_name: str|None = None,
                        module_name: str|None = None,
                        save_game_name: str|None = None) -> tuple[str, bool, StartSessionResult|None]:
    
    user_name = user.name
    user_id = user.id

    # Get or create user
    game_user: User = await get_user(engine.db, user_name, str("dis_" + str(user_id)))

    # Make sure user has at least one party
    err_str, err, _ = await engine.load_default_party(game_user)
    if err:
        return (err_str, err, None)

    session: ChatbotGameSession|None = None
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
        assert module_name is not None and party_name is not None
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
        agent: Agent = engine.create_chatbot_agent(channel_name, MODEL_ENDPOINT)
        lobby: ChatLobbyDriver|None = engine.create_chatbot_lobby(game_user, agent)
    else:
        agent: Agent = session.agent
        lobby: ChatLobbyDriver|None = session.lobby

    game: ChatGameDriver|None
    if action == "new_game" or action == "resume_game":
        assert module_name is not None
        assert party_name is not None
        assert save_game_name is not None
        game = engine.create_chatbot_game(
                        game_user,
                        agent,
                        start_game_action=action,
                        module_name=module_name, 
                        party_name=party_name,
                        save_game_name=save_game_name)
    elif session is not None:
        game = session.game
    else:
        game = None

    new_thread = False
    if create_thread and not thread:
        assert channel is not None
        thread = discord.utils.get(channel.threads, name=channel_name)
        if not thread:
            assert channel is not None
            # Create a new thread for this user if needed
            thread = await channel.create_thread(
                name=channel_name,
                type=discord.ChannelType.private_thread
            )
            # Add the bot/user to the thread
            assert discord_client.user is not None
            await thread.add_user(discord_client.user)
            await thread.add_user(user)
            new_thread = True
        await thread.join()
        session_id = str(thread.id)

    # Remove all other sessions for this user. Delete Any private thread.
    remove_sessions = []
    for rem_session_id, rem_session in active_sessions.items():
        if rem_session.game_user.id == user.id and \
                rem_session.is_thread and \
                rem_session_id != session_id:
            remove_sessions.append(rem_session_id)
    for rem_session_id in remove_sessions:
        rem_session = active_sessions[rem_session_id]
        rem_thread: discord.Thread = cast(discord.Thread, rem_session.channel)
        if rem_thread and rem_thread.type == discord.ChannelType.private_thread:
            try:
                await rem_thread.delete()
            except:
                pass
        del active_sessions[rem_session_id]

    assert channel is not None
    assert session_id is not None

    # Keep track of the session.
    session = ChatbotGameSession(
        agent,
        engine,
        game,
        lobby,
        game_user,
        cast(discord.TextChannel, (thread or channel)),
        (thread is not None),
        (thread.id if thread else channel.id))
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

    result: StartSessionResult = { "new_thread": new_thread, "thread": thread, "session": session }
    return ("ok", False, result)

# ------------------
# Dev Channels
# ------------------

async def start_dev_channels(guild: discord.Guild) -> None:

    user = cast(discord.User, discord.utils.get(guild.members, name=config["dev_user"]))
    assert user is not None

    for guild_channel in guild.channels:
        channel_name = guild_channel.name
        if channel_name in config["dev_channels"]:

            channel = cast(discord.TextChannel, guild_channel)

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
            assert info is not None
            
            channel_session = info["session"]
            game: ChatGameDriver|None = channel_session.game
            
            if game and not game.is_started:
                try:
                    result = await game.start_game()
                except:
                    result = traceback.format_exc()
            
                await send_to_channel(channel, result, game)

# ------------------
# Message Sender
# ------------------

async def send_to_channel_msg(channel: discord.TextChannel, msg: str) -> discord.Message|None:
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
        elif line.startswith("@card("):
            (param1, param2) = re.search(r'@card\("([^"]*)", "([^"]*)"\)', line).groups()
        else:
            out_lines.append(line)
    msg = "\n".join(out_lines)
    if len(msg) > 2000:
        msg = msg[:2000]
    if image_path:
        return await channel.send(msg, file=discord.File(image_path))
    elif msg:
        return await channel.send(msg)
    else:
        return None

async def send_to_channel(channel: discord.TextChannel, msg: str, game: ChatGameDriver|None = None, split_lines: bool = True) -> None:
    if not msg:
        return
    sent_message: bool = False

    # Should we split this message into multiple messages with dialog portraits?
    if game and split_lines:
        split_msgs = game.split_dialog(msg)
        if len(split_msgs) > 1:
            for index, split_msg in enumerate(split_msgs):
                last_para = index == len(split_msgs) - 1
                try:
                    if isinstance(split_msg, bytes):
                        await channel.send(file=discord.File(io.BytesIO(split_msg), filename="dialog.png"))
                    else:
                        await send_to_channel_msg(channel, cast(str, split_msg))
                except Exception as e:
                    tb = traceback.format_exc()
                    if ERROR_LOGGING:
                        print(tb)
                if not last_para:
                    await asyncio.sleep(0.5)
            sent_message = True

    # Send message if we haven't split above
    if not sent_message:
        await send_to_channel_msg(channel, msg)
    
    # Show button menu?
#    if game and game.button_tag is not None:
#        button_tag = game.button_tag
#        game.button_tag = None
#        await show_button_menu(game, channel, button_tag)

async def show_button_menu(game: ChatGameDriver, channel: discord.TextChannel, button_tag: str) -> None:
    state = {}
    view = discord.ui.View()
    state["view"] = view
    state["channel"] = channel
    state["user_name"] = ""    

    async def clicked(interaction: discord.Interaction, index: int) -> None:
        if interaction.user.name not in game.player_map:
            await interaction.response.send_message(content=f"You are not playing a character in this game. Ask the main player to assign " + \
                                                            f"you a character to play by telling the referee '{interaction.user.name} is playing <character name>'", ephemeral=True)
        return
        state["clicked_index"] = index
        state["user_name"] = interaction.user.name
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
                resp = await game.call_actions("", [(state["action"], args)])
            channel: discord.TextChannel = state["channel"]
            await interaction.response.send_message(resp, ephemeral=True)
        else:
            for index, button_info in enumerate(state["buttons"]):
                button = discord.ui.Button(label=button_info["text"])
                callback: Callable[[discord.Interaction, int], None] = state["callback"]
                button.callback = lambda interaction, index=index: callback(interaction, index) #type: ignore
                view.add_item(button)
            await interaction.response.send_message(content=state["choices"] + state["sentence"], view=view, ephemeral=True)

    state["callback"] = clicked
    resp, has_buttons = await game.get_buttons(button_tag, state)
    if not has_buttons:
        return
    for index, button_info in enumerate(state["buttons"]):
        button = discord.ui.Button(label=button_info["text"])
        button.callback = lambda interaction, index=index: clicked(interaction, index)
        view.add_item(button)
    state["message"] = await channel.send(content=state["choices"] + state["sentence"], view=view)

@discord_client.event
async def on_ready():

    # Start the session update timer callback
    global timer_task
    timer_task = asyncio.create_task(timer_update_func())

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

async def get_session(channel: discord.TextChannel, user: discord.User) -> ChatbotGameSession|None:

    guild = cast(discord.Guild, channel.guild)
    game: ChatGameDriver|None = None
    lobby: ChatLobbyDriver|None = None

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
            return None
        else:
            # Restart a session if it's in the saved channel state (so we can warm load sessions on
            # reload and players don't lose their state.)
            thread = cast(discord.Thread, channel)
            if channel_state["mode"] == "game":
                err_str, err, info = await start_session(
                                                          user, 
                                                          guild,
                                                          action="resume_game",
                                                          thread=thread
                                                        )
            else:
                err_str, err, info = await start_session(
                                                          cast(discord.User, user), 
                                                          cast(discord.Guild, guild),
                                                          action="lobby", 
                                                          thread=thread
                                                        )
            if err:
                await thread.send(err_str)
                return
            assert info is not None
            channel_session = info["session"]

            try:
                if channel_session.game is not None:
                    game = channel_session.game
                    assert game is not None
                    _ = await game.start_game()
                else:
                    lobby = channel_session.lobby
                    assert lobby is not None
                    _ = await lobby.start_lobby()

            except Exception as e:
                tb = traceback.format_exc()
                if ERROR_LOGGING:
                    print(tb)
                await channel.send(traceback.format_exc())
                return    
    
    return channel_session

def add_user_message(session: ChatbotGameSession, user_name: str, content: str) -> None:
    # Add the message to the waiting message list for the AI to respond to. It will
    # respond on a timer to avoid being spammed (it will respond to all messages
    # at the same time)
    session.messages.append((user_name, content))
    session.last_message_time = datetime.now()

@discord_client.event
async def on_message(message: discord.Message):

    # Ignore our own msgs
    user = cast(discord.User, message.author)
    if user == discord_client.user:
        return

    # Get the session
    channel = cast(discord.TextChannel, message.channel)
    channel_session = await get_session(channel, user)
    if channel_session is None:
        return

    # Ignore users messages among themselves
    content = message.content
    if content.startswith("!"):
        return

    add_user_message(channel_session, user.name, content)

async def process_messages(channel_session: ChatbotGameSession) -> None:
    
    messages = channel_session.messages
    if len(messages) == 0:
        return

    # Can't respond more frequently than the message rate limit
    last_response_time: datetime = channel_session.last_response_time
    current_time = datetime.now()
    if int((current_time - last_response_time).total_seconds()) < MESSAGE_RATE_LIMIT:
        return

    # Wait a small amount of time before responding
    last_response_time: datetime = channel_session.last_message_time
    if (current_time - last_response_time).total_seconds() < 1.5:
        return

    channel = cast(discord.TextChannel, channel_session.channel)    
    guild = cast(discord.Guild, channel.guild)

    content = ""
    game: ChatGameDriver|None = channel_session.game
    assert game is not None
    player_char_map = game.player_map

    # Include all messages from players. Find the player's characters and prepend
    # the character name to the message.
    for msg in messages:
        username, message = msg
        # For testing with one discord user
        if DEVELOPER_MODE:
            if message.startswith("@"):
                username = message.split(" ", 1)[0][1:]
                message = message[len(username) + 2:]
        # User used a "CharName:" prefix in his message. Is playing more than
        # one character.
        char_name: str = ""
        char_colon_pos = message.find(": ", 0, 30)
        if char_colon_pos > 1:
            maybe_char_name = message.split(":", 1)[0]
            if maybe_char_name in game.characters:
                char_name = maybe_char_name
                message = message[char_colon_pos + 2:]
        # Check to make sure user is one of the players (ignore all other users)
        if username in player_char_map:
            char_list = player_char_map[username]
            if char_name:
                # Player is not playing this character.. skip
                if char_name not in char_list:
                    continue
            else:
                char_name = player_char_map[username][0]
            content += f"{char_name}: {message}\n\n"

    try:
        channel_state = await engine.get_channel_state(guild.id, channel.id)

        lobby = channel_session.lobby
        game = channel_session.game
        assert game is not None and lobby is not None

        # If game is running, call game, otherwise call the lobby
        if game is not None and game.is_started and not game.game_over:
            result = await game.player_action(content)
            # Handle returning to lobby from game
            if game.exit_to_lobby:
                game.exit_to_lobby = False
                channel_session.game = None
                # Save the current state for this channel
                channel_state["mode"] = "lobby"
                await engine.set_channel_state(guild.id, channel.id, channel_state)               
                # Restart the lobby   
                result = await lobby.start_lobby()
        else:
            result = await lobby.player_action(content)
            # Handle starting a game from lobby
            if lobby.start_the_game:
                start_game_action = cast(str, lobby.start_game_action)
                module_name = cast(str, lobby.start_game_module_name)
                party_name = cast(str, lobby.start_game_party_name)
                save_game_name = cast(str, lobby.start_game_save_game_name)
                lobby.start_the_game = False
                game = engine.create_chatbot_game( 
                                channel_session.game_user, 
                                channel_session.agent, 
                                start_game_action=start_game_action,
                                module_name=module_name, 
                                party_name=party_name,
                                save_game_name=save_game_name)
                channel_session.game = game
                # Save the current state for this channel
                channel_state["mode"] = "game"
                await engine.set_channel_state(guild.id, channel.id, channel_state)               
                result = await game.start_game()

    except Exception as e:
        tb = traceback.format_exc()
        if ERROR_LOGGING:
            print(tb)
        result = ""

    channel_session.messages = []
    channel_session.last_response_time = datetime.now()

    if result != "":
        await send_to_channel(channel, result, game)

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

    channel = cast(discord.TextChannel, interaction.channel)

    root_channel_name = (getattr(channel, "parent", None) or channel).name
    if not root_channel_name.startswith(CHANNEL_PREFIX):
        await interaction.response.send_message(f"DungeonGod commands are only available in channels or threads of channels with a \"{CHANNEL_PREFIX}\" prefix.")
        return

    # We create a private thread if this command is called in a channel
    is_thread: bool = (channel.type == discord.ChannelType.private_thread or 
                       channel.type == discord.ChannelType.public_thread)
    thread: discord.Thread|None = (cast(discord.Thread, channel) if is_thread else None)

    err_str, err, result = await start_session(
                                                cast(discord.User, interaction.user), 
                                                cast(discord.Guild, interaction.guild), 
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
    assert result is not None

    session = result["session"]
    game: ChatGameDriver|None = session.game
    is_thread = session.is_thread
    thread = cast(discord.Thread|None, session.channel if is_thread else None)

    try:
        if thread is not None and thread.id != channel.id:
            await interaction.response.send_message(f"Your game is started in thread <#{thread.id}>.", ephemeral=True, delete_after=30)
        else:
            await interaction.response.send_message(f"Starting game..")
        assert(game)
        result = await game.start_game()

    except Exception as e:
        tb = traceback.format_exc()
        if ERROR_LOGGING:
            print(tb)
        result = ""

    if result != "":
        await send_to_channel(cast(discord.TextChannel, thread or channel), result, game)

# make the slash command 
#@discord_tree.command(name="dgod_resume_game", description="Creates a thread and resumes the last game.", 
#                      guild=discord.Object(id=DEV_DISCORD_GUILD_ID))
@discord_tree.command(name="dgod_resume_game", description="Resume your last DungeonGod game in progress.")
async def dgod_resume_game(interaction: discord.Interaction): 

    channel = cast(discord.TextChannel, interaction.channel)

    root_channel_name = (getattr(channel, "parent", None) or channel).name
    if not root_channel_name.startswith(CHANNEL_PREFIX):
        await interaction.response.send_message(f"DungeonGod commands are only available in channels or threads of channels with a \"{CHANNEL_PREFIX}\" prefix.")
        return   

    # We create a private thread if this command is called in a channel
    is_thread: bool = (channel.type == discord.ChannelType.private_thread or 
                       channel.type == discord.ChannelType.public_thread)
    thread: discord.Thread|None = (cast(discord.Thread, channel) if is_thread else None)

    err_str, err, result = await start_session(
                                                cast(discord.User, interaction.user), 
                                                cast(discord.Guild, interaction.guild),
                                                action="resume_game",
                                                channel=channel, 
                                                create_thread=not is_thread,
                                                thread=thread
                                              )


    if err:
        await channel.send(err_str)
        return
    assert result is not None

    session = result["session"]
    game: ChatGameDriver|None = session.game
    is_thread = session.is_thread
    thread = cast(discord.Thread|None, session.channel if is_thread else None)

    try:
        if thread is not None and thread.id != channel.id:
            await interaction.response.send_message(f"Your game has been resumed in thread <#{thread.id}>.", ephemeral=True, delete_after=30)
        else:
            await interaction.response.send_message(f"Resuming game..")
        assert(game)
        result = await game.start_game()

    except Exception as e:
        tb = traceback.format_exc()
        if ERROR_LOGGING:
            print(tb)
        result = ""            

    if result != "":
        await send_to_channel(cast(discord.TextChannel, thread or channel), result, game)

# make the slash command
#@discord_tree.command(name="dgod_lobby", description="Creates a lobby thread to build a party and start a game.", 
#                      guild=discord.Object(id=DEV_DISCORD_GUILD_ID))
@discord_tree.command(name="dgod_lobby", description="Opens a DungeonGod lobby to manage your parties and start a game.")
async def dgod_lobby(interaction: discord.Interaction): 

    channel = cast(discord.TextChannel, interaction.channel)

    root_channel_name = (getattr(channel, "parent", None) or channel).name
    if not root_channel_name.startswith(CHANNEL_PREFIX):
        await interaction.response.send_message(f"DungeonGod commands are only available in channels or threads of channels with a \"{CHANNEL_PREFIX}\" prefix.")
        return

    # We create a private thread if this command is called in a channel
    is_thread: bool = (channel.type == discord.ChannelType.private_thread or 
                       channel.type == discord.ChannelType.public_thread)
    thread: discord.Thread|None = cast(discord.Thread, channel if is_thread else None)

    err_str, err, result = await start_session(
                                                cast(discord.User, interaction.user), 
                                                cast(discord.Guild, interaction.guild),
                                                action="lobby",
                                                channel=channel,
                                                create_thread=not is_thread,
                                                thread=thread,
                                              )

    if err:
        await channel.send(err_str)
        return
    assert result is not None

    session = result["session"]
    lobby: ChatLobbyDriver|None = session.lobby
    is_thread = session.is_thread
    thread: discord.Thread|None = (cast(discord.Thread, session.channel) if is_thread else None)

    try:
        if thread is not None and thread.id != channel.id:
            await interaction.response.send_message(f"Your lobby has been started in thread <#{thread.id}>.", ephemeral=True, delete_after=30)
        else:
            await interaction.response.send_message(f"Starting lobby..")
        assert(lobby)
        result = await lobby.start_lobby()

    except Exception as e:
        tb = traceback.format_exc()
        if ERROR_LOGGING:
            print(tb)
        result = ""

    if result != "":
        await send_to_channel(cast(discord.TextChannel, thread or channel), result)

#######################
# GAME COMMANDS
#######################

# make the slash command
@discord_tree.command(name="look", description="Look at an item, npc, monster, inventory item, or poi at the current location.")
async def look(interaction: discord.Interaction): 

    channel = cast(discord.TextChannel, interaction.channel)

    root_channel_name = (getattr(channel, "parent", None) or channel).name
    if not root_channel_name.startswith(CHANNEL_PREFIX):
        await interaction.response.send_message(f"DungeonGod commands are only available in channels or threads of channels with a \"{CHANNEL_PREFIX}\" prefix.")
        return

    # We create a private thread if this command is called in a channel
    is_thread: bool = (channel.type == discord.ChannelType.private_thread or 
                       channel.type == discord.ChannelType.public_thread)
    thread: discord.Thread|None = cast(discord.Thread, channel if is_thread else None)

    err_str, err, result = await start_session(
                                                cast(discord.User, interaction.user), 
                                                cast(discord.Guild, interaction.guild),
                                                action="lobby",
                                                channel=channel,
                                                create_thread=not is_thread,
                                                thread=thread,
                                              )

    if err:
        await channel.send(err_str)
        return
    assert result is not None

    session = result["session"]
    lobby: ChatLobbyDriver|None = session.lobby
    is_thread = session.is_thread
    thread: discord.Thread|None = (cast(discord.Thread, session.channel) if is_thread else None)

    try:
        if thread is not None and thread.id != channel.id:
            await interaction.response.send_message(f"Your lobby has been started in thread <#{thread.id}>.", ephemeral=True, delete_after=30)
        else:
            await interaction.response.send_message(f"Starting lobby..")
        assert(lobby)
        result = await lobby.start_lobby()

    except Exception as e:
        tb = traceback.format_exc()
        if ERROR_LOGGING:
            print(tb)
        result = ""

    if result != "":
        await send_to_channel(cast(discord.TextChannel, thread or channel), result)


async def timer_update_func() -> None:
    last_update_time = datetime.min

    while True:            
        try:
            cur_time = datetime.now()
            dt = (cur_time - last_update_time).total_seconds()
            if dt > 0.5:
                dt = 0.5

            for session in active_sessions.values():

                # Process game update (if game is active)
                game: ChatGameDriver|None = session.game
                if game:
                    channel: discord.TextChannel = session.channel
                    if game.is_started and not game.game_over and not game.exit_to_lobby:
                        resp = await game.timer_update(dt)
                        if resp:
                            await send_to_channel(channel, resp, game)

                # Process awaiting messages for channel
                await process_messages(session)

            # Set last update success time
            last_update_time = cur_time

        except Exception as e:
            tb = traceback.format_exc()
            if ERROR_LOGGING:
                print(tb)

        await asyncio.sleep(0.25)

def run_discord_chatbot() -> None:
    discord_client.run(DISCORD_TOKEN)
