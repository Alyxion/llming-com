"""Tests for llming_com.command -- CommandRegistry, @command decorator, CommandDef."""

from typing import Optional

import pytest

from llming_com.command import (
    CommandDef,
    CommandError,
    CommandParam,
    CommandRegistry,
    CommandScope,
    _python_type_to_json_type,
    command,
)


# ── CommandRegistry CRUD ─────────────────────────────────────────


class TestCommandRegistry:
    def test_register_and_get(self):
        reg = CommandRegistry()
        cmd = CommandDef(name="test", description="A test", scope=CommandScope.GLOBAL, handler=lambda: None)
        reg.register(cmd)
        assert reg.get("test") is cmd

    def test_get_missing(self):
        reg = CommandRegistry()
        assert reg.get("nonexistent") is None

    def test_list_commands_all(self):
        reg = CommandRegistry()
        reg.register(CommandDef(name="a", description="", scope=CommandScope.GLOBAL, handler=lambda: None))
        reg.register(CommandDef(name="b", description="", scope=CommandScope.SESSION, handler=lambda: None))
        assert len(reg.list_commands()) == 2

    def test_list_commands_filter_by_app(self):
        reg = CommandRegistry()
        reg.register(CommandDef(name="a", description="", scope=CommandScope.GLOBAL, handler=lambda: None, app="lodge"))
        reg.register(CommandDef(name="b", description="", scope=CommandScope.GLOBAL, handler=lambda: None, app="hub"))
        reg.register(CommandDef(name="c", description="", scope=CommandScope.GLOBAL, handler=lambda: None, app=""))
        lodge = reg.list_commands(app_filter="lodge")
        names = [c.name for c in lodge]
        assert "a" in names
        assert "c" in names  # universal
        assert "b" not in names

    def test_list_commands_no_filter_returns_all(self):
        reg = CommandRegistry()
        reg.register(CommandDef(name="x", description="", scope=CommandScope.GLOBAL, handler=lambda: None, app="lodge"))
        assert len(reg.list_commands(app_filter="")) == 1

    def test_by_scope(self):
        reg = CommandRegistry()
        reg.register(CommandDef(name="g", description="", scope=CommandScope.GLOBAL, handler=lambda: None))
        reg.register(CommandDef(name="s", description="", scope=CommandScope.SESSION, handler=lambda: None))
        globals_ = reg.by_scope(CommandScope.GLOBAL)
        sessions = reg.by_scope(CommandScope.SESSION)
        assert len(globals_) == 1
        assert globals_[0].name == "g"
        assert len(sessions) == 1
        assert sessions[0].name == "s"

    def test_register_overwrites(self):
        reg = CommandRegistry()
        cmd1 = CommandDef(name="x", description="first", scope=CommandScope.GLOBAL, handler=lambda: None)
        cmd2 = CommandDef(name="x", description="second", scope=CommandScope.GLOBAL, handler=lambda: None)
        reg.register(cmd1)
        reg.register(cmd2)
        assert reg.get("x").description == "second"
        assert len(reg.list_commands()) == 1


# ── @command decorator ───────────────────────────────────────────


class TestCommandDecorator:
    def test_basic_registration(self):
        reg = CommandRegistry()

        @command("hello", description="Say hello", registry=reg)
        async def hello(controller, name: str):
            pass

        cmd = reg.get("hello")
        assert cmd is not None
        assert cmd.name == "hello"
        assert cmd.description == "Say hello"

    def test_params_extracted(self):
        reg = CommandRegistry()

        @command("greet", registry=reg)
        async def greet(controller, name: str, count: int = 3):
            pass

        cmd = reg.get("greet")
        assert len(cmd.params) == 2
        names = [p.name for p in cmd.params]
        assert "name" in names
        assert "count" in names

    def test_injected_params_excluded(self):
        reg = CommandRegistry()

        @command("test", registry=reg)
        async def test_fn(session_id, entry, controller, request, registry, name: str):
            pass

        cmd = reg.get("test")
        assert len(cmd.params) == 1
        assert cmd.params[0].name == "name"

    def test_required_vs_optional(self):
        reg = CommandRegistry()

        @command("cmd", registry=reg)
        async def cmd(controller, required_param: str, optional_param: str = "default"):
            pass

        c = reg.get("cmd")
        req = next(p for p in c.params if p.name == "required_param")
        opt = next(p for p in c.params if p.name == "optional_param")
        assert req.required is True
        assert opt.required is False
        assert opt.default == "default"

    def test_optional_type_unwrapped(self):
        reg = CommandRegistry()

        @command("cmd", registry=reg)
        async def cmd(controller, name: Optional[str] = None):
            pass

        c = reg.get("cmd")
        assert c.params[0].type is str

    def test_scope_default_session(self):
        reg = CommandRegistry()

        @command("cmd", registry=reg)
        async def cmd(controller):
            pass

        assert reg.get("cmd").scope == CommandScope.SESSION

    def test_scope_global(self):
        reg = CommandRegistry()

        @command("cmd", scope=CommandScope.GLOBAL, registry=reg)
        async def cmd():
            pass

        assert reg.get("cmd").scope == CommandScope.GLOBAL

    def test_http_method(self):
        reg = CommandRegistry()

        @command("cmd", http_method="GET", registry=reg)
        async def cmd():
            pass

        assert reg.get("cmd").http_method == "GET"

    def test_tags(self):
        reg = CommandRegistry()

        @command("cmd", tags=["admin", "debug"], registry=reg)
        async def cmd():
            pass

        assert reg.get("cmd").tags == ["admin", "debug"]

    def test_command_def_attached_to_function(self):
        reg = CommandRegistry()

        @command("cmd", registry=reg)
        async def cmd():
            pass

        assert hasattr(cmd, "_command_def")
        assert cmd._command_def.name == "cmd"

    def test_uses_docstring_as_description(self):
        reg = CommandRegistry()

        @command("cmd", registry=reg)
        async def cmd():
            """This is a docstring."""
            pass

        assert reg.get("cmd").description == "This is a docstring."


# ── CommandDef.input_schema ──────────────────────────────────────


class TestInputSchema:
    def test_basic_schema(self):
        cmd = CommandDef(
            name="test", description="", scope=CommandScope.GLOBAL, handler=lambda: None,
            params=[CommandParam(name="text", type=str, required=True)],
        )
        schema = cmd.input_schema()
        assert schema["type"] == "object"
        assert "text" in schema["properties"]
        assert "text" in schema["required"]

    def test_session_scope_adds_session_id(self):
        cmd = CommandDef(
            name="test", description="", scope=CommandScope.SESSION, handler=lambda: None,
            params=[CommandParam(name="msg", type=str, required=True)],
        )
        schema = cmd.input_schema()
        assert "session_id" in schema["properties"]
        assert "session_id" in schema["required"]

    def test_global_scope_no_session_id(self):
        cmd = CommandDef(
            name="test", description="", scope=CommandScope.GLOBAL, handler=lambda: None,
            params=[],
        )
        schema = cmd.input_schema()
        assert "session_id" not in schema.get("properties", {})


# ── CommandDef.to_dict ───────────────────────────────────────────


class TestToDict:
    def test_serialization(self):
        cmd = CommandDef(
            name="send", description="Send msg", scope=CommandScope.SESSION,
            handler=lambda: None, http_method="POST", tags=["chat"],
            params=[CommandParam(name="text", type=str, required=True)],
        )
        d = cmd.to_dict()
        assert d["name"] == "send"
        assert d["description"] == "Send msg"
        assert d["scope"] == "session"
        assert d["http_method"] == "POST"
        assert d["tags"] == ["chat"]
        assert len(d["params"]) == 1
        assert d["params"][0]["name"] == "text"
        assert d["params"][0]["json_type"] == "string"
        assert d["params"][0]["required"] is True


# ── CommandError ─────────────────────────────────────────────────


class TestCommandError:
    def test_status_and_detail(self):
        err = CommandError(404, "Not found")
        assert err.status_code == 404
        assert err.detail == "Not found"
        assert str(err) == "Not found"

    def test_is_exception(self):
        err = CommandError(500, "Fail")
        assert isinstance(err, Exception)


# ── Type mapping ─────────────────────────────────────────────────


class TestPythonTypeToJsonType:
    def test_str(self):
        assert _python_type_to_json_type(str) == "string"

    def test_int(self):
        assert _python_type_to_json_type(int) == "integer"

    def test_float(self):
        assert _python_type_to_json_type(float) == "number"

    def test_bool(self):
        assert _python_type_to_json_type(bool) == "boolean"

    def test_list(self):
        assert _python_type_to_json_type(list) == "array"
        assert _python_type_to_json_type(list[str]) == "array"

    def test_dict(self):
        assert _python_type_to_json_type(dict) == "object"
        assert _python_type_to_json_type(dict[str, int]) == "object"

    def test_unknown_defaults_to_string(self):
        assert _python_type_to_json_type(bytes) == "string"
