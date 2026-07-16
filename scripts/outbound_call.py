"""Place an outbound call from the agent (used to demo the missed-outbound →
callback scenario).

Usage:
    python -m scripts.outbound_call +919876543210 "Confirming your Thursday appointment"

If the call goes unanswered, the call_ended webhook records an owed callback;
when that person rings the clinic number back, the agent opens with the
original context instead of starting cold.
"""
import sys

from retell import Retell

from app.config import get_settings

settings = get_settings()


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: python -m scripts.outbound_call <to_number_e164> [context]")
    to_number = sys.argv[1]
    context = sys.argv[2] if len(sys.argv) > 2 else "We called to confirm your upcoming appointment."

    if not settings.retell_phone_number:
        sys.exit("RETELL_PHONE_NUMBER missing in .env (buy a number first)")

    client = Retell(api_key=settings.retell_api_key)
    call = client.call.create_phone_call(
        from_number=settings.retell_phone_number,
        to_number=to_number,
        override_agent_id=settings.retell_agent_id or None,
        retell_llm_dynamic_variables={
            "owed_callback_context": context,
            "caller_phone": to_number,
        },
        metadata={"callback_context": context},
    )
    print(f"outbound call placed: {call.call_id} -> {to_number}")
    print("Let it ring unanswered to test the callback scenario, then call the clinic number back.")


if __name__ == "__main__":
    main()
