import os
from dotenv import load_dotenv
load_dotenv()

EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")
BASE_SUBSTACK_URL = os.getenv("BASE_SUBSTACK_URL")
