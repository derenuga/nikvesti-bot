"""
Робота з Google Sheets через той самий service account, що використовується
для Google Analytics (GA4_CREDENTIALS).

ВАЖЛИВО: звичайний (не-Workspace) service account НЕ має власного сховища
на Google Drive і не може створювати нові файли — спроба викликає
403 "The caller does not have permission". Тому таблицю потрібно створити
вручну у звичайному Google-акаунті один раз, поділитися нею (Share) з
email service account (права Editor), і вказати її ID в env-змінній
SPREADSHEET_ID. Код нижче лише дописує рядки в уже існуючу таблицю.
"""

import json
import os

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SHEET_HEADERS = ["Дата", "Хто взяв", "Замовник", "Сума", "Тендер"]
SHEET_NAME = "Тендери"

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")

_sheets_service = None


def _get_credentials():
    creds_json = os.environ.get("GA4_CREDENTIALS")
    if not creds_json:
        raise Exception("GA4_CREDENTIALS не знайдено в змінних середовища")
    info = json.loads(creds_json)
    return Credentials.from_service_account_info(info, scopes=SCOPES)


def _get_sheets_service():
    global _sheets_service
    if _sheets_service is None:
        creds = _get_credentials()
        _sheets_service = build("sheets", "v4", credentials=creds)
    return _sheets_service


def ensure_headers():
    """Перевіряє, чи є заголовки в першому рядку, і додає їх, якщо порожньо."""
    if not SPREADSHEET_ID:
        raise Exception("SPREADSHEET_ID не задано в змінних середовища")

    print(f"Sheets: перевіряю заголовки в таблиці {SPREADSHEET_ID}")
    service = _get_sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A1:E1",
    ).execute()

    if not result.get("values"):
        print("Sheets: заголовків немає, додаю")
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{SHEET_NAME}!A1",
            valueInputOption="RAW",
            body={"values": [SHEET_HEADERS]},
        ).execute()
        print("Sheets: заголовки додано")
    else:
        print("Sheets: заголовки вже є")


def append_pickup_row(date_str, taken_by, buyer, amount, tender_id):
    print(f"Sheets: append_pickup_row викликано для tender_id={tender_id}")

    if not SPREADSHEET_ID:
        print("Sheets: ПОМИЛКА — SPREADSHEET_ID не задано в змінних середовища")
        raise Exception("SPREADSHEET_ID не задано в змінних середовища")

    print(f"Sheets: SPREADSHEET_ID={SPREADSHEET_ID}")

    print("Sheets: отримую credentials та сервіс")
    service = _get_sheets_service()
    print("Sheets: сервіс готовий")

    tender_url = f"https://prozorro.gov.ua/tender/{tender_id}"
    tender_cell = f'=HYPERLINK("{tender_url}"; "{tender_id}")'

    row = [date_str, taken_by, buyer, amount, tender_cell]
    print(f"Sheets: формую рядок для запису: {row}")

    print(f"Sheets: відправляю append-запит до {SHEET_NAME}!A:E")
    result = service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A:E",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()
    print(f"Sheets: append виконано, відповідь API: {result.get('updates', result)}")
