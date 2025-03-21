import requests
import logging
from tenacity import retry, wait_fixed, stop_after_attempt, retry_if_exception_type
from datetime import datetime, timedelta
import time
import threading
from queue import Queue
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from config import USE_PROXY, PROXY_HOST, PROXY_PORT, PROXY_TYPE

# Get module logger
logger = logging.getLogger(__name__)

def create_session():
    """Create a requests session with appropriate proxy configuration"""
    session = requests.Session()
    session.trust_env = False  # Don't use environment variables for proxy
    
    # Configure retries
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    if USE_PROXY:
        session.proxies = {
            'http': f"{PROXY_TYPE}://{PROXY_HOST}:{PROXY_PORT}",
            'https': f"{PROXY_TYPE}://{PROXY_HOST}:{PROXY_PORT}",
        }
        logger.info(f"Train scraper using proxy: {session.proxies}")
    else:
        session.proxies = {}  # Empty proxy dict
        logger.info("Train scraper not using proxy")
    
    return session

class AlibabaTrainScraper:
    def __init__(self, availability_queue, stop_event):
        self.url = "https://ws.alibaba.ir/api/v2/train/available"
        self.headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        }
        self.trains_info = []
        self.availability_queue = availability_queue
        self.stop_event = stop_event
        logger.info("Initialized AlibabaTrainScraper.")

    @retry(wait=wait_fixed(5), stop=stop_after_attempt(5),
           retry=retry_if_exception_type(requests.exceptions.RequestException))
    def get_trains_for_date(self, origin, destination, departure_date):
        """Fetch available trains for a specific date."""
        logger.info(f"Fetching trains for {departure_date}.")
        data = {
            "origin": origin,
            "destination": destination,
            "departureDate": departure_date,
            "passengerCount": 1,
            "ticketType": "Family"
        }

        # Create a new session for each request
        with create_session() as session:
            response = session.post(self.url, json=data, headers=self.headers, verify=True)
            if response.status_code == 200:
                train_data = response.json()
                for train in train_data.get('result', {}).get('departing', []):
                    train_info = {
                        "origin": train.get("originName"),
                        "destination": train.get("destinationName"),
                        "departure": train.get("moveDatetime"),
                        "seat": train.get("seat"),
                        "price": train.get("cost"),
                    }
                    self.trains_info.append(train_info)
                logger.info(f"Found {len(self.trains_info)} trains for {departure_date}.")
            else:
                logger.error(f"Failed to fetch trains: {response.status_code} {response.text}")

    def collect_trains(self, origin, destination, start_date, end_date):
        """Continuously fetch trains for multiple dates until availability is found or stopped."""
        logger.info(f"Starting continuous train data collection from {start_date} to {end_date}.")
        cycle_number = 0
        while not self.stop_event.is_set():
            cycle_number += 1
            self.trains_info.clear()  # Clear previous train info
            current_date = datetime.strptime(start_date, "%Y-%m-%d")
            end_date_obj = datetime.strptime(end_date, "%Y-%m-%d")

            while current_date <= end_date_obj:
                if self.stop_event.is_set():
                    logger.info("Stopping train data collection as requested.")
                    return
                self.get_trains_for_date(origin, destination, current_date.strftime("%Y-%m-%d"))
                if self.trains_info:
                    logger.info("Train availability found. Updating queue.")
                    self.availability_queue.put(self.trains_info.copy())
                current_date += timedelta(days=1)

            logger.info(f"Completed cycle {cycle_number} of train data collection. Restarting.")
