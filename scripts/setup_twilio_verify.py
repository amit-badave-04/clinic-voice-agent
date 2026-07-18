"""Create (or find) the Twilio Verify service used for caller OTP.

Run: python -m scripts.setup_twilio_verify

Idempotent by friendly name. Prints the service SID — set it as
TWILIO_VERIFY_SERVICE_SID in .env and push to Fly via
`python -m scripts.push_fly_secrets --only TWILIO_VERIFY_SERVICE_SID`.
"""
import sys

import httpx

from app.config import get_settings

settings = get_settings()

VERIFY_API = "https://verify.twilio.com/v2"
SERVICE_NAME = "clinic-voice-agent-otp"


def main() -> None:
    if not (settings.twilio_account_sid and settings.twilio_auth_token):
        sys.exit("ERROR: TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN missing in .env")
    auth = (settings.twilio_account_sid, settings.twilio_auth_token)
    with httpx.Client(auth=auth, timeout=30.0) as client:
        services = client.get(f"{VERIFY_API}/Services").json().get("services", [])
        existing = next((s for s in services if s.get("friendly_name") == SERVICE_NAME), None)
        if existing:
            print(f"verify service exists: {existing['sid']}")
            sid = existing["sid"]
        else:
            response = client.post(
                f"{VERIFY_API}/Services",
                data={"FriendlyName": SERVICE_NAME, "CodeLength": 6},
            )
            if response.status_code >= 400:
                sys.exit(f"ERROR: service create failed: {response.text}")
            sid = response.json()["sid"]
            print(f"verify service created: {sid}")
    print("\nNext steps:")
    print(f"  1. Set TWILIO_VERIFY_SERVICE_SID={sid} in .env")
    print("  2. python -m scripts.push_fly_secrets --only TWILIO_ACCOUNT_SID,TWILIO_AUTH_TOKEN,TWILIO_VERIFY_SERVICE_SID")


if __name__ == "__main__":
    main()
