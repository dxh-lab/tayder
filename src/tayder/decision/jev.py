"""Bounded TypeSafe HTTP adapter. All failures become unavailable observations."""
import math
import time
import httpx

from tayder.decision.snapshot import OPTIONS


def validate_response(data):
    if not isinstance(data.get("model"), str) or not data["model"]:
        raise ValueError("missing_model")
    answer = data["answers"]["regime"]
    probs = answer["probabilities"]
    if answer["type"] != "choice" or answer["choice"] not in OPTIONS or set(probs) != set(OPTIONS):
        raise ValueError("invalid_choice")
    values = [*probs.values(), answer["confidence"]]
    if any(type(x) not in (int, float) or not math.isfinite(x) or not 0 <= x <= 1 for x in values):
        raise ValueError("invalid_probability")
    if abs(sum(probs.values()) - 1) > 1e-6 or probs[answer["choice"]] != max(probs.values()):
        raise ValueError("invalid_distribution")
    usage = data.get("usage", {})
    if any(type(v) is not int or v < 0 for v in usage.values()):
        raise ValueError("invalid_usage")
    return {"status": "ok", "model": data["model"], "answer": answer, "usage": usage}


class JevClient:
    def __init__(self, api_key, timeout=2.0, *, transport=None):
        self.api_key, self.timeout, self.transport = api_key, timeout, transport

    def evaluate(self, request):
        started = time.monotonic()
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=False,
                              trust_env=False, transport=self.transport) as client:
                response = client.post("https://api.typesafe.ai/v1/systemone",
                    headers={"Authorization": f"Bearer {self.api_key}"}, json=request)
                response.raise_for_status()
                result = validate_response(response.json())
        except Exception as exc:
            # Never persist HTTP bodies, request headers, or provider exception text.
            result = {"status": "unavailable", "error_type": type(exc).__name__}
        result["latency_ms"] = (time.monotonic() - started) * 1000
        return result
