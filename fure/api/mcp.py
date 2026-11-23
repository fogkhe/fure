"""Authenticated, dynamic MCP tools backed by the regular execution service."""

from uuid import UUID

from fastapi import FastAPI
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.providers import Provider
from fastmcp.tools import Tool, ToolResult
from pydantic import PrivateAttr

from fure.api.v1.presenters import run_result_response
from fure.core.auth import Permission, Principal
from fure.core.function import Function
from fure.database.repositories import FunctionRepository
from fure.services.auth import AuthService, require
from fure.services.errors import ServiceError, Unauthorized


class APIKeyVerifier(TokenVerifier):
    def __init__(self, app: FastAPI):
        super().__init__(required_scopes=[Permission.MCP_ACCESS.value])
        self.app = app

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            principal = await AuthService(self.app.state.database).authenticate(
                token, api_key=True
            )
        except Unauthorized:
            return None
        return AccessToken(
            token=token,
            client_id=str(principal.key_id),
            subject=str(principal.user.id),
            scopes=sorted(principal.permissions),
        )


async def current_principal(app: FastAPI) -> Principal:
    token = get_access_token()
    if token is None:
        raise ToolError("API key required")
    principal = await AuthService(app.state.database).authenticate(
        token.token, api_key=True
    )
    require(principal, Permission.MCP_ACCESS)
    return principal


class FunctionTool(Tool):
    _app: FastAPI = PrivateAttr()
    _function_id: UUID = PrivateAttr()
    _null_input: bool = PrivateAttr()

    def __init__(self, app: FastAPI, function: Function):
        schema = function.input_schema
        null_input = schema.get("type") == "null"
        # MCP arguments are always objects, including for a function(input: None).
        parameters = (
            {"type": "object", "properties": {}, "additionalProperties": False}
            if null_input
            else {**schema, "type": "object"}
        )
        super().__init__(
            name=function.name, description=function.description, parameters=parameters
        )
        self._app = app
        self._function_id = function.id
        self._null_input = null_input

    async def run(self, arguments: dict) -> ToolResult:
        try:
            principal = await current_principal(self._app)
            require(principal, Permission.FUNCTIONS_RUN)
            if self._null_input and arguments:
                raise ToolError("This function accepts no arguments")
            run = await self._app.state.executions.execute(
                self._function_id,
                None if self._null_input else arguments,
                principal=principal,
            )
            result = run_result_response(run, principal).model_dump(mode="json")
            return ToolResult(structured_content={**result, "run_id": str(run.id)})
        except ServiceError as error:
            result = {"detail": str(error), "code": error.code}
            if error.run_id is not None:
                result["run_id"] = str(error.run_id)
            return ToolResult(structured_content=result, is_error=True)


class FunctionProvider(Provider):
    def __init__(self, app: FastAPI):
        super().__init__()
        self.app = app

    async def get_tasks(self):
        # Tools are request-scoped and run through ExecutionService, not Docket.
        return []

    async def _list_tools(self):
        principal = await current_principal(self.app)
        if not principal.allows(Permission.FUNCTIONS_READ):
            return []
        async with self.app.state.database.sessions() as session:
            functions, _ = await FunctionRepository(session).list(
                page=0,
                size=1000,
                enabled=True,
                function_ids=principal.function_ids or frozenset(),
            )
        return [
            FunctionTool(self.app, function)
            for function in functions
            if function.runnable
        ]

    async def _get_tool(self, name, version=None):
        principal = await current_principal(self.app)
        if not principal.permissions.intersection(
            {Permission.FUNCTIONS_READ, Permission.FUNCTIONS_RUN}
        ):
            return None
        async with self.app.state.database.sessions() as session:
            function = await FunctionRepository(session).by_name(name)
        if (
            function is None
            or not function.runnable
            or not principal.allows_function(function.id)
        ):
            return None
        return FunctionTool(self.app, function)


def create_mcp_app(app: FastAPI):
    server = FastMCP(
        "Fure",
        auth=APIKeyVerifier(app),
        providers=[FunctionProvider(app)],
        mask_error_details=True,
    )
    return server.http_app(path="/", stateless_http=True, json_response=True)
