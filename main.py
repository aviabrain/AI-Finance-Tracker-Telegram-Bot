import os
import sqlite3
import logging
import json
import uuid
import csv
from io import StringIO
import re
from datetime import datetime, timedelta, timezone
from dateutil.relativedelta import relativedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from openai import OpenAI
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg')

# --- ⚠️ IMPORTANT: PASTE YOUR API KEYS HERE AND KEEP THEM SECRET ---
TELEGRAM_BOT_TOKEN = "TG_API_KEY"
OPENAI_API_KEY = "OPENAI_API"
# ----------------------------------------------------

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize OpenAI Client
client = OpenAI(api_key=OPENAI_API_KEY)


# =========================================================================================
# DATABASE FUNCTIONS
# =========================================================================================
## --- MODIFIED: A fully robust function to safely upgrade any old database schema ---
def init_db():
    """
    Initializes and safely migrates the database schema to the latest version.
    Checks for the existence of all required tables and columns before creating or altering them.
    """
    conn = sqlite3.connect('finance_tracker.db')
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")

    # --- 1. Ensure 'users' table exists ---
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            phone_number TEXT NOT NULL,
            first_name TEXT,
            registration_date TEXT NOT NULL
        )
    ''')

    # --- 2. Ensure 'transactions' table exists ---
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            type TEXT NOT NULL,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            balance REAL NOT NULL,
            description TEXT
        )
    ''')

    # --- 3. Safely add ALL required columns to the 'transactions' table if they are missing ---
    cursor.execute("PRAGMA table_info(transactions)")
    existing_trans_columns = {row[1] for row in cursor.fetchall()}
    
    # This dictionary now includes EVERY column added after the very first version
    required_trans_columns = {
        "currency": "TEXT NOT NULL DEFAULT 'UZS'",  # The missing check is now included!
        "debtor_name": "TEXT",
        "return_date": "TEXT",
        "notified": "INTEGER DEFAULT 0",
        "debt_status": "TEXT DEFAULT 'open'",
        "is_deleted": "INTEGER DEFAULT 0"
    }

    for col_name, col_type in required_trans_columns.items():
        if col_name not in existing_trans_columns:
            logger.info(f"Upgrading 'transactions' table: Adding missing column '{col_name}'.")
            cursor.execute(f"ALTER TABLE transactions ADD COLUMN {col_name} {col_type}")

    conn.commit()
    conn.close()
    logger.info("Database initialized and schema verified successfully.")
def is_user_registered(user_id: int) -> bool:
    conn = sqlite3.connect('finance_tracker.db')
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def register_user(user_id: int, phone_number: str, first_name: str):
    conn = sqlite3.connect('finance_tracker.db')
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO users (user_id, phone_number, first_name, registration_date) VALUES (?, ?, ?, ?)",
        (user_id, phone_number, first_name, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()
    logger.info(f"New user registered: {user_id}")

def get_last_balance(user_id: int, currency: str) -> float:
    conn = sqlite3.connect('finance_tracker.db')
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM transactions WHERE user_id = ? AND currency = ? AND is_deleted = 0 ORDER BY id DESC LIMIT 1", (user_id, currency.upper()))
    result = cursor.fetchone()
    conn.close()
    return float(result[0]) if result else 0.0

def add_multiple_transactions(user_id: int, transactions: list) -> (dict, list):
    conn = sqlite3.connect('finance_tracker.db')
    cursor = conn.cursor()
    balances = {'UZS': get_last_balance(user_id, 'UZS'), 'USD': get_last_balance(user_id, 'USD')}
    new_transaction_ids = []

    for trans in transactions:
        amount = float(trans['amount'])
        trans_type = trans['type'].lower()
        currency = trans.get('currency', 'UZS').upper()

        if trans_type == 'expense': balances[currency] -= amount
        elif trans_type == 'income': balances[currency] += amount
        else: continue

        debtor = trans.get('debtor_name')
        return_date = trans.get('return_date')
            
        cursor.execute(
            "INSERT INTO transactions (user_id, date, type, category, amount, currency, balance, description, debtor_name, return_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, datetime.now(timezone.utc).isoformat(), trans['type'], trans['category'], amount, currency, balances[currency], trans['description'], debtor, return_date)
        )
        new_transaction_ids.append(cursor.lastrowid)

    conn.commit()
    conn.close()
    logger.info(f"{len(transactions)} transactions added for user {user_id}. IDs: {new_transaction_ids}")
    return balances, new_transaction_ids

def get_transactions_for_period(user_id: int, start_date: datetime, end_date: datetime, trans_type: str):
    conn = sqlite3.connect('finance_tracker.db')
    cursor = conn.cursor()
    query = "SELECT category, currency, SUM(amount) FROM transactions WHERE user_id = ? AND date BETWEEN ? AND ? AND type = ? AND is_deleted = 0 GROUP BY category, currency ORDER BY SUM(amount) DESC"
    params = [user_id, start_date.isoformat(), end_date.isoformat(), trans_type]
    cursor.execute(query, tuple(params))
    results = cursor.fetchall()
    conn.close()
    return results

def get_all_transactions(user_id: int) -> list:
    conn = sqlite3.connect('finance_tracker.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    query = "SELECT * FROM transactions WHERE user_id = ? AND is_deleted = 0 ORDER BY id DESC"
    cursor.execute(query, (user_id,))
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return results

def delete_transaction_and_recalculate(user_id: int, transaction_id: int):
    conn = sqlite3.connect('finance_tracker.db')
    conn.isolation_level = None
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN")
        cursor.execute("SELECT * FROM transactions WHERE id = ? AND user_id = ? AND is_deleted = 0", (transaction_id, user_id))
        trans_to_delete_row = cursor.fetchone()
        if not trans_to_delete_row: raise ValueError("Transaction not found.")
        
        trans_to_delete = dict(zip([d[0] for d in cursor.description], trans_to_delete_row))
        
        cursor.execute("UPDATE transactions SET is_deleted = 1 WHERE id = ?", (transaction_id,))
        cursor.execute("SELECT balance FROM transactions WHERE user_id = ? AND currency = ? AND id < ? AND is_deleted = 0 ORDER BY id DESC LIMIT 1", (user_id, trans_to_delete['currency'], transaction_id))
        previous_balance = cursor.fetchone()
        current_balance = float(previous_balance[0]) if previous_balance else 0.0
        
        cursor.execute("SELECT id, type, amount FROM transactions WHERE user_id = ? AND currency = ? AND id > ? AND is_deleted = 0 ORDER BY id ASC", (user_id, trans_to_delete['currency'], transaction_id))
        subsequent_transactions = cursor.fetchall()
        
        for sub_id, sub_type, sub_amount in subsequent_transactions:
            if sub_type.lower() == 'expense': current_balance -= sub_amount
            else: current_balance += sub_amount
            cursor.execute("UPDATE transactions SET balance = ? WHERE id = ?", (current_balance, sub_id))
        
        conn.commit()
        logger.info(f"Atomically deleted transaction {transaction_id} for user {user_id} and recalculated balances.")
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to delete transaction {transaction_id}. Rolled back. Error: {e}")
        raise
    finally:
        conn.close()

# =========================================================================================
# AI & PARSING FUNCTIONS
# =========================================================================================
def voice_to_text(audio_file_path: str) -> str:
    try:
        with open(audio_file_path, "rb") as audio_file:
            return client.audio.transcriptions.create(model="whisper-1", file=audio_file).text
    except Exception as e:
        logger.error(f"Error in Whisper API: {e}")
        return ""

def text_to_transactions(text: str) -> list:
    prompt = f"""
    Analyze the financial text below. Extract all individual transactions mentioned. The text is: "{text}"
    Your task is to identify and list all transactions. For each transaction, provide:
    1. "type": Must be "income" or "expense".
    2. "amount": A number, without any currency symbols or text. Convert 'k' to thousands (e.g., '10k' is 10000).
    3. "category": A suitable category from this list: Food, Transport, Health, Education, Salary, Gift, Debt, Debt Repayment, Entertainment, Shopping, Bills, Other.
    4. "description": The specific phrase related to this single transaction.
    5. "currency": "USD" if words like 'dollar', 'dollars', 'usd', or '$' are used. Otherwise, default to "UZS".

    *** SPECIAL INSTRUCTIONS FOR DEBT ***
    - If a user LENDS money (e.g., "gave money to", "lent to"), set "category" to "Debt" and "type" to "expense". Extract "debtor_name" and "return_date" (YYYY-MM-DD).
    - If a user RECEIVES money BACK (e.g., "got back from", "returned the money"), set "category" to "Debt Repayment" and "type" to "income".

    Respond ONLY with a valid JSON object containing a single key "transactions".
    
    Example (Lending): {{"transactions": [{{"type": "expense", "amount": 10000, "category": "Debt", "description": "i gave aziz 10k he should return it on september 25", "currency": "UZS", "debtor_name": "Aziz", "return_date": "2025-09-25"}}]}}
    Example (Repayment): {{"transactions": [{{"type": "income", "amount": 10000, "category": "Debt Repayment", "description": "aziz gave me back 10k", "currency": "UZS"}}]}}
    """
    try:
        response = client.chat.completions.create(
            model="gpt-4-turbo", messages=[{"role": "system", "content": "You are an expert financial assistant."}, {"role": "user", "content": prompt}], response_format={"type": "json_object"})
        return json.loads(response.choices[0].message.content).get("transactions", [])
    except Exception as e:
        logger.error(f"Error in LLM API or JSON parsing for transactions: {e}")
        return []

def fallback_parser(text: str) -> list:
    transactions = []
    pattern = re.compile(r"(spent|paid|gave|got|received)\s+([\d,.]+k?)\s*(usd|dollar|dollars)?\s*(?:on|for)?\s*(.+)", re.IGNORECASE)
    match = pattern.search(text)
    if match:
        action, amount_str, currency_str, description = match.groups()
        amount = float(amount_str.lower().replace('k', '000').replace(',', ''))
        trans_type = 'expense' if action.lower() in ['spent', 'paid', 'gave'] else 'income'
        currency = 'USD' if currency_str and currency_str.lower() in ['usd', 'dollar', 'dollars'] else 'UZS'
        transactions.append({"type": trans_type, "amount": amount, "category": "Other", "description": description.strip(), "currency": currency})
        logger.info(f"Fallback parser successfully extracted a transaction: {transactions[-1]}")
    return transactions

# =========================================================================================
# HELPER & REPORTING FUNCTIONS
# =========================================================================================
def escape_markdown(text: str) -> str:
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join(f'\\{char}' if char in escape_chars else char for char in text)

def create_main_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [["📊 Balance", "📜 History"], ["📈 Summary", "💬 Feedback"]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def create_registration_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [[KeyboardButton("Share Phone Number", request_contact=True)]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

def parse_timeframe_to_dates(timeframe_str: str, year: int = None, month: int = None) -> (datetime, datetime):
    today = datetime.now(timezone.utc)
    if not year: year = today.year
    if timeframe_str == "today":
        start_date = today.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = today.replace(hour=23, minute=59, second=59, microsecond=999999)
    elif timeframe_str == "this_month":
        start_date = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_date = (start_date + relativedelta(months=1)) - timedelta(seconds=1)
    elif timeframe_str == "this_year":
        start_date = today.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end_date = today.replace(month=12, day=31, hour=23, minute=59, second=59, microsecond=999999)
    elif timeframe_str == "month" and month is not None:
        start_date = datetime(year, month, 1, tzinfo=timezone.utc)
        end_date = (start_date + relativedelta(months=1)) - timedelta(seconds=1)
    else:
        start_date = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_date = (start_date + relativedelta(months=1)) - timedelta(seconds=1)
    return start_date, end_date

def generate_pie_chart(data: list, title: str, currency: str) -> str:
    if not data: return ""
    labels = [item[0] for item in data]
    sizes = [item[2] for item in data]
    total = sum(sizes)
    top_n = 6
    if len(labels) > top_n:
        consolidated_labels = labels[:top_n - 1]
        consolidated_sizes = sizes[:top_n - 1]
        other_total = sum(sizes[top_n - 1:])
        consolidated_labels.append("Other")
        consolidated_sizes.append(other_total)
    else:
        consolidated_labels, consolidated_sizes = labels, sizes
    plt.style.use('seaborn-v0_8-pastel')
    fig, ax = plt.subplots(figsize=(8, 6))
    wedges, _, autotexts = ax.pie(consolidated_sizes, autopct=lambda p: '{:,.0f}\n({:.1f}%)'.format(p * sum(consolidated_sizes) / 100.0, p), startangle=90, textprops=dict(color="black"))
    ax.axis('equal')
    ax.legend(wedges, consolidated_labels, title="Categories", loc="center left", bbox_to_anchor=(1, 0, 0.5, 1))
    plt.setp(autotexts, size=8, weight="bold")
    ax.set_title(f"{title}\nTotal: {total:,.2f} {currency}", size=14, weight="bold")
    chart_filename = f"chart_{currency}_{uuid.uuid4()}.png"
    plt.savefig(chart_filename, bbox_inches="tight")
    plt.close()
    return chart_filename

## --- CORRECTED: Restored the missing 'end_index' calculation ---
def generate_history_page(transactions: list, page: int) -> (str, InlineKeyboardMarkup):
    items_per_page = 5
    start_index = page * items_per_page
    end_index = start_index + items_per_page # <-- THIS LINE WAS MISSING
    paginated_transactions = transactions[start_index:end_index]
    
    total_pages = (len(transactions) + items_per_page - 1) // items_per_page if transactions else 0
    message_parts = [f"*📜 Your Transaction History \\(Page {page + 1}/{total_pages}\\)*"]
    separator = escape_markdown("\n--------------------------\n")
    keyboard_buttons = []

    for trans in paginated_transactions:
        # Using .get() for robustness with older database entries
        date_str = "Unknown Date"
        if trans.get('date'):
            try:
                date_obj = datetime.fromisoformat(trans['date'])
                date_str = date_obj.strftime('%d-%b-%Y')
            except (ValueError, TypeError):
                # Fallback for old date formats
                try:
                    date_obj = datetime.strptime(trans['date'], '%Y-%m-%d %H:%M:%S')
                    date_str = date_obj.strftime('%d-%b-%Y')
                except (ValueError, TypeError):
                    pass

        icon = "🟢" if trans.get('type', '').lower() == 'income' else "🔴"
        amount_str = f"{float(trans.get('amount', 0)):,.2f} {trans.get('currency', 'UZS')}"
        
        entry = [
            separator,
            f"🗓️ *Date*: {escape_markdown(date_str)}",
            f"{icon} *Amount*: {escape_markdown(amount_str)}",
            f"🏷️ *Category*: {escape_markdown(trans.get('category', 'N/A'))}",
            f"📝 *Comment*: {escape_markdown(trans.get('description', ''))}"
        ]

        if trans.get('category') == 'Debt' and trans.get('debtor_name'):
            debt_status = trans.get('debt_status', 'open').title()
            status_icon = "✅" if debt_status == 'Paid' else "⏳"
            debt_info = f"👤 *Lent to*: {escape_markdown(trans.get('debtor_name', 'Unknown'))}\n*Status*: {status_icon} {escape_markdown(debt_status)}"
            if trans.get('return_date'):
                debt_info += f"\n🗓️ *Returns*: {escape_markdown(trans.get('return_date'))}"
            entry.append(debt_info)

            if debt_status == 'Open':
                keyboard_buttons.append([InlineKeyboardButton(f"Mark Debt #{trans['id']} as Paid", callback_data=f"debt_paid_{trans['id']}")])
        
        message_parts.extend(entry)
    
    pagination_row = []
    if page > 0:
        pagination_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"history_{page - 1}"))
    if end_index < len(transactions):
        pagination_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"history_{page + 1}"))
    
    if pagination_row:
        keyboard_buttons.append(pagination_row)
        
    final_text = '\n'.join(message_parts) if paginated_transactions else "You have no transactions to display."
    return final_text, InlineKeyboardMarkup(keyboard_buttons)
# =========================================================================================
# TELEGRAM BOT HANDLERS
# =========================================================================================
async def registration_gatekeeper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user and is_user_registered(user.id): return True
    prompt_message = "Please register to use the bot. Tap the button below to share your phone number."
    reply_markup = create_registration_keyboard()
    if update.message: await update.message.reply_text(prompt_message, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.reply_text(prompt_message, reply_markup=reply_markup)
        await update.callback_query.answer()
    return False

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_user_registered(user.id):
        await update.message.reply_text(f"Welcome back, {user.first_name}!", reply_markup=create_main_keyboard())
    else:
        welcome_message = f"Hello, {user.first_name}! Welcome.\nPlease share your phone number to get started."
        await update.message.reply_text(welcome_message, reply_markup=create_registration_keyboard())

async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact, user = update.message.contact, update.effective_user
    if contact.user_id != user.id:
        await update.message.reply_text("Please use the button to share your own contact for security.")
        return
    register_user(user.id, contact.phone_number, user.first_name)
    success_message = "✅ Thank you for registering!\nYou can now use all features."
    await update.message.reply_text(success_message, reply_markup=create_main_keyboard())

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await registration_gatekeeper(update, context): return
    user_id = update.effective_user.id
    balance_uzs, balance_usd = get_last_balance(user_id, 'UZS'), get_last_balance(user_id, 'USD')
    message_text = (f"📊 *Your Current Balances:*\n\n"
                    f"🇺🇿 *UZS Balance:* {escape_markdown(f'{balance_uzs:,.2f}')}\n"
                    f"🇺🇸 *USD Balance:* {escape_markdown(f'{balance_usd:,.2f}')}")
    await update.message.reply_text(message_text, parse_mode='MarkdownV2')

async def transactions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await registration_gatekeeper(update, context): return
    all_transactions = get_all_transactions(update.effective_user.id)
    if not all_transactions:
        await update.message.reply_text("You have no transactions recorded yet.")
        return
    text, reply_markup = generate_history_page(all_transactions, 0)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='MarkdownV2')

async def feedback_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await registration_gatekeeper(update, context): return
    feedback_text = (
        "Got a suggestion or feedback? I'd love to hear it!\n\n"
        "Please reach out to the developer directly on Telegram: @alien457"
    )
    await update.message.reply_text(feedback_text)


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await registration_gatekeeper(update, context): return
    user_id = update.effective_user.id
    await update.message.reply_text("Generating your transaction history as a CSV file...")
    try:
        transactions = get_all_transactions(user_id)
        if not transactions:
            await update.message.reply_text("You have no transactions to export.")
            return
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(transactions[0].keys())
        for trans in transactions: writer.writerow(trans.values())
        output.seek(0)
        await update.message.reply_document(document=output, filename=f"transactions_{user_id}_{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv")
    except Exception as e:
        logger.error(f"Failed to generate export for user {user_id}: {e}")
        await update.message.reply_text("Sorry, an error occurred while creating your export file.")

async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await registration_gatekeeper(update, context): return
    keyboard = [
        [InlineKeyboardButton("Today", callback_data="summary_generate_today")],
        [InlineKeyboardButton("This Month", callback_data="summary_generate_this_month")],
        [InlineKeyboardButton("Choose Period...", callback_data="summary_show_periods")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text('Please choose a period for the expense summary:', reply_markup=reply_markup)

async def generate_and_send_summary(update: Update, context: ContextTypes.DEFAULT_TYPE, start_date: datetime, end_date: datetime, title_period: str):
    query = update.callback_query
    user_id = query.from_user.id
    original_message = query.message
    records = get_transactions_for_period(user_id, start_date, end_date, "expense")

    if not records:
        text_to_send = f"No expenses found for {title_period}."
        await original_message.edit_text(escape_markdown(text_to_send), parse_mode='MarkdownV2')
        return

    text_to_send = f"Crunching the numbers for {title_period}..."
    await original_message.edit_text(escape_markdown(text_to_send), parse_mode='MarkdownV2')

    generated_files = []
    try:
        for currency in ["UZS", "USD"]:
            currency_records = [rec for rec in records if rec[1] == currency]
            if not currency_records: continue
            total_amount = sum(amount for _, _, amount in currency_records)
            icon = "🇺🇿" if currency == "UZS" else "🇺🇸"
            summary_parts = [f"{icon} *Expense Breakdown in {currency} for {escape_markdown(title_period)}*", f"Total: *{escape_markdown(f'{total_amount:,.2f} {currency}')}*"]
            for category, _, amount in currency_records:
                summary_parts.append(f"• {escape_markdown(category)}: {escape_markdown(f'{amount:,.2f}')}")
            caption_text = '\n'.join(summary_parts)
            chart_title = f"Expense Breakdown ({title_period})"
            chart_file = generate_pie_chart(currency_records, chart_title, currency)
            if chart_file:
                generated_files.append(chart_file)
                with open(chart_file, 'rb') as photo:
                    await context.bot.send_photo(chat_id=user_id, photo=photo, caption=caption_text, parse_mode='MarkdownV2')
        await original_message.delete()
    finally:
        for file_path in generated_files:
            if os.path.exists(file_path): os.remove(file_path)
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await registration_gatekeeper(update, context): return
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data.split('_')
    action = data[0]

    if action == "undo":
        try:
            delete_transaction_and_recalculate(user_id, int(data[1]))
            await query.edit_message_text("✅ Transaction undone successfully.", reply_markup=None)
        except Exception:
            await query.edit_message_text("❌ This action could not be completed.", reply_markup=None)

    elif action == "debt" and data[1] == "paid":
        transaction_id = int(data[2])
        conn = sqlite3.connect('finance_tracker.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM transactions WHERE id = ? AND user_id = ? AND is_deleted = 0", (transaction_id, user_id))
        debt_trans_row = cursor.fetchone()
        conn.close()
        if debt_trans_row and dict(debt_trans_row)['debt_status'] == 'open':
            debt_trans = dict(debt_trans_row)
            add_multiple_transactions(user_id, [{"type": "income", "amount": debt_trans['amount'], "category": "Debt Repayment", "description": f"Repayment from {debt_trans['debtor_name']}", "currency": debt_trans['currency']}])
            conn_update = sqlite3.connect('finance_tracker.db')
            cursor_update = conn_update.cursor()
            cursor_update.execute("UPDATE transactions SET debt_status = 'paid' WHERE id = ?", (transaction_id,))
            conn_update.commit()
            conn_update.close()
            await query.edit_message_text(f"✅ Debt #{transaction_id} marked as paid and income logged.", reply_markup=None)
        else:
            await query.edit_message_text("❌ This action could not be completed.", reply_markup=None)
    
    elif action == "summary":
        sub_action = data[1]
        if sub_action == "show" and data[2] == "periods":
            keyboard = []
            months = [datetime(2024, i, 1).strftime('%B') for i in range(1, 13)]
            row = []
            for i, month_name in enumerate(months):
                row.append(InlineKeyboardButton(month_name, callback_data=f"summary_generate_month_{i+1}"))
                if (i + 1) % 3 == 0: keyboard.append(row); row = []
            if row: keyboard.append(row)
            keyboard.append([InlineKeyboardButton("Whole Year", callback_data="summary_generate_this_year")])
            await query.edit_message_text("Please select a specific period:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif sub_action == "generate":
            timeframe_str = "_".join(data[2:])
            if timeframe_str == "today":
                start_date, end_date = parse_timeframe_to_dates("today")
                await generate_and_send_summary(update, context, start_date, end_date, "Today")
            elif timeframe_str == "this_month":
                start_date, end_date = parse_timeframe_to_dates("this_month")
                await generate_and_send_summary(update, context, start_date, end_date, "This Month")
            elif timeframe_str == "this_year":
                start_date, end_date = parse_timeframe_to_dates("this_year")
                await generate_and_send_summary(update, context, start_date, end_date, "This Year")
            elif data[2] == "month":
                month_num = int(data[3])
                start_date, end_date = parse_timeframe_to_dates("month", month=month_num)
                month_name = start_date.strftime('%B %Y')
                await generate_and_send_summary(update, context, start_date, end_date, month_name)

    elif action == "history":
        page = int(data[1])
        all_transactions = get_all_transactions(user_id)
        text, reply_markup = generate_history_page(all_transactions, page)
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode='MarkdownV2')

async def process_natural_language_text(text: str, user_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE):
    transactions = text_to_transactions(text)
    if not transactions:
        logger.warning(f"LLM parsing failed for user {user_id}. Attempting fallback.")
        transactions = fallback_parser(text)
    if not transactions:
        await update.message.reply_text(f"I couldn't understand that. Please try again, for example: 'spent 50k on food' or 'lent 100k to Aziz'.")
        return
    
    new_balances, new_ids = add_multiple_transactions(user_id, transactions)
    
    header = "✅ Transaction successfully logged:" if len(transactions) == 1 else "✅ Successfully Logged Multiple Transactions:"
    reply_parts = [header]
    
    for trans in transactions:
        icon = "🟢" if trans['type'].lower() == 'income' else "🔴"
        amount_f = escape_markdown(f"{float(trans['amount']):,.2f} {trans.get('currency', 'UZS').upper()}")
        category_f = escape_markdown(trans['category'].title())
        comment_f = escape_markdown(trans['description'])
        
        if trans['category'].title() == 'Debt':
            debtor_f = escape_markdown(trans.get('debtor_name') or 'unknown')
            return_f = escape_markdown(trans.get('return_date') or 'unknown')
            transaction_detail = (f"\n{icon} *Type*: Expense\n💰 *Amount*: {amount_f}\n🏷️ *Category*: {category_f}\n"
                                  f"👤 *who*: {debtor_f}\n🗓️ *when return*: {return_f}\n📝 *Comment*: {comment_f}")
        elif trans['category'].title() == 'Debt Repayment':
             transaction_detail = (f"\n{icon} *Type*: Income\n💰 *Amount*: {amount_f}\n🏷️ *Category*: {category_f}\n"
                                   f"📝 *Comment*: {comment_f}")
        else:
            transaction_detail = (f"\n{icon} *Type*: {escape_markdown(trans['type'].title())}\n💰 *Amount*: {amount_f}\n"
                                  f"🏷️ *Category*: {category_f}\n📝 *Comment*: {comment_f}")
        reply_parts.append(transaction_detail)
    
    balance_uzs_f = escape_markdown(f"{new_balances['UZS']:,.2f} UZS")
    balance_usd_f = escape_markdown(f"{new_balances['USD']:,.2f} USD")
    reply_parts.append(f"\n\n📊 *Your new balances are:*\n🇺🇿 {balance_uzs_f}\n🇺🇸 {balance_usd_f}")
    
    reply_markup = None
    if len(new_ids) == 1:
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("Undo (60s)", callback_data=f"undo_{new_ids[0]}")]])

    message = await update.message.reply_text('\n'.join(reply_parts), parse_mode='MarkdownV2', reply_markup=reply_markup)
    
    if reply_markup:
        context.job_queue.run_once(lambda ctx: ctx.bot.edit_message_reply_markup(chat_id=user_id, message_id=message.message_id, reply_markup=None), 60)

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await registration_gatekeeper(update, context): return
    user_id, text = update.effective_user.id, update.message.text
    if text == "📊 Balance": await balance_command(update, context)
    elif text == "📜 History": await transactions_command(update, context)
    elif text == "📈 Summary": await summary_command(update, context)
    elif text == "💬 Feedback": await feedback_command(update, context)
    else: await process_natural_language_text(text, user_id, update, context)
    
async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await registration_gatekeeper(update, context): return
    user_id = update.effective_user.id
    file_path = f"audio_{uuid.uuid4()}.ogg"
    try:
        voice_file = await update.message.voice.get_file()
        await voice_file.download_to_drive(file_path)
        transcribed_text = voice_to_text(file_path)
        if not transcribed_text:
            await update.message.reply_text("Sorry, I couldn't recognize the speech in your voice message.")
            return
        await process_natural_language_text(transcribed_text, user_id, update, context)
    finally:
        if os.path.exists(file_path): os.remove(file_path)

async def check_due_debts(context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect('finance_tracker.db')
    cursor = conn.cursor()
    today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    cursor.execute("SELECT id, user_id, debtor_name, amount, currency FROM transactions WHERE category = 'Debt' AND return_date <= ? AND notified = 0 AND is_deleted = 0 AND debt_status = 'open'", (today_str,))
    due_debts = cursor.fetchall()
    conn.close()
    
    for debt_id, user_id, debtor, amount, currency in due_debts:
        try:
            message = (f"🔔 *Debt Reminder*\n\n"
                       f"Reminder: *{escape_markdown(debtor)}* is due to return the money you lent.\n\n"
                       f"💰 Amount: *{escape_markdown(f'{amount:,.2f} {currency}')}*")
            await context.bot.send_message(chat_id=user_id, text=message, parse_mode='MarkdownV2')
            
            conn_update = sqlite3.connect('finance_tracker.db')
            cursor_update = conn_update.cursor()
            cursor_update.execute("UPDATE transactions SET notified = 1 WHERE id = ?", (debt_id,))
            conn_update.commit()
            conn_update.close()
            logger.info(f"Sent debt reminder for transaction ID {debt_id} to user {user_id}")
        except Exception as e:
            logger.error(f"Failed to send debt reminder for transaction ID {debt_id}: {e}")

# =========================================================================================
# MAIN BOT EXECUTION
# =========================================================================================
def main():
    if "YOUR_TELEGRAM_BOT_TOKEN" in TELEGRAM_BOT_TOKEN or "YOUR_OPENAI_API_KEY" in OPENAI_API_KEY:
        print("!!! ERROR: Please paste your API keys into the script. !!!"); return
    
    init_db()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    job_queue = application.job_queue
    job_queue.run_repeating(check_due_debts, interval=3600, first=10)
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    application.add_handler(CommandHandler("export", export_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("transactions", transactions_command))
    application.add_handler(CommandHandler("feedback", feedback_command))
    application.add_handler(CommandHandler("summary", summary_command))
    application.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, handle_voice_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    print("Bot is running... Press Ctrl-C to stop.")
    application.run_polling()

if __name__ == '__main__':
    main()
