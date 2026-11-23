"""Headless test harness: run a full case review without adk web.
Usage (inside container, cwd=/app/agents): python -m preauth.run_case PA-001
Prints every agent turn (author name) and the final supervisor message."""
import asyncio, sys

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from preauth.agent import root_agent


async def main(case_id):
    svc = InMemorySessionService()
    session = await svc.create_session(app_name="preauth", user_id="tester", session_id="s1")
    runner = Runner(agent=root_agent, app_name="preauth", session_service=svc)
    msg = types.Content(role="user", parts=[types.Part(text=(f"Review case {case_id}. Produce the "
                                                           "final result object for the reviewer."))])
    async for event in runner.run_async(user_id="tester", session_id="s1", new_message=msg):
        author = getattr(event, "author", "")
        if event.is_final_response() and event.content and event.content.parts:
            print(f"\n===== FINAL ({author}) =====")
            print("".join(p.text or "" for p in event.content.parts))
        elif author and event.content and event.content.parts and any(p.function_call for p in event.content.parts):
            calls = [f"{p.function_call.name}({list(p.function_call.args)})" for p in event.content.parts if p.function_call]
            print(f"[{author}] -> {', '.join(calls)}")
    session = await svc.get_session(app_name="preauth", user_id="tester", session_id="s1")
    print("\n[session state] reviewer_name:", session.state.get("reviewer_name"),
          "| reviewer_employee_id:", session.state.get("reviewer_employee_id"))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "PA-001"))
