"""A2A version-1 intent commands mapped to durable procurement tasks."""
import json
from uuid import uuid4

from google.protobuf.json_format import MessageToDict

from . import audit
from .canonical import canonical_json
from .db import DomainError
from .foundation import correlation_id
from .purchase_intent_integrity import IntentRequest, Amendment

COMMANDS = {"create_intent", "get_intent", "amend_intent", "resume_intent"}


def send(service, params, principal, mandate):
    from .a2a import t, artifact, command_data, metadata
    from .purchase_intent_surfaces import authority
    from hashlib import sha256
    message = params.message
    parts = [MessageToDict(p)["data"] for p in message.parts if p.WhichOneof("content") == "data"]
    if len(parts) != 1:
        return None
    command = command_data(parts[0])
    name = command.get("command")
    if name not in COMMANDS:
        return None
    if command.get("version") != 1 or not message.message_id or message.role != t.Role.ROLE_USER:
        raise DomainError("VALIDATION_ERROR", "A version-1 intent command and user message id are required", 422)
    authority(principal, name != "get_intent")
    args = command.get("arguments", {})
    scope = f"{principal.kind}:{principal.id}"
    digest = sha256(canonical_json(MessageToDict(message))).hexdigest()
    with service.db.connect(write=True) as conn:
        previous = conn.execute("SELECT * FROM a2a_messages WHERE owner=? AND message_id=?", (scope, message.message_id)).fetchone()
        if previous:
            if previous["digest"] != digest:
                raise DomainError("IDEMPOTENCY_CONFLICT", "Message id belongs to another command", 409)
            task = service._load(conn, previous["task_id"], principal)
            result = service.gateway.intents.get(metadata(task)["intent_id"], principal.id, mandate)
        else:
            intents = service.gateway.intents
            if name == "create_intent":
                result = intents.create(IntentRequest.model_validate(args.get("request", args)), principal.id, mandate)
            elif name == "get_intent":
                result = intents.get(args["intent_id"], principal.id, mandate)
            elif name == "resume_intent":
                result = intents.resume(args["intent_id"], args["expected_version"], principal.id, mandate)
            else:
                result = intents.amend(args["intent_id"], Amendment(request=args["request"], expected_version=args["expected_version"]), principal.id, mandate)
            task = service._load(conn, message.task_id, principal) if message.task_id else t.Task(id=uuid4().hex, context_id=message.context_id or uuid4().hex)
            if metadata(task).get("intent_id") not in {None, result["id"]}:
                raise DomainError("IDEMPOTENCY_CONFLICT", "Task belongs to another intent", 409)
            task.history.append(message)
            task.metadata.update({"owner": principal.id, "owner_scope": scope, "intent_id": result["id"],
                "correlation_id": correlation_id(), "context": {"customer_id": result["request"]["customer_id"], "environment": service.gateway.settings.environment}})
            service._save(conn, task, principal.id)
            conn.execute("INSERT INTO a2a_messages VALUES(?,?,?,?)", (scope, message.message_id, digest, task.id))
        state = task_state(result)
        task.metadata.update({"intent_version": result["version"]})
        task.artifacts.clear()
        artifact(task, "purchase_intent", result)
        service._update(conn, task, state, "Purchase intent " + result["state"], result)
        return task


def task_state(result):
    from .a2a import t
    return (t.TaskState.TASK_STATE_COMPLETED if result["state"] in {"CONFIRMED", "CLOSED"} else
                 t.TaskState.TASK_STATE_CANCELED if result["state"] in {"CANCELLED", "EXPIRED"} else
                 t.TaskState.TASK_STATE_AUTH_REQUIRED if result["next_action"] in {"WAIT_FOR_APPROVAL", "CONTACT_ADMIN"} else
                 t.TaskState.TASK_STATE_WORKING if result["state"] in {"ORDERED", "SUBMISSION_UNKNOWN", "CANCEL_REQUESTED"} else t.TaskState.TASK_STATE_INPUT_REQUIRED)


def refresh(service, task, principal):
    from .a2a import metadata, artifact
    id = metadata(task).get("intent_id")
    if not id:
        return task
    result = service.gateway.intents.get(id, principal.id)
    if int(metadata(task).get("intent_version", 0)) == result["version"]:
        return task
    with service.db.connect(write=True) as conn:
        task.metadata.update({"intent_version": result["version"]})
        task.artifacts.clear()
        artifact(task, "purchase_intent", result)
        service._update(conn, task, task_state(result), "Purchase intent " + result["state"], result)
    return task
