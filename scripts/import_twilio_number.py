"""One-command PSTN setup: buy a Twilio US number, wire it to Retell via an
Elastic SIP trunk, and bind the receptionist agent + inbound webhook.

Why this path: Retell's direct number purchase requires a US-issued ID
(Persona check), which an individual in India cannot pass. Twilio US local
numbers have no such requirement — an upgraded Twilio account (card + $20
deposit) suffices — and Retell officially supports imported Twilio numbers.

Prereqs (manual, ~10 min):
  1. Create a Twilio account and UPGRADE it (Console -> Billing -> add card,
     $20 minimum). Trial accounts play a preamble on every call and restrict
     callers — upgrading is mandatory for a clean demo.
  2. Put TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in .env.

Run:
    python -m scripts.import_twilio_number              # buys any US number
    python -m scripts.import_twilio_number --area-code 415

Idempotent-ish: re-running reuses the trunk/credential list by friendly name.
After success it prints the number — set RETELL_PHONE_NUMBER in .env and as a
Fly secret.
"""
import argparse
import secrets
import sys

import httpx
from retell import Retell

from app.config import get_settings

settings = get_settings()

TRUNK_NAME = "clinic-voice-agent-trunk"
CRED_LIST_NAME = "clinic-voice-agent-creds"
RETELL_ORIGINATION = "sip:sip.retellai.com"

TWILIO_API = "https://api.twilio.com/2010-04-01"
TWILIO_TRUNKING = "https://trunking.twilio.com/v1"


def twilio_client(sid: str, token: str) -> httpx.Client:
    return httpx.Client(auth=(sid, token), timeout=30.0)


def _die(msg: str) -> None:
    sys.exit(f"ERROR: {msg}")


def ensure_trunk(client: httpx.Client, sid: str) -> dict:
    trunks = client.get(f"{TWILIO_TRUNKING}/Trunks").json().get("trunks", [])
    for trunk in trunks:
        if trunk["friendly_name"] == TRUNK_NAME:
            print(f"trunk exists: {trunk['sid']} ({trunk['domain_name']})")
            return trunk
    domain = f"clinic-voice-{secrets.token_hex(4)}.pstn.twilio.com"
    response = client.post(
        f"{TWILIO_TRUNKING}/Trunks",
        data={"FriendlyName": TRUNK_NAME, "DomainName": domain},
    )
    if response.status_code >= 400:
        _die(f"trunk create failed: {response.text}")
    trunk = response.json()
    print(f"trunk created: {trunk['sid']} ({trunk['domain_name']})")
    return trunk


def ensure_origination(client: httpx.Client, trunk_sid: str) -> None:
    urls = client.get(f"{TWILIO_TRUNKING}/Trunks/{trunk_sid}/OriginationUrls").json().get(
        "origination_urls", []
    )
    if any(u["sip_url"] == RETELL_ORIGINATION for u in urls):
        print("origination URL already points at Retell")
        return
    response = client.post(
        f"{TWILIO_TRUNKING}/Trunks/{trunk_sid}/OriginationUrls",
        data={
            "FriendlyName": "retell",
            "SipUrl": RETELL_ORIGINATION,
            "Weight": 1,
            "Priority": 1,
            "Enabled": "true",
        },
    )
    if response.status_code >= 400:
        _die(f"origination url failed: {response.text}")
    print("origination URL -> sip.retellai.com")


def ensure_credentials(client: httpx.Client, account_sid: str, trunk_sid: str) -> tuple[str, str]:
    """Returns (username, password). Password can't be read back — if the list
    already exists we mint a fresh credential in it."""
    lists = client.get(f"{TWILIO_API}/Accounts/{account_sid}/SIP/CredentialLists.json").json().get(
        "credential_lists", []
    )
    cred_list = next((c for c in lists if c["friendly_name"] == CRED_LIST_NAME), None)
    if not cred_list:
        response = client.post(
            f"{TWILIO_API}/Accounts/{account_sid}/SIP/CredentialLists.json",
            data={"FriendlyName": CRED_LIST_NAME},
        )
        if response.status_code >= 400:
            _die(f"credential list failed: {response.text}")
        cred_list = response.json()
        print(f"credential list created: {cred_list['sid']}")
    username = f"retell{secrets.token_hex(3)}"
    password = secrets.token_urlsafe(12) + "aA1"  # Twilio wants mixed-case + digit
    response = client.post(
        f"{TWILIO_API}/Accounts/{account_sid}/SIP/CredentialLists/{cred_list['sid']}/Credentials.json",
        data={"Username": username, "Password": password},
    )
    if response.status_code >= 400:
        _die(f"credential create failed: {response.text}")
    print(f"sip credential created: {username}")
    # attach the list to the trunk (idempotent-ish: 409/duplicate is fine)
    attach = client.post(
        f"{TWILIO_TRUNKING}/Trunks/{trunk_sid}/CredentialLists",
        data={"CredentialListSid": cred_list["sid"]},
    )
    if attach.status_code >= 400 and "already" not in attach.text.lower():
        print(f"note: credential list attach said: {attach.status_code} {attach.text[:200]}")
    return username, password


def buy_number(client: httpx.Client, account_sid: str, area_code: str | None) -> dict:
    existing = client.get(f"{TWILIO_API}/Accounts/{account_sid}/IncomingPhoneNumbers.json").json().get(
        "incoming_phone_numbers", []
    )
    if existing:
        number = existing[0]
        print(f"reusing existing Twilio number: {number['phone_number']}")
        return number
    data = {"AreaCode": area_code} if area_code else {"AreaCode": "628"}
    response = client.post(
        f"{TWILIO_API}/Accounts/{account_sid}/IncomingPhoneNumbers.json", data=data
    )
    if response.status_code >= 400:
        _die(
            f"number purchase failed: {response.text}\n"
            "If this mentions trial restrictions, upgrade the Twilio account first."
        )
    number = response.json()
    print(f"number purchased: {number['phone_number']}")
    return number


def attach_number_to_trunk(client: httpx.Client, trunk_sid: str, number_sid: str) -> None:
    response = client.post(
        f"{TWILIO_TRUNKING}/Trunks/{trunk_sid}/PhoneNumbers", data={"PhoneNumberSid": number_sid}
    )
    if response.status_code >= 400 and "already" not in response.text.lower():
        _die(f"attach number to trunk failed: {response.text}")
    print("number attached to trunk")


def import_into_retell(phone_number: str, domain: str, username: str, password: str) -> None:
    retell = Retell(api_key=settings.retell_api_key)
    response = retell.phone_number.import_(
        phone_number=phone_number,
        termination_uri=domain,
        sip_trunk_auth_username=username,
        sip_trunk_auth_password=password,
        inbound_agents=[{"agent_id": settings.retell_agent_id, "weight": 1}],
        inbound_webhook_url=f"{settings.app_base_url}/retell/inbound",
        nickname="clinic-voice-agent",
    )
    print(f"imported into Retell: {response.phone_number}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--area-code", default=None)
    args = parser.parse_args()

    sid = getattr(settings, "twilio_account_sid", "")
    token = getattr(settings, "twilio_auth_token", "")
    if not sid or not token:
        _die("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN missing in .env")
    if not settings.retell_agent_id:
        _die("RETELL_AGENT_ID missing in .env")

    client = twilio_client(sid, token)
    account = client.get(f"{TWILIO_API}/Accounts/{sid}.json").json()
    if account.get("type") == "Trial":
        print(
            "WARNING: this Twilio account is still a TRIAL — inbound calls will play a "
            "trial preamble and callers may be restricted. Upgrade before the demo."
        )

    trunk = ensure_trunk(client, sid)
    ensure_origination(client, trunk["sid"])
    username, password = ensure_credentials(client, sid, trunk["sid"])
    number = buy_number(client, sid, args.area_code)
    attach_number_to_trunk(client, trunk["sid"], number["sid"])
    import_into_retell(number["phone_number"], trunk["domain_name"], username, password)

    print("\nDONE. Final steps:")
    print(f"  1. Set RETELL_PHONE_NUMBER={number['phone_number']} in .env")
    print(f"  2. flyctl secrets set RETELL_PHONE_NUMBER={number['phone_number']}")
    print(f"  3. Call {number['phone_number']} from your phone (ISD) to verify.")


if __name__ == "__main__":
    main()
