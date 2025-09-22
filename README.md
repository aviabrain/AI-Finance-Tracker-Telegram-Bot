# AI Finance Tracker Telegram Bot

This is an open-source Telegram bot that helps you track your personal finances using natural language. You can record expenses and income, track debts, and view financial summaries, all through simple voice messages or text.

## Features

*   **Natural Language Processing:** Add transactions by sending messages like "spent 50k on groceries" or "received 100 dollars from John."
*   **Voice-to-Text:** Record your transactions by sending a voice message.
*   **Multi-Currency Support:** Tracks finances in both UZS and USD.
*   **Debt Management:** Keep a record of money you've lent and get reminders when it's due.
*   **Financial Reports:** Get daily, monthly, or yearly summaries of your expenses with visual charts.
*   **Data Export:** Download your entire transaction history as a CSV file.
*   **Secure and Private:** This is a self-hosted bot, meaning you have full control over your data.

## How to Use the Bot (From 0 to Hero)

### Step 1: Create Your Own Telegram Bot

1.  Open Telegram and search for the **@BotFather**.
2.  Start a chat with the BotFather and send the `/newbot` command.
3.  Follow the instructions to choose a name and username for your bot.
4.  The BotFather will give you a unique **API Token**. Copy this token and keep it safe.

### Step 2: Get Your OpenAI API Key

1.  Go to the [OpenAI website](https://openai.com/) and create an account.
2.  Navigate to the API section and create a new **secret key**.
3.  Copy this API key.

### Step 3: Set Up the Bot on Your Server

1.  **Download the Code:**
    *   Clone this repository to your server: `git clone [URL of your GitHub repository]`

2.  **Install Dependencies:**
    *   Navigate into the project directory: `cd [repository name]`
    *   Install the required Python libraries: `pip install python-telegram-bot openai matplotlib`

3.  **Add Your API Keys:**
    *   Open the `main.py` file in a text editor.
    *   Find the following lines and paste your API keys:
        ```python
        TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
        OPENAI_API_KEY = "YOUR_OPENAI_API_KEY"
        ```

4.  **Run the Bot:**
    *   Execute the script from your terminal: `python main.py`

### Step 4: Start Using Your Bot in Telegram

1.  Find your bot in Telegram by searching for its username.
2.  Start a chat and follow the on-screen instructions to register.
3.  You're all set! Start sending messages like:
    *   "I spent 15,000 on a taxi"
    *   "Got my salary of 500 usd"
    *   "Lent 20k to my friend, he will return it next week"

## Contributing

Contributions are welcome! If you have any ideas for improvements or find any bugs, feel free to open an issue or submit a pull request.
