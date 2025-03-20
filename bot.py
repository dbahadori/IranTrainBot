import time
import os
import threading
from queue import Queue, Empty
from datetime import datetime, timedelta
from enum import Enum

import requests
import logging

from requests import RequestException
from fuzzywuzzy import fuzz

from config import TELEGRAM_BOT_TOKEN
from train_scraper import AlibabaTrainScraper
from flight_scraper import AlibabaFlightScraper
from utils import get_search_dates
from i18n_utils import setup_i18n, t, get_language_keyboard, get_language_name

# Set up internationalization
setup_i18n()

# Set the proxy for HTTP and HTTPS using SOCKS5
os.environ['HTTP_PROXY'] = 'socks5h://localhost:1089'
os.environ['HTTPS_PROXY'] = 'socks5h://localhost:1089'

class TrainCity(Enum):
    TEHRAN = {"domainCode": "11320000", "name": "تهران", "value": "THR"}
    ISFAHAN = {"domainCode": "21310000", "name": "اصفهان", "value": "IFN"}
    RASHT = {"domainCode": "54310000", "name": "رشت", "value": "RHD"}
    TABRIZ = {"domainCode": "26310000", "name": "تبریز", "value": "TBZ"}
    YEREVAN = {"domainCode": "156001", "name": "ایروان", "value": "EVN"}
    ISTANBUL = {"domainCode": "117002", "name": "استانبول", "value": "IST"}
    YAZD = {"domainCode": "93310000", "name": "یزد", "value": "AZD"}
    SHIRAZ = {"domainCode": "41310000", "name": "شیراز", "value": "SYZ"}
    AHVAZ = {"domainCode": "36310000", "name": "اهواز", "value": "AWZ"}
    MASHHAD = {"domainCode": "31310000", "name": "مشهد", "value": "MHD"}

class TelegramBot:
    def __init__(self, token):
        self.token = token
        self.api_url = f"https://api.telegram.org/bot{self.token}"
        logging.basicConfig(level=logging.INFO)
        logging.info("Initialized TelegramBot with token.")
        self.availability_queue = Queue()
        self.stop_event = threading.Event()
        self.reset_event = threading.Event()
        self.pending_flights = {}  # Store pending flights for each user
        self.user_flight_index = {}  # Track the current index of results for each user
        self.waiting_for_more = {}  # Track waiting state per user
        self.user_languages = {}  # Store user language preferences

        # Add attributes to store user inputs
        self.origin = "THR"
        self.destination = "SYZ"
        self.start_date = "2025-03-20"
        self.end_date = "2025-03-26"

    def get_user_language(self, chat_id):
        """Get the user's preferred language, defaulting to Farsi."""
        return self.user_languages.get(str(chat_id), 'fa')

    def translate(self, key, chat_id, **kwargs):
        """Translate a key for a specific user."""
        return t(key, locale=self.get_user_language(chat_id), **kwargs)

    def get_updates_with_retry(self, offset=None, retries=3, delay=5):
        logging.info("Fetching updates with retry logic.")
        url = f"{self.api_url}/getUpdates"
        params = {"offset": offset, "timeout": 100}
        proxies = {
            'http': 'socks5h://127.0.0.1:1089',
            'https': 'socks5h://127.0.0.1:1089',
        }

        for _ in range(retries):
            try:
                response = requests.get(url, params=params, proxies=proxies, timeout=100)
                logging.info(f"Response content: {response.content}")
                response.raise_for_status()
                return response.json().get("result", [])
            except RequestException as e:
                logging.error(f"Error fetching updates: {e}. Retrying...")
                time.sleep(delay)
        return []

    def send_message(self, chat_id, text, reply_markup=None):
        logging.info(f"Sending message to chat_id {chat_id}.")
        url = f"{self.api_url}/sendMessage"
        data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            data["reply_markup"] = reply_markup

        try:
            response = requests.post(url, json=data)
            logging.info(f"Response content: {response.content}")
            response.raise_for_status()
            logging.info(f"Message sent to chat_id {chat_id}.")
        except requests.exceptions.RequestException as e:
            logging.error(f"Error sending message to {chat_id}: {e}")

    def send_welcome_message(self, chat_id):
        """Send a welcome message with improved guidance."""
        try:
            # Get translations for all welcome components
            title = self.translate('welcome.title', chat_id)
            description = self.translate('welcome.description', chat_id)
            how_to_use = self.translate('welcome.how_to_use', chat_id)
            steps = self.translate('welcome.steps', chat_id)
            
            # Build welcome message
            welcome_text = f"{title}\n\n{description}\n\n{how_to_use}\n"
            
            # Add steps if available
            if isinstance(steps, list):
                for i, step in enumerate(steps, 1):
                    welcome_text += f"{i}. {step}\n"
            
            # Send message with menu
            self.send_message(chat_id, welcome_text, reply_markup=self.build_menu(chat_id))
        except Exception as e:
            logging.error(f"Error sending welcome message: {str(e)}")
            # Fallback to a simple welcome message
            self.send_message(
                chat_id,
                "Welcome to Train & Flight Notifier!",
                reply_markup=self.build_menu(chat_id)
            )

    def build_menu(self, chat_id):
        """Build a menu with improved UX/UI."""
        logging.info("Building menu for the bot.")
        
        # Get current settings for display
        origin_name = next((city.value["name"] for city in TrainCity if city.value["value"] == self.origin), self.origin)
        dest_name = next((city.value["name"] for city in TrainCity if city.value["value"] == self.destination), self.destination)
        date_range = f"{self.start_date} → {self.end_date}"
        
        # Build the menu with proper translations
        return {
            "inline_keyboard": [
                # Language selection at the top
                [{"text": self.translate("menu.language", chat_id), "callback_data": "change_language"}],
                
                # Travel settings section
                [
                    {"text": self.translate("menu.from", chat_id, city=origin_name), "callback_data": "set_origin"},
                    {"text": self.translate("menu.to", chat_id, city=dest_name), "callback_data": "set_destination"}
                ],
                [{"text": self.translate("menu.dates", chat_id, range=date_range), "callback_data": "set_date_range"}],
                
                # Search actions
                [
                    {"text": self.translate("menu.find_trains", chat_id), "callback_data": "check_trains"},
                    {"text": self.translate("menu.find_flights", chat_id), "callback_data": "check_flights"}
                ],
                
                # Controls and utilities
                [
                    {"text": self.translate("menu.stop", chat_id), "callback_data": "stop"},
                    {"text": self.translate("menu.reset", chat_id), "callback_data": "reset"},
                    {"text": self.translate("menu.help", chat_id), "callback_data": "help"}
                ]
            ]
        }

    def build_city_keyboard(self, action, chat_id):
        logging.info(f"Building city keyboard for {action}.")
        keyboard = []
        row = []
        for city in TrainCity:
            row.append({"text": city.value["name"], "callback_data": f"{action}:{city.value['value']}"})
            if len(row) == 3:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        return {"inline_keyboard": keyboard}

    def build_interval_keyboard(self, chat_id):
        logging.info("Building interval keyboard.")
        intervals = [5, 7, 10, 14]
        keyboard = [[{"text": f"{days} {self.translate('dates.days', chat_id)}", "callback_data": f"interval:{days}"}] for days in intervals]
        return {"inline_keyboard": keyboard}

    def handle_callback_query(self, callback_query):
        logging.info("Handling callback query.")
        chat_id = callback_query["message"]["chat"]["id"]
        data = callback_query["data"]

        if data == "change_language":
            self.send_message(
                chat_id,
                self.translate("menu.select_language", chat_id),
                reply_markup=get_language_keyboard()
            )
        elif data.startswith("lang:"):
            new_lang = data.split(":")[1]
            self.user_languages[str(chat_id)] = new_lang
            self.send_message(
                chat_id,
                self.translate("menu.language_changed", chat_id, language=get_language_name(new_lang)),
                reply_markup=self.build_menu(chat_id)
            )
        elif data == "set_origin":
            self.send_message(
                chat_id,
                self.translate("cities.select_origin", chat_id),
                reply_markup=self.build_city_keyboard("origin", chat_id)
            )
        elif data == "set_destination":
            self.send_message(
                chat_id,
                self.translate("cities.select_destination", chat_id),
                reply_markup=self.build_city_keyboard("destination", chat_id)
            )
        elif data == "set_date_range":
            self.send_message(
                chat_id,
                self.translate("dates.select_interval", chat_id),
                reply_markup=self.build_interval_keyboard(chat_id)
            )
        elif data == "check_trains":
            origin_name = next((city.value["name"] for city in TrainCity if city.value["value"] == self.origin), self.origin)
            dest_name = next((city.value["name"] for city in TrainCity if city.value["value"] == self.destination), self.destination)
            
            self.send_message(
                chat_id,
                self.translate("search.searching_trains", chat_id,
                             origin=origin_name,
                             destination=dest_name,
                             start_date=self.start_date,
                             end_date=self.end_date)
            )
            self.stop_event.clear()
            self.reset_event.clear()
            self.start_train_checking(self.origin, self.destination, self.start_date, self.end_date)
            notify_thread = threading.Thread(target=self.notify_user, args=(chat_id,))
            notify_thread.start()
        elif data == "check_flights":
            origin_name = next((city.value["name"] for city in TrainCity if city.value["value"] == self.origin), self.origin)
            dest_name = next((city.value["name"] for city in TrainCity if city.value["value"] == self.destination), self.destination)
            
            self.send_message(
                chat_id,
                self.translate("search.searching_flights", chat_id,
                             origin=origin_name,
                             destination=dest_name,
                             start_date=self.start_date,
                             end_date=self.end_date)
            )
            self.stop_event.clear()
            self.reset_event.clear()
            self.start_flight_checking(self.origin, self.destination, self.start_date, self.end_date)
            notify_thread = threading.Thread(target=self.notify_user, args=(chat_id,))
            notify_thread.start()
        elif data == "stop":
            self.send_message(chat_id, self.translate("search.stopped", chat_id))
            self.stop_event.set()
        elif data == "reset":
            self.send_message(
                chat_id,
                self.translate("search.reset_confirm", chat_id),
                reply_markup={"inline_keyboard": [
                    [{"text": self.translate("search.reset_yes", chat_id), "callback_data": "confirm_reset"}],
                    [{"text": self.translate("search.reset_no", chat_id), "callback_data": "cancel_reset"}]
                ]}
            )
        elif data == "confirm_reset":
            self.send_message(chat_id, self.translate("search.restarting", chat_id))
            self.reset_event.set()
            self.stop_event.clear()
            self.start_train_checking(self.origin, self.destination, self.start_date, self.end_date)
            notify_thread = threading.Thread(target=self.notify_user, args=(chat_id,))
            notify_thread.start()
        elif data == "cancel_reset":
            self.send_message(
                chat_id,
                self.translate("search.continuing", chat_id),
                reply_markup=self.build_menu(chat_id)
            )
        elif data == "help":
            help_text = (
                f"{self.translate('help.title', chat_id)}\n\n"
                f"{self.translate('help.description', chat_id)}\n\n"
                f"{self.translate('help.main_commands', chat_id)}\n"
                f"• {self.translate('help.commands.from_to', chat_id)}\n"
                f"• {self.translate('help.commands.dates', chat_id)}\n"
                f"• {self.translate('help.commands.find_trains', chat_id)}\n"
                f"• {self.translate('help.commands.find_flights', chat_id)}\n"
                f"• {self.translate('help.commands.stop', chat_id)}\n"
                f"• {self.translate('help.commands.reset', chat_id)}\n\n"
                f"{self.translate('help.text_commands', chat_id)}\n"
            )
            for cmd in self.translate('help.text_command_list', chat_id):
                help_text += f"• {cmd}\n"
            
            self.send_message(chat_id, help_text, reply_markup=self.build_menu(chat_id))
        elif data.startswith("origin:"):
            city_code = data.split(":")[1]
            city_name = next((city.value["name"] for city in TrainCity if city.value["value"] == city_code), city_code)
            self.origin = city_code
            logging.info(f"Origin set to: {self.origin}")
            self.send_message(
                chat_id,
                self.translate("cities.origin_set", chat_id, city=city_name, code=city_code),
                reply_markup=self.build_menu(chat_id)
            )
        elif data.startswith("destination:"):
            city_code = data.split(":")[1]
            city_name = next((city.value["name"] for city in TrainCity if city.value["value"] == city_code), city_code)
            self.destination = city_code
            logging.info(f"Destination set to: {self.destination}")
            self.send_message(
                chat_id,
                self.translate("cities.destination_set", chat_id, city=city_name, code=city_code),
                reply_markup=self.build_menu(chat_id)
            )
        elif data.startswith("interval:"):
            days = int(data.split(":")[1])
            today = datetime.today()
            self.start_date = today.strftime("%Y-%m-%d")
            self.end_date = (today + timedelta(days=days)).strftime("%Y-%m-%d")
            logging.info(f"Date range set from {self.start_date} to {self.end_date} based on {days} days.")
            self.send_message(
                chat_id,
                self.translate("dates.range_set", chat_id,
                             start=self.start_date,
                             end=self.end_date,
                             days=days),
                reply_markup=self.build_menu(chat_id)
            )
        elif data.startswith("more:"):
            user_id = int(data.split(":")[1])
            self.send_next_batch(user_id)

    def start_train_checking(self, origin, destination, start_date, end_date):
        scraper = AlibabaTrainScraper(self.availability_queue, self.stop_event)
        train_thread = threading.Thread(target=scraper.collect_trains, args=(origin, destination, start_date, end_date))
        train_thread.start()

    def start_flight_checking(self, origin, destination, start_date, end_date):
        scraper = AlibabaFlightScraper(self.availability_queue, self.stop_event)
        flight_thread = threading.Thread(target=scraper.collect_flights, args=(origin, destination, start_date, end_date))
        flight_thread.start()

    def notify_user(self, chat_id):
        """Handle incoming flight/train information from the queue"""
        batch_size = 10
        current_items = []
        
        # Initialize waiting state for this user
        self.waiting_for_more[chat_id] = False
        self.pending_flights[chat_id] = []
        self.user_flight_index[chat_id] = 0
        
        while not self.stop_event.is_set():
            try:
                # If we're waiting for user to click More, just sleep and continue
                if self.waiting_for_more.get(chat_id, False):
                    time.sleep(1)
                    continue

                # If we have no items to show, try to get more from queue
                if len(current_items) == 0:
                    try:
                        new_items = self.availability_queue.get(timeout=1)
                        if isinstance(new_items, list):
                            # Filter and sort valid items
                            valid_items = [item for item in new_items if item.get('seat', 0) > 0]
                            valid_items.sort(key=lambda x: (x['leaveDateTime'], x['priceAdult']))
                            current_items.extend(valid_items)
                            logging.info(f"Got {len(valid_items)} new items from queue")
                    except Empty:
                        time.sleep(1)
                        continue

                # If we have items to show and we're not waiting for More
                if len(current_items) > 0 and not self.waiting_for_more.get(chat_id, False):
                    # Take up to batch_size items
                    batch = current_items[:batch_size]
                    
                    # Send the batch
                    for i, item in enumerate(batch, start=len(self.pending_flights[chat_id]) + 1):
                        try:
                            is_train = 'trainNumber' in item
                            
                            # Format date and time for better readability
                            try:
                                departure_dt = datetime.fromisoformat(item['leaveDateTime'].replace('Z', '+00:00'))
                                arrival_dt = datetime.fromisoformat(item['arrivalDateTime'].replace('Z', '+00:00'))
                                departure_date = departure_dt.strftime("%Y-%m-%d")
                                departure_time = departure_dt.strftime("%H:%M")
                                arrival_time = arrival_dt.strftime("%H:%M")
                            except ValueError:
                                departure_date = item['leaveDateTime'].split('T')[0]
                                departure_time = item['leaveDateTime'].split('T')[1][:5]
                                arrival_time = item['arrivalDateTime'].split('T')[1][:5]
                            
                            # Format price to be more readable
                            try:
                                price = f"{int(item['priceAdult']):,}"
                            except (ValueError, TypeError):
                                price = item['priceAdult']
                            
                            # Get city names instead of codes
                            origin_name = next((city.value["name"] for city in TrainCity if city.value["value"] == item['origin']), item['origin'])
                            dest_name = next((city.value["name"] for city in TrainCity if city.value["value"] == item['destination']), item['destination'])
                            
                            if is_train:
                                message = self.translate('results.train.title', chat_id, number=i) + "\n\n"
                                message += self.translate('results.train.route', chat_id, origin=origin_name, destination=dest_name) + "\n"
                                message += self.translate('results.train.date', chat_id, date=departure_date) + "\n"
                                message += self.translate('results.train.time', chat_id, departure=departure_time, arrival=arrival_time) + "\n"
                                message += self.translate('results.train.train_info', chat_id, type=item.get('trainType', 'Standard'), number=item['trainNumber']) + "\n"
                                message += self.translate('results.train.seats', chat_id, count=item['seat']) + "\n"
                                message += self.translate('results.train.price', chat_id, amount=price)
                            else:
                                message = self.translate('results.flight.title', chat_id, number=i) + "\n\n"
                                message += self.translate('results.flight.route', chat_id, origin=origin_name, destination=dest_name) + "\n"
                                message += self.translate('results.flight.date', chat_id, date=departure_date) + "\n"
                                message += self.translate('results.flight.time', chat_id, departure=departure_time, arrival=arrival_time) + "\n"
                                message += self.translate('results.flight.flight_info', chat_id, airline=item['airlineCode'], number=item['flightNumber']) + "\n"
                                message += self.translate('results.flight.seats', chat_id, count=item['seat']) + "\n"
                                message += self.translate('results.flight.price', chat_id, amount=price)
                            
                            self.send_message(chat_id, message)
                        except Exception as e:
                            logging.error(f"Error sending message for item {i}: {str(e)}", exc_info=True)
                            continue

                    # Add sent items to pending_flights and remove from current_items
                    self.pending_flights[chat_id].extend(batch)
                    current_items = current_items[len(batch):]

                    # If we have more items to show (either in current_items or potentially in queue)
                    if len(current_items) > 0 or not self.stop_event.is_set():
                        try:
                            total_shown = len(self.pending_flights[chat_id])
                            self.send_message(
                                chat_id,
                                self.translate("search.showing_results", chat_id,
                                             start=total_shown - len(batch) + 1,
                                             end=total_shown),
                                reply_markup={"inline_keyboard": [
                                    [{"text": self.translate("menu.more", chat_id), "callback_data": f"more:{chat_id}"}],
                                    [{"text": self.translate("menu.new_search", chat_id), "callback_data": "reset"}]
                                ]}
                            )
                            self.waiting_for_more[chat_id] = True
                            logging.info(f"Waiting for more click from user {chat_id}")
                        except Exception as e:
                            logging.error(f"Error sending 'More' button: {str(e)}", exc_info=True)

            except Exception as e:
                logging.error(f"Error in notify_user: {str(e)}", exc_info=True)
                time.sleep(1)

        logging.info(f"Notify user thread stopped for chat_id: {chat_id}")

    def send_next_batch(self, chat_id):
        """Handle user request for next batch of items"""
        logging.info(f"Processing next batch request from user {chat_id}")
        
        # Reset the waiting flag when user clicks More
        self.waiting_for_more[chat_id] = False
        logging.info(f"Reset waiting flag for user {chat_id}")

    def process_messages(self):
        logging.info("Processing incoming messages and callbacks.")
        offset = None
        while True:
            updates = self.get_updates_with_retry(offset)
            for update in updates:
                logging.info(f"Received update: {update}")
                offset = update["update_id"] + 1

                if "message" in update:
                    chat_id = update["message"]["chat"]["id"]
                    # Set default language to Farsi for new users
                    if str(chat_id) not in self.user_languages:
                        self.user_languages[str(chat_id)] = 'fa'
                    
                    text = update["message"].get("text", "").strip().lower()
                    logging.info(f"Processing text: {text}")
                    if text.startswith("origin:"):
                        city_input = text.split(":")[1].strip().upper()
                        logging.info(f"User input for origin: {city_input}")
                        # Use fuzzy matching to find the best match
                        best_match = None
                        highest_ratio = 0
                        for city in TrainCity:
                            # Compare with both city code and name
                            ratio_name = fuzz.ratio(city_input, city.value["name"].upper())
                            ratio_code = fuzz.ratio(city_input, city.value["value"].upper())
                            if ratio_name > highest_ratio:
                                highest_ratio = ratio_name
                                best_match = city
                            if ratio_code > highest_ratio:
                                highest_ratio = ratio_code
                                best_match = city
                        if best_match and highest_ratio > 80:  # Use a threshold of 80 for similarity
                            self.origin = best_match.value["value"]
                            logging.info(f"Origin set to: {self.origin}")
                            self.send_message(
                                chat_id,
                                self.translate("cities.origin_set", chat_id, city=best_match.value["name"], code=self.origin),
                                reply_markup=self.build_menu(chat_id)
                            )
                        else:
                            self.send_message(
                                chat_id,
                                self.translate("cities.invalid_city", chat_id),
                                reply_markup=self.build_menu(chat_id)
                            )
                    elif text.startswith("destination:"):
                        city_input = text.split(":")[1].strip().upper()
                        logging.info(f"User input for destination: {city_input}")
                        # Use fuzzy matching to find the best match
                        best_match = None
                        highest_ratio = 0
                        for city in TrainCity:
                            # Compare with both city code and name
                            ratio_name = fuzz.ratio(city_input, city.value["name"].upper())
                            ratio_code = fuzz.ratio(city_input, city.value["value"].upper())
                            if ratio_name > highest_ratio:
                                highest_ratio = ratio_name
                                best_match = city
                            if ratio_code > highest_ratio:
                                highest_ratio = ratio_code
                                best_match = city
                        if best_match and highest_ratio > 80:  # Use a threshold of 80 for similarity
                            self.destination = best_match.value["value"]
                            logging.info(f"Destination set to: {self.destination}")
                            self.send_message(
                                chat_id,
                                self.translate("cities.destination_set", chat_id, city=best_match.value["name"], code=self.destination),
                                reply_markup=self.build_menu(chat_id)
                            )
                        else:
                            self.send_message(
                                chat_id,
                                self.translate("cities.invalid_city", chat_id),
                                reply_markup=self.build_menu(chat_id)
                            )
                    elif text.startswith("days:"):
                        try:
                            # Convert Persian numerals to English numerals
                            persian_to_english = str.maketrans('۰۱۲۳۴۵۶۷۸۹', '0123456789')
                            days_str = text.split(":")[1].strip().translate(persian_to_english)
                            logging.info(f"User input for days: {days_str}")
                            days = int(days_str)
                            today = datetime.today()
                            self.start_date = today.strftime("%Y-%m-%d")
                            self.end_date = (today + timedelta(days=days)).strftime("%Y-%m-%d")
                            logging.info(f"Date range set from {self.start_date} to {self.end_date} based on {days} days.")
                            self.send_message(
                                chat_id,
                                self.translate("dates.range_set", chat_id,
                                             start=self.start_date,
                                             end=self.end_date,
                                             days=days),
                                reply_markup=self.build_menu(chat_id)
                            )
                        except ValueError:
                            logging.error("Invalid number of days input.")
                            self.send_message(
                                chat_id,
                                self.translate("dates.invalid_days", chat_id),
                                reply_markup=self.build_menu(chat_id)
                            )
                    elif text == "/start":
                        self.send_welcome_message(chat_id)
                    elif text == "more":
                        if chat_id in self.pending_flights and chat_id in self.user_flight_index:
                            if self.user_flight_index[chat_id] < len(self.pending_flights[chat_id]):
                                self.send_next_batch(chat_id)
                            else:
                                self.send_message(
                                    chat_id,
                                    self.translate("search.no_more_items", chat_id),
                                    reply_markup=self.build_menu(chat_id)
                                )
                        else:
                            self.send_message(
                            chat_id,
                                self.translate("search.no_more_items", chat_id),
                                reply_markup=self.build_menu(chat_id)
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
