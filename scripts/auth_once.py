import os

from pyrogram import Client


SESSION_NAME = os.getenv("SESSION_NAME", "/app/session/my_account")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
PHONE_NUMBER = os.getenv("PHONE_NUMBER")


with Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH, phone_number=PHONE_NUMBER) as app:
    me = app.get_me()
    print(f"AUTH_OK @{me.username or me.id}")
