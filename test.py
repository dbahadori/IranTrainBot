import requests


def test():
    response = requests.get('https://api.telegram.org/bot8108600835:AAFcgvv4yd1-rk76qtdAUAgFMynd5vbhUkc/getMe', proxies={"http": None, "https": None})
    print(response.json())

if __name__ == "__main__":
    test()