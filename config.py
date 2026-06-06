import os
from dotenv import load_dotenv
from urllib.parse import quote_plus
 
load_dotenv()
 
BOT_TOKEN     = os.getenv("BOT_TOKEN", "")
MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID", "0"))
 
API_ID   = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
 
ACTIVITY_THRESHOLD  = int(os.getenv("ACTIVITY_THRESHOLD", "10"))
ACTIVITY_WINDOW_SEC = int(os.getenv("ACTIVITY_WINDOW_SEC", "60"))
 
JOIN_DELAY_MIN = int(os.getenv("JOIN_DELAY_MIN", "5"))
JOIN_DELAY_MAX = int(os.getenv("JOIN_DELAY_MAX", "15"))
 
 
def _build_db_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    if url:
        return url
    # Railway PGPASSWORD da maxsus belgi bo'lsa quote_plus bilan encode qilamiz
    user     = os.getenv("PGUSER") or os.getenv("POSTGRES_USER", "postgres")
    password = quote_plus(os.getenv("PGPASSWORD") or os.getenv("POSTGRES_PASSWORD", ""))
    host     = os.getenv("PGHOST", "localhost")
    port     = os.getenv("PGPORT", "5432")
    dbname   = os.getenv("PGDATABASE") or os.getenv("POSTGRES_DB", "railway")
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"
 
 
DATABASE_URL = _build_db_url()
 
