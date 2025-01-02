from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.filters.command import Command, CommandStart
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from func.interactions import *
import asyncio
import traceback
import io
import base64
import sqlite3
bot = Bot(token=token)
dp = Dispatcher()

commands = [
    types.BotCommand(command="start", description="Start"),
    types.BotCommand(command="reset", description="Reset Chat"),
]

ACTIVE_CHATS = {}
ACTIVE_CHATS_LOCK = contextLock()
modelname = os.getenv("INITMODEL")
mention = None
selected_prompt_id = None  # Variable to store the selected prompt ID
CHAT_TYPE_GROUP = "group"
CHAT_TYPE_SUPERGROUP = "supergroup"

def init_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id INTEGER PRIMARY KEY, name TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS chats
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  role TEXT,
                  content TEXT,
                  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                  FOREIGN KEY (user_id) REFERENCES users(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS system_prompts
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  prompt TEXT,
                  is_global BOOLEAN,
                  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                  FOREIGN KEY (user_id) REFERENCES users(id))''')
    conn.commit()
    conn.close()

def register_user(user_id, user_name):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users VALUES (?, ?)", (user_id, user_name))
    conn.commit()
    conn.close()

def save_chat_message(user_id, role, content):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("INSERT INTO chats (user_id, role, content) VALUES (?, ?, ?)",
              (user_id, role, content))
    conn.commit()
    conn.close()

async def get_bot_info():
    global mention
    if mention is None:
        get = await bot.get_me()
        mention = f"@{get.username}"
    return mention

@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    start_message = f"Welcome, <b>{message.from_user.first_name}</b>!"
    await message.answer(
        start_message,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

@dp.message(Command("reset"))
async def command_reset_handler(message: Message) -> None:
    if message.from_user.id in allowed_ids:
        if message.from_user.id in ACTIVE_CHATS:
            async with ACTIVE_CHATS_LOCK:
                ACTIVE_CHATS.pop(message.from_user.id)
            logging.info(f"Chat has been reset for {message.from_user.first_name}")
            await bot.send_message(
                chat_id=message.chat.id,
                text="Chat has been reset",
            )

@dp.message()
@perms_allowed
async def handle_message(message: types.Message):
    await get_bot_info()
    
    if message.chat.type == "private":
        await ollama_request(message)
        return

    if await is_mentioned_in_group_or_supergroup(message):
        thread = await collect_message_thread(message)
        prompt = format_thread_for_prompt(thread)
        
        await ollama_request(message, prompt)

async def is_mentioned_in_group_or_supergroup(message: types.Message):
    if message.chat.type not in ["group", "supergroup"]:
        return False
    
    is_mentioned = (
        (message.text and message.text.startswith(mention)) or
        (message.caption and message.caption.startswith(mention))
    )
    
    is_reply_to_bot = (
        message.reply_to_message and 
        message.reply_to_message.from_user.id == bot.id
    )
    
    return is_mentioned or is_reply_to_bot

async def collect_message_thread(message: types.Message, thread=None):
    if thread is None:
        thread = []
    
    thread.insert(0, message)
    
    if message.reply_to_message:
        await collect_message_thread(message.reply_to_message, thread)
    
    return thread

def format_thread_for_prompt(thread):
    prompt = "Conversation thread:\n\n"
    for msg in thread:
        sender = "User" if msg.from_user.id != bot.id else "Bot"
        content = msg.text or msg.caption or "[No text content]"
        prompt += f"{sender}: {content}\n\n"
    
    prompt += "History:"
    return prompt

async def process_image(message):
    image_base64 = ""
    if message.content_type == "photo":
        image_buffer = io.BytesIO()
        await bot.download(message.photo[-1], destination=image_buffer)
        image_base64 = base64.b64encode(image_buffer.getvalue()).decode("utf-8")
    return image_base64

async def add_prompt_to_active_chats(message, prompt, image_base64, modelname, system_prompt=None):
    async with ACTIVE_CHATS_LOCK:
        # Prepare the messages list
        messages = []
        
        # Add system prompt if provided and not already present
        if system_prompt:
            # Check if a system message already exists
            existing_system_messages = [msg for msg in ACTIVE_CHATS.get(message.from_user.id, {}).get('messages', []) if msg.get('role') == 'system']
            
            if not existing_system_messages:
                messages.append({
                    "role": "system",
                    "content": system_prompt
                })
        
        # Add existing messages if the chat exists, excluding any existing system messages
        if ACTIVE_CHATS.get(message.from_user.id):
            messages.extend([msg for msg in ACTIVE_CHATS[message.from_user.id].get("messages", []) if msg.get('role') != 'system'])
        
        # Add the new user message
        messages.append({
            "role": "user",
            "content": prompt,
            "images": ([image_base64] if image_base64 else []),
        })
        
        # Update or create the active chat
        ACTIVE_CHATS[message.from_user.id] = {
            "model": modelname,
            "messages": messages,
            "stream": True,
            "keep_alive": "5h",
        }

async def handle_response(message, response_data, full_response):
    full_response_stripped = full_response.strip()
    if full_response_stripped == "":
        return
    if response_data.get("done"):
        text = f"{full_response_stripped}"
        await send_response(message, text)
        async with ACTIVE_CHATS_LOCK:
            if ACTIVE_CHATS.get(message.from_user.id) is not None:
                ACTIVE_CHATS[message.from_user.id]["messages"].append(
                    {"role": "assistant", "content": full_response_stripped}
                )
        logging.info(
            f"[Response]: '{full_response_stripped}' for {message.from_user.first_name} {message.from_user.last_name}"
        )
        return True
    return False

async def send_response(message, text):
    # A negative message.chat.id is a group message
    if message.chat.id < 0 or message.chat.id == message.from_user.id:
        await bot.send_message(chat_id=message.chat.id, text=text,parse_mode=ParseMode.MARKDOWN)
    else:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=message.message_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
        )

async def ollama_request(message: types.Message, prompt: str = None):
    try:
        full_response = ""
        await bot.send_chat_action(message.chat.id, "typing")
        image_base64 = await process_image(message)
        
        # Determine the prompt
        if prompt is None:
            prompt = message.text or message.caption

        # Retrieve and prepare system prompt if selected
        system_prompt = None
        if selected_prompt_id is not None:
            system_prompts = get_system_prompts(user_id=message.from_user.id, is_global=None)
            if system_prompts:
                # Find the specific prompt by ID
                for sp in system_prompts:
                    if sp[0] == selected_prompt_id:
                        system_prompt = sp[2]
                        break
                
                if system_prompt is None:
                    logging.warning(f"Selected prompt ID {selected_prompt_id} not found for user {message.from_user.id}")

        # Save the user's message
        save_chat_message(message.from_user.id, "user", prompt)

        # Prepare the active chat with the system prompt
        await add_prompt_to_active_chats(message, prompt, image_base64, modelname, system_prompt)
        
        logging.info(
            f"[OllamaAPI]: Processing '{prompt}' for {message.from_user.first_name} {message.from_user.last_name}"
        )
        
        # Get the payload from active chats
        payload = ACTIVE_CHATS.get(message.from_user.id)
        
        # Generate response
        async for response_data in generate(payload, modelname, prompt):
            msg = response_data.get("message")
            if msg is None:
                continue
            chunk = msg.get("content", "")
            full_response += chunk

            if any([c in chunk for c in ".\n!?"]) or response_data.get("done"):
                if await handle_response(message, response_data, full_response):
                    save_chat_message(message.from_user.id, "assistant", full_response)
                    break

    except Exception as e:
        print(f"-----\n[OllamaAPI-ERR] CAUGHT FAULT!\n{traceback.format_exc()}\n-----")
        await bot.send_message(
            chat_id=message.chat.id,
            text=f"Something went wrong: {str(e)}",
            parse_mode=ParseMode.HTML,
        )

async def main():
    init_db()
    allowed_ids = load_allowed_ids_from_db()
    print(f"allowed_ids: {allowed_ids}")
    await bot.set_my_commands(commands)
    await dp.start_polling(bot, skip_update=True)

if __name__ == "__main__":
    asyncio.run(main())
