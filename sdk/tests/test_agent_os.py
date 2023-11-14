from __future__ import annotations

import asyncio
import contextlib
from asyncio import get_event_loop
from typing import Annotated, Type

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from httpx import AsyncClient
from pydantic import BaseModel, Field

from eidolon_sdk.agent_memory import AgentMemory, SymbolicMemory
from eidolon_sdk.agent import CodeAgent, register, Agent
from eidolon_sdk.agent_machine import AgentMachine
from eidolon_sdk.agent_os import AgentOS
from eidolon_sdk.agent_program import AgentProgram
from eidolon_sdk.impl.local_symbolic_memory import LocalSymbolicMemory


@pytest.fixture
def app():
    return FastAPI()


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
async def async_client(app):
    async with AsyncClient(app=app, base_url="http://test") as client:
        yield client


@pytest.fixture
def os_manager(app):
    @contextlib.contextmanager
    def fn(*agents: Type[Agent], memory_override: SymbolicMemory = None):
        programs = [AgentProgram(
            name=agent.__name__.lower(),
            implementation="tests.test_agent_os." + agent.__qualname__
        ) for agent in agents]
        mem = memory_override or LocalSymbolicMemory(
            implementation=LocalSymbolicMemory.__module__ + "." + LocalSymbolicMemory.__qualname__)
        machine = AgentMachine(agent_memory=AgentMemory(
            symbolic_memory=mem
        ), agent_io={}, agent_programs=programs)
        os = AgentOS(machine=machine, machine_yaml="")
        os.start(app)
        try:
            yield
        finally:
            os.stop()

    return fn


class HelloWorldResponse(BaseModel):
    question: str
    answer: str


class HelloWorld(CodeAgent):
    counter = 0  # todo, this is a hack to make sure function is called. Should wrap with mock instead

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        HelloWorld.counter = 0

    @register(state="idle", transition_to=['idle', 'terminated'])
    async def idle(self, question: Annotated[
        str, Field(description="The question to ask. Can be anything, but it better be hello")]):
        HelloWorld.counter += 1
        if question == "hello":
            return HelloWorldResponse(question=question, answer="world")
        elif question == "exception":
            raise Exception("some unexpected error")
        else:
            raise HTTPException(status_code=501, detail="huge system error handling unprecedented edge case")


class ParamTester(CodeAgent):
    last_call = None

    @register(state="idle")
    async def foo(self, x: int, y: int = 5, z: Annotated[int, Field(description="z is a param")] = 10):
        ParamTester.last_call = (x, y, z)
        return dict(x=x, y=y, z=z)


@pytest.fixture(autouse=True)
def memory():
    return LocalSymbolicMemory(implementation=LocalSymbolicMemory.__module__ + "." + LocalSymbolicMemory.__qualname__)


def test_empty_start(client, os_manager):
    with os_manager():
        docs = client.get("/docs")
        assert docs.status_code == 200


def test_program(client, os_manager):
    with os_manager(HelloWorld):
        response = client.post("/helloworld", json=dict(question="hello"))
        assert response.status_code == 202


def test_requests_to_running_process(client, os_manager):
    with os_manager(HelloWorld):
        pid = client.post("/helloworld", json=dict(question="hello")).json()['process_id']
        response = client.post(f"/helloworld/{pid}/idle", json=dict(question="hello"))
        assert response.status_code == 202


def test_program_actually_calls_code(client, os_manager):
    with os_manager(HelloWorld):
        client.post("/helloworld", json=dict(question="hello"))
        assert HelloWorld.counter == 1


def test_non_annotated_params(client, os_manager):
    with os_manager(ParamTester):
        response = client.post("/paramtester", json=dict(x=1, y=2, z=3))
        assert response.status_code == 202
        assert ParamTester.last_call == (1, 2, 3)


def test_defaults(client, os_manager):
    with os_manager(ParamTester):
        response = client.post("/paramtester", json=dict(x=1))
        assert response.status_code == 202
        assert ParamTester.last_call == (1, 5, 10)


def test_required_param_missing(client, os_manager):
    with os_manager(ParamTester):
        response = client.post("/paramtester", json=dict())
        assert response.status_code == 422


@pytest.mark.parametrize("use_mongo", [False, True])
@pytest.mark.asyncio
async def test_retrieve_result(async_client, os_manager, symbolic_memory, use_mongo):
    with os_manager(HelloWorld, memory_override=symbolic_memory if use_mongo else None):
        post = await async_client.post("/helloworld", json=dict(question="hello"))
        response = await async_client.get(f"/helloworld/{post.json()['process_id']}")
        assert response.status_code == 200
        assert response.json()['data'] == dict(question="hello", answer="world")


@pytest.mark.parametrize("use_mongo", [False, True])
@pytest.mark.asyncio
async def test_program_http_error(async_client, os_manager, symbolic_memory, use_mongo):
    with os_manager(HelloWorld, memory_override=symbolic_memory if use_mongo else None):
        post = await async_client.post("/helloworld", json=dict(question="hola"))
        response = await async_client.get(f"/helloworld/{post.json()['process_id']}")
        assert response.status_code == 501
        assert response.json()['detail'] == "huge system error handling unprecedented edge case"


def test_program_error(client, os_manager):
    with os_manager(HelloWorld):
        pid = client.post("/helloworld", json=dict(question="exception")).json()['process_id']
        response = client.get(f"/helloworld/{pid}")
        assert response.status_code == 500
        assert response.json()['error'] == "some unexpected error"


class MemTester(CodeAgent):
    @register(state="idle")
    async def add(self, x: int):
        await self.agent_memory.symbolic_memory.insert_one("test", dict(x=x))


# todo, lets parameterize this one with mongo as well
@pytest.mark.parametrize("use_mongo", [False, True])
@pytest.mark.asyncio
async def test_programs_can_call_memory(async_client, os_manager, memory, symbolic_memory, use_mongo):
    if use_mongo:
        memory = symbolic_memory
    with os_manager(MemTester, memory_override=memory):
        await async_client.post("/memtester", json=dict(x=1))
        found = await memory.find_one("test", dict(x=1))
        assert found
        assert found['x'] == 1
