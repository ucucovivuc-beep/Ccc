import os

# Railway: задай переменные в разделе Variables
# Локально: можешь вписать прямо сюда

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН_БОТА")
API_ID    = int(os.getenv("API_ID", "12345678"))
API_HASH  = os.getenv("API_HASH", "твой_api_hash")
