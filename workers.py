import asyncio
import re
import time
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import FloodWait, RPCError, SessionRevoked, AuthKeyUnregistered, UserDeactivated, UserDeactivatedBan
from config import API_ID, API_HASH, BOT_USER_ID
from database import get_user, get_all_users, DEFAULT_FISH_RULES, DEFAULT_COOKED_RULES

active_tasks = {}

BTN_SELL = "فروش ماهی"
BTN_CAT = "بده پیشی بخوره"
BTN_FRIDGE = "بندازش تو یخچال"

HARVEST_CD = 30 * 60
FRIDGE_CHECK_CD = 20 * 60

cooldowns = {}

# صف سراسری بین همه اکانت‌ها
_global_lock = asyncio.Lock()
_last_global_action = 0.0
GLOBAL_GAP = 10  # ثانیه بین هر کلیک/ارسال


async def global_slot(tag: str = ""):
    """صف مشترک — قبل از هر کلیک یا send_message"""
    global _last_global_action
    async with _global_lock:
        now = time.time()
        wait = GLOBAL_GAP - (now - _last_global_action)
        if wait > 0:
            print(f"🧊 صف {wait:.1f}s {tag}")
            await asyncio.sleep(wait)
        _last_global_action = time.time()


def safe_text(message: Message) -> str:
    try:
        return message.text or message.caption or ""
    except Exception:
        return ""


def parse_wait_seconds(text: str):
    if not text:
        return None
    m = re.search(r"(?:باید|پخیدن\s*:)\s*(\d{1,3}):(\d{2})", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.search(r"بعد از\s*(\d{1,3}):(\d{2})", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return None


def set_cd(phone: str, key: str, seconds: int):
    if phone not in cooldowns:
        cooldowns[phone] = {}
    cooldowns[phone][key] = time.time() + max(3, seconds)


def get_cd_left(phone: str, key: str) -> float:
    if phone not in cooldowns:
        return 0
    return max(0, cooldowns[phone].get(key, 0) - time.time())


def btn_texts(message: Message) -> list:
    try:
        if not message.reply_markup or not message.reply_markup.inline_keyboard:
            return []
        out = []
        for row in message.reply_markup.inline_keyboard:
            for b in row:
                out.append((b.text or "").strip())
        return out
    except Exception:
        return []


def has_btn(message: Message, *names) -> bool:
    texts = btn_texts(message)
    for n in names:
        n = (n or "").strip()
        for t in texts:
            if t == n or (n and n in t):
                return True
    return False


async def click_exact(message: Message, exact_text: str) -> bool:
    if not exact_text or not message.reply_markup or not message.reply_markup.inline_keyboard:
        return False
    exact_text = exact_text.strip()
    try:
        for row in message.reply_markup.inline_keyboard:
            for btn in row:
                t = (btn.text or "").strip()
                if t == exact_text or (exact_text and exact_text in t):
                    try:
                        await global_slot(f"click:{t[:20]}")
                        await message.click(t if t else 0)
                        print(f"✅ کلیک شد روی: {t or '(خالی)'}")
                        return True
                    except Exception as e:
                        print(f"❌ خطا در کلیک: {e}")
                        return False
    except Exception as e:
        print(f"❌ خطا click_exact: {e}")
    print(f"⚠️ دکمه «{exact_text}» پیدا نشد")
    return False


async def click_index(message: Message, index: int) -> bool:
    try:
        await global_slot(f"idx:{index}")
        await message.click(index)
        print(f"✅ کلیک index={index}")
        return True
    except Exception as e:
        print(f"❌ خطا کلیک index={index}: {e}")
        return False


async def click_empty_fish_buttons(message: Message) -> bool:
    if not message.reply_markup or not message.reply_markup.inline_keyboard:
        return False
    idx = 0
    try:
        for row in message.reply_markup.inline_keyboard:
            for btn in row:
                raw = btn.text or ""
                t = raw.strip()
                if "ارتقا" in raw:
                    idx += 1
                    continue
                if not t or t in ("\u200b", "​", "‌", ""):
                    try:
                        await global_slot(f"fish:{idx}")
                        await message.click(idx)
                        print(f"✅ کلیک ماهی خالی index={idx}")
                        await asyncio.sleep(1.5)
                        return True
                    except Exception as e:
                        print(f"❌ خطا کلیک ماهی: {e}")
                idx += 1
    except Exception as e:
        print(f"❌ خطا click_empty: {e}")
    return False


async def rescue_loop(client: Client, chat_id: int, msg_id: int, rescue_btn: str):
    for i in range(40):
        try:
            msg = await client.get_messages(chat_id, msg_id)
            if not msg or not msg.reply_markup or not msg.reply_markup.inline_keyboard:
                print("✅ دکمه نجات ناپدید شد")
                break
            clicked = False
            if rescue_btn:
                target = rescue_btn.strip()
                for row in msg.reply_markup.inline_keyboard:
                    for btn in row:
                        if target in (btn.text or ""):
                            await global_slot(f"rescue:{i+1}")
                            await msg.click(btn.text)
                            clicked = True
                            break
                    if clicked:
                        break
            if not clicked:
                await global_slot(f"rescue0:{i+1}")
                await msg.click(0)
            print(f"🚑 نجات کلیک {i+1}")
            await asyncio.sleep(1.4)
        except Exception as e:
            print(f"خطا نجات: {e}")
            break


def detect_fish_level(text: str):
    for lv in ["افسانه", "حماسی", "کمیاب", "غیرمعمول", "معمولی", "اسطوره"]:
        if lv in (text or ""):
            return lv
    return None


def choose_fish_action(text: str, rules: dict) -> str:
    if "یخچال پر" in text or "یخچال پره" in text:
        return "sell"
    level = detect_fish_level(text)
    if level and level in rules:
        return rules[level]
    return "sell"


async def handle_fish_catch(message: Message, rules: dict, phone: str):
    text = safe_text(message)
    action = choose_fish_action(text, rules)
    print(f"🎣 تصمیم صید: {action} | سطح: {detect_fish_level(text)}")
    if action == "fridge":
        ok = await click_exact(message, BTN_FRIDGE)
        if ok:
            set_cd(phone, "fridge_check", 8)
        else:
            await click_exact(message, BTN_SELL)
    elif action == "cat":
        ok = await click_exact(message, BTN_CAT)
        if not ok:
            await click_exact(message, BTN_SELL)
    else:
        await click_exact(message, BTN_SELL)


async def handle_fridge_message(message: Message, phone: str, rules: dict, cooked_rules: dict):
    text = safe_text(message)

    if "پخت و پز" in text or "آیا از پخیدن" in text or "درحال پخیدن" in text:
        wait = parse_wait_seconds(text)
        if wait:
            set_cd(phone, "cook", wait + 10)
            print(f"⏳ تایم پخت: {wait}s")
        if message.reply_markup:
            await click_index(message, 0)
        return

    is_list = (
        "ظرفیت یخچال" in text
        or ("یخچال میویی" in text and any("ارتقا" in t for t in btn_texts(message)))
    )
    if is_list and not has_btn(message, BTN_SELL, BTN_CAT, "بپوخش"):
        if "خالی است" in text:
            print("❄️ یخچال خالی")
            return

        is_cooked = "پخته" in text
        level = detect_fish_level(text)
        action_map = cooked_rules if is_cooked else rules
        action = action_map.get(level, "fridge") if level else "fridge"

        print(f"❄️ لیست یخچال | سطح={level} | پخته={is_cooked} | عمل={action}")

        if action == "keep":
            print("🧊 نگهداری")
            return

        await click_empty_fish_buttons(message)
        return

    if has_btn(message, BTN_SELL, BTN_CAT, "بپوخش") or "میخوای چیکارش کنی" in text:
        is_cooked = "پخته" in text
        level = detect_fish_level(text)
        action_map = cooked_rules if is_cooked else rules
        action = action_map.get(level, "sell") if level else "sell"

        print(f"🐟 منوی ماهی | سطح={level} | پخته={is_cooked} | عمل={action}")

        if action == "keep":
            print("🧊 نگهداری")
            return

        if not is_cooked and action == "fridge":
            if has_btn(message, "بپوخش"):
                print("🍳 بپوخش (متن)")
                await click_exact(message, "بپوخش")
            else:
                print("🍳 بپوخش (index=2)")
                await click_index(message, 2)
            return

        if action == "cat":
            ok = await click_exact(message, BTN_CAT)
            if not ok:
                await click_exact(message, BTN_SELL)
        else:
            await click_exact(message, BTN_SELL)
        return


async def process_bot_message(c: Client, message: Message, phone: str):
    try:
        u = get_user(phone)
        if not u or not u["is_active"]:
            return

        text = safe_text(message)
        print(f"📩 [{phone}] پیام: {text[:90]}")

        try:
            if message.reply_markup and message.reply_markup.inline_keyboard:
                print("🔘 دکمه‌ها:")
                for ri, row in enumerate(message.reply_markup.inline_keyboard):
                    for bi, btn in enumerate(row):
                        print(f"   [{ri},{bi}] = '{btn.text}'")
        except Exception:
            pass

        harvest_btn = (u.get("harvest_button") or "برداشت میو پوینت ها").strip()
        rescue_btn = (u.get("rescue_button") or "نجات پیشی خیابونی").strip()
        rules = u.get("fish_rules") or DEFAULT_FISH_RULES
        cooked_rules = u.get("cooked_rules") or DEFAULT_COOKED_RULES

        if "ماهیا هنوز خوابن" in text or ("باید" in text and "صبر" in text):
            wait = parse_wait_seconds(text)
            if wait:
                set_cd(phone, "catch", wait + 3)
                print(f"⏳ cooldown ماهی: {wait}s")

        if "بعد از" in text and "میو" in text:
            wait = parse_wait_seconds(text)
            if wait:
                set_cd(phone, "meow", wait + 3)
                print(f"⏳ cooldown میو: {wait}s")

        if u.get("rescue_enabled") and ("نجات پیشی" in text or "پیشی خیابونی" in text):
            if "موفقیت نجات" in text or "صاحب یک خونه" in text:
                return
            asyncio.create_task(rescue_loop(c, message.chat.id, message.id, rescue_btn))
            return

        if u.get("catch_enabled") and (
            "یخچال میویی" in text
            or "پخت و پز" in text
            or "میخوای چیکارش کنی" in text
            or "درحال پخیدن" in text
            or "ظرفیت یخچال" in text
            or "پخته" in text
        ):
            await handle_fridge_message(message, phone, rules, cooked_rules)
            return

        if u.get("catch_enabled"):
            has_fish_btns = has_btn(message, BTN_SELL, BTN_CAT, BTN_FRIDGE)
            if has_fish_btns or ("گرفتید" in text and "🎣" in text):
                if has_fish_btns:
                    await handle_fish_catch(message, rules, phone)
                    return

        # برداشت — هر ۳۰ دقیقه یک‌بار
        if u.get("fish_enabled") and has_btn(message, harvest_btn, "برداشت میو"):
            left = get_cd_left(phone, "harvest")
            if left > 0:
                print(f"⏳ برداشت صبر: {int(left)}s")
            else:
                ok = await click_exact(message, harvest_btn)
                if ok:
                    set_cd(phone, "harvest", HARVEST_CD)
                    print(f"✅ برداشت OK — {HARVEST_CD // 60} دقیقه صبر")
            return

    except Exception as e:
        print(f"⚠️ خطا پردازش پیام [{phone}]: {e}")


async def selfbot_worker(phone: str):
    print(f"🚀 Worker شروع شد برای {phone}")

    while True:
        user = get_user(phone)
        if not user or not user["is_active"] or not user["session_string"]:
            print(f"⏳ {phone} غیرفعال - صبر...")
            await asyncio.sleep(20)
            continue

        if user["selected_groups"]:
            chat_ids = [int(g) for g in user["selected_groups"]]
        else:
            chat_ids = [-1003998125518]
            print("⚠️ گروه پیش‌فرض")

        print(f"📋 گروه‌های هدف {phone}: {chat_ids}")

        # پاکسازی session_string از کاراکترهای اضافی
        session_str = user["session_string"].strip().replace("\n", "").replace("\r", "").replace(" ", "")

        client = Client(
            name=f"sb_{phone}",
            session_string=session_str,
            api_id=API_ID,
            api_hash=API_HASH,
            in_memory=True
        )

        try:
            await client.start()
            me = await client.get_me()
            print(f"✅ {phone} آنلاین → {me.first_name}")

            try:
                async for _ in client.get_dialogs(limit=200):
                    pass
                print("📥 دیالوگ‌ها لود شد")
            except Exception as e:
                print(f"⚠️ دیالوگ: {e}")

            valid = []
            for cid in chat_ids:
                try:
                    chat = await client.get_chat(cid)
                    valid.append(cid)
                    print(f"✅ peer: {cid} → {getattr(chat, 'title', cid)}")
                except Exception as e:
                    print(f"⚠️ resolve نشد (ادامه): {cid} → {e}")
                    valid.append(cid)

            if not valid:
                await client.stop()
                await asyncio.sleep(30)
                continue
            chat_ids = valid

            @client.on_message(filters.chat(chat_ids) & filters.user(BOT_USER_ID))
            async def on_new(c: Client, message: Message):
                await process_bot_message(c, message, phone)

            @client.on_edited_message(filters.chat(chat_ids) & filters.user(BOT_USER_ID))
            async def on_edit(c: Client, message: Message):
                print(f"✏️ [{phone}] پیام ویرایش شد")
                await process_bot_message(c, message, phone)

            async def meow_loop():
                while True:
                    u = get_user(phone)
                    if not u or not u["is_active"] or not u["meow_enabled"]:
                        await asyncio.sleep(15)
                        continue
                    left = get_cd_left(phone, "meow")
                    if left > 0:
                        await asyncio.sleep(min(left, 30))
                        continue
                    interval = max(30, int(u.get("meow_interval") or 300))
                    for cid in chat_ids:
                        try:
                            await global_slot("میو")
                            await client.send_message(cid, "میو")
                            print(f"😺 [{phone}] میو → {cid}")
                            await asyncio.sleep(2)
                        except Exception as e:
                            print(f"❌ میو: {e}")
                    await asyncio.sleep(interval)

            async def fish_loop():
                while True:
                    u = get_user(phone)
                    if not u or not u["is_active"] or not u["fish_enabled"]:
                        await asyncio.sleep(15)
                        continue
                    interval = max(30, int(u.get("fish_interval") or 600))
                    for cid in chat_ids:
                        try:
                            await global_slot("پیشی")
                            await client.send_message(cid, "پیشی")
                            print(f"🐱 [{phone}] پیشی → {cid}")
                            await asyncio.sleep(4)
                        except Exception as e:
                            print(f"❌ پیشی: {e}")
                    await asyncio.sleep(interval)

            async def catch_loop():
                while True:
                    u = get_user(phone)
                    if not u or not u["is_active"] or not u.get("catch_enabled"):
                        await asyncio.sleep(15)
                        continue
                    left = get_cd_left(phone, "catch")
                    if left > 0:
                        await asyncio.sleep(min(left, 30))
                        continue
                    interval = max(30, int(u.get("catch_interval") or 120))
                    for cid in chat_ids:
                        try:
                            await global_slot("ماهی")
                            await client.send_message(cid, "ماهی")
                            print(f"🎣 [{phone}] ماهی → {cid}")
                            await asyncio.sleep(3)
                        except Exception as e:
                            print(f"❌ ماهی: {e}")
                    await asyncio.sleep(interval)

            async def fridge_loop():
                while True:
                    u = get_user(phone)
                    if not u or not u["is_active"] or not u.get("catch_enabled"):
                        await asyncio.sleep(30)
                        continue

                    left_cook = get_cd_left(phone, "cook")
                    if left_cook > 0:
                        await asyncio.sleep(min(left_cook, 30))
                        continue

                    left_fr = get_cd_left(phone, "fridge_check")
                    if left_fr > 0:
                        await asyncio.sleep(min(left_fr, 60))
                        continue

                    for cid in chat_ids:
                        try:
                            await global_slot("یخچال")
                            await client.send_message(cid, "یخچال میویی")
                            print(f"❄️ [{phone}] چک یخچال → {cid}")
                            set_cd(phone, "fridge_check", FRIDGE_CHECK_CD)
                            await asyncio.sleep(5)
                        except Exception as e:
                            print(f"❌ یخچال: {e}")
                    await asyncio.sleep(60)

            tasks = [
                asyncio.create_task(meow_loop()),
                asyncio.create_task(fish_loop()),
                asyncio.create_task(catch_loop()),
                asyncio.create_task(fridge_loop()),
            ]

            while True:
                u = get_user(phone)
                if not u or not u["is_active"]:
                    break
                await asyncio.sleep(10)

            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        except (SessionRevoked, AuthKeyUnregistered, UserDeactivated, UserDeactivatedBan) as e:
            print(f"🚫 Session مرده/باطل شده برای {phone}: {e}")
            # اینجا می‌تونی is_active را False کنی اگر بخوای
            break
        except Exception as e:
            print(f"❌ خطای بزرگ {phone}: {e}")
        finally:
            try:
                await client.stop()
            except Exception:
                pass
            print(f"🛑 {phone} متوقف شد")

        await asyncio.sleep(15)


def start_worker(phone: str, loop):
    if phone in active_tasks and not active_tasks[phone].done():
        return
    task = asyncio.run_coroutine_threadsafe(selfbot_worker(phone), loop)
    active_tasks[phone] = task
    print(f"▶️ تسک برای {phone} ساخته شد")


def stop_worker(phone: str):
    if phone in active_tasks and not active_tasks[phone].done():
        active_tasks[phone].cancel()
        del active_tasks[phone]
        print(f"⏹️ تسک {phone} متوقف شد")


def start_all_active(loop):
    users = get_all_users()
    for u in users:
        if u["is_active"]:
            start_worker(u["phone"], loop)
