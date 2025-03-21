import time
import os
import threading
from queue import Queue, Empty
from datetime import datetime, timedelta
from enum import Enum
import pytz
import requests
import logging
import urllib3

from requests import RequestException
from fuzzywuzzy import fuzz

from config import (
    TELEGRAM_BOT_TOKEN,
    USE_PROXY,
    PROXY_HOST,
    PROXY_PORT,
    PROXY_TYPE,
    DEFAULT_SEARCH_DAYS
)
from train_scraper import AlibabaTrainScraper
from flight_scraper import AlibabaFlightScraper
from utils import get_search_dates
from i18n_utils import setup_i18n, t, get_language_keyboard, get_language_name

urllib3.getproxies = lambda: {}
urllib3.proxy_from_url = lambda url, **kw: None

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(), logging.FileHandler('bot.log')]
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

setup_i18n()

for env_var in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'SOCKS_PROXY', 'socks_proxy', 'NO_PROXY',
                'no_proxy']:
    os.environ.pop(env_var, None)

if not USE_PROXY:
    os.environ['NO_PROXY'] = '*'
else:
    proxy_url = f"{PROXY_TYPE}://{PROXY_HOST}:{PROXY_PORT}"
    os.environ['HTTP_PROXY'] = proxy_url
    os.environ['HTTPS_PROXY'] = proxy_url
    logger.info(f"Using proxy: {proxy_url}")


def create_session():
    session = requests.Session()
    session.trust_env = False
    if USE_PROXY:
        session.proxies = {
            'http': f"{PROXY_TYPE}://{PROXY_HOST}:{PROXY_PORT}",
            'https': f"{PROXY_TYPE}://{PROXY_HOST}:{PROXY_PORT}",
        }
    else:
        session.proxies = {}
    return session


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
        self.availability_queues = {}  # Per-user queues
        self.stop_events = {}  # Per-user stop events
        self.reset_events = {}  # Per-user reset events
        self.pending_flights = {}
        self.user_flight_index = {}
        self.waiting_for_more = {}
        self.user_languages = {}
        self.current_search_type = {}
        self.origin = {}  # Changed to per-user
        self.destination = {}  # Changed to per-user
        self.start_date = {}  # Changed to per-user
        self.end_date = {}  # Changed to per-user
        self.scraper_threads = {}
        self.notify_threads = {}
        self.price_filters = {}  # Filter attributes
        self.time_filters = {}
        self.seat_filters = {}
        logger.info("Bot initialized with token.")
        self.settings_changed = {}  # Track if settings/filters changed per user

    def get_language_keyboard(self):
        return {
            "inline_keyboard": [
                [{"text": "🇮🇷 فارسی", "callback_data": "lang_fa"}],
                [{"text": "🇬🇧 English", "callback_data": "lang_en"}]
            ]
        }

    def get_language_name(self, lang_code):
        language_names = {'fa': 'فارسی', 'en': 'English'}
        return language_names.get(lang_code, lang_code)

    def get_user_language(self, chat_id):
        return self.user_languages.get(str(chat_id), 'fa')

    def translate(self, key, chat_id, **kwargs):
        return t(key, locale=self.get_user_language(chat_id), **kwargs)

    def get_updates_with_retry(self, offset=None, retries=3, delay=5):
        logger.info("Fetching updates with retry logic.")
        url = f"{self.api_url}/getUpdates"
        params = {"offset": offset, "timeout": 100}
        for _ in range(retries):
            try:
                with create_session() as session:
                    response = session.get(url, params=params, timeout=100, verify=True)
                    response.raise_for_status()
                return response.json().get("result", [])
            except RequestException as e:
                logger.error(f"Error fetching updates: {e}. Retrying...")
                time.sleep(delay)
        return []

    def send_message(self, chat_id, text, reply_markup=None):
        logger.info(f"Sending message to chat_id {chat_id}.")
        url = f"{self.api_url}/sendMessage"
        data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            data["reply_markup"] = reply_markup
        try:
            with create_session() as session:
                response = session.post(url, json=data, verify=True)
                response.raise_for_status()
                logger.info(f"Message sent to chat_id {chat_id}.")
        except RequestException as e:
            logger.error(f"Error sending message to {chat_id}: {e}")

    def send_welcome_message(self, chat_id):
        try:
            title = self.translate('welcome.title', chat_id)
            description = self.translate('welcome.description', chat_id)
            how_to_use = self.translate('welcome.how_to_use', chat_id)
            steps = self.translate('welcome.steps', chat_id)
            welcome_text = f"{title}\n\n{description}\n\n{how_to_use}\n"
            if isinstance(steps, list):
                for i, step in enumerate(steps, 1):
                    welcome_text += f"{i}. {step}\n"
            self.send_message(chat_id, welcome_text, reply_markup=self.build_menu(chat_id))
        except Exception as e:
            logger.error(f"Error sending welcome message: {str(e)}")
            self.send_message(chat_id, "Welcome to Train & Flight Notifier!", reply_markup=self.build_menu(chat_id))

    def build_menu(self, chat_id):
        logger.info(f"Building menu for chat_id {chat_id}.")
        origin_name = next(
            (city.value["name"] for city in TrainCity if city.value["value"] == self.origin.get(chat_id, "THR")), "THR")
        dest_name = next(
            (city.value["name"] for city in TrainCity if city.value["value"] == self.destination.get(chat_id, "SYZ")),
            "SYZ")
        arrow = "←" if self.get_user_language(chat_id) == 'fa' else "→"
        tehran_tz = pytz.timezone('Asia/Tehran')
        default_start = self.start_date.get(chat_id, datetime.now(tehran_tz).strftime("%Y-%m-%d"))
        default_end = self.end_date.get(chat_id,
                                        (datetime.now(tehran_tz) + timedelta(days=DEFAULT_SEARCH_DAYS)).strftime(
                                            "%Y-%m-%d"))
        date_range = f"{default_start} {arrow} {default_end}"
        return {
            "inline_keyboard": [
                [{"text": self.translate("menu.language", chat_id), "callback_data": "change_language"}],
                [{"text": "⚙️ " + self.translate("menu.settings", chat_id), "callback_data": "settings"}],
                [
                    {"text": "🏙️ " + self.translate("menu.from", chat_id, city=origin_name),
                     "callback_data": "set_origin"},
                    {"text": "🏙️ " + self.translate("menu.to", chat_id, city=dest_name),
                     "callback_data": "set_destination"}
                ],
                [{"text": "📅 " + self.translate("menu.dates", chat_id, range=date_range),
                  "callback_data": "set_date_range"}],
                [{"text": "🎛️ " + self.translate("filters.title", chat_id), "callback_data": "filters"}],
                # Changed from "menu.filters"
                [{"text": "🔍 " + self.translate("menu.search", chat_id), "callback_data": "search"}],
                [
                    {"text": "🚆 " + self.translate("menu.find_trains", chat_id), "callback_data": "check_trains"},
                    {"text": "✈️ " + self.translate("menu.find_flights", chat_id), "callback_data": "check_flights"}
                ],
                [{"text": "⚡️ " + self.translate("menu.controls", chat_id), "callback_data": "controls"}],
                [
                    {"text": "⏹️ " + self.translate("menu.stop", chat_id), "callback_data": "stop"},
                    {"text": "🔄 " + self.translate("menu.reset", chat_id), "callback_data": "reset"}
                ],
                [{"text": "❓ " + self.translate("menu.help", chat_id), "callback_data": "help"}]
            ]
        }
    def build_filter_menu(self, chat_id):
        logger.info(f"Building filter menu for chat_id {chat_id}")
        price_filter = self.price_filters.get(chat_id, None)
        time_filter = self.time_filters.get(chat_id, None)
        seat_filter = self.seat_filters.get(chat_id, None)

        price_text = self.translate("filters.all", chat_id) if price_filter is None else self.translate(
            "filters.price_value", chat_id, amount=price_filter // 10000000)
        time_text = self.translate("filters.all", chat_id) if time_filter is None else self.translate(
            f"filters.time.{time_filter}", chat_id)
        seat_text = self.translate("filters.all", chat_id) if seat_filter is None else self.translate(
            "filters.seats_value", chat_id, count=seat_filter)

        return {
            "inline_keyboard": [
                [{"text": self.translate("filters.price_label", chat_id, value=price_text),
                  "callback_data": "filter_price"}],
                [{"text": self.translate("filters.time_label", chat_id, value=time_text),
                  "callback_data": "filter_time"}],
                [{"text": self.translate("filters.seats_label", chat_id, value=seat_text),
                  "callback_data": "filter_seats"}],
                [{"text": self.translate("filters.save", chat_id), "callback_data": "save_filters"}]
            ]
        }

    def build_price_filter_menu(self, chat_id):
        return {
            "inline_keyboard": [
                [{"text": self.translate("filters.price_1m", chat_id), "callback_data": "price:1"}],
                [{"text": self.translate("filters.price_2m", chat_id), "callback_data": "price:2"}],
                [{"text": self.translate("filters.price_3m", chat_id), "callback_data": "price:3"}],
                [{"text": self.translate("filters.price_4m", chat_id), "callback_data": "price:4"}],
                [{"text": self.translate("filters.price_5m", chat_id), "callback_data": "price:5"}],
                [{"text": self.translate("filters.all", chat_id), "callback_data": "price:none"}],
                [{"text": self.translate("menu.back", chat_id), "callback_data": "filters"}]
            ]
        }

    def build_time_filter_menu(self, chat_id):
        return {
            "inline_keyboard": [
                [{"text": self.translate("filters.time.morning", chat_id), "callback_data": "time:morning"}],
                [{"text": self.translate("filters.time.afternoon", chat_id), "callback_data": "time:afternoon"}],
                [{"text": self.translate("filters.time.evening", chat_id), "callback_data": "time:evening"}],
                [{"text": self.translate("filters.all", chat_id), "callback_data": "time:none"}],
                [{"text": self.translate("menu.back", chat_id), "callback_data": "filters"}]
            ]
        }

    def build_seat_filter_menu(self, chat_id):
        return {
            "inline_keyboard": [
                [{"text": self.translate("filters.seats_1", chat_id), "callback_data": "seats:1"}],
                [{"text": self.translate("filters.seats_2", chat_id), "callback_data": "seats:2"}],
                [{"text": self.translate("filters.seats_3", chat_id), "callback_data": "seats:3"}],
                [{"text": self.translate("filters.seats_4", chat_id), "callback_data": "seats:4"}],
                [{"text": self.translate("filters.seats_5", chat_id), "callback_data": "seats:5"}],
                [{"text": self.translate("filters.all", chat_id), "callback_data": "seats:none"}],
                [{"text": self.translate("menu.back", chat_id), "callback_data": "filters"}]
            ]
        }

    def build_city_keyboard(self, action, chat_id):
        logger.info(f"Building city keyboard for {action} for chat_id {chat_id}.")
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
        logger.info(f"Building interval keyboard for chat_id {chat_id}.")
        intervals = [5, 7, 10, 14]
        keyboard = [[{"text": f"{days} {self.translate('dates.days', chat_id)}", "callback_data": f"interval:{days}"}]
                    for days in intervals]
        return {"inline_keyboard": keyboard}

    def clear_search_state(self, chat_id):
        logger.info(f"clear_search_state() called for user {chat_id}")
        # Stop existing threads
        if chat_id in self.stop_events:
            self.stop_events[chat_id].set()
        if chat_id in self.scraper_threads and self.scraper_threads[chat_id].is_alive():
            self.scraper_threads[chat_id].join(timeout=5)
            if self.scraper_threads[chat_id].is_alive():
                logger.warning(f"Scraper thread for chat_id {chat_id} did not stop in time")
        if chat_id in self.notify_threads and self.notify_threads[chat_id].is_alive():
            self.notify_threads[chat_id].join(timeout=5)
            if self.notify_threads[chat_id].is_alive():
                logger.warning(f"Notify thread for chat_id {chat_id} did not stop in time")
        # Clear the queue
        if chat_id in self.availability_queues:
            while not self.availability_queues[chat_id].empty():
                try:
                    self.availability_queues[chat_id].get_nowait()
                except Empty:
                    break
        # Reset state
        self.pending_flights[chat_id] = []
        self.user_flight_index[chat_id] = 0
        self.waiting_for_more[chat_id] = False
        self.current_search_type[chat_id] = None
        self.stop_events[chat_id] = threading.Event()  # Reset for new search
        logger.info(f"Cleared search state for user {chat_id}")
        # No send_message here

    def handle_callback_query(self, callback_query):
        logger.info("Handling callback query.")
        chat_id = callback_query["message"]["chat"]["id"]
        data = callback_query["data"]

        if chat_id not in self.stop_events:
            self.stop_events[chat_id] = threading.Event()
            self.availability_queues[chat_id] = Queue()

        if data == "new_search":
            self.clear_search_state(chat_id)
            self.send_message(chat_id, self.translate("search.select_search_type", chat_id), reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "🚆 " + self.translate("menu.find_trains", chat_id), "callback_data": "check_trains"},
                        {"text": "✈️ " + self.translate("menu.find_flights", chat_id), "callback_data": "check_flights"}
                    ],
                    [{"text": "🔙 " + self.translate("menu.back", chat_id), "callback_data": "back"}]
                ]
            })
        elif data == "settings":
            origin_name = next(
                (city.value["name"] for city in TrainCity if city.value["value"] == self.origin.get(chat_id, "THR")),
                "THR")
            dest_name = next((city.value["name"] for city in TrainCity if
                              city.value["value"] == self.destination.get(chat_id, "SYZ")), "SYZ")
            arrow = "←" if self.get_user_language(chat_id) == 'fa' else "→"
            date_range = f"{self.start_date.get(chat_id, datetime.now(pytz.timezone('Asia/Tehran')).strftime('%Y-%m-%d'))} {arrow} {self.end_date.get(chat_id, (datetime.now(pytz.timezone('Asia/Tehran')) + timedelta(days=DEFAULT_SEARCH_DAYS)).strftime('%Y-%m-%d'))}"
            self.send_message(chat_id, self.translate("menu.settings", chat_id), reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "🏙️ " + self.translate("menu.from", chat_id, city=origin_name),
                         "callback_data": "set_origin"},
                        {"text": "🏙️ " + self.translate("menu.to", chat_id, city=dest_name),
                         "callback_data": "set_destination"}
                    ],
                    [{"text": "📅 " + self.translate("menu.dates", chat_id, range=date_range),
                      "callback_data": "set_date_range"}],
                    [{"text": "🔙 " + self.translate("menu.back", chat_id), "callback_data": "back"}]
                ]
            })
        elif data == "search":
            if chat_id in self.current_search_type and self.current_search_type[chat_id] is not None:
                # Proceed with existing search type
                if self.current_search_type[chat_id] == "train":
                    self.clear_search_state(chat_id)
                    self.current_search_type[chat_id] = "train"
                    origin_name = next(
                        (city.value["name"] for city in TrainCity if
                         city.value["value"] == self.origin.get(chat_id, "THR")),
                        "THR")
                    dest_name = next((city.value["name"] for city in TrainCity if
                                      city.value["value"] == self.destination.get(chat_id, "SYZ")), "SYZ")
                    self.send_message(chat_id, self.translate("search.searching_trains", chat_id, origin=origin_name,
                                                              destination=dest_name,
                                                              start_date=self.start_date.get(chat_id),
                                                              end_date=self.end_date.get(chat_id),
                                                              arrow=self.translate('common.arrow', chat_id)))
                    self.start_train_checking(chat_id, self.origin.get(chat_id, "THR"),
                                              self.destination.get(chat_id, "SYZ"),
                                              self.start_date.get(chat_id), self.end_date.get(chat_id))
                    notify_thread = threading.Thread(target=self.notify_user, args=(chat_id,))
                    notify_thread.start()
                    self.notify_threads[chat_id] = notify_thread
                elif self.current_search_type[chat_id] == "flight":
                    self.clear_search_state(chat_id)
                    self.current_search_type[chat_id] = "flight"
                    origin_name = next(
                        (city.value["name"] for city in TrainCity if
                         city.value["value"] == self.origin.get(chat_id, "THR")),
                        "THR")
                    dest_name = next((city.value["name"] for city in TrainCity if
                                      city.value["value"] == self.destination.get(chat_id, "SYZ")), "SYZ")
                    self.send_message(chat_id, self.translate("search.searching_flights", chat_id, origin=origin_name,
                                                              destination=dest_name,
                                                              start_date=self.start_date.get(chat_id),
                                                              end_date=self.end_date.get(chat_id),
                                                              arrow=self.translate('common.arrow', chat_id)))
                    self.start_flight_checking(chat_id, self.origin.get(chat_id, "THR"),
                                               self.destination.get(chat_id, "SYZ"),
                                               self.start_date.get(chat_id), self.end_date.get(chat_id))
                    notify_thread = threading.Thread(target=self.notify_user, args=(chat_id,))
                    notify_thread.start()
                    self.notify_threads[chat_id] = notify_thread
            else:
                # Prompt for search type
                self.send_message(chat_id, self.translate("search.select_search_type", chat_id), reply_markup={
                    "inline_keyboard": [
                        [
                            {"text": "🚆 " + self.translate("menu.find_trains", chat_id),
                             "callback_data": "check_trains"},
                            {"text": "✈️ " + self.translate("menu.find_flights", chat_id),
                             "callback_data": "check_flights"}
                        ],
                        [{"text": "🔙 " + self.translate("menu.back", chat_id), "callback_data": "back"}]
                    ]
                })
        elif data == "controls":
            self.send_message(chat_id, self.translate("menu.controls", chat_id), reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "⏹️ " + self.translate("menu.stop", chat_id), "callback_data": "stop"},
                        {"text": "🔄 " + self.translate("menu.reset", chat_id), "callback_data": "reset"}
                    ],
                    [{"text": "❓ " + self.translate("menu.help", chat_id), "callback_data": "help"}],
                    [{"text": "🔙 " + self.translate("menu.back", chat_id), "callback_data": "back"}]
                ]
            })
        elif data == "filters":
            self.send_message(chat_id, self.translate("filters.title", chat_id),
                              reply_markup=self.build_filter_menu(chat_id))
        elif data == "filter_price":
            self.send_message(chat_id, self.translate("filters.select_price", chat_id),
                              reply_markup=self.build_price_filter_menu(chat_id))
        elif data == "filter_time":
            self.send_message(chat_id, self.translate("filters.select_time", chat_id),
                              reply_markup=self.build_time_filter_menu(chat_id))
        elif data == "filter_seats":
            self.send_message(chat_id, self.translate("filters.select_seats", chat_id),
                              reply_markup=self.build_seat_filter_menu(chat_id))
        elif data.startswith("price:"):
            value = data.split(":")[1]
            # Store price filter in Rials (1M Tomans = 10M Rials)
            self.price_filters[chat_id] = int(value) * 10000000 if value != "none" else None
            price_display = f"{int(value)}" if value != "none" else self.translate("filters.all", chat_id)
            self.send_message(chat_id, self.translate("filters.price_set", chat_id, amount=price_display),
                              reply_markup=self.build_filter_menu(chat_id))
        elif data.startswith("time:"):
            value = data.split(":")[1]
            self.time_filters[chat_id] = value if value != "none" else None
            time_display = self.translate(f"filters.time.{value}", chat_id) if value != "none" else self.translate(
                "filters.all", chat_id)
            self.send_message(chat_id, self.translate("filters.time_set", chat_id, period=time_display),
                              reply_markup=self.build_filter_menu(chat_id))
        elif data.startswith("seats:"):
            value = data.split(":")[1]
            self.seat_filters[chat_id] = int(value) if value != "none" else None
            seats_display = value if value != "none" else self.translate("filters.all", chat_id)
            self.send_message(chat_id, self.translate("filters.seats_set", chat_id, count=seats_display),
                              reply_markup=self.build_filter_menu(chat_id))
        elif data == "save_filters":
            self.settings_changed[chat_id] = True  # Mark settings as changed
            self.send_message(chat_id, self.translate("filters.saved", chat_id),
                              reply_markup=self.build_menu(chat_id))
        elif data == "back":
            self.send_message(chat_id, self.translate("menu.main_menu", chat_id), reply_markup=self.build_menu(chat_id))
        elif data == "set_origin":
            self.send_message(chat_id, self.translate("cities.select_origin", chat_id),
                              reply_markup=self.build_city_keyboard("set_origin", chat_id))
        elif data == "set_destination":
            self.send_message(chat_id, self.translate("cities.select_destination", chat_id),
                              reply_markup=self.build_city_keyboard("set_destination", chat_id))
            self.settings_changed[chat_id] = True  # Mark settings as changed
        elif data == "set_date_range":
            self.send_message(chat_id, self.translate("dates.select_range", chat_id),
                              reply_markup=self.build_interval_keyboard(chat_id))
            self.settings_changed[chat_id] = True  # Mark settings as changed
        elif data.startswith("interval:"):
            try:
                days = int(data.split(":")[1])
                tehran_tz = pytz.timezone('Asia/Tehran')
                today = datetime.now(tehran_tz)
                self.start_date[chat_id] = today.strftime("%Y-%m-%d")
                self.end_date[chat_id] = (today + timedelta(days=days)).strftime("%Y-%m-%d")
                self.send_message(chat_id, self.translate("dates.range_set", chat_id, start=self.start_date[chat_id],
                                                          end=self.end_date[chat_id], days=days),
                                  reply_markup=self.build_menu(chat_id))
            except ValueError:
                self.send_message(chat_id, self.translate("dates.invalid_days", chat_id),
                                  reply_markup=self.build_menu(chat_id))
        elif data.startswith("set_origin:"):
            city_code = data.split(":")[1]
            city = next((city for city in TrainCity if city.value["value"] == city_code), None)
            if city:
                self.origin[chat_id] = city_code
                self.settings_changed[chat_id] = True  # Mark settings as changed
                self.send_message(chat_id,
                                  self.translate("cities.origin_set", chat_id, city=city.value["name"], code=city_code),
                                  reply_markup=self.build_menu(chat_id))
            else:
                self.send_message(chat_id, self.translate("cities.invalid_city", chat_id),
                                  reply_markup=self.build_menu(chat_id))
        elif data.startswith("set_destination:"):
            city_code = data.split(":")[1]
            city = next((city for city in TrainCity if city.value["value"] == city_code), None)
            if city:
                self.destination[chat_id] = city_code
                self.send_message(chat_id, self.translate("cities.destination_set", chat_id, city=city.value["name"],
                                                          code=city_code), reply_markup=self.build_menu(chat_id))
            else:
                self.send_message(chat_id, self.translate("cities.invalid_city", chat_id),
                                  reply_markup=self.build_menu(chat_id))
        elif data == "check_flights":
            self.clear_search_state(chat_id)
            self.current_search_type[chat_id] = "flight"
            self.settings_changed[chat_id] = False  # Reset flag
            origin_name = next(
                (city.value["name"] for city in TrainCity if city.value["value"] == self.origin.get(chat_id, "THR")),
                "THR")
            dest_name = next((city.value["name"] for city in TrainCity if
                              city.value["value"] == self.destination.get(chat_id, "SYZ")), "SYZ")
            # Get filter values
            price_filter = self.price_filters.get(chat_id)
            time_filter = self.time_filters.get(chat_id)
            seat_filter = self.seat_filters.get(chat_id)
            # Format filter displays
            price_display = f"{price_filter // 10000000}M" if price_filter else self.translate("filters.all", chat_id)
            time_display = self.translate(f"filters.time.{time_filter}", chat_id) if time_filter else self.translate(
                "filters.all", chat_id)
            seat_display = str(seat_filter) if seat_filter else self.translate("filters.all", chat_id)
            self.send_message(chat_id, self.translate("search.searching_flights", chat_id,
                                                      origin=origin_name,
                                                      destination=dest_name,
                                                      start_date=self.start_date.get(chat_id),
                                                      end_date=self.end_date.get(chat_id),
                                                      arrow=self.translate('common.arrow', chat_id),
                                                      price=price_display,
                                                      time=time_display,
                                                      seats=seat_display))
            self.start_flight_checking(chat_id, self.origin.get(chat_id, "THR"), self.destination.get(chat_id, "SYZ"),
                                       self.start_date.get(chat_id), self.end_date.get(chat_id))
            notify_thread = threading.Thread(target=self.notify_user, args=(chat_id,))
            notify_thread.start()
            self.notify_threads[chat_id] = notify_thread
        elif data == "check_trains":
            self.clear_search_state(chat_id)
            self.current_search_type[chat_id] = "train"
            self.settings_changed[chat_id] = False  # Reset flag
            origin_name = next(
                (city.value["name"] for city in TrainCity if city.value["value"] == self.origin.get(chat_id, "THR")),
                "THR")
            dest_name = next((city.value["name"] for city in TrainCity if
                              city.value["value"] == self.destination.get(chat_id, "SYZ")), "SYZ")
            # Get filter values
            price_filter = self.price_filters.get(chat_id)
            time_filter = self.time_filters.get(chat_id)
            seat_filter = self.seat_filters.get(chat_id)
            # Format filter displays
            price_display = f"{price_filter // 10000000}M" if price_filter else self.translate("filters.all", chat_id)
            time_display = self.translate(f"filters.time.{time_filter}", chat_id) if time_filter else self.translate(
                "filters.all", chat_id)
            seat_display = str(seat_filter) if seat_filter else self.translate("filters.all", chat_id)
            self.send_message(chat_id, self.translate("search.searching_trains", chat_id,
                                                      origin=origin_name,
                                                      destination=dest_name,
                                                      start_date=self.start_date.get(chat_id),
                                                      end_date=self.end_date.get(chat_id),
                                                      arrow=self.translate('common.arrow', chat_id),
                                                      price=price_display,
                                                      time=time_display,
                                                      seats=seat_display))
            self.start_train_checking(chat_id, self.origin.get(chat_id, "THR"), self.destination.get(chat_id, "SYZ"),
                                      self.start_date.get(chat_id), self.end_date.get(chat_id))
            notify_thread = threading.Thread(target=self.notify_user, args=(chat_id,))
            notify_thread.start()
            self.notify_threads[chat_id] = notify_thread
        elif data == "stop":
            self.clear_search_state(chat_id)
            self.send_message(chat_id, self.translate("search.stopped", chat_id))
        elif data == "reset":
            if not self.current_search_type.get(chat_id):
                self.send_message(chat_id, self.translate("search.select_search_type", chat_id),
                                  reply_markup=self.build_menu(chat_id))
            elif self.settings_changed.get(chat_id, False):
                # Settings changed, only offer restart
                self.send_message(chat_id, self.translate("search.reset_confirm_changed", chat_id), reply_markup={
                    "inline_keyboard": [
                        [{"text": self.translate("search.reset_yes", chat_id), "callback_data": "reset_yes"}],
                        [{"text": self.translate("menu.back", chat_id), "callback_data": "back"}]
                    ]
                })
            else:
                # No settings changed, offer both options
                self.send_message(chat_id, self.translate("search.reset_confirm", chat_id), reply_markup={
                    "inline_keyboard": [
                        [{"text": self.translate("search.reset_yes", chat_id), "callback_data": "reset_yes"}],
                        [{"text": self.translate("search.reset_no", chat_id), "callback_data": "reset_no"}]
                    ]
                })
        elif data == "reset_yes":
            if chat_id in self.current_search_type and self.current_search_type[chat_id] is not None:
                search_type = self.current_search_type[chat_id]
                origin = self.origin.get(chat_id, "THR")
                destination = self.destination.get(chat_id, "SYZ")
                start_date = self.start_date.get(chat_id)
                end_date = self.end_date.get(chat_id)

                # Get filter values
                price_filter = self.price_filters.get(chat_id)
                time_filter = self.time_filters.get(chat_id)
                seat_filter = self.seat_filters.get(chat_id)
                price_display = f"{price_filter // 10000000}M" if price_filter else self.translate("filters.all",
                                                                                                   chat_id)
                time_display = self.translate(f"filters.time.{time_filter}",
                                              chat_id) if time_filter else self.translate("filters.all", chat_id)
                seat_display = str(seat_filter) if seat_filter else self.translate("filters.all", chat_id)

                # Clear the current state
                self.clear_search_state(chat_id)

                # Restore the search type and restart
                self.current_search_type[chat_id] = search_type
                origin_name = next(
                    (city.value["name"] for city in TrainCity if city.value["value"] == origin), "THR")
                dest_name = next(
                    (city.value["name"] for city in TrainCity if city.value["value"] == destination), "SYZ")

                if search_type == "train":
                    self.send_message(chat_id, self.translate("search.restarting", chat_id) + " " +
                                      self.translate("menu.find_trains", chat_id))
                    self.send_message(chat_id, self.translate("search.searching_trains", chat_id,
                                                              origin=origin_name,
                                                              destination=dest_name,
                                                              start_date=start_date,
                                                              end_date=end_date,
                                                              arrow=self.translate('common.arrow', chat_id),
                                                              price=price_display,
                                                              time=time_display,
                                                              seats=seat_display))
                    self.start_train_checking(chat_id, origin, destination, start_date, end_date)
                    notify_thread = threading.Thread(target=self.notify_user, args=(chat_id,))
                    notify_thread.start()
                    self.notify_threads[chat_id] = notify_thread
                elif search_type == "flight":
                    self.send_message(chat_id, self.translate("search.restarting", chat_id) + " " +
                                      self.translate("menu.find_flights", chat_id))
                    self.send_message(chat_id, self.translate("search.searching_flights", chat_id,
                                                              origin=origin_name,
                                                              destination=dest_name,
                                                              start_date=start_date,
                                                              end_date=end_date,
                                                              arrow=self.translate('common.arrow', chat_id),
                                                              price=price_display,
                                                              time=time_display,
                                                              seats=seat_display))
                    self.start_flight_checking(chat_id, origin, destination, start_date, end_date)
                    notify_thread = threading.Thread(target=self.notify_user, args=(chat_id,))
                    notify_thread.start()
                    self.notify_threads[chat_id] = notify_thread
            else:
                self.clear_search_state(chat_id)
                self.send_message(chat_id, self.translate("search.select_search_type", chat_id), reply_markup={
                    "inline_keyboard": [
                        [
                            {"text": "🚆 " + self.translate("menu.find_trains", chat_id),
                             "callback_data": "check_trains"},
                            {"text": "✈️ " + self.translate("menu.find_flights", chat_id),
                             "callback_data": "check_flights"}
                        ],
                        [{"text": "🔙 " + self.translate("menu.back", chat_id), "callback_data": "back"}]
                    ]
                })
        elif data == "reset_no":
            if not self.current_search_type.get(chat_id) or self.availability_queues[chat_id].empty():
                self.send_message(chat_id, self.translate("search.select_search_type", chat_id),
                                  reply_markup=self.build_menu(chat_id))
            else:
                self.send_message(chat_id, self.translate("search.continuing", chat_id))
                self.waiting_for_more[chat_id] = False  # Resume notify_user to process next items
        elif data == "more":
            self.waiting_for_more[chat_id] = False
            logger.info(f"User {chat_id} clicked More")
        elif data == "help":
            self.send_message(chat_id, self.translate("help.title", chat_id))
            self.send_message(chat_id, self.translate("help.description", chat_id))
            self.send_message(chat_id, self.translate("help.main_commands", chat_id))
            for command in ["from_to", "dates", "find_trains", "find_flights", "stop", "reset"]:
                self.send_message(chat_id, self.translate(f"help.commands.{command}", chat_id))
            self.send_message(chat_id, self.translate("help.text_commands", chat_id))
            for command in self.translate("help.text_command_list", chat_id):
                self.send_message(chat_id, command)
        elif data == "change_language":
            self.send_message(chat_id, self.translate("menu.select_language", chat_id),
                              reply_markup=self.get_language_keyboard())
        elif data.startswith("lang_"):
            lang_code = data.split("_")[1]
            self.user_languages[str(chat_id)] = lang_code
            self.send_message(chat_id, self.translate("menu.language_changed", chat_id,
                                                      language=self.get_language_name(lang_code)))
            self.send_message(chat_id, self.translate("menu.main_menu", chat_id), reply_markup=self.build_menu(chat_id))
        else:
            logger.warning(f"Unknown callback data: {data}")

    def start_train_checking(self, chat_id, origin, destination, start_date, end_date):
        scraper = AlibabaTrainScraper(self.availability_queues[chat_id], self.stop_events[chat_id])
        train_thread = threading.Thread(target=scraper.collect_trains, args=(origin, destination, start_date, end_date))
        train_thread.start()
        self.scraper_threads[chat_id] = train_thread

    def start_flight_checking(self, chat_id, origin, destination, start_date, end_date):
        scraper = AlibabaFlightScraper(self.availability_queues[chat_id], self.stop_events[chat_id])
        flight_thread = threading.Thread(target=scraper.collect_flights,
                                         args=(origin, destination, start_date, end_date))
        flight_thread.start()
        self.scraper_threads[chat_id] = flight_thread

    def notify_user(self, chat_id):
        """Handle incoming flight/train information from the queue with filters"""
        batch_size = 10
        current_items = []
        search_type = self.current_search_type.get(chat_id)

        # Initialize waiting state for this user
        self.waiting_for_more[chat_id] = False
        self.pending_flights[chat_id] = []
        self.user_flight_index[chat_id] = 0

        while not self.stop_events[chat_id].is_set():
            try:
                # If we're waiting for user to click More, just sleep and continue
                if self.waiting_for_more.get(chat_id, False):
                    time.sleep(1)
                    continue

                # If we have no items to show, try to get more from queue
                if len(current_items) == 0:
                    try:
                        new_items = self.availability_queues[chat_id].get(timeout=1)
                        if isinstance(new_items, list):
                            # Filter valid items with search type and filters
                            valid_items = []
                            for item in new_items:
                                is_train = 'trainNumber' in item
                                if not ((search_type == "train" and is_train) or
                                        (search_type == "flight" and not is_train)):
                                    continue

                                # Apply filters
                                price = int(item.get('priceAdult', 0))
                                seats = item.get('seat', 0)
                                try:
                                    departure_dt = datetime.fromisoformat(item['leaveDateTime'].replace('Z', '+00:00'))
                                    departure_hour = departure_dt.hour
                                except ValueError:
                                    departure_time = item['leaveDateTime'].split('T')[1][:5]
                                    departure_hour = int(departure_time.split(':')[0])

                                # Price filter
                                price_limit = self.price_filters.get(chat_id)
                                if price_limit and price >= price_limit:
                                    continue

                                # Seat filter
                                seat_min = self.seat_filters.get(chat_id)
                                if seat_min and seats < seat_min:
                                    continue

                                # Time filter
                                time_filter = self.time_filters.get(chat_id)
                                if time_filter:
                                    if time_filter == "morning" and not (6 <= departure_hour < 12):
                                        continue
                                    elif time_filter == "afternoon" and not (12 <= departure_hour < 18):
                                        continue
                                    elif time_filter == "evening" and not (18 <= departure_hour < 24):
                                        continue

                                if seats > 0:
                                    valid_items.append(item)

                            valid_items.sort(key=lambda x: (x['leaveDateTime'], x['priceAdult']))
                            current_items.extend(valid_items)
                            logger.info(f"Got {len(valid_items)} filtered items from queue for chat_id {chat_id}")
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
                            origin_name = next(
                                (city.value["name"] for city in TrainCity if city.value["value"] == item['origin']),
                                item['origin'])
                            dest_name = next((city.value["name"] for city in TrainCity if
                                              city.value["value"] == item['destination']), item['destination'])

                            if is_train:
                                message = self.translate('results.train.title', chat_id, number=i) + "\n\n"
                                message += self.translate('results.train.route', chat_id,
                                                          origin=origin_name,
                                                          destination=dest_name,
                                                          arrow=self.translate('common.arrow', chat_id)) + "\n"
                                message += self.translate('results.train.date', chat_id, date=departure_date) + "\n"
                                message += self.translate('results.train.time', chat_id, departure=departure_time,
                                                          arrival=arrival_time) + "\n"
                                message += self.translate('results.train.train_info', chat_id,
                                                          type=item.get('trainType', 'Standard'),
                                                          number=item['trainNumber']) + "\n"
                                message += self.translate('results.train.seats', chat_id, count=item['seat']) + "\n"
                                message += self.translate('results.train.price', chat_id, amount=price)
                            else:
                                message = self.translate('results.flight.title', chat_id, number=i) + "\n\n"
                                message += self.translate('results.flight.route', chat_id,
                                                          origin=origin_name,
                                                          destination=dest_name,
                                                          arrow=self.translate('common.arrow', chat_id)) + "\n"
                                message += self.translate('results.flight.date', chat_id, date=departure_date) + "\n"
                                message += self.translate('results.flight.time', chat_id, departure=departure_time,
                                                          arrival=arrival_time) + "\n"
                                message += self.translate('results.flight.flight_info', chat_id,
                                                          airline=item['airlineCode'],
                                                          number=item['flightNumber']) + "\n"
                                message += self.translate('results.flight.seats', chat_id, count=item['seat']) + "\n"
                                message += self.translate('results.flight.price', chat_id, amount=price)

                            self.send_message(chat_id, message)
                        except Exception as e:
                            logger.error(f"Error sending message for item {i} for chat_id {chat_id}: {str(e)}",
                                         exc_info=True)
                            continue

                    # Add sent items to pending_flights and remove from current_items
                    self.pending_flights[chat_id].extend(batch)
                    current_items = current_items[len(batch):]

                    # If we have more items to show (either in current_items or potentially in queue)
                    if len(current_items) > 0 or not self.stop_events[chat_id].is_set():
                        try:
                            total_shown = len(self.pending_flights[chat_id])
                            self.send_message(
                                chat_id,
                                self.translate("search.showing_results", chat_id,
                                               start=total_shown - len(batch) + 1,
                                               end=total_shown),
                                reply_markup={"inline_keyboard": [
                                    [{"text": self.translate("menu.more", chat_id), "callback_data": "more"}],
                                    [{"text": self.translate("menu.main_menu", chat_id),
                                      "callback_data": "back"}]
                                ]}
                            )
                            self.waiting_for_more[chat_id] = True
                            logger.info(f"Waiting for more click from user {chat_id}")
                        except Exception as e:
                            logger.error(f"Error sending 'More' button for chat_id {chat_id}: {str(e)}", exc_info=True)

            except Exception as e:
                logger.error(f"Error in notify_user for chat_id {chat_id}: {str(e)}", exc_info=True)
                time.sleep(1)

        logger.info(f"Notify user thread stopped for chat_id: {chat_id}")

    def send_next_batch(self, chat_id):
        logger.info(f"Processing next batch request from user {chat_id}")
        self.waiting_for_more[chat_id] = False
        logger.info(f"Reset waiting flag for user {chat_id}")

    def process_messages(self):
        logger.info("Processing incoming messages and callbacks.")
        offset = None
        while True:
            updates = self.get_updates_with_retry(offset)
            for update in updates:
                logger.info(f"Received update: {update}")
                offset = update["update_id"] + 1

                if "message" in update:
                    chat_id = update["message"]["chat"]["id"]
                    if str(chat_id) not in self.user_languages:
                        self.user_languages[str(chat_id)] = 'fa'
                    if chat_id not in self.stop_events:
                        tehran_tz = pytz.timezone('Asia/Tehran')
                        current_time = datetime.now(tehran_tz)
                        self.origin[chat_id] = "THR"
                        self.destination[chat_id] = "SYZ"
                        self.start_date[chat_id] = current_time.strftime("%Y-%m-%d")
                        self.end_date[chat_id] = (current_time + timedelta(days=DEFAULT_SEARCH_DAYS)).strftime(
                            "%Y-%m-%d")
                        self.stop_events[chat_id] = threading.Event()
                        self.availability_queues[chat_id] = Queue()
                        self.pending_flights[chat_id] = []
                        self.user_flight_index[chat_id] = 0
                        self.waiting_for_more[chat_id] = False

                    text = update["message"].get("text", "").strip().lower()
                    logger.info(f"Processing text: {text} for chat_id {chat_id}")
                    if text.startswith("origin:"):
                        city_input = text.split(":")[1].strip().upper()
                        best_match = None
                        highest_ratio = 0
                        for city in TrainCity:
                            ratio_name = fuzz.ratio(city_input, city.value["name"].upper())
                            ratio_code = fuzz.ratio(city_input, city.value["value"].upper())
                            if ratio_name > highest_ratio:
                                highest_ratio = ratio_name
                                best_match = city
                            if ratio_code > highest_ratio:
                                highest_ratio = ratio_code
                                best_match = city
                        if best_match and highest_ratio > 80:
                            self.origin[chat_id] = best_match.value["value"]
                            logger.info(f"Origin set to: {self.origin[chat_id]} for chat_id {chat_id}")
                            self.send_message(chat_id, self.translate("cities.origin_set", chat_id,
                                                                      city=best_match.value["name"],
                                                                      code=self.origin[chat_id]),
                                              reply_markup=self.build_menu(chat_id))
                        else:
                            self.send_message(chat_id, self.translate("cities.invalid_city", chat_id),
                                              reply_markup=self.build_menu(chat_id))
                    elif text.startswith("destination:"):
                        city_input = text.split(":")[1].strip().upper()
                        best_match = None
                        highest_ratio = 0
                        for city in TrainCity:
                            ratio_name = fuzz.ratio(city_input, city.value["name"].upper())
                            ratio_code = fuzz.ratio(city_input, city.value["value"].upper())
                            if ratio_name > highest_ratio:
                                highest_ratio = ratio_name
                                best_match = city
                            if ratio_code > highest_ratio:
                                highest_ratio = ratio_code
                                best_match = city
                        if best_match and highest_ratio > 80:
                            self.destination[chat_id] = best_match.value["value"]
                            logger.info(f"Destination set to: {self.destination[chat_id]} for chat_id {chat_id}")
                            self.send_message(chat_id, self.translate("cities.destination_set", chat_id,
                                                                      city=best_match.value["name"],
                                                                      code=self.destination[chat_id]),
                                              reply_markup=self.build_menu(chat_id))
                        else:
                            self.send_message(chat_id, self.translate("cities.invalid_city", chat_id),
                                              reply_markup=self.build_menu(chat_id))
                    elif text.startswith("days:"):
                        try:
                            persian_to_english = str.maketrans('۰۱۲۳۴۵۶۷۸۹', '0123456789')
                            days_str = text.split(":")[1].strip().translate(persian_to_english)
                            days = int(days_str)
                            tehran_tz = pytz.timezone('Asia/Tehran')
                            today = datetime.now(tehran_tz)
                            self.start_date[chat_id] = today.strftime("%Y-%m-%d")
                            self.end_date[chat_id] = (today + timedelta(days=days)).strftime("%Y-%m-%d")
                            logger.info(
                                f"Date range set from {self.start_date[chat_id]} to {self.end_date[chat_id]} based on {days} days for chat_id {chat_id}")
                            self.send_message(chat_id,
                                              self.translate("dates.range_set", chat_id, start=self.start_date[chat_id],
                                                             end=self.end_date[chat_id], days=days),
                                              reply_markup=self.build_menu(chat_id))
                        except ValueError:
                            logger.error(f"Invalid number of days input for chat_id {chat_id}")
                            self.send_message(chat_id, self.translate("dates.invalid_days", chat_id),
                                              reply_markup=self.build_menu(chat_id))
                    elif text == "/start":
                        self.send_welcome_message(chat_id)
                    elif text == "more":
                        if chat_id in self.pending_flights and chat_id in self.user_flight_index:
                            if self.user_flight_index[chat_id] < len(self.pending_flights[chat_id]):
                                self.send_next_batch(chat_id)
                            else:
                                self.send_message(chat_id, self.translate("search.no_more_items", chat_id),
                                                  reply_markup=self.build_menu(chat_id))
                        else:
                            self.send_message(chat_id, self.translate("search.no_more_items", chat_id),
                                              reply_markup=self.build_menu(chat_id))
                elif "callback_query" in update:
                    callback_query = update["callback_query"]
                    self.handle_callback_query(callback_query)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    bot = TelegramBot(TELEGRAM_BOT_TOKEN)
    logger.info("Bot is running...")
    bot.process_messages()