import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from cli import load_requests, make_ctx, make_request_payload  # noqa: E402


def get_request(rid: str) -> dict:
    for r in load_requests():
        if r["id"] == rid:
            return r
    raise KeyError(rid)


def setup(rid: str):
    req = get_request(rid)
    ctx = make_ctx(req)
    return req, ctx, make_request_payload(req, ctx)


class ScriptLLM:
    def __init__(self, decisions):
        self.decisions = list(decisions)

    def complete(self, system, user):
        if not self.decisions:
            return json.dumps({"thought": "end", "action": "finish", "arguments": {"summary": ""}})
        d = self.decisions.pop(0)
        return d if isinstance(d, str) else json.dumps(d, ensure_ascii=False)
