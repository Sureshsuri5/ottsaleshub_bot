"""Read an Indian bank credit SMS and pull out the amount and reference.

Banks all say the same thing in different words, so this matches on shape
rather than on any one bank's phrasing:

    "Rs.250.00 credited to A/c XX1234 on 07-08-26 by UPI ref no 523456789012"
    "INR 1,250.50 credited ... UPI/CR/312345678901/JOHN"
    "Received Rs 99 in your Kotak Bank AC X1234 from ... UPI Ref:412345678901"

Two rules keep this honest:

* A message must look like *money arriving*. Debits, balance alerts, OTPs and
  autopay reminders all mention amounts, and crediting an order from any of
  them would be wrong.
* A 12-digit reference is required. That is the UPI RRN, and it is what makes a
  credit unique — without it there is nothing to stop the same rupees being
  counted twice.
"""

import re
from datetime import datetime, timezone

# UPI RRNs are 12 digits. Bounded by non-digits so a longer number can't match.
UTR = re.compile(r"(?<!\d)(\d{12})(?!\d)")

# Wallets use their own reference formats — FamPay sends "txn ID FMPIB6379394986".
# Anchored on the label so a date or an account number can't be mistaken for one.
LABELLED_REF = re.compile(
    r"(?:txn\s*id|transaction\s*id|ref(?:erence)?\s*(?:no\.?|id|:)?|utr|rrn)"
    r"[:\s#\-]*([A-Z0-9]{8,24})", re.I)

AMOUNT = re.compile(
    r"(?:rs|inr|₹)\.?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I)

# The amount that arrived, not the balance left afterwards. FamPay's alert
# carries both — "received Rs.2.0 ... balance is Rs.192.06" — and reading the
# wrong one would settle a ₹2 order against a ₹192 payment.
CREDITED_AMOUNT = re.compile(
    r"(?:received|credited|credit(?:ed)?\s+with)\s*"
    r"(?:of\s*)?(?:rs|inr|₹)\.?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I)

# Words that mean money came in.
CREDIT = re.compile(r"\b(credited|received|credit)\b", re.I)

# Words that mean this is not an incoming payment, however much it looks like
# one. Checked first: "debited" and "credited" both appear in some messages.
NOT_CREDIT = re.compile(
    r"\b(debited|debit|withdrawn|spent|paid to|sent to|failed|reversed|"
    r"declined|otp|will be debited|mandate|autopay|due|reminder|"
    r"insufficient)\b", re.I)


# BharatPe and some wallet alerts carry no reference number at all:
#   "You've received Rs.1.0 from KASABOYINA PAVAN KUMAR! ... BharatPe Account"
# There is nothing unique in that, so a key is built from the payer and the
# amount instead. It is weaker than an RRN — see synth_key below.
PAYER = re.compile(r"from\s+([A-Z][A-Za-z .]{2,40}?)\s*[!.,]", re.I)


def synth_key(amount: float, payer: str, when) -> str:
    """A stand-in reference for alerts that carry none.

    Payer, amount and the minute it arrived. Two payments only collide if the
    same person sends the same amount inside the same minute — unlikely, and
    with UNIQUE_AMOUNTS on, two open orders never ask for the same amount
    anyway. It is still weaker than a real RRN, which is why the shop should
    prefer a bank SMS when both arrive.
    """
    who = re.sub(r"[^a-z0-9]+", "", (payer or "").lower())[:18] or "unknown"
    return f"x:{who}:{amount:.2f}:{when:%Y%m%d%H%M}"


def parse(text: str) -> tuple[float, str] | None:
    """Return (amount, utr) for a credit SMS, or None if it isn't one."""
    if not text:
        return None
    body = " ".join(text.split())

    if NOT_CREDIT.search(body) or not CREDIT.search(body):
        return None

    money = CREDITED_AMOUNT.search(body) or AMOUNT.search(body)
    if not money:
        return None
    try:
        amount = float(money.group(1).replace(",", ""))
    except ValueError:
        return None
    if amount <= 0:
        return None
    amount = round(amount, 2)

    ref = LABELLED_REF.search(body) or UTR.search(body)
    if ref:
        return amount, ref.group(1).upper()

    # No reference in the message. Only accept that when the payer is named —
    # otherwise there is nothing at all to tell two credits apart.
    payer = PAYER.search(body)
    if not payer:
        return None
    return amount, synth_key(amount, payer.group(1),
                             datetime.now(timezone.utc))
