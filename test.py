import requests


def test():
    import requests
    import socket

    print("Current IP Address: ", socket.gethostbyname(socket.gethostname()))
    

    response = requests.get(
        # Remove the hardcoded URL
        'https://api.telegram.org/bot8108600835:AAFcgvv4yd1-rk76qtdAUAgFMynd5vbhUkc/getMe',
        timeout=10
    )

    print(response.json())


if __name__ == "__main__":
    test()