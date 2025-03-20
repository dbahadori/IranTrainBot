from datetime import datetime, timedelta

def get_search_dates(n_days=7):
    """Generate a list of dates from today up to `n_days` ahead."""
    today = datetime.today()
    return [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n_days)]
