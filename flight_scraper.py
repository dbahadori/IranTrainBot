import json
import time
import requests
from datetime import datetime, timedelta, timezone
import logging
from tenacity import retry, wait_fixed, stop_after_attempt, retry_if_exception_type
import requests.exceptions
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
        logger.info(f"Flight scraper using proxy: {session.proxies}")
    else:
        session.proxies = {}  # Empty proxy dict
        logger.info("Flight scraper not using proxy")
    
    return session

class AlibabaFlightScraper:
    def __init__(self, availability_queue, stop_event):
        self.url = "https://ws.alibaba.ir/api/v1/flights/domestic/available"
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
            "Origin": "https://www.alibaba.ir",
            "Referer": "https://www.alibaba.ir/",
            "sec-ch-ua": "\"Microsoft Edge\";v=\"131\", \"Chromium\";v=\"131\", \"Not_A Brand\";v=\"24\"",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Linux\"",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "tracing-device": "N,Edge,131.0.0.0,N,N,Linux",
            "tracing-sessionid": "1740397262071"
        }
        self.flights_info = []
        self.availability_queue = availability_queue
        self.stop_event = stop_event
        logger.info("Initialized AlibabaFlightScraper.")

    @retry(wait=wait_fixed(5), stop=stop_after_attempt(5),
           retry=retry_if_exception_type(requests.exceptions.RequestException))
    def get_flights_for_date(self, origin, destination, departure_date):
        """Fetch available flights for a given date."""

        # Convert departure date to datetime object
        departure_date_obj = datetime.strptime(departure_date, "%Y-%m-%d").date()

        # Get today's date in UTC
        current_utc_date = datetime.now(timezone.utc).date()  # Correct

        # Ensure departure date is today or in the future
        if departure_date_obj < current_utc_date:
            logger.error(f"Invalid departure date: {departure_date}. Must be today or in the future (UTC).")
            return

        logger.info(f"Getting flights for date: {departure_date}")

        data = {
            "origin": origin,
            "destination": destination,
            "departureDate": departure_date,  #  Now correctly formatted and validated
            "returnDate": "",
            "adult": 1,
            "child": 0,
            "infant": 0
        }

        try:
            # Create a new session for each request
            with create_session() as session:
                initial_response = session.post(self.url, json=data, headers=self.headers, verify=True)

                if initial_response.status_code == 200:
                    initial_json = initial_response.json()
                    logger.info(f"Initial request successful for date: {departure_date}")
                else:
                    logger.error(f"Failed to make the initial request for date: {departure_date}")
                    logger.error(f"Status code: {initial_response.status_code}")
                    logger.error(f"Response: {initial_response.text}")
                    return

                request_id = initial_json['result']['requestId']
                new_url = f"https://ws.alibaba.ir/api/v1/flights/domestic/available/{request_id}"
                response = session.get(new_url, headers=self.headers, verify=True)

                if response.status_code == 200:
                    flight_data = response.json()
                    logger.info(f"Second request successful for date: {departure_date}")
                else:
                    logger.error(f"Failed to make the second request for date: {departure_date}")
                    logger.error(f"Status code: {response.status_code}")
                    logger.error(f"Response: {response.text}")
                    return

        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed for date: {departure_date}: {e}")
            raise  # Re-raises the exception to trigger retry logic

        for flight in flight_data['result']['departing']:
            flight_info = {
                "origin": flight.get("origin"),
                "destination": flight.get("destination"),
                "airlineCode": flight.get("airlineCode"),
                "flightNumber": flight.get("flightNumber"),
                "leaveDateTime": flight.get("leaveDateTime"),
                "arrivalDateTime": flight.get("arrivalDateTime"),
                "priceChild": flight.get("priceChild"),
                "priceAdult": flight.get("priceAdult"),
                "priceInfant": flight.get("priceInfant"),
                "class": flight.get("class"),
                "classType": flight.get("classType"),
                "status": flight.get("status"),
                "classTypeName": flight.get("classTypeName"),
                "seat": flight.get("seat")
            }
            self.flights_info.append(flight_info)
        logger.info(f"Collected flights for date: {departure_date}")

    def collect_flights(self, origin, destination, start_date, end_date):
        """Collect flights for multiple days, ensuring date format and validation."""
        logger.info(f"Starting flight collection from {start_date} to {end_date}")
        start_date_obj = datetime.strptime(start_date, '%Y-%m-%d')
        end_date_obj = datetime.strptime(end_date, '%Y-%m-%d')
        current_date_obj = start_date_obj

        while current_date_obj <= end_date_obj and not self.stop_event.is_set():
            departure_date = current_date_obj.strftime('%Y-%m-%d')
            self.get_flights_for_date(origin, destination, departure_date)
            if self.flights_info:
                logger.info("Flight availability found. Updating queue.")
                self.availability_queue.put(self.flights_info.copy())
            current_date_obj += timedelta(days=1)
        logger.info("Completed flight data collection.")
