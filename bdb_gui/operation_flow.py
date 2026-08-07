from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .current_operation import OperationDetails


FlowStatus = Literal["pending", "active", "success", "failed"]


@dataclass(frozen=True)
class OperationFlowStep:
    key: str
    label: str
    status: FlowStatus
    detail: str


@dataclass(frozen=True)
class OperationFlow:
    overall_status: FlowStatus
    summary: str
    steps: tuple[OperationFlowStep, ...]

    def status_for(self, key: str) -> FlowStatus:
        for step in self.steps:
            if step.key == key:
                return step.status
        raise KeyError(key)


def build_operation_flow(operation: OperationDetails) -> OperationFlow:
    """Translate the read-only Journal projection into a small user-facing flow.

    The function does not read files, execute commands, or infer rollback/promotion
    that is not present in the supplied projection.
    """

    if operation.status is not None:
        return _build_canonical_operation_flow(operation)

    state = operation.state.strip().lower()
    result = (operation.result_status or "").strip().lower()
    has_error = bool(operation.error_code) or result in {"failed", "error"}

    accepted = "success"
    workspace = _workspace_status(state, operation.workspace_revision)
    editing = _editing_status(state, has_error)
    testing = _testing_status(state, result, has_error)
    result_step = _result_status(state, result, has_error)
    completion = _completion_status(state, result, has_error)

    steps = (
        OperationFlowStep("accepted", "Zadanie przyjęte", accepted, operation.command_id),
        OperationFlowStep(
            "workspace",
            "Izolowany workspace",
            workspace,
            _workspace_detail(operation),
        ),
        OperationFlowStep(
            "editing",
            "Zmiana kodu",
            editing,
            operation.operation or "Oczekiwanie na opis operacji",
        ),
        OperationFlowStep(
            "testing",
            "Testy",
            testing,
            operation.profile_id or "Profil testowy nie został jeszcze zapisany",
        ),
        OperationFlowStep(
            "result",
            "Wynik i checkpoint",
            result_step,
            _result_detail(operation),
        ),
        OperationFlowStep(
            "completion",
            "Zakończenie sesji",
            completion,
            operation.session_state or "Stan sesji niedostępny",
        ),
    )

    overall = _overall_status(state, result, has_error)
    return OperationFlow(overall, _summary(overall, operation), steps)


def _build_canonical_operation_flow(operation: OperationDetails) -> OperationFlow:
    status = operation.status or {}
    execution = str(status.get("execution", "")).strip().lower()
    result = str(status.get("result", "")).strip().lower()
    promotion = str(status.get("promotion", "")).strip().lower()
    delivery = str(status.get("delivery", "")).strip().lower()
    session = str(status.get("session", "")).strip().lower()
    terminal = status.get("terminal") is True
    state = operation.state.strip().lower()

    failed = execution == "failed"
    promotion_blocked = promotion == "blocked"

    if failed:
        overall: FlowStatus = "failed"
        summary = f"Błąd operacji — {operation.error_code or operation.state}."
    elif promotion_blocked:
        overall = "failed"
        summary = "Promocja zmian jest zablokowana. Wynik istnieje, ale nie ma prawidłowego potwierdzenia promocji."
    elif terminal:
        overall = "success"
        summary = "Operacja zakończona. Wynik został dostarczony i promocja jest zakończona."
    elif promotion == "pending" and result == "published":
        overall = "active"
        summary = "Promowanie zmian — wykonanie i testy są zakończone, oczekujemy na potwierdzenie promocji."
    elif execution == "running" and state in {"effect_recorded", "result_staged"}:
        overall = "active"
        summary = "Trwa testowanie i utrwalanie wyniku operacji."
    elif execution == "running":
        overall = "active"
        summary = "Trwa wykonywanie operacji."
    elif execution == "queued":
        overall = "active"
        summary = "Operacja oczekuje na rozpoczęcie wykonania."
    else:
        overall = "active"
        summary = "Operacja oczekuje na zakończenie pozostałych etapów."

    if failed:
        editing: FlowStatus = "failed"
    elif execution == "running" and state == "executing":
        editing = "active"
    elif execution == "succeeded" or state in {"effect_recorded", "result_staged", "result_published", "acknowledged"}:
        editing = "success"
    else:
        editing = "pending"

    if failed:
        testing: FlowStatus = "failed" if result != "published" else "success"
    elif result in {"staged", "published"} or execution == "succeeded":
        testing = "success"
    elif execution == "running" and state in {"effect_recorded", "result_staged"}:
        testing = "active"
    else:
        testing = "pending"

    if failed:
        result_step: FlowStatus = "failed"
    elif result == "published":
        result_step = "success"
    elif result == "staged":
        result_step = "active"
    else:
        result_step = "pending"

    if promotion_blocked:
        promotion_step: FlowStatus = "failed"
    elif promotion in {"promoted", "not_required"}:
        promotion_step = "success"
    elif promotion == "pending" and result == "published":
        promotion_step = "active"
    else:
        promotion_step = "pending"

    if failed or promotion_blocked:
        completion: FlowStatus = "failed"
    elif terminal:
        completion = "success"
    elif session == "completing":
        completion = "active"
    else:
        completion = "pending"

    steps = (
        OperationFlowStep("accepted", "Zadanie przyjęte", "success", operation.command_id),
        OperationFlowStep(
            "workspace",
            "Izolowany workspace",
            "active" if execution == "queued" else "success",
            _workspace_detail(operation),
        ),
        OperationFlowStep("editing", "Wykonywanie zmiany", editing, operation.state),
        OperationFlowStep(
            "testing",
            "Testy",
            testing,
            f"execution={execution}, result={result}",
        ),
        OperationFlowStep(
            "result",
            "Wynik",
            result_step,
            operation.result_status or result or "brak wyniku",
        ),
        OperationFlowStep(
            "promotion",
            "Promocja",
            promotion_step,
            f"promotion={promotion}, delivery={delivery}",
        ),
        OperationFlowStep(
            "completion",
            "Zakończenie",
            completion,
            f"session={session}, terminal={str(terminal).lower()}",
        ),
    )
    return OperationFlow(overall, summary, steps)


def empty_operation_flow() -> OperationFlow:
    return OperationFlow(
        "pending",
        "Brak aktywnej operacji do pokazania.",
        tuple(
            OperationFlowStep(key, label, "pending", "—")
            for key, label in (
                ("accepted", "Zadanie przyjęte"),
                ("workspace", "Izolowany workspace"),
                ("editing", "Zmiana kodu"),
                ("testing", "Testy"),
                ("result", "Wynik i checkpoint"),
                ("completion", "Zakończenie sesji"),
            )
        ),
    )


def _workspace_status(state: str, revision: int | None) -> FlowStatus:
    if revision is not None or state in {
        "claimed",
        "executing",
        "effect_recorded",
        "result_staged",
        "result_published",
        "completed",
        "failed",
    }:
        return "success"
    if state in {"discovered", "validated"}:
        return "active"
    return "pending"


def _editing_status(state: str, has_error: bool) -> FlowStatus:
    if state == "executing":
        return "active"
    if state in {"effect_recorded", "result_staged", "result_published", "completed"}:
        return "success"
    if state == "failed" or has_error:
        return "failed"
    return "pending"


def _testing_status(state: str, result: str, has_error: bool) -> FlowStatus:
    if result == "success":
        return "success"
    if has_error:
        return "failed"
    if state in {"effect_recorded", "result_staged", "result_published"}:
        return "active"
    return "pending"


def _result_status(state: str, result: str, has_error: bool) -> FlowStatus:
    if result == "success" or state in {"result_published", "completed"}:
        return "success"
    if has_error or state == "failed":
        return "failed"
    if state == "result_staged":
        return "active"
    return "pending"


def _completion_status(state: str, result: str, has_error: bool) -> FlowStatus:
    if has_error or state == "failed":
        return "failed"
    if state == "completed" or (state == "result_published" and result == "success"):
        return "success"
    if state == "result_published":
        return "active"
    return "pending"


def _overall_status(state: str, result: str, has_error: bool) -> FlowStatus:
    if has_error or state == "failed":
        return "failed"
    if state == "completed" or (state == "result_published" and result == "success"):
        return "success"
    return "active"


def _workspace_detail(operation: OperationDetails) -> str:
    if operation.workspace_revision is None:
        return "Workspace nie ma jeszcze zapisanej rewizji"
    return f"Rewizja {operation.workspace_revision}"


def _result_detail(operation: OperationDetails) -> str:
    if operation.error_code:
        return f"Błąd: {operation.error_code}"
    if operation.result_status:
        return f"Status wyniku: {operation.result_status}"
    return "Wynik nie został jeszcze zapisany"


def _summary(status: FlowStatus, operation: OperationDetails) -> str:
    if status == "success":
        return "Operacja zakończyła się powodzeniem według read-only projekcji Journalu."
    if status == "failed":
        detail = operation.error_code or operation.result_status or operation.state
        return f"Operacja wymaga uwagi: {detail}."
    return f"Operacja jest w toku: {operation.state}."
