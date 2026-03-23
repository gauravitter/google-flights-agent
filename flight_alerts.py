import re
import email
import imaplib
import asyncio
from bs4 import BeautifulSoup
from collections import defaultdict
from loguru import logger
from telegram import Bot
import os

# --- CONFIG ---
EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")
IMAP_SERVER = "imap.gmail.com"

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Logging setup
logger.add("flight_agent.log", rotation="1 day", retention="7 days")

# --- HELPERS ---
def split_message(text, chunk_size=4000):
    return [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]

# --- TELEGRAM ---
async def send_telegram(message):
    try:
        bot = Bot(token=BOT_TOKEN)
        for chunk in split_message(message):
            await bot.send_message(chat_id=CHAT_ID, text=chunk)
        logger.info("Telegram message sent")
    except Exception:
        logger.exception("Failed to send Telegram message")

# --- EMAIL PARSER ---
def parse_email_text(text):
    flights_data = []

    # Normalize text: convert CRLF to LF, strip lines, remove empty lines
    lines = [line.strip() for line in text.replace('\r\n', '\n').split('\n') if line.strip()]

    source = destination = dep_date = ret_date = "Unknown"

    # --- Find route and dates anywhere in the lines ---
    for line in lines:
        route_match = re.search(r"([A-Za-z ]+?)\s+to\s+([A-Za-z ]+)", line)
        if route_match and source == "Unknown" and destination == "Unknown":
            source, destination = route_match.groups()
            source = source.strip()
            destination = destination.strip()
        dates_match = re.search(r"([A-Za-z]{3}\s\d{1,2}\s[A-Za-z]{3})\s*[–-]\s*([A-Za-z]{3}\s\d{1,2}\s[A-Za-z]{3})", line)
        if dates_match and dep_date == "Unknown" and ret_date == "Unknown":
            dep_date, ret_date = dates_match.groups()
        if source != "Unknown" and dep_date != "Unknown":
            break

    # --- Find first flight block ---
    for idx, line in enumerate(lines):
        # Match time: 20:20 – 00:30+1
        time_match = re.search(r"(\d{1,2}:\d{2})\s*[–-]\s*(\d{1,2}:\d{2}(?:\+\d)?)", line)
        if not time_match:
            continue
        dep_time, arr_time = time_match.groups()

        # Look ahead up to 5 lines for airline and airports
        airline = dep_airport = arr_airport = "Unknown"
        for j in range(1, 6):
            if idx + j >= len(lines):
                break
            info_line = lines[idx + j]
            airline_match = re.search(r"(Ryanair|EasyJet|British Airways|Lufthansa|KLM|Wizz Air|Jet2)", info_line)
            airports_match = re.search(r"([A-Z]{3})[–-]([A-Z]{3})", info_line)
            if airline_match:
                airline = airline_match.group(1)
            if airports_match:
                dep_airport, arr_airport = airports_match.groups()
            if airline != "Unknown" and dep_airport != "Unknown":
                break

        # Look ahead up to 5 lines for price
        price = None
        for j in range(1, 6):
            if idx + j >= len(lines):
                break
            price_match = re.search(r"£\s?(\d+)", lines[idx + j])
            if price_match:
                price = int(price_match.group(1))
                break
        if price is None:
            continue

        # Append first flight only
        flights_data.append({
            "source": source,
            "destination": destination,
            "dep": dep_date,
            "ret": ret_date,
            "dep_time": dep_time,
            "arr_time": arr_time,
            "airline": airline,
            "dep_airport": dep_airport,
            "arr_airport": arr_airport,
            "price": price
        })
        break  # only first flight

    return flights_data

# --- MAIN ---
def main():
    logger.info("Starting flight agent")

    # --- Gmail login ---
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(EMAIL, PASSWORD)
        mail.select("inbox")
    except Exception:
        logger.exception("Gmail login failed")
        return

    # --- Fetch emails ---
    try:
        status, messages = mail.search(None, '(FROM "noreply-travel@google.com" UNSEEN)')
        if status != "OK" or not messages[0]:
            logger.info("No new emails found")
            asyncio.run(send_telegram("❌ No flight alerts found today."))
            return
        mail_ids = messages[0].split()
    except Exception:
        logger.exception("Failed to fetch emails")
        return

    flights_data = []

    # --- Process emails ---
    for mail_id in mail_ids:
        try:
            _, msg_data = mail.fetch(mail_id, "(RFC822)")
        except Exception:
            logger.exception("Failed to fetch email")
            continue

        for response_part in msg_data:
            if not isinstance(response_part, tuple):
                continue

            try:
                msg = email.message_from_bytes(response_part[1])
            except Exception:
                logger.exception("Failed to parse email bytes")
                continue

            # --- Log timestamp and subject ---
            email_subject = msg.get("Subject", "No Subject")
            email_date = msg.get("Date", "Unknown Date")
            logger.info(f"Processing email | Date: {email_date} | Subject: {email_subject}")

            for part in msg.walk():
                if part.get_content_type() != "text/html":
                    continue
                try:
                    html = part.get_payload(decode=True).decode(errors="ignore")
                    soup = BeautifulSoup(html, "html.parser")
                    text = soup.get_text(separator="\n")
                except Exception:
                    logger.exception("HTML parsing failed")
                    continue

                flights_data.extend(parse_email_text(text))

    if not flights_data:
        final_report = "❌ No good flight deals found today."
        asyncio.run(send_telegram(final_report))
        return

    # --- GROUP & SORT ---
    grouped = defaultdict(list)
    for f in flights_data:
        grouped[(f["source"], f["destination"])].append(f)

    report_lines = []
    for (source, dest), flights in grouped.items():
        f = flights[0]  # first flight only
        report_lines.append(
            f"{source} | {dest} | {f['dep']} → {f['ret']} | {f['dep_time']} → {f['arr_time']} | {f['airline']} | {f['dep_airport']}–{f['arr_airport']} | £{f['price']}"
        )

    # --- Final report ---
    report_lines.sort(key=lambda x: int(re.search(r"£(\d+)", x).group(1)) if "£" in x else 999999)
    final_report = (
        "✈️ Daily Flight Deals\n\n"
        "Source | Destination | Departure → Return | Departure → Arrival | Airline | Airports | Price\n"
    )
    final_report += "\n".join(report_lines[:50])

    logger.info("Report generated")
    
    # --- Send ---
    asyncio.run(send_telegram(final_report))

if __name__ == "__main__":
    main()
