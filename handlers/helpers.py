from datetime import datetime
from calendar import monthrange

MONTHS_UK = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "січень": 1, "лютий": 2, "березень": 3, "квітень": 4,
    "травень": 5, "червень": 6, "липень": 7, "серпень": 8,
    "вересень": 9, "жовтень": 10, "листопад": 11, "грудень": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9,
    "oct": 10, "nov": 11, "dec": 12,
}

MONTHS_UA = {
    1: "Січень", 2: "Лютий", 3: "Березень", 4: "Квітень",
    5: "Травень", 6: "Червень", 7: "Липень", 8: "Серпень",
    9: "Вересень", 10: "Жовтень", 11: "Листопад", 12: "Грудень"
}

def parse_month_arg(args):
    if not args:
        return None, None, None
    month_str = args[0].lower()
    month_num = MONTHS_UK.get(month_str)
    if not month_num:
        return None, None, None
    now = datetime.now()
    year = now.year if month_num <= now.month else now.year - 1
    last_day = monthrange(year, month_num)[1]
    start = datetime(year, month_num, 1)
    end = datetime(year, month_num, last_day, 23, 59, 59)
    label = f"{MONTHS_UA[month_num]} {year}"
    return start, end, label

import requests
from bs4 import BeautifulSoup

def get_author_from_url(url):
    try:
        response = requests.get(url, timeout=5)
        if response.status_code != 200:
            return None
        soup = BeautifulSoup(response.text, "html.parser")
        author_tag = soup.find("meta", attrs={"name": "author"})
        if author_tag:
            return author_tag.get("content")
        author_tag = soup.find("meta", property="article:author")
        if author_tag:
            return author_tag.get("content")
        return None
    except:
        return None
