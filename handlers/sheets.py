"""
Робота з Google Sheets через той самий service account, що використовується
для Google Analytics (GA4_CREDENTIALS).

Таблиця створюється автоматично при першому записі і її ID зберігається
в storage.py (/data/prozorro_state.json), щоб не створювати нову таблицю
при кожному рестарті бота.
"""

import json
import os

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from handlers import storage

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

SHEET_HEADERS = ["Дата", "Хто взяв", "Замовник", "Сума", "Тендер"]

_sheets_service = None
_drive_service = None


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


def _create_spreadsheet():
    service = _get_sheets_service()
    spreadsheet = {
        "properties": {"title": "Тендери Прозорро — МикВісті"},
        "sheets": [{"properties": {"title": "Тендери"}}],
    }
    result = service.spreadsheets().create(
        body=spreadsheet, fields="spreadsheetId"
    ).execute()
    spreadsheet_id = result["spreadsheetId"]

    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range="Тендери!A1",
        valueInputOption="RAW",
        body={"values": [SHEET_HEADERS]},
    ).execute()

    return spreadsheet_id


def _get_or_create_spreadsheet_id():
    spreadsheet_id = storage.get_spreadsheet_id()
    if spreadsheet_id:
        return spreadsheet_id
    spreadsheet_id = _create_spreadsheet()
    storage.set_spreadsheet_id(spreadsheet_id)
    return spreadsheet_id


def append_pickup_row(date_str, taken_by, buyer, amount, tender_id):
    spreadsheet_id = _get_or_create_spreadsheet_id()
    service = _get_sheets_service()

    tender_url = f"https://prozorro.gov.ua/tender/{tender_id}"
    tender_cell = f'=HYPERLINK("{tender_url}"; "{tender_id}")'

    row = [date_str, taken_by, buyer, amount, tender_cell]

    service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range="Тендери!A:E",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()
