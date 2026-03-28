import re
import email
import imaplib
import asyncio
from bs4 import BeautifulSoup
from collections import defaultdict
from loguru import logger
from telegram import Bot
import os
from dotenv import load_dotenv
from email.header import decode_header
import unicodedata

# Logging setup
logger.add("flight_agent.log", rotation="1 day", retention="7 days")

# Only load .env if present
dotenv_path = '.env'
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)
    logger.info("Loaded environment variables from .env file")

# Read environment variables
EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")
IMAP_SERVER = "imap.gmail.com"
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Check for missing critical variables
missing_vars = [var for var in ["EMAIL", "PASSWORD", "BOT_TOKEN", "CHAT_ID"] if not os.getenv(var)]
if missing_vars:
    logger.error(f"Missing environment variables: {', '.join(missing_vars)}")
    raise ValueError(f"Missing environment variables: {', '.join(missing_vars)}")


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

# --- EMAIL PARSERS ---

# Format 1: existing parser (single flight with detailed info)
def parse_email_text(text):
    flights_data = []

    lines = [line.strip() for line in text.replace('\r\n', '\n').split('\n') if line.strip()]
    source = destination = dep_date = ret_date = "--"

    # Find route and dates
    for line in lines:
        route_match = re.search(r"([A-Za-z ]+?)\s+to\s+([A-Za-z ]+)", line)
        if route_match and source == "--" and destination == "--":
            source, destination = route_match.groups()
            source = source.strip()
            destination = destination.strip()
        dates_match = re.search(r"([A-Za-z]{3}\s\d{1,2}\s[A-Za-z]{3})\s*[–-]\s*([A-Za-z]{3}\s\d{1,2}\s[A-Za-z]{3})", line)
        if dates_match and dep_date == "--" and ret_date == "--":
            dep_date, ret_date = dates_match.groups()
        if source != "--" and dep_date != "--":
            break

    # Find first flight block
    for idx, line in enumerate(lines):
        time_match = re.search(r"(\d{1,2}:\d{2})\s*[–-]\s*(\d{1,2}:\d{2}(?:\+\d)?)", line)
        if not time_match:
            continue
        dep_time, arr_time = time_match.groups()

        # Look ahead up to 5 lines for airline and airports
        airline = dep_airport = arr_airport = "--"
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
            if airline != "--" and dep_airport != "--":
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

# Format 2: multiple routes, only prices and dates
def parse_email_text_format2(text):
    flights_data = []
    lines = [line.strip() for line in text.replace('\r\n', '\n').split('\n') if line.strip()]

    i = 0
    while i < len(lines):
        # Start of a flight block: route line
        route_match = re.match(r"([A-Za-z ]+?)\s+to\s+([A-Za-z ]+)", lines[i], re.IGNORECASE)
        if not route_match:
            i += 1
            continue

        source, destination = route_match.groups()
        source = source.strip()
        destination = destination.strip()

        # Collect block lines until next route or end
        block_lines = []
        j = i + 1
        while j < len(lines):
            if re.match(r"([A-Za-z ]+?)\s+to\s+([A-Za-z ]+)", lines[j], re.IGNORECASE):
                break
            block_lines.append(lines[j])
            j += 1

        # Extract dates
        dep_date, ret_date = "--", "--"
        for line in block_lines:
            dates_match = re.search(
                r"([A-Za-z]{3}\s\d{1,2}\s[A-Za-z]{3})\s*[–-]\s*([A-Za-z]{3}\s\d{1,2}\s[A-Za-z]{3})",
                line
            )
            if dates_match:
                dep_date, ret_date = dates_match.groups()
                break

        # Extract price (first £ in block)
        price = None
        for line in block_lines:
            price_match = re.search(r"£\s?([\d ,]+)", line)
            if price_match:
                price_str = price_match.group(1)
                price = int(price_str.replace(" ", "").replace(",", ""))
                break

        if price is not None:
            flights_data.append({
                "source": source,
                "destination": destination,
                "dep": dep_date,
                "ret": ret_date,
                "dep_time": "--",
                "arr_time": "--",
                "airline": "--",
                "dep_airport": "--",
                "arr_airport": "--",
                "price": price
            })

        # Move to next block
        i = j

    return flights_data

def decode_email_subject(subject):
    decoded_parts = decode_header(subject)
    decoded_subject = ""
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            decoded_subject += part.decode(encoding or "utf-8", errors="ignore")
        else:
            decoded_subject += part
    return decoded_subject.strip()

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

            # Log timestamp and subject
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

                # --- Subject-based parser dispatch ---
                subject = unicodedata.normalize("NFKC", decode_email_subject(email_subject))

                # Format 1: tracked single flight
                if re.search(r"Your tracked flight to .*? is now £\d+", subject):
                    flights_data.extend(parse_email_text(text))
                # Format 2: multiple flights
                elif re.search(r"Prices for your tracked flights to .* have changed", subject):
                    flights_data.extend(parse_email_text_format2(text))
                else:
                    logger.warning(f"Unknown email format: {repr(subject)}")

    if not flights_data:
        final_report = "❌ No good flight deals found today."
        asyncio.run(send_telegram(final_report))
        return

    # Group flights by (source, destination)
    grouped = defaultdict(list)
    for f in flights_data:
        # Normalize whitespace
        source = re.sub(r"\s+", " ", f["source"].strip())
        destination = re.sub(r"\s+", " ", f["destination"].strip())
        key = (source, destination)
        grouped[key].append(f)

    # Build report: keep only the cheapest flight per route
    report_lines = []
    for (source, dest), flights in grouped.items():
        # Find the flight with the lowest price
        cheapest_flight = min(flights, key=lambda x: x["price"])
        report_lines.append(
            f"{source} | {dest} | {cheapest_flight['dep']} → {cheapest_flight['ret']} | "
            f"{cheapest_flight['dep_time']} | {cheapest_flight['arr_time']} | {cheapest_flight['airline']} | "
            f"{cheapest_flight['dep_airport']}–{cheapest_flight['arr_airport']} | £{cheapest_flight['price']}"
        )

    # Sort report lines by price ascending
    report_lines.sort(key=lambda x: int(re.search(r"£(\d+)", x).group(1)))

    # Final report text
    final_report = "✈️ Daily Flight Deals\n\n"
    final_report += "Source | Destination | Departure → Return | Dep Time | Arr Time | Airline | Airports | Price\n"
    final_report += "\n".join(report_lines[:50])  # top 50 flights if needed

    logger.info("Report generated")

    # --- Send ---
    asyncio.run(send_telegram(final_report))


if __name__ == "__main__":
    main()