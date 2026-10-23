"""
Mafia Telegram Bot — PRO MAX versiya (v3)
===========================================
Yangiliklar:
  - Guruhga qo'shmasdan ham o'ynash mumkin: botning shaxsiy chatida (DM)
    /tezkor bilan tezkor moslashtirish (matchmaking) yoki /xona bilan
    shaxsiy xona ochib, do'stlaringizga XONA KODINI berib DM orqali
    taklif qilasiz.
  - Real o'yinchilar yetmasa, bot (AI) o'yinchilar avtomatik to'ldiradi
    (kamida 3 real odam yetarli).
  - VIP holati MAXFIY: /timati va barcha VIP buyruqlariga javob FAQAT
    o'sha buyruqni yozgan odamning shaxsiy (DM) chatiga yuboriladi —
    guruhda bo'lsin, DM'da bo'lsin, boshqa hech kim ko'rmaydi.
  - Har bir o'yinchiga avatar-emoji, tun/kun avto-timer, /app orqali
    Telegram Mini App (WebApp) tugmasi.

MUHIM — XAVFSIZLIK:
  Tokenni kodga emas, environment variable orqali bering:
      export MAFIA_BOT_TOKEN="tokeningiz"
      python mafia_bot.py

MUHIM — WEBAPP:
  Telegram WebApp tugmasi faqat HTTPS manzilga ishora qiladi.
  webapp/index.html faylini GitHub Pages (yoki boshqa hosting) ga
  joylab, MAFIA_WEBAPP_URL ga o'sha manzilni bering.
"""

import asyncio
import logging
import os
import random
import string
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, WebAppInfo
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

# .env faylini mafia_bot.py joylashgan papkadan yuklaymiz
load_dotenv(Path(__file__).resolve().parent / ".env")

# ==========================================================
# SOZLAMALAR
# ==========================================================
BOT_TOKEN = os.getenv("MAFIA_BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError(
        "❌ MAFIA_BOT_TOKEN environment variable topilmadi.\n"
        "   .env faylida MAFIA_BOT_TOKEN ni tekshiring."
    )

WEBAPP_URL = os.getenv(
    "MAFIA_WEBAPP_URL",
    "https://akbarjonovtemur460-cloud.github.io/-mafia-webapp/"
)


SECRET_CODE = "/timati"
VIP_USERS: Set[int] = set()

NIGHT_TIMEOUT = 60
DAY_TIMEOUT = 90
QUEUE_WAIT_TIMEOUT = 30  # tezkor navbat kutish vaqti (soniya)

MIN_REAL_PLAYERS = 3
TARGET_TOTAL_PLAYERS = 6
MAX_PLAYERS = 10

AVATARS = [
    "🦊", "🐺", "🐻", "🦁", "🐯", "🐸", "🐵", "🐼",
    "🦉", "🦅", "🐍", "🐙", "🦂", "🦀", "🐲", "🦄",
    "🐰", "🐨", "🐷", "🐱", "🐶", "🐹", "🦇", "🦈",
]
BOT_NAMES = [
    "Shodmon-bot", "Dilnoza-bot", "Jasur-bot", "Malika-bot",
    "Aziz-bot", "Nodira-bot", "Sardor-bot", "Gulnora-bot",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("mafia_bot")

router = Router()

# Xona kodi -> Game. Guruh o'yini ham, shaxsiy (DM) o'yini ham shu yerda.
GAMES: Dict[str, "Game"] = {}
# Tezkor o'yin navbati: user_id -> (name, bosilgan vaqt)
QUICK_QUEUE: Dict[int, str] = {}
QUICK_QUEUE_TASK: Optional[asyncio.Task] = None

ROLE_DESC = {
    "mafia": "🔫 Siz <b>MAFIA</b>siz. Tunda bir odamni o'ldirasiz. Kunduzi o'zingizni begunoh qilib ko'rsating.",
    "doctor": "💉 Siz <b>DOCTOR</b>siz. Tunda bir odamni davolay olasiz. Mafiani to'xtatishga harakat qiling.",
    "detective": "🔎 Siz <b>DETECTIVE</b>siz. Tunda bir odamni tekshirib, mafia yoki emasligini bilib olasiz.",
    "citizen": "👤 Siz <b>CITIZEN</b>siz. Maxsus qobiliyatingiz yo'q. Mafiani topib ovoz bering.",
}


# ==========================================================
# MODELLAR
# ==========================================================
@dataclass
class Player:
    user_id: int
    name: str
    avatar: str
    role: str = ""
    alive: bool = True
    is_bot: bool = False

    @property
    def display(self) -> str:
        return f"{self.avatar} {self.name}"


@dataclass
class Game:
    room_code: str
    mode: str                       # "group" | "private"
    host_id: int
    group_chat_id: Optional[int] = None
    players: Dict[int, Player] = field(default_factory=dict)
    phase: str = "lobby"
    day: int = 0
    night_actions: Dict[str, int] = field(default_factory=dict)
    votes: Dict[int, int] = field(default_factory=dict)
    timer_task: Optional[asyncio.Task] = None
    used_avatars: Set[str] = field(default_factory=set)
    next_bot_id: int = -1

    @property
    def broadcast_chats(self) -> List[int]:
        """Umumiy e'lonlar qayerga yuborilishi kerak."""
        if self.mode == "group":
            return [self.group_chat_id]
        # private: har bir real o'yinchining shaxsiy chatiga (user_id == DM chat_id)
        return [p.user_id for p in self.players.values() if not p.is_bot]


def new_room_code() -> str:
    while True:
        code = "".join(random.choices(string.digits, k=4))
        if code not in GAMES:
            return code


def alive_players(game: Game) -> List[Player]:
    return [p for p in game.players.values() if p.alive]


def pick_avatar(game: Game) -> str:
    available = [a for a in AVATARS if a not in game.used_avatars]
    if not available:
        available = AVATARS
        game.used_avatars.clear()
    avatar = random.choice(available)
    game.used_avatars.add(avatar)
    return avatar


def cancel_timer(game: Game):
    if game.timer_task and not game.timer_task.done():
        game.timer_task.cancel()
    game.timer_task = None


async def broadcast(bot: Bot, game: Game, text: str, reply_markup=None):
    """Guruh rejimida bitta chatga, shaxsiy rejimda har bir o'yinchiga alohida yuboradi."""
    for chat_id in game.broadcast_chats:
        try:
            await bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="HTML")
        except Exception:
            log.warning("Xabar yuborilmadi: chat_id=%s", chat_id)


# ==========================================================
# BOT (AI) O'YINCHILARNI TO'LDIRISH
# ==========================================================
def fill_with_bots(game: Game):
    used_names = {p.name for p in game.players.values()}
    target = max(TARGET_TOTAL_PLAYERS, len(game.players))
    target = min(target, MAX_PLAYERS)

    pool = [n for n in BOT_NAMES if n not in used_names]
    random.shuffle(pool)

    while len(game.players) < target:
        name = pool.pop() if pool else f"Bot-{abs(game.next_bot_id)}"
        bot_player = Player(user_id=game.next_bot_id, name=name, avatar=pick_avatar(game), is_bot=True)
        game.players[game.next_bot_id] = bot_player
        game.next_bot_id -= 1


# ==========================================================
# ROL TARQATISH / G'ALABA
# ==========================================================
async def send_role_dm(bot: Bot, game: Game, player: Player):
    if player.is_bot:
        return
    text = (
        f"🎭 <b>Sizning rolingiz: {player.role.upper()}</b>\n\n"
        f"{ROLE_DESC[player.role]}\n\n"
        f"👤 Avataringiz: {player.avatar}\n"
        f"🆔 Xona kodi: <code>{game.room_code}</code>\n"
    )
    await bot.send_message(player.user_id, text, parse_mode="HTML")


async def check_win(bot: Bot, game: Game) -> bool:
    alive = alive_players(game)
    mafia_alive = sum(1 for p in alive if p.role == "mafia")
    others = len(alive) - mafia_alive

    if mafia_alive == 0 or mafia_alive >= others:
        winner = "🏆 <b>Shahar aholisi yutdi!</b>" if mafia_alive == 0 else "🏆 <b>Mafia yutdi!</b>"
        text = winner + "\n\n📋 <b>Yakuniy ro'yxat:</b>\n"
        for p in game.players.values():
            status = "✅" if p.alive else "💀"
            bot_tag = " 🤖" if p.is_bot else ""
            text += f"{status} {p.display}{bot_tag} — {p.role.upper()}\n"
        cancel_timer(game)
        await broadcast(bot, game, text)
        GAMES.pop(game.room_code, None)
        return True
    return False


# ==========================================================
# TUN FAZASI
# ==========================================================
async def send_action_dm(bot: Bot, game: Game, player: Player, action: str, text: str, targets: List[Player]):
    if player.is_bot or not targets:
        return
    kb = InlineKeyboardBuilder()
    for target in targets:
        kb.button(text=target.display, callback_data=f"night:{action}:{game.room_code}:{target.user_id}")
    kb.adjust(2)
    try:
        await bot.send_message(
            player.user_id,
            f"{text}\n\n🆔 Xona: <code>{game.room_code}</code>",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    except Exception:
        log.warning("%s ga DM yuborilmadi", player.name)


def bot_night_decision(game: Game, player: Player, action: str) -> Optional[int]:
    alive = alive_players(game)
    if action == "kill":
        candidates = [p for p in alive if p.role != "mafia"]
    elif action == "heal":
        candidates = alive
    elif action == "check":
        candidates = [p for p in alive if p.user_id != player.user_id]
    else:
        candidates = []
    return random.choice(candidates).user_id if candidates else None


async def begin_night(bot: Bot, game: Game):
    cancel_timer(game)
    game.phase = "night"
    game.night_actions = {}

    await broadcast(
        bot, game,
        f"🌙 <b>Tun {game.day + 1} boshlandi.</b>\nRollar DMda harakat qiladi.\n"
        f"⏰ {NIGHT_TIMEOUT} soniyada hal bo'lmasa, avtomatik yakunlanadi.",
    )

    alive = alive_players(game)
    for player in alive:
        if player.role == "mafia":
            if player.is_bot:
                choice = bot_night_decision(game, player, "kill")
                if choice:
                    game.night_actions["kill"] = choice
            else:
                targets = [p for p in alive if p.user_id != player.user_id]
                await send_action_dm(bot, game, player, "kill", "🔫 Kimni o'ldirasiz?", targets)
        elif player.role == "doctor":
            if player.is_bot:
                choice = bot_night_decision(game, player, "heal")
                if choice:
                    game.night_actions["heal"] = choice
            else:
                await send_action_dm(bot, game, player, "heal", "💉 Kimni davolaysiz?", alive)
        elif player.role == "detective":
            if player.is_bot:
                choice = bot_night_decision(game, player, "check")
                if choice:
                    game.night_actions["check"] = choice
            else:
                targets = [p for p in alive if p.user_id != player.user_id]
                await send_action_dm(bot, game, player, "check", "🔎 Kimni tekshirasiz?", targets)

    await maybe_resolve_night(bot, game)
    if game.phase == "night":
        game.timer_task = asyncio.create_task(night_timeout_watcher(bot, game))


async def night_timeout_watcher(bot: Bot, game: Game):
    try:
        await asyncio.sleep(NIGHT_TIMEOUT)
    except asyncio.CancelledError:
        return
    if game.phase == "night":
        await broadcast(bot, game, "⏰ Vaqt tugadi — tun majburan yakunlanmoqda.")
        await resolve_night(bot, game)


async def maybe_resolve_night(bot: Bot, game: Game):
    if game.phase != "night":
        return
    mapping = {"mafia": "kill", "doctor": "heal", "detective": "check"}
    required = {mapping[p.role] for p in alive_players(game) if p.role in mapping}
    if required.issubset(set(game.night_actions.keys())):
        cancel_timer(game)
        await resolve_night(bot, game)


async def resolve_night(bot: Bot, game: Game):
    if game.phase != "night":
        return
    cancel_timer(game)
    game.phase = "night_resolving"

    kill_target = game.night_actions.get("kill")
    heal_target = game.night_actions.get("heal")
    check_target = game.night_actions.get("check")
    dead_names = []

    if kill_target and kill_target != heal_target:
        victim = game.players.get(kill_target)
        if victim and victim.alive:
            victim.alive = False
            dead_names.append(f"{victim.display} ({victim.role})")

    if check_target:
        detective = next((p for p in game.players.values() if p.alive and p.role == "detective"), None)
        target = game.players.get(check_target)
        if detective and target and not detective.is_bot:
            result = "MAFIA 🔴" if target.role == "mafia" else "mafia EMAS 🟢"
            try:
                await bot.send_message(
                    detective.user_id,
                    f"🔎 <b>Tekshiruv natijasi</b>\n\n👤 {target.display} — {result}",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    if dead_names:
        await broadcast(bot, game, f"🌙 <b>Tun tugadi.</b>\n💀 O'lganlar: {', '.join(dead_names)}")
    else:
        await broadcast(bot, game, "🌙 <b>Tun tugadi.</b> Hech kim o'lmadi. 💚")

    if await check_win(bot, game):
        return
    await begin_day(bot, game)


# ==========================================================
# KUN FAZASI
# ==========================================================
async def bot_cast_votes(game: Game):
    alive = alive_players(game)
    for player in alive:
        if player.is_bot and player.user_id not in game.votes:
            candidates = [p for p in alive if p.user_id != player.user_id]
            if candidates:
                game.votes[player.user_id] = random.choice(candidates).user_id


async def begin_day(bot: Bot, game: Game):
    cancel_timer(game)
    game.phase = "day"
    game.day += 1
    game.votes = {}
    await bot_cast_votes(game)

    alive = alive_players(game)
    kb = InlineKeyboardBuilder()
    for player in alive:
        kb.button(text=player.display, callback_data=f"vote:{game.room_code}:{player.user_id}")
    kb.adjust(2)

    names = "\n".join(f"• {p.display}{' 🤖' if p.is_bot else ''}" for p in alive)
    await broadcast(
        bot, game,
        f"☀️ <b>Kun {game.day}</b>\n\n👥 <b>Tiriklar:</b>\n{names}\n\n"
        f"🗳 Ovoz berish boshlandi.\n"
        f"⏰ {DAY_TIMEOUT} soniyada avtomatik yakunlanadi.",
        reply_markup=kb.as_markup(),
    )

    if len(game.votes) >= len(alive):
        await resolve_vote(bot, game)
        return
    game.timer_task = asyncio.create_task(day_timeout_watcher(bot, game))


async def day_timeout_watcher(bot: Bot, game: Game):
    try:
        await asyncio.sleep(DAY_TIMEOUT)
    except asyncio.CancelledError:
        return
    if game.phase == "day":
        await broadcast(bot, game, "⏰ Vaqt tugadi — ovoz berish majburan yakunlanmoqda.")
        await resolve_vote(bot, game)


async def resolve_vote(bot: Bot, game: Game):
    if game.phase != "day":
        return
    cancel_timer(game)
    game.phase = "day_resolving"

    counts: Dict[int, int] = {}
    for target_id in game.votes.values():
        counts[target_id] = counts.get(target_id, 0) + 1

    if not counts:
        await broadcast(bot, game, "🗳 Ovozlar yo'q. Hech kim osilmadi.")
    else:
        max_votes = max(counts.values())
        top = [uid for uid, c in counts.items() if c == max_votes]
        if len(top) == 1:
            victim = game.players.get(top[0])
            if victim and victim.alive:
                victim.alive = False
                await broadcast(
                    bot, game,
                    f"🗳 <b>Ko'pchilik ovoz bilan</b> {victim.display} osildi.\n"
                    f"🎭 Roli: <b>{victim.role.upper()}</b>",
                )
        else:
            await broadcast(bot, game, "🗳 Ovozlar teng. Hech kim osilmadi.")

    if await check_win(bot, game):
        return
    await begin_night(bot, game)


# ==========================================================
# O'YINNI BOSHLASH (umumiy funksiya — guruh ham, shaxsiy ham)
# ==========================================================
async def launch_game(bot: Bot, game: Game):
    real_count = len(game.players)
    fill_with_bots(game)
    added_bots = len(game.players) - real_count
    if added_bots > 0:
        await broadcast(bot, game, f"🤖 {added_bots} ta bot o'yinchi qo'shildi (real o'yinchilar yetarli emas edi).")

    roles = ["mafia", "doctor", "detective"]
    roles += ["citizen"] * (len(game.players) - 3)
    random.shuffle(roles)
    for player, role in zip(game.players.values(), roles):
        player.role = role

    failed = []
    for player in game.players.values():
        if player.is_bot:
            continue
        try:
            await send_role_dm(bot, game, player)
        except Exception:
            failed.append(player.name)

    if failed:
        cancel_timer(game)
        GAMES.pop(game.room_code, None)
        await broadcast(bot, game, "❌ Ba'zi o'yinchilarga DM yuborilmadi (avval botga /start yozishlari kerak). O'yin bekor qilindi.")
        return

    await broadcast(bot, game, "🎬 <b>O'yin boshlandi!</b> Rollar DMga yuborildi.")
    await begin_night(bot, game)


# ==========================================================
# TEZKOR O'YIN (matchmaking navbati) — faqat DM
# ==========================================================
async def quick_queue_watcher(bot: Bot):
    """Navbatni kuzatadi: yetarli odam yig'ilsa yoki vaqt tugasa — xona yaratadi."""
    global QUICK_QUEUE_TASK
    try:
        await asyncio.sleep(QUICK_QUEUE_TIMEOUT_STEP())
    except asyncio.CancelledError:
        return

    if len(QUICK_QUEUE) >= MIN_REAL_PLAYERS or (QUICK_QUEUE and True):
        await start_quick_game(bot)
    QUICK_QUEUE_TASK = None


def QUICK_QUEUE_TIMEOUT_STEP() -> int:
    return QUEUE_WAIT_TIMEOUT


async def start_quick_game(bot: Bot):
    if len(QUICK_QUEUE) < 1:
        return
    if len(QUICK_QUEUE) < MIN_REAL_PLAYERS:
        for uid in list(QUICK_QUEUE.keys()):
            try:
                await bot.send_message(
                    uid,
                    f"⌛ Hamkorlar yetarli yig'ilmadi ({len(QUICK_QUEUE)}/{MIN_REAL_PLAYERS}). "
                    "Yana urinib ko'ring: /tezkor"
                )
            except Exception:
                pass
        QUICK_QUEUE.clear()
        return

    room_code = new_room_code()
    game = Game(room_code=room_code, mode="private", host_id=next(iter(QUICK_QUEUE)))
    GAMES[room_code] = game

    for uid, name in QUICK_QUEUE.items():
        avatar = pick_avatar(game)
        game.players[uid] = Player(user_id=uid, name=name, avatar=avatar)
    QUICK_QUEUE.clear()

    await broadcast(bot, game, f"✅ <b>Tezkor o'yin topildi!</b> Xona: <code>{room_code}</code>")
    await launch_game(bot, game)


async def add_to_quick_queue(bot: Bot, user_id: int, name: str):
    global QUICK_QUEUE_TASK
    if user_id in QUICK_QUEUE:
        return False
    QUICK_QUEUE[user_id] = name
    if len(QUICK_QUEUE) >= TARGET_TOTAL_PLAYERS:
        if QUICK_QUEUE_TASK:
            QUICK_QUEUE_TASK.cancel()
            QUICK_QUEUE_TASK = None
        await start_quick_game(bot)
    elif QUICK_QUEUE_TASK is None:
        QUICK_QUEUE_TASK = asyncio.create_task(quick_queue_watcher(bot))
    return True


# ==========================================================
# VIP / SIRLI KOD — javob FAQAT yozuvchining DM'iga boradi
# ==========================================================
async def vip_reply(bot: Bot, user_id: int, text: str):
    """VIP javobi hech qachon ochiq chatga (guruhga) yozilmaydi — faqat DM."""
    try:
        await bot.send_message(user_id, text, parse_mode="HTML")
    except Exception:
        log.warning("VIP javobi yuborilmadi: %s botga /start yozmagan bo'lishi mumkin.", user_id)


@router.message(Command("timati"))
async def cmd_timati(message: Message):
    VIP_USERS.add(message.from_user.id)
    await vip_reply(
        message.bot, message.from_user.id,
        "🎁 <b>VIP REJIM FAOLLASHTIRILDI!</b> (bu xabarni faqat siz ko'rasiz)\n\n"
        "👁 /vipse &lt;xona_kodi&gt; — rollarni ko'rish\n"
        "💀 /vipkill &lt;xona_kodi&gt; &lt;ism&gt; — o'ldirish\n"
        "💚 /viprevive &lt;xona_kodi&gt; &lt;ism&gt; — tiriltirish\n"
        "☀️ /vipday &lt;xona_kodi&gt; — kunduzni boshlash\n"
        "🌙 /vipnight &lt;xona_kodi&gt; — tunni boshlash\n"
        "🛑 /vipstop &lt;xona_kodi&gt; — to'xtatish\n"
        "📋 /viplist — barcha xonalar"
    )


def _vip_parse(message: Message, need_extra: bool = False):
    parts = message.text.split(maxsplit=2 if need_extra else 1)
    if len(parts) < 2:
        return None, None
    room_code = parts[1].strip()
    extra = parts[2].strip() if need_extra and len(parts) > 2 else None
    return room_code, extra


@router.message(Command("vipse"))
async def cmd_vipse(message: Message):
    if message.from_user.id not in VIP_USERS:
        return
    room_code, _ = _vip_parse(message)
    game = GAMES.get(room_code) if room_code else None
    if not game:
        await vip_reply(message.bot, message.from_user.id, "❌ Bunday xona topilmadi. /vipse 1234")
        return
    text = "🎭 <b>Barcha rollar:</b>\n\n"
    for p in game.players.values():
        status = "✅" if p.alive else "💀"
        bot_tag = " 🤖" if p.is_bot else ""
        text += f"{status} {p.display}{bot_tag} — <b>{p.role.upper()}</b>\n"
    text += f"\n📊 Faza: <b>{game.phase}</b> | Kun: <b>{game.day}</b>"
    await vip_reply(message.bot, message.from_user.id, text)


@router.message(Command("vipkill"))
async def cmd_vipkill(message: Message):
    if message.from_user.id not in VIP_USERS:
        return
    room_code, target_name = _vip_parse(message, need_extra=True)
    game = GAMES.get(room_code) if room_code else None
    if not game or not target_name:
        await vip_reply(message.bot, message.from_user.id, "Foydalanish: /vipkill 1234 Ism")
        return
    victim = next((p for p in game.players.values() if target_name.lower() in p.name.lower()), None)
    if not victim or not victim.alive:
        await vip_reply(message.bot, message.from_user.id, "❌ Bunday tirik o'yinchi topilmadi.")
        return
    victim.alive = False
    await vip_reply(message.bot, message.from_user.id, f"💀 {victim.display} o'ldirildi. Roli: {victim.role.upper()}")
    await check_win(message.bot, game)


@router.message(Command("viprevive"))
async def cmd_viprevive(message: Message):
    if message.from_user.id not in VIP_USERS:
        return
    room_code, target_name = _vip_parse(message, need_extra=True)
    game = GAMES.get(room_code) if room_code else None
    if not game or not target_name:
        await vip_reply(message.bot, message.from_user.id, "Foydalanish: /viprevive 1234 Ism")
        return
    victim = next((p for p in game.players.values() if target_name.lower() in p.name.lower()), None)
    if not victim or victim.alive:
        await vip_reply(message.bot, message.from_user.id, "❌ Bunday o'lik o'yinchi topilmadi.")
        return
    victim.alive = True
    await vip_reply(message.bot, message.from_user.id, f"💚 {victim.display} tiriltirildi.")


@router.message(Command("vipday"))
async def cmd_vipday(message: Message):
    if message.from_user.id not in VIP_USERS:
        return
    room_code, _ = _vip_parse(message)
    game = GAMES.get(room_code) if room_code else None
    if not game:
        await vip_reply(message.bot, message.from_user.id, "Foydalanish: /vipday 1234")
        return
    await begin_day(message.bot, game)


@router.message(Command("vipnight"))
async def cmd_vipnight(message: Message):
    if message.from_user.id not in VIP_USERS:
        return
    room_code, _ = _vip_parse(message)
    game = GAMES.get(room_code) if room_code else None
    if not game:
        await vip_reply(message.bot, message.from_user.id, "Foydalanish: /vipnight 1234")
        return
    await begin_night(message.bot, game)


@router.message(Command("vipstop"))
async def cmd_vipstop(message: Message):
    if message.from_user.id not in VIP_USERS:
        return
    room_code, _ = _vip_parse(message)
    game = GAMES.get(room_code) if room_code else None
    if game:
        cancel_timer(game)
        GAMES.pop(game.room_code, None)
    await vip_reply(message.bot, message.from_user.id, "🛑 Xona to'xtatildi.")


@router.message(Command("viplist"))
async def cmd_viplist(message: Message):
    if message.from_user.id not in VIP_USERS:
        return
    if not GAMES:
        await vip_reply(message.bot, message.from_user.id, "📭 Aktiv xonalar yo'q.")
        return
    text = "📋 <b>Aktiv xonalar:</b>\n\n"
    for code, g in GAMES.items():
        text += f"🆔 <code>{code}</code> ({g.mode}) — Faza: {g.phase} | O'yinchilar: {len(g.players)}\n"
    await vip_reply(message.bot, message.from_user.id, text)


# ==========================================================
# ASOSIY BUYRUQLAR
# ==========================================================
@router.message(Command("start"))
async def cmd_start(message: Message):
    is_private = message.chat.type == "private"
    extra = (
        "\n🎮 <b>Shaxsiy chatda o'ynash:</b>\n"
        "/tezkor — tezkor o'yin (boshqa kutayotganlar bilan avtomatik birlashadi)\n"
        "/xona — shaxsiy xona ochish (do'stlaringizga kod bering)\n"
        "/join &lt;kod&gt; — kod bilan xonaga qo'shilish\n"
        if is_private else ""
    )
    await message.answer(
        "🎭 <b>Mafia O'yini Botiga xush kelibsiz!</b> (PRO MAX)\n\n"
        "📌 <b>Guruhda ishlatiladigan buyruqlar:</b>\n"
        "/newgame — yangi o'yin yaratish\n"
        "/join — o'yinga qo'shilish\n"
        "/startgame — o'yinni boshlash (faqat host)\n"
        "/endvote — ovoz berishni yakunlash (faqat host)\n"
        "/stop — o'yinni bekor qilish (faqat host)\n"
        "/app — Mini App'ni ochish\n"
        f"{extra}\n"
        f"👥 Kamida <b>{MIN_REAL_PLAYERS} ta real o'yinchi</b> yetarli — qolganini bot to'ldiradi!",
        parse_mode="HTML",
    )


@router.message(Command("app"))
async def cmd_app(message: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🎮 Mini App'ni ochish", web_app=WebAppInfo(url=WEBAPP_URL))
    await message.answer("📱 Barcha qurilmalarda bir xil ishlaydi.", reply_markup=kb.as_markup())


# ---------- Tezkor o'yin (DM, matchmaking) ----------
@router.message(Command("tezkor"))
async def cmd_tezkor(message: Message, bot: Bot):
    if message.chat.type != "private":
        await message.answer("❌ Tezkor o'yin faqat botning shaxsiy chatida ishlaydi. Bot bilan DM oching.")
        return
    added = await add_to_quick_queue(bot, message.from_user.id, message.from_user.full_name)
    if not added:
        await message.answer("⏳ Siz allaqachon navbatdasiz...")
        return
    await message.answer(
        f"🕐 Navbatga qo'shildingiz ({len(QUICK_QUEUE)} kishi kutmoqda).\n"
        f"Kamida {MIN_REAL_PLAYERS} kishi yig'ilsa yoki {QUEUE_WAIT_TIMEOUT}s o'tsa, o'yin boshlanadi."
    )


# ---------- Shaxsiy xona (DM) ----------
@router.message(Command("xona"))
async def cmd_xona(message: Message):
    if message.chat.type != "private":
        await message.answer("❌ Shaxsiy xona faqat DM'da ochiladi.")
        return
    room_code = new_room_code()
    game = Game(room_code=room_code, mode="private", host_id=message.from_user.id)
    avatar = pick_avatar(game)
    game.players[message.from_user.id] = Player(
        user_id=message.from_user.id, name=message.from_user.full_name, avatar=avatar
    )
    GAMES[room_code] = game
    await message.answer(
        f"🏠 <b>Shaxsiy xona yaratildi!</b>\n🆔 Kod: <code>{room_code}</code>\n\n"
        f"Do'stlaringiz bu kodni botga <code>/join {room_code}</code> deb yozishi kerak (DM'da).\n"
        f"Kamida {MIN_REAL_PLAYERS} kishi yig'ilgach /startgame yozing.",
        parse_mode="HTML",
    )


@router.message(Command("newgame"))
async def cmd_newgame(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("❌ Bu komanda faqat guruhda ishlaydi. Shaxsiy o'yin uchun /xona yoki /tezkor.")
        return
    room_code = str(message.chat.id)
    if room_code in GAMES:
        await message.answer("❌ Bu guruhda allaqachon o'yin bor.")
        return
    game = Game(room_code=room_code, mode="group", host_id=message.from_user.id, group_chat_id=message.chat.id)
    GAMES[room_code] = game
    await message.answer(
        "🎮 <b>Mafia o'yini yaratildi!</b>\n\n"
        "📌 /join bilan qo'shiling\n"
        f"👥 Kamida {MIN_REAL_PLAYERS} ta real o'yinchi yetarli\n"
        "▶️ Host /startgame bilan boshlaydi\n"
        "⚠️ Avval botga /start yozib qo'ying!",
        parse_mode="HTML",
    )


@router.message(Command("join"))
async def cmd_join(message: Message):
    args = message.text.split(maxsplit=1)
    is_private = message.chat.type == "private"

    if is_private:
        if len(args) < 2:
            await message.answer("Foydalanish: <code>/join 1234</code> (xona kodi bilan)", parse_mode="HTML")
            return
        room_code = args[1].strip()
    else:
        room_code = str(message.chat.id)

    game = GAMES.get(room_code)
    if not game:
        await message.answer("❌ Bunday xona topilmadi.")
        return
    if game.phase != "lobby":
        await message.answer("❌ O'yin allaqachon boshlangan.")
        return

    user_id = message.from_user.id
    if user_id in game.players:
        await message.answer("❌ Siz allaqachon qo'shilgansiz.")
        return
    if len(game.players) >= MAX_PLAYERS:
        await message.answer(f"❌ O'yinchi limiti to'ldi ({MAX_PLAYERS} ta).")
        return

    avatar = pick_avatar(game)
    game.players[user_id] = Player(user_id=user_id, name=message.from_user.full_name, avatar=avatar)

    names = "\n".join(f"• {p.display}" for p in game.players.values())
    await broadcast(
        message.bot, game,
        f"✅ <b>{avatar} {message.from_user.full_name}</b> qo'shildi.\n\n"
        f"👥 <b>Ishtirokchilar ({len(game.players)}):</b>\n{names}",
    )


@router.message(Command("startgame"))
async def cmd_startgame(message: Message, bot: Bot):
    is_private = message.chat.type == "private"
    room_code = str(message.chat.id) if not is_private else None

    if is_private:
        # DM'da: foydalanuvchi o'zi host bo'lgan oxirgi lobby-xonasini topamiz
        game = next(
            (g for g in GAMES.values() if g.mode == "private" and g.host_id == message.from_user.id and g.phase == "lobby"),
            None,
        )
    else:
        game = GAMES.get(room_code)

    if not game:
        await message.answer("❌ Avval /newgame yoki /xona bilan o'yin yarating.")
        return
    if game.phase != "lobby":
        await message.answer("❌ O'yin allaqachon boshlangan.")
        return
    if message.from_user.id != game.host_id:
        await message.answer("❌ Faqat host o'yinni boshlay oladi.")
        return
    if len(game.players) < MIN_REAL_PLAYERS:
        await message.answer(f"❌ Kamida {MIN_REAL_PLAYERS} ta real o'yinchi kerak.")
        return

    await launch_game(bot, game)


@router.message(Command("endvote"))
async def cmd_endvote(message: Message, bot: Bot):
    game = _find_game_for_message(message)
    if not game:
        await message.answer("❌ O'yin topilmadi.")
        return
    if message.from_user.id != game.host_id:
        await message.answer("❌ Faqat host ovoz berishni yakunlay oladi.")
        return
    if game.phase != "day":
        await message.answer("❌ Hozir ovoz berish fazasi emas.")
        return
    await resolve_vote(bot, game)


@router.message(Command("stop"))
async def cmd_stop(message: Message):
    game = _find_game_for_message(message)
    if not game:
        await message.answer("❌ O'yin topilmadi.")
        return
    if message.from_user.id != game.host_id:
        await message.answer("❌ Faqat host o'yinni bekor qila oladi.")
        return
    cancel_timer(game)
    GAMES.pop(game.room_code, None)
    await message.answer("🛑 O'yin bekor qilindi.")


def _find_game_for_message(message: Message) -> Optional[Game]:
    if message.chat.type == "private":
        return next(
            (g for g in GAMES.values() if g.mode == "private" and message.from_user.id in g.players),
            None,
        )
    return GAMES.get(str(message.chat.id))


# ==========================================================
# CALLBACK HANDLERLAR
# ==========================================================
@router.callback_query(F.data.startswith("night:"))
async def night_action(callback: CallbackQuery, bot: Bot):
    try:
        _, action, room_code, target_s = callback.data.split(":")
        target_id = int(target_s)
    except Exception:
        await callback.answer("Xato ma'lumot.", show_alert=True)
        return

    game = GAMES.get(room_code)
    if not game:
        await callback.answer("O'yin topilmadi.", show_alert=True)
        return

    player = game.players.get(callback.from_user.id)
    if not player or not player.alive:
        await callback.answer("Siz o'yinda emassiz yoki o'lgansiz.", show_alert=True)
        return
    if game.phase != "night":
        await callback.answer("Hozir tun fazasi emas.", show_alert=True)
        return
    if action == "kill" and player.role != "mafia":
        await callback.answer("Siz mafia emassiz.", show_alert=True)
        return
    if action == "heal" and player.role != "doctor":
        await callback.answer("Siz doctor emassiz.", show_alert=True)
        return
    if action == "check" and player.role != "detective":
        await callback.answer("Siz detective emassiz.", show_alert=True)
        return

    target = game.players.get(target_id)
    if not target or not target.alive:
        await callback.answer("Bu o'yinchi tirik emas.", show_alert=True)
        return
    if action in ("kill", "check") and target_id == player.user_id:
        await callback.answer("O'zingizni tanlay olmaysiz.", show_alert=True)
        return
    if action in game.night_actions:
        await callback.answer("Siz allaqachon tanlov qildingiz.", show_alert=True)
        return

    game.night_actions[action] = target_id
    await callback.answer("✅ Qabul qilindi.")
    try:
        if callback.message:
            await callback.message.edit_text(f"✅ Tanlandi: {target.display}")
    except Exception:
        pass
    await maybe_resolve_night(bot, game)


@router.callback_query(F.data.startswith("vote:"))
async def vote_action(callback: CallbackQuery, bot: Bot):
    try:
        _, room_code, target_s = callback.data.split(":")
        target_id = int(target_s)
    except Exception:
        await callback.answer("Xato ma'lumot.", show_alert=True)
        return

    game = GAMES.get(room_code)
    if not game:
        await callback.answer("O'yin topilmadi.", show_alert=True)
        return
    if game.phase != "day":
        await callback.answer("Hozir ovoz berish fazasi emas.", show_alert=True)
        return

    voter = game.players.get(callback.from_user.id)
    if not voter or not voter.alive:
        await callback.answer("Siz tirik o'yinchi emassiz.", show_alert=True)
        return
    if voter.user_id in game.votes:
        await callback.answer("Siz allaqachon ovoz bergansiz.", show_alert=True)
        return

    target = game.players.get(target_id)
    if not target or not target.alive:
        await callback.answer("Bu o'yinchi tirik emas.", show_alert=True)
        return
    if target_id == voter.user_id:
        await callback.answer("O'zingizga ovoz bera olmaysiz.", show_alert=True)
        return

    game.votes[voter.user_id] = target_id
    await callback.answer("🗳 Ovoz qabul qilindi.")
    if len(game.votes) >= len(alive_players(game)):
        await resolve_vote(bot, game)


# ==========================================================
# ISHGA TUSHIRISH
# ==========================================================
async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    log.info("✅ Bot ishga tushdi...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
