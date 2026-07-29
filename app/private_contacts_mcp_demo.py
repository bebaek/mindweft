from __future__ import annotations

import argparse
import json
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, Response

from app.mcp import DEFAULT_MCP_PROTOCOL_VERSION, PRIVATE_VALUES_META_KEY

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766

CONTACTS_LIST_TOOL = {
    "name": "contacts_list",
    "description": (
        "List demo contacts. Contact fields are opaque {{pii:kind:reference}} placeholders; "
        "preserve each placeholder exactly when answering the user."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "contacts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "email": {"type": "string"},
                    },
                    "required": ["name", "email"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["contacts"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class DemoContact:
    name: str
    email: str


DEMO_CONTACTS = (
    DemoContact(name="Alice Smith", email="alice@example.com"),
    DemoContact(name="Bob Jones", email="bob@example.com"),
)


class PrivateContactsMCPServer:
    def __init__(
        self,
        contacts: Sequence[DemoContact] = DEMO_CONTACTS,
        *,
        reference_factory: Callable[[], str] | None = None,
    ) -> None:
        self._contacts = tuple(contacts)
        self._reference_factory = reference_factory or (lambda: secrets.token_urlsafe(16))

    def handle(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        request_id = payload.get("id")
        method = payload.get("method")
        if method == "notifications/initialized":
            return None
        if request_id is None:
            return None
        if method == "initialize":
            return self._result(
                request_id,
                {
                    "protocolVersion": DEFAULT_MCP_PROTOCOL_VERSION,
                    "serverInfo": {
                        "name": "minigent-private-contacts-demo",
                        "version": "0.1.0",
                    },
                    "capabilities": {"tools": {}},
                },
            )
        if method == "tools/list":
            return self._result(request_id, {"tools": [CONTACTS_LIST_TOOL]})
        if method == "tools/call":
            return self._handle_tool_call(request_id, payload.get("params"))
        return self._error(request_id, -32601, f"Unsupported MCP method '{method}'")

    def _handle_tool_call(self, request_id: Any, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "tools/call params must be an object")
        if params.get("name") != "contacts_list":
            return self._error(request_id, -32602, "Unknown tool")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict) or arguments:
            return self._error(request_id, -32602, "contacts_list takes no arguments")

        structured_content: dict[str, Any] = {"contacts": []}
        private_values: dict[str, str] = {}
        for contact in self._contacts:
            name_reference = self._reference_factory()
            email_reference = self._reference_factory()
            private_values[name_reference] = contact.name
            private_values[email_reference] = contact.email
            structured_content["contacts"].append(
                {
                    "name": f"{{{{pii:name:{name_reference}}}}}",
                    "email": f"{{{{pii:email:{email_reference}}}}}",
                }
            )

        return self._result(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Found {len(self._contacts)} contacts. Preserve the placeholders "
                            "from structuredContent exactly in the final answer."
                        ),
                    }
                ],
                "structuredContent": structured_content,
                "_meta": {PRIVATE_VALUES_META_KEY: private_values},
            },
        )

    @staticmethod
    def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }


def create_app(server: PrivateContactsMCPServer | None = None) -> FastAPI:
    app = FastAPI(title="Minigent private contacts MCP demo")
    private_contacts = server or PrivateContactsMCPServer()

    @app.post("/mcp", response_model=None)
    async def mcp_endpoint(request: Request) -> Response | dict[str, Any]:
        try:
            payload = await request.json()
        except ValueError:
            return Response(
                content=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Invalid JSON"},
                    }
                ),
                status_code=400,
                media_type="application/json",
            )
        if not isinstance(payload, dict):
            return Response(
                content=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32600, "message": "Payload must be an object"},
                    }
                ),
                status_code=400,
                media_type="application/json",
            )
        result = private_contacts.handle(payload)
        if result is None:
            return Response(status_code=202)
        return result

    return app


app = create_app()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a local MCP server that demonstrates private contact placeholders."
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
