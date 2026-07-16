"""Print full transcript + tool calls for specific call IDs.
Run: python -m scripts.dump_transcript <call_id> [<call_id> ...]"""
import io
import sys

from retell import Retell

from app.config import get_settings

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


def main() -> None:
    client = Retell(api_key=get_settings().retell_api_key)
    for call_id in sys.argv[1:]:
        call = client.call.retrieve(call_id)
        data = call.model_dump()
        print("=" * 78)
        print(f"CALL {call_id}")
        for utt in data.get("transcript_with_tool_calls") or []:
            role = utt.get("role", "?")
            if role == "tool_call_invocation":
                print(f"  >> TOOL {utt.get('name')} args={utt.get('arguments')}")
            elif role == "tool_call_result":
                print(f"  << RESULT {(utt.get('content') or '')[:220]}")
            else:
                print(f"{role.upper()}: {utt.get('content')}")


if __name__ == "__main__":
    main()
