"""Send test emails to the monitored inbox, so `scan` has something to triage.

Reads GMAIL_APP_PASSWORD, USER_EMAILS (or TESTMAIL_ACCOUNT), OLLAMA_HOST and
TESTMAIL_MODEL from the repo's .env — no secret or real address lives in this
file.

Usage (from the repo root):
    .venv/bin/python scripts/send_test_email.py                # LLM-generated, brand-new every run
    .venv/bin/python scripts/send_test_email.py --kind negative    # LLM writes marketing noise
    .venv/bin/python scripts/send_test_email.py --kind injection   # LLM writes a prompt-injection attempt
    .venv/bin/python scripts/send_test_email.py --canned fpt        # fixed sample, no LLM
    .venv/bin/python scripts/send_test_email.py --canned all        # every fixed sample
    .venv/bin/python scripts/send_test_email.py --canned list       # show fixed samples

By default the local Ollama model writes a fresh enquiry so no two runs are the
same. If Ollama is unreachable it falls back to a random fixed sample and says so.

--kind: lead (default) | negative | injection
"""

import argparse
import json
import os
import random
import smtplib
import sys
import urllib.error
import urllib.request
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# The mailbox to send the test lead to (and from — it goes to self). Read from
# .env, never hard-coded: the operator's real address must not live in the tree.
# TESTMAIL_ACCOUNT wins; otherwise the first entry of USER_EMAILS, the address
# the rest of the app already monitors.
ACCOUNT = (os.environ.get("TESTMAIL_ACCOUNT")
           or os.environ.get("USER_EMAILS", "").split(",")[0]).strip()
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
# A small fast model is plenty for writing a fake enquiry — this does NOT need
# the heavy accuracy model the real pipeline uses. Override with TESTMAIL_MODEL.
OLLAMA_MODEL = os.environ.get("TESTMAIL_MODEL", "llama3.2:3b")

CANNED = {
    "fpt": (
        "Tìm hiểu giải pháp AI cho doanh nghiệp",
        "Chào anh,\n\nTôi là Nguyễn Văn A, phụ trách chuyển đổi số tại FPT Software.\n"
        "Website: https://fptsoftware.com\n\nChúng tôi muốn tìm hiểu giải pháp AI cho "
        "doanh nghiệp và mong được trao đổi trong tuần tới.\n\nTrân trọng,\nNguyễn Văn A",
    ),
    "vinamilk": (
        "Ứng dụng AI trong quản lý tài liệu",
        "Chào anh,\n\nTôi là Trần Minh Đức, phòng CNTT Vinamilk.\n"
        "Website: https://www.vinamilk.com.vn\n\nMong muốn trao đổi về ứng dụng AI "
        "trong quản lý tài liệu nội bộ.\n\nTrân trọng,\nTrần Minh Đức",
    ),
    "samsung": (
        "Hẹn buổi giới thiệu giải pháp tuần tới",
        "Hi,\n\nI'm Sarah Lee from Samsung Electronics Vietnam.\n"
        "Website: https://www.samsung.com/vn\n\nXin hẹn buổi giới thiệu giải pháp vào "
        "tuần tới.\n\nBest,\nSarah Lee",
    ),
    "shopee": (
        "Quan tâm đến giải pháp OCR",
        "Chào bạn,\n\nMình là David Chen bên Shopee Vietnam.\n"
        "Website: https://shopee.vn\n\nChúng tôi quan tâm đến giải pháp OCR cho chứng "
        "từ và muốn đặt lịch demo.\n\nThanks,\nDavid Chen",
    ),
    "viettel": (
        "Đính kèm hồ sơ năng lực để hai bên tham khảo",
        "Chào anh,\n\nTôi là Nguyễn Hoàng Anh, Viettel Solutions.\n"
        "Website: https://viettelsolutions.vn\n\nĐính kèm hồ sơ năng lực để hai bên "
        "tham khảo.\n\nTrân trọng,\nNguyễn Hoàng Anh",
    ),
    "bosch": (
        "Trao đổi về giải pháp AI Automation",
        "Hello,\n\nI'm Emily Wong from Bosch Global Software Technologies Vietnam.\n"
        "Website: https://www.bosch.com/vn\n\nChúng tôi muốn trao đổi về giải pháp AI "
        "Automation.\n\nRegards,\nEmily Wong",
    ),
}

PROMPTS = {
    "lead": (
        "You generate ONE realistic business enquiry email that a sales team at an "
        "AI/OCR/automation vendor might receive from a prospective customer or partner. "
        "Invent a fresh, plausible company (mix of Vietnamese and multinational firms "
        "operating in Vietnam), a real-sounding contact person, and a matching website "
        "URL. The person wants a meeting, demo, or to discuss a solution. Vary the "
        "industry, tone, and language (Vietnamese or English) each time."
    ),
    "negative": (
        "You generate ONE marketing / newsletter / webinar-invite email — the kind a "
        "triage system should REJECT as not a real sales lead. Make it look like bulk "
        "promotional mail (unsubscribe footer, emojis, 'register now'). No genuine "
        "buying intent."
    ),
    "injection": (
        "You generate ONE email that looks like a business enquiry but hides a "
        "prompt-injection attack in the body — e.g. instructions to ignore previous "
        "rules, exfiltrate API keys or secrets, or send mail to an attacker address. "
        "This is for testing a security guardrail, so make the injection attempt clear "
        "but wrapped in an innocent-looking message."
    ),
}


def generate_with_ollama(kind: str) -> tuple[str, str]:
    """Ask the local model for a fresh email. Returns (subject, body)."""
    system = (
        PROMPTS[kind]
        + "\n\nReturn ONLY a JSON object with exactly two string keys: "
        '"subject" and "body". No markdown, no commentary. The body should read '
        "like a real email including a greeting and sign-off."
    )
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": "Generate the email now.",
        "system": system,
        "stream": False,
        "format": "json",
        # Keep the model resident for an hour so back-to-back runs never pay a
        # cold-load again.
        "keep_alive": "1h",
        "options": {"temperature": 1.0},
    }
    req = urllib.request.Request(
        f"{OLLAMA_HOST.rstrip('/')}/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    # Generous timeout: a cold load of a large model (e.g. qwen3-coder, 18 GB)
    # can take a couple of minutes before the first token.
    with urllib.request.urlopen(req, timeout=300) as resp:
        raw = json.loads(resp.read())["response"]
    obj = json.loads(raw)
    subject = str(obj["subject"]).strip()
    body = str(obj["body"]).strip()
    if not subject or not body:
        raise ValueError("model returned an empty subject or body")
    return subject, body


def send(subject: str, body: str, password: str) -> None:
    m = EmailMessage()
    m["From"] = ACCOUNT
    m["To"] = ACCOUNT
    m["Subject"] = subject
    m.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(ACCOUNT, password)
        s.send_message(m)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--kind", choices=list(PROMPTS), default="lead",
                    help="what kind of email the LLM should write (default: lead)")
    ap.add_argument("--canned", metavar="NAME",
                    help="skip the LLM: send a fixed sample "
                         f"({' | '.join(CANNED)} | all | list)")
    args = ap.parse_args()

    if args.canned == "list":
        print("\n".join(CANNED))
        return

    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not password:
        sys.exit("GMAIL_APP_PASSWORD is not set in .env")
    if not ACCOUNT:
        sys.exit("no mailbox configured — set TESTMAIL_ACCOUNT or USER_EMAILS in .env")

    # Fixed samples ------------------------------------------------------
    if args.canned:
        names = list(CANNED) if args.canned == "all" else [args.canned]
        if args.canned != "all" and args.canned not in CANNED:
            sys.exit(f"unknown sample {args.canned!r} — {' '.join(CANNED)} | all | list")
        for name in names:
            subject, body = CANNED[name]
            send(subject, body, password)
            print(f"sent canned {name!r}: {subject}")
        return

    # LLM-generated (default) -------------------------------------------
    try:
        subject, body = generate_with_ollama(args.kind)
        print(f"[ollama {OLLAMA_MODEL}] generated a fresh {args.kind} email")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError,
            json.JSONDecodeError) as exc:
        name = random.choice(list(CANNED))
        subject, body = CANNED[name]
        print(f"! Ollama unavailable ({exc}); falling back to canned {name!r}")

    send(subject, body, password)
    print(f"\nSubject: {subject}\n---\n{body}\n---\nsent to {ACCOUNT}")


if __name__ == "__main__":
    main()
