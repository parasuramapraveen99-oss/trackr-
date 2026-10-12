"""Send a test email via Mailgun's HTTP API. Reads credentials AND the test recipient
from the environment -- run via `python -m dotenv run -- python scripts/mailgun_send.py`
or export MAILGUN_API_KEY / MAILGUN_DOMAIN / MAILGUN_BASE_URL / MAILGUN_TEST_RECIPIENT
first. Nothing here is client-facing; this only proves Mailgun sending works at all."""

import os

import requests


def send_simple_message() -> requests.Response:
    domain = os.environ["MAILGUN_DOMAIN"]
    base_url = os.environ["MAILGUN_BASE_URL"]
    recipient = os.environ.get("MAILGUN_TEST_RECIPIENT", "Test Recipient <test@example.com>")
    return requests.post(
        f"{base_url}/v3/{domain}/messages",
        auth=("api", os.environ["MAILGUN_API_KEY"]),
        data={
            "from": f"Mailgun Sandbox <postmaster@{domain}>",
            "to": recipient,
            "subject": "Hello from the Mailgun sandbox",
            "text": "Congratulations, you just sent an email with Mailgun!",
        },
    )


if __name__ == "__main__":
    response = send_simple_message()
    print(response.status_code, response.json())
