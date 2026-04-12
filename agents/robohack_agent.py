from typing import List

from brain_client.agent_types import Agent


class RobohackAgent(Agent):
    """
    Robohack 2026 agent.

    Uses Gemini to react to the environment (gemini_react) and hardcoded
    skills for immediate greetings and speech (greet, say).
    """

    @property
    def id(self) -> str:
        return "robohack_agent"

    @property
    def display_name(self) -> str:
        return "Robohack Agent"

    def get_skills(self) -> List[str]:
        return [
            "innate-os/greet",
            "innate-os/say",
            "innate-os/gemini_react",
            "innate-os/head_emotion",
            "innate-os/navigate_to_position",
        ]

    def get_inputs(self) -> List[str]:
        return ["micro"]

    def get_prompt(self) -> str:
        return """You are Maurice, an autonomous robot at Robohack 2026.

Available skills:
  greet          – wave head + say hello to a person (pass optional 'greeting' text)
  say            – speak any text aloud (pass 'text' or a 'preset' key)
  gemini_react   – ask Gemini Vision AI to analyse the camera image and decide
                   what action to take and what to say (pass optional 'situation' description)
  head_emotion   – express an emotion via head tilt (happy/sad/excited/thinking/surprised/…)
  navigate_to_position – move to x,y,theta coordinates

Behaviour rules:
1. When you first see a person → call greet.
2. Every 30–60 seconds of inactivity → call gemini_react to survey your environment.
3. Match head_emotion to your mood/context between other actions.
4. Use say for direct, short announcements (e.g. "I am going to move now").
5. Never explain what you are about to do — just do it.
6. If the user says 'stop', STOP immediately. Do NOT retry."""

    def uses_gaze(self) -> bool:
        return True
