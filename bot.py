import time

import requests
import logging

from requests import RequestException

from config import TELEGRAM_BOT_TOKEN
from scraper import AlibabaTrainScraper
from utils import get_search_dates

class TelegramBot:
    def __init__(self, token):
        self.token = token
        self.api_url = f"https://api.telegram.org/bot{self.token}"
        logging.basicConfig(level=logging.INFO)

    def get_updates_with_retry(self, offset=None, retries=3, delay=5):
        """Fetch updates from Telegram with retry logic."""
        url = f"{self.api_url}/getUpdates"
        params = {"offset": offset, "timeout": 100}

        for _ in range(retries):
            try:
                response = requests.get(url, params=params, timeout=100)
                response.raise_for_status()  # Raise exception for non-200 responses
                return response.json().get("result", [])
            except RequestException as e:
                logging.error(f"Error fetching updates: {e}. Retrying...")
                time.sleep(delay)
        return []

    def send_message(self, chat_id, text, reply_markup=None):
        """Send a message with an optional inline keyboard."""
        url = f"{self.api_url}/sendMessage"
        data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            data["reply_markup"] = reply_markup

        try:
            response = requests.post(url, json=data)
            response.raise_for_status()  # Raise exception for non-200 responses
            logging.info(f"Message sent to chat_id {chat_id}.")
        except requests.exceptions.RequestException as e:
            logging.error(f"Error sending message to {chat_id}: {e}")

    def build_menu(self):
        """Build a menu for the bot."""
        return {
            "inline_keyboard": [
                [{"text": "Check Trains", "callback_data": "check_trains"}],
                [{"text": "Help", "callback_data": "help"}],
            ]
        }

    def handle_callback_query(self, callback_query):
        """Handle menu option selection."""
        chat_id = callback_query["message"]["chat"]["id"]
        data = callback_query["data"]

        if data == "check_trains":
            self.send_message(chat_id, "Checking trains... Please wait!")
            scraper = AlibabaTrainScraper()
            search_dates = get_search_dates()
            start_date = search_dates[0]
            end_date = search_dates[-1]
            scraper.collect_trains("THR", "SYZ", start_date, end_date)

            if scraper.trains_info:
                for train in scraper.trains_info:
                    message = (
                        f"Train available!\n"
                        f"From: {train['origin']}\n"
                        f"To: {train['destination']}\n"
                        f"Departure: {train['departure']}\n"
                        f"Seats: {train['seat']}\n"
                        f"Price: {train['price']} IRR"
                    )
                    self.send_message(chat_id, message)
            else:
                self.send_message(chat_id, "No trains available.")
        elif data == "help":
            self.send_message(
                chat_id,
                "Welcome to the Train Notifier Bot!\n\n"
                "Choose an option from the menu:\n"
                "- <b>Check Trains:</b> Find available trains.\n"
                "- <b>Help:</b> View instructions."
            )

    def process_messages(self):
        """Process incoming messages and callbacks."""
        offset = None
        while True:
            updates = self.get_updates_with_retry(offset)
            for update in updates:
                offset = update["update_id"] + 1

                if "message" in update:
                    chat_id = update["message"]["chat"]["id"]
                    text = update["message"].get("text", "").strip().lower()
                    if text == "/start":
                        self.send_message(
                            chat_id,
                            "Welcome! Use the menu below to interact with the bot:",
                            reply_markup={"inline_keyboard": self.build_menu()["inline_keyboard"]}
                        )

                elif "callback_query" in update:
                    callback_query = update["callback_query"]
                    self.handle_callback_query(callback_query)

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    bot = TelegramBot(TELEGRAM_BOT_TOKEN)
    logging.info("Bot is running...")
    bot.process_messages()
