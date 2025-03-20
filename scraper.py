import requests
import logging
from tenacity import retry, wait_fixed, stop_after_attempt, retry_if_exception_type
from datetime import datetime, timedelta

class AlibabaTrainScraper:
    def __init__(self):
        self.url = "https://ws.alibaba.ir/api/v2/train/available"
        self.headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        }
        self.trains_info = []
        logging.basicConfig(level=logging.INFO)

    @retry(wait=wait_fixed(5), stop=stop_after_attempt(5),
           retry=retry_if_exception_type(requests.exceptions.RequestException))
    def get_trains_for_date(self, origin, destination, departure_date):
        """Fetch available trains for a specific date."""
        logging.info(f"Fetching trains for {departure_date}.")
        data = {
            "origin": origin,
            "destination": destination,
            "departureDate": departure_date,
            "passengerCount": 1,
            "ticketType": "Family"
        }

        response = requests.post(self.url, json=data, headers=self.headers)
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
            logging.info(f"Found {len(self.trains_info)} trains for {departure_date}.")
        else:
            logging.error(f"Failed to fetch trains: {response.status_code} {response.text}")

    def collect_trains(self, origin, destination, start_date, end_date):
        """Fetch trains for multiple dates."""
        logging.info(f"Collecting train data from {start_date} to {end_date}.")
        current_date = datetime.strptime(start_date, "%Y-%m-%d")
        end_date_obj = datetime.strptime(end_date, "%Y-%m-%d")

        while current_date <= end_date_obj:
            self.get_trains_for_date(origin, destination, current_date.strftime("%Y-%m-%d"))
            current_date += timedelta(days=1)
