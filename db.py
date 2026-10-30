import os
from datetime import datetime, timedelta, timezone
import aiosqlite

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
DB_PATH = os.path.join(DATA_DIR, "repetition.db")

# Интервалы по этапам в часах: 0 -> 24ч -> 72ч -> 7 дней (168ч) -> 14 дней (336ч)
STAGES_HOURS = {
    0: 24,
    1: 24,
    2: 72,
    3: 168,
    4: 336
}
MAX_STAGE = 4

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        # Таблица карточек
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER NOT NULL,
                subject TEXT NOT NULL,
                front_text TEXT,
                front_photo_id TEXT,
                back_text TEXT,
                question TEXT,
                check_type TEXT NOT NULL, -- 'typing' | 'choice'
                correct_answer TEXT NOT NULL,
                options_str TEXT,         -- Варианты через ';' (для choice)
                stage INTEGER NOT NULL DEFAULT 0,
                next_review_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        
        # Таблица статистики и прогресса пользователя
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_stats (
                tg_id INTEGER PRIMARY KEY,
                total_reviewed INTEGER NOT NULL DEFAULT 0,
                today_reviewed INTEGER NOT NULL DEFAULT 0,
                deleted_cards INTEGER NOT NULL DEFAULT 0,
                last_rep_at TEXT,         -- Время последнего /rep
                last_reminder_sent_at TEXT,
                reminder_count INTEGER NOT NULL DEFAULT 0,
                last_reset_date TEXT      -- Для сброса today_reviewed раз в сутки
            )
        """)
        
        await db.execute("CREATE INDEX IF NOT EXISTS idx_cards_due ON cards (tg_id, next_review_at)")
        await db.commit()

async def get_user_subjects(tg_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT DISTINCT subject FROM cards WHERE tg_id = ?", (tg_id,))
        rows = await cur.fetchall()
        return [row[0] for row in rows]

async def add_card(
    tg_id: int,
    subject: str,
    front_text: str | None,
    front_photo_id: str | None,
    back_text: str | None,
    question: str | None,
    check_type: str,
    correct_answer: str,
    options_str: str | None = None
) -> int:
    next_review = datetime.now(timezone.utc) + timedelta(hours=STAGES_HOURS[0])
    next_str = next_review.strftime("%Y-%m-%d %H:%M:%S")

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO cards (tg_id, subject, front_text, front_photo_id, back_text, 
                               question, check_type, correct_answer, options_str, stage, next_review_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """, (tg_id, subject, front_text, front_photo_id, back_text, question, check_type, correct_answer, options_str, next_str))
        card_id = cur.lastrowid
        await db.commit()
        return card_id

async def get_card(card_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM cards WHERE id = ?", (card_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

async def delete_card(card_id: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT tg_id FROM cards WHERE id = ?", (card_id,))
        row = await cur.fetchone()
        if not row:
            return None
        tg_id = row['tg_id']
        
        await db.execute("DELETE FROM cards WHERE id = ?", (card_id,))
        # Обновляем статистику удаленных
        await db.execute("""
            INSERT INTO user_stats (tg_id, deleted_cards) VALUES (?, 1)
            ON CONFLICT(tg_id) DO UPDATE SET deleted_cards = deleted_cards + 1
        """, (tg_id,))
        await db.commit()
        return tg_id

async def update_card(card_id: int, kwargs: dict):
    fields = []
    values = []
    for k, v in kwargs.items():
        fields.append(f"{k} = ?")
        values.append(v)
    values.append(card_id)
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE cards SET {', '.join(fields)} WHERE id = ?", values)
        await db.commit()

async def get_due_cards(tg_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT * FROM cards WHERE tg_id = ? AND next_review_at <= datetime('now')
            ORDER BY next_review_at ASC
        """, (tg_id,))
        return [dict(r) for r in (await cur.fetchall())]

async def process_review_result(card_id: int, is_correct: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM cards WHERE id = ?", (card_id,))
        card = await cur.fetchone()
        if not card:
            return None
        
        tg_id = card['tg_id']
        old_stage = card['stage']
        
        if is_correct:
            new_stage = min(old_stage + 1, MAX_STAGE)
        else:
            new_stage = 1 # Сброс на 24ч (этап 1)
            
        hours = STAGES_HOURS.get(new_stage, 24)
        next_review = datetime.now(timezone.utc) + timedelta(hours=hours)
        next_str = next_review.strftime("%Y-%m-%d %H:%M:%S")
        
        await db.execute("""
            UPDATE cards SET stage = ?, next_review_at = ? WHERE id = ?
        """, (new_stage, next_str, card_id))
        
        # Обновляем статистику пользователя
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        
        # Проверяем не наступил ли новый день для сброса today_reviewed
        await db.execute("INSERT OR IGNORE INTO user_stats (tg_id) VALUES (?)", (tg_id,))
        stat_cur = await db.execute("SELECT last_reset_date, today_reviewed FROM user_stats WHERE tg_id = ?", (tg_id,))
        stat_row = await stat_cur.fetchone()
        
        today_rev = stat_row['today_reviewed']
        if stat_row['last_reset_date'] != today_str:
            today_rev = 0
            
        await db.execute("""
            UPDATE user_stats 
            SET total_reviewed = total_reviewed + 1,
                today_reviewed = ?,
                last_reset_date = ?
            WHERE tg_id = ?
        """, (today_rev + 1, today_str, tg_id))
        
        await db.commit()
        return card, new_stage

async def record_rep_command(tg_id: int):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO user_stats (tg_id) VALUES (?)", (tg_id,))
        await db.execute("""
            UPDATE user_stats 
            SET last_rep_at = ?, reminder_count = 0, last_reminder_sent_at = NULL 
            WHERE tg_id = ?
        """, (now_str, tg_id))
        await db.commit()

async def get_user_stats_data(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM user_stats WHERE tg_id = ?", (tg_id,))
        row = await cur.fetchone()
        if not row:
            return {"total_reviewed": 0, "today_reviewed": 0, "deleted_cards": 0}
        
        # Проверим сброс дневной статистики
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_rev = row['today_reviewed']
        if row['last_reset_date'] != today_str:
            today_rev = 0
            
        return {
            "total_reviewed": row['total_reviewed'],
            "today_reviewed": today_rev,
            "deleted_cards": row['deleted_cards']
        }

async def get_user_cards_count(tg_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM cards WHERE tg_id = ?", (tg_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0