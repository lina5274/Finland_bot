import os
import logging
from twilio.rest import Client
from flask import Flask, request
import psycopg2
import openai
from langdetect import detect, LangDetectException
import asyncio

try:
    # Получение версии Python
    import sys
    print(f"Python version: {sys.version}")

    # Настройка логирования
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

    # Получение переменных окружения
    TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
    TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
    TIMESCALE_CONNECTION_STRING = os.getenv('TIMESCALE_CONNECTION_STRING')
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
    TWILIO_PHONE_NUMBER = os.getenv('TWILIO_PHONE_NUMBER')

    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TIMESCALE_CONNECTION_STRING, OPENAI_API_KEY, TWILIO_PHONE_NUMBER]):
        raise ValueError("Не все необходимые переменные окружения установлены.")

except Exception as e:
    print(f"Ошибка при инициализации: {str(e)}")
    exit(1)

openai.api_key = OPENAI_API_KEY

app = Flask(__name__)

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

async def create_tables():
    conn = psycopg2.connect(TIMESCALE_CONNECTION_STRING)
    c = conn.cursor()

    query_create_chat_history_table = """
    CREATE TABLE IF NOT EXISTS chat_history (
        id SERIAL PRIMARY KEY,
        user_id TEXT,
        message_role TEXT,
        message_content TEXT,
        timestamp TIMESTAMPTZ DEFAULT NOW()
    );
    """
    c.execute(query_create_chat_history_table)

    query_create_hypertable = """
    SELECT create_hypertable('chat_history', 'timestamp');
    """
    c.execute(query_create_hypertable)

    query_create_users_table = """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        whatsapp_id TEXT UNIQUE,
        name TEXT,
        language TEXT DEFAULT 'en'
    );
    """
    c.execute(query_create_users_table)

    conn.commit()
    c.close()
    conn.close()

def save_message(user_id, message_role, message_content):
    conn = psycopg2.connect(TIMESCALE_CONNECTION_STRING)
    c = conn.cursor()

    query_insert_message = """
    INSERT INTO chat_history (user_id, message_role, message_content)
    VALUES (%s, %s, %s);
    """
    c.execute(query_insert_message, (user_id, message_role, message_content))

    conn.commit()
    c.close()
    conn.close()

async def get_chat_history(user_id):
    conn = psycopg2.connect(TIMESCALE_CONNECTION_STRING)
    c = conn.cursor()

    query_get_history = """
    SELECT message_role, message_content FROM chat_history
    WHERE user_id = %s ORDER BY timestamp DESC LIMIT 10;
    """
    c.execute(query_get_history, (user_id,))
    chat_history = c.fetchall()
    c.close()
    conn.close()
    return chat_history[::-1]

def check_user_exists(whatsapp_id):
    conn = psycopg2.connect(TIMESCALE_CONNECTION_STRING)
    c = conn.cursor()

    query_check_user = """
    SELECT * FROM users WHERE whatsapp_id = %s;
    """
    c.execute(query_check_user, (whatsapp_id,))
    user_exists = bool(c.fetchone())
    c.close()
    conn.close()
    return user_exists

def save_user(whatsapp_id, name):
    conn = psycopg2.connect(TIMESCALE_CONNECTION_STRING)
    c = conn.cursor()

    query_insert_user = """
    INSERT INTO users (whatsapp_id, name)
    VALUES (%s, %s);
    """
    c.execute(query_insert_user, (whatsapp_id, name))

    conn.commit()
    c.close()
    conn.close()

def update_user_language(user_id, language):
    conn = psycopg2.connect(TIMESCALE_CONNECTION_STRING)
    c = conn.cursor()

    query_update_language = """
    UPDATE users SET language = %s WHERE whatsapp_id = %s;
    """
    c.execute(query_update_language, (language, user_id))

    conn.commit()
    c.close()
    conn.close()

async def get_ai_response(user_id, message):
    chat_history = await get_chat_history(user_id)

    messages = [
        {"role": role, "content": content} for role, content in chat_history
    ]

    user_info_query = """
    SELECT name, language FROM users WHERE whatsapp_id = %s;
    """
    conn = psycopg2.connect(TIMESCALE_CONNECTION_STRING)
    c = conn.cursor()
    c.execute(user_info_query, (user_id,))
    user_info = c.fetchone()
    c.close()
    conn.close()

    system_messages = {
        'ru': f"Вы помощник-продавец. Помните историю разговора и отвечайте осознанно. Отвечайте на том же языке, на котором задают вопрос. Пользователь: {user_info[0]}",
        'en': f"You are a sales assistant. Remember the conversation and answer thoughtfully. Reply in the same language as the user's message. User: {user_info[0]}"
    }
    messages.insert(0, {"role": "system", "content": system_messages[user_info[1]]})

    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=messages,
        max_tokens=500,
        temperature=0.7,
    )
    return response.choices[0].message.content

async def save_ai_response(user_id, ai_response):
    await save_message(user_id, 'ai', ai_response)

@app.route('/bot', methods=['POST'])
async def handle_whatsapp():
    incoming_msg = request.values.get('Body', '').lower()
    whatsapp_id = request.values.get('From')

    # Сохранение сообщения пользователя
    await save_message(whatsapp_id, 'user', incoming_msg)

    # Проверка существования пользователя
    if not check_user_exists(whatsapp_id):
        save_user(whatsapp_id, incoming_msg)

    # Определение языка пользователя с использованием значения по умолчанию
    user_language = 'en'  # Значение по умолчанию

    try:
        detected_language = detect(incoming_msg)
        if detected_language == 'ru':
            user_language = 'ru'
        elif detected_language != 'en':
            logging.warning(f"Detected language '{detected_language}' is not supported. Using default 'en'.")
    except LangDetectException as e:
        logging.warning(f"Failed to detect language for message: {incoming_msg}. Error: {str(e)}")

    # Обновление языка пользователя в базе данных
    update_user_language(whatsapp_id, user_language)

    # Получение ответа от ИИ-помощника
    ai_response = await get_ai_response(whatsapp_id, incoming_msg)

    # Сохранение ответа ИИ-помощника в базу данных
    await save_ai_response(whatsapp_id, ai_response)

    # Отправка ответа пользователю
    message_body = ai_response[:256]  # Limit to 256 characters
    client.messages.create(
        from_=TWILIO_PHONE_NUMBER,
        body=message_body,
        to=whatsapp_id
    )

    return "Message processed successfully"

if __name__ == "__main__":
    asyncio.run(create_tables())
    app.run(host='0.0.0.0', port=5000)
