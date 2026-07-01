"""REST endpoints the Odoo gatekeeper calls to launch AI workflows.

The workflow's first leg includes an LLM call, so we acknowledge with ``202``
immediately and run the graph (up to the human-approval interrupt) as a tracked
background task. The pre-generated ``run_id`` is returned synchronously so Odoo
can store it on the ``ai.ops.task`` record.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, status

from ..schemas import FraudTaskRequest, ReconciliationTaskRequest, TaskAccepted
from ..security import require_bearer

router = APIRouter(prefix="/v1/tasks", tags=["tasks"], dependencies=[Depends(require_bearer)])


@router.post("/fraud", response_model=TaskAccepted, status_code=status.HTTP_202_ACCEPTED)
async def start_fraud(req: FraudTaskRequest, request: Request) -> TaskAccepted:
    runtime = request.app.state.runtime
    run_id = f"fr-{uuid.uuid4()}"
    runtime.spawn(runtime._log_task_error(run_id)(runtime.start_fraud(req, run_id=run_id)))
    return TaskAccepted(run_id=run_id, odoo_task_ref=req.odoo_task_ref)


@router.post("/reconciliation", response_model=TaskAccepted, status_code=status.HTTP_202_ACCEPTED)
async def start_reconciliation(req: ReconciliationTaskRequest, request: Request) -> TaskAccepted:
    runtime = request.app.state.runtime
    run_id = f"rc-{uuid.uuid4()}"
    runtime.spawn(runtime._log_task_error(run_id)(runtime.start_reconciliation(req, run_id=run_id)))
    return TaskAccepted(run_id=run_id, odoo_task_ref=req.odoo_task_ref)
