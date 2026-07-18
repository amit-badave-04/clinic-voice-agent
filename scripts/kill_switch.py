"""Emergency stop for the whole system.

Run: python -m scripts.kill_switch on|off|status

"on" does BOTH halves of a real kill:
  1. Sets the clinic_policies 'kill_switch' row — web-call minting returns 503
     and the inbound webhook stops answering with context.
  2. Unbinds the agent from the phone number. This is what actually rejects
     PSTN calls: Retell declines an inbound call only when the webhook sends
     no override AND the number has no bound agent. With an agent still bound,
     a call would connect (context-free) regardless of the webhook.

"off" reverses both. "status" reports both halves, since a half-flipped state
(flag on, agent still bound) only degrades context instead of stopping calls.
"""
import sys

from retell import Retell
from sqlalchemy import text

from app.config import get_settings
from app.db.session import SessionLocal

settings = get_settings()


async def _set_flag(value: str) -> None:
    async with SessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO clinic_policies (key, value) VALUES ('kill_switch', :v) "
                "ON CONFLICT (key) DO UPDATE SET value = :v"
            ),
            {"v": value},
        )
        await session.commit()


async def _get_flag() -> str:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT value FROM clinic_policies WHERE key = 'kill_switch'")
            )
        ).first()
        return row.value if row else "off"


def _phone_binding(client: Retell):
    numbers = client.phone_number.list()
    items = getattr(numbers, "items", numbers)
    return next((n for n in items if n.phone_number == settings.retell_phone_number), None)


def main() -> None:
    import asyncio

    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    client = Retell(api_key=settings.retell_api_key)

    if command == "on":
        asyncio.run(_set_flag("on"))
        client.phone_number.update(settings.retell_phone_number, inbound_agents=[])
        print("KILL SWITCH ON: web minting stopped, phone agent unbound — inbound calls now decline.")
    elif command == "off":
        client.phone_number.update(
            settings.retell_phone_number,
            inbound_agents=[{"agent_id": settings.retell_agent_id, "weight": 1}],
        )
        asyncio.run(_set_flag("off"))
        print("kill switch off: phone agent rebound, web minting open.")
    else:
        flag = asyncio.run(_get_flag())
        binding = _phone_binding(client)
        agents = getattr(binding, "inbound_agents", None) if binding else None
        print(f"db flag: {flag}")
        print(f"phone {settings.retell_phone_number} inbound agents: {agents or 'NONE (calls decline)'}")
        if flag == "on" and agents:
            print("WARNING: half-flipped — flag is on but the agent is still bound; PSTN calls still connect.")


if __name__ == "__main__":
    main()
