from __future__ import annotations

from datetime import datetime, timezone
import uuid

from psycopg.types.json import Json

from app.db import ensure_schema, get_connection


RUN_STATUS_QUEUED = "queued"
RUN_STATUS_IN_PROGRESS = "in_progress"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"

TASK_STATUS_QUEUED = "queued"
TASK_STATUS_IN_PROGRESS = "in_progress"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_SKIPPED = "skipped"
TASK_STATUS_CANCELLED = "cancelled"

PROPOSAL_STATUS_PROPOSED = "proposed"
PROPOSAL_STATUS_PROMOTED = "promoted"
PROPOSAL_STATUS_REJECTED = "rejected"
PROPOSAL_STATUS_NEEDS_REVIEW = "needs_review"

VALIDATION_OUTCOME_PASSED = "passed"
VALIDATION_OUTCOME_FAILED = "failed"
VALIDATION_OUTCOME_NEEDS_REVIEW = "needs_review"

REVIEW_STATUS_PENDING = "pending"
REVIEW_STATUS_APPROVED = "approved"
REVIEW_STATUS_REJECTED = "rejected"

WORKFLOW_OWNERSHIP_V1 = "newark_parcel_ownership_v1"
WORKFLOW_LEAD_SYNTHESIS_V1 = "newark_lead_synthesis_v1"
WORKFLOW_CONTACT_ENRICHMENT_V1 = "newark_contact_enrichment_v1"


def _utc_now():
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return "%s-%s" % (prefix, uuid.uuid4().hex)


def _json_ready(value):
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except TypeError:
            return value
    return value


def _iso(value):
    return value.isoformat() if value else None


def _review_state_for_run(cursor, run_id):
    cursor.execute(
        """
        SELECT
            CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM agent_reviews
                    WHERE agent_run_id = %s AND status = %s
                ) THEN %s
                WHEN EXISTS (
                    SELECT 1
                    FROM agent_reviews
                    WHERE agent_run_id = %s AND status = %s
                ) THEN %s
                WHEN EXISTS (
                    SELECT 1
                    FROM agent_reviews
                    WHERE agent_run_id = %s AND status = %s
                ) THEN %s
                ELSE 'not_required'
            END AS review_state
        """,
        (
            run_id,
            REVIEW_STATUS_PENDING,
            REVIEW_STATUS_PENDING,
            run_id,
            REVIEW_STATUS_REJECTED,
            REVIEW_STATUS_REJECTED,
            run_id,
            REVIEW_STATUS_APPROVED,
            REVIEW_STATUS_APPROVED,
        ),
    )
    row = cursor.fetchone()
    return row["review_state"] if row else "not_required"


def _row_to_event_payload(event):
    return {
        "id": event["id"],
        "step_name": event["step_name"],
        "status": event["status"],
        "message": event["message"],
        "payload": event["payload"] or {},
        "created_at": _iso(event["created_at"]),
    }


def _row_to_task_payload(row):
    return {
        "id": row["id"],
        "agent_run_id": row["agent_run_id"],
        "parent_task_id": row["parent_task_id"],
        "task_type": row["task_type"],
        "status": row["status"],
        "attempt": row["attempt"],
        "input_payload": row["input_payload"] or {},
        "output_payload": row["output_payload"] or {},
        "error_message": row["error_message"],
        "started_at": _iso(row["started_at"]),
        "completed_at": _iso(row["completed_at"]),
        "created_at": _iso(row["created_at"]),
    }


def _row_to_artifact_payload(row):
    return {
        "id": row["id"],
        "agent_run_id": row["agent_run_id"],
        "produced_by_task_id": row["produced_by_task_id"],
        "artifact_type": row["artifact_type"],
        "payload": row["payload"] or {},
        "source_document_id": row["source_document_id"],
        "created_at": _iso(row["created_at"]),
    }


def _row_to_validation_payload(row):
    return {
        "id": row["id"],
        "agent_run_id": row["agent_run_id"],
        "proposal_id": row["proposal_id"],
        "validator_name": row["validator_name"],
        "outcome": row["outcome"],
        "message": row["message"],
        "payload": row["payload"] or {},
        "created_at": _iso(row["created_at"]),
    }


def _row_to_review_payload(row):
    return {
        "id": row["id"],
        "agent_run_id": row["agent_run_id"],
        "proposal_id": row["proposal_id"],
        "status": row["status"],
        "reviewer": row["reviewer"],
        "notes": row["notes"],
        "decision_payload": row["decision_payload"] or {},
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _proposal_with_children(cursor, row):
    cursor.execute(
        """
        SELECT *
        FROM agent_validations
        WHERE proposal_id = %s
        ORDER BY created_at, id
        """,
        (row["id"],),
    )
    validations = [_row_to_validation_payload(item) for item in cursor.fetchall()]
    cursor.execute(
        """
        SELECT *
        FROM agent_reviews
        WHERE proposal_id = %s
        ORDER BY created_at DESC
        """,
        (row["id"],),
    )
    review = cursor.fetchone()
    return {
        "id": row["id"],
        "agent_run_id": row["agent_run_id"],
        "produced_by_task_id": row["produced_by_task_id"],
        "proposal_type": row["proposal_type"],
        "status": row["status"],
        "review_state": row["review_state"],
        "decision": row["decision"],
        "payload": row["payload"] or {},
        "evidence_refs": row["evidence_refs"] or [],
        "promoted_at": _iso(row["promoted_at"]),
        "created_at": _iso(row["created_at"]),
        "validations": validations,
        "review": _row_to_review_payload(review) if review else None,
    }


def _run_payload(cursor, row, include_events=True):
    review_state = _review_state_for_run(cursor, row["id"])
    cursor.execute("SELECT COUNT(*) AS count FROM agent_tasks WHERE agent_run_id = %s", (row["id"],))
    task_count = int(cursor.fetchone()["count"])
    cursor.execute("SELECT COUNT(*) AS count FROM agent_artifacts WHERE agent_run_id = %s", (row["id"],))
    artifact_count = int(cursor.fetchone()["count"])
    cursor.execute("SELECT COUNT(*) AS count FROM agent_proposals WHERE agent_run_id = %s", (row["id"],))
    proposal_count = int(cursor.fetchone()["count"])
    cursor.execute(
        "SELECT COUNT(*) AS count FROM agent_reviews WHERE agent_run_id = %s AND status = %s",
        (row["id"], REVIEW_STATUS_PENDING),
    )
    pending_review_count = int(cursor.fetchone()["count"])
    events = []
    if include_events:
        cursor.execute(
            """
            SELECT id, step_name, status, message, payload, created_at
            FROM agent_run_events
            WHERE agent_run_id = %s
            ORDER BY created_at, id
            """,
            (row["id"],),
        )
        events = [_row_to_event_payload(item) for item in cursor.fetchall()]
    return {
        "id": row["id"],
        "market_id": row["market_id"],
        "entity_type": row["entity_type"],
        "entity_id": row["entity_id"],
        "workflow_type": row["workflow_type"],
        "status": row["status"],
        "current_step": row["current_step"],
        "review_state": review_state,
        "requested_at": _iso(row["requested_at"]),
        "started_at": _iso(row["started_at"]),
        "completed_at": _iso(row["completed_at"]),
        "error_message": row["error_message"],
        "state_payload": row["state_payload"] or {},
        "task_count": task_count,
        "artifact_count": artifact_count,
        "proposal_count": proposal_count,
        "pending_review_count": pending_review_count,
        "events": events,
    }


def _get_open_run(cursor, market_id, entity_type, entity_id, workflow_type):
    cursor.execute(
        """
        SELECT *
        FROM agent_runs
        WHERE market_id = %s
          AND entity_type = %s
          AND entity_id = %s
          AND workflow_type = %s
          AND status IN (%s, %s)
        ORDER BY requested_at DESC
        LIMIT 1
        """,
        (
            market_id,
            entity_type,
            entity_id,
            workflow_type,
            RUN_STATUS_QUEUED,
            RUN_STATUS_IN_PROGRESS,
        ),
    )
    return cursor.fetchone()


def enqueue_agent_run(connection, market_id, entity_type, entity_id, workflow_type, state_payload=None):
    ensure_schema(connection)
    with connection.cursor() as cursor:
        existing = _get_open_run(cursor, market_id, entity_type, entity_id, workflow_type)
        if existing:
            connection.commit()
            return {"run_id": existing["id"], "created": False}

        run_id = _new_id("agent-run")
        payload = _json_ready(state_payload or {})
        cursor.execute(
            """
            INSERT INTO agent_runs (
                id,
                market_id,
                entity_type,
                entity_id,
                workflow_type,
                status,
                current_step,
                requested_at,
                state_payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                market_id,
                entity_type,
                entity_id,
                workflow_type,
                RUN_STATUS_QUEUED,
                "queued",
                _utc_now(),
                Json(payload),
            ),
        )
        cursor.execute(
            """
            INSERT INTO agent_run_events (agent_run_id, step_name, status, message, payload)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (run_id, "queued", RUN_STATUS_QUEUED, "Agent run queued.", Json(payload)),
        )
    connection.commit()
    return {"run_id": run_id, "created": True}


def get_agent_run(connection, run_id):
    ensure_schema(connection)
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM agent_runs WHERE id = %s", (run_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return _run_payload(cursor, row, include_events=True)


def list_agent_runs(
    connection,
    workflow_type=None,
    status=None,
    entity_id=None,
    review_state=None,
    persona=None,
    promotion_state=None,
    source_family=None,
    limit=100,
):
    ensure_schema(connection)
    clauses = ["1=1"]
    params = {"limit": limit}
    if workflow_type:
        clauses.append("workflow_type = %(workflow_type)s")
        params["workflow_type"] = workflow_type
    if status:
        clauses.append("status = %(status)s")
        params["status"] = status
    if entity_id:
        clauses.append("entity_id = %(entity_id)s")
        params["entity_id"] = entity_id
    if persona:
        clauses.append("state_payload ->> 'persona' = %(persona)s")
        params["persona"] = persona
    if promotion_state:
        clauses.append("state_payload ->> 'promotion_state' = %(promotion_state)s")
        params["promotion_state"] = promotion_state
    sql = """
        SELECT *
        FROM agent_runs
        WHERE
    """ + " AND ".join(clauses) + """
        ORDER BY requested_at DESC
        LIMIT %(limit)s
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        payloads = [_run_payload(cursor, row, include_events=False) for row in rows]
    if source_family:
        payloads = [
            row
            for row in payloads
            if source_family in (row["state_payload"].get("source_families") or [])
        ]
    if review_state:
        payloads = [row for row in payloads if row["review_state"] == review_state]
    return payloads


def get_agent_run_tasks(connection, run_id):
    ensure_schema(connection)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT *
            FROM agent_tasks
            WHERE agent_run_id = %s
            ORDER BY created_at, id
            """,
            (run_id,),
        )
        return [_row_to_task_payload(row) for row in cursor.fetchall()]


def get_agent_run_artifacts(connection, run_id):
    ensure_schema(connection)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT *
            FROM agent_artifacts
            WHERE agent_run_id = %s
            ORDER BY created_at, id
            """,
            (run_id,),
        )
        return [_row_to_artifact_payload(row) for row in cursor.fetchall()]


def get_agent_run_proposals(connection, run_id):
    ensure_schema(connection)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT *
            FROM agent_proposals
            WHERE agent_run_id = %s
            ORDER BY created_at, id
            """,
            (run_id,),
        )
        rows = cursor.fetchall()
        return [_proposal_with_children(cursor, row) for row in rows]


def get_agent_proposal(connection, proposal_id):
    ensure_schema(connection)
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM agent_proposals WHERE id = %s", (proposal_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return _proposal_with_children(cursor, row)


def get_latest_agent_run_for_entity(connection, market_id, entity_type, entity_id, workflow_type=None):
    ensure_schema(connection)
    clauses = ["market_id = %(market_id)s", "entity_type = %(entity_type)s", "entity_id = %(entity_id)s"]
    params = {
        "market_id": market_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
    }
    if workflow_type:
        clauses.append("workflow_type = %(workflow_type)s")
        params["workflow_type"] = workflow_type
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT *
            FROM agent_runs
            WHERE
            """
            + " AND ".join(clauses)
            + """
            ORDER BY requested_at DESC
            LIMIT 1
            """,
            params,
        )
        row = cursor.fetchone()
        if not row:
            return None
        return _run_payload(cursor, row, include_events=False)


def cancel_agent_run(connection, run_id, note="Agent run cancelled."):
    ensure_schema(connection)
    now = _utc_now()
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM agent_runs WHERE id = %s", (run_id,))
        row = cursor.fetchone()
        if not row:
            return None
        cursor.execute(
            """
            UPDATE agent_runs
            SET status = %s,
                current_step = %s,
                completed_at = COALESCE(completed_at, %s),
                error_message = NULL
            WHERE id = %s
            """,
            (RUN_STATUS_CANCELLED, "cancelled", now, run_id),
        )
        cursor.execute(
            """
            INSERT INTO agent_run_events (agent_run_id, step_name, status, message, payload)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (run_id, "cancelled", RUN_STATUS_CANCELLED, note, Json({})),
        )
        cursor.execute(
            """
            UPDATE agent_tasks
            SET status = CASE
                    WHEN status IN (%s, %s) THEN %s
                    ELSE status
                END,
                completed_at = CASE
                    WHEN status IN (%s, %s) THEN COALESCE(completed_at, %s)
                    ELSE completed_at
                END
            WHERE agent_run_id = %s
            """,
            (
                TASK_STATUS_QUEUED,
                TASK_STATUS_IN_PROGRESS,
                TASK_STATUS_CANCELLED,
                TASK_STATUS_QUEUED,
                TASK_STATUS_IN_PROGRESS,
                now,
                run_id,
            ),
        )
    connection.commit()
    return get_agent_run(connection, run_id)


def retry_agent_run(connection, run_id):
    ensure_schema(connection)
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM agent_runs WHERE id = %s", (run_id,))
        row = cursor.fetchone()
        if not row:
            return None
        retry_state = dict(row["state_payload"] or {})
        retry_state["retry_of"] = run_id
    result = enqueue_agent_run(
        connection,
        row["market_id"],
        row["entity_type"],
        row["entity_id"],
        row["workflow_type"],
        retry_state,
    )
    return get_agent_run(connection, result["run_id"])


def get_or_create_review(connection, proposal_id, agent_run_id, notes=None):
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM agent_reviews WHERE proposal_id = %s", (proposal_id,))
        row = cursor.fetchone()
        if row:
            return _row_to_review_payload(row)
        review_id = _new_id("review")
        cursor.execute(
            """
            INSERT INTO agent_reviews (
                id, agent_run_id, proposal_id, status, notes, decision_payload
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                review_id,
                agent_run_id,
                proposal_id,
                REVIEW_STATUS_PENDING,
                notes,
                Json({}),
            ),
        )
        row = cursor.fetchone()
        cursor.execute(
            """
            UPDATE agent_proposals
            SET status = %s,
                review_state = %s
            WHERE id = %s
            """,
            (PROPOSAL_STATUS_NEEDS_REVIEW, REVIEW_STATUS_PENDING, proposal_id),
        )
    connection.commit()
    return _row_to_review_payload(row)


def resolve_review(connection, proposal_id, approved, reviewer="analyst", notes=None, decision_payload=None):
    now = _utc_now()
    status = REVIEW_STATUS_APPROVED if approved else REVIEW_STATUS_REJECTED
    proposal_status = PROPOSAL_STATUS_PROPOSED if approved else PROPOSAL_STATUS_REJECTED
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM agent_proposals WHERE id = %s", (proposal_id,))
        proposal = cursor.fetchone()
        if not proposal:
            return None
        cursor.execute("SELECT * FROM agent_reviews WHERE proposal_id = %s", (proposal_id,))
        review = cursor.fetchone()
        if review:
            cursor.execute(
                """
                UPDATE agent_reviews
                SET status = %s,
                    reviewer = %s,
                    notes = %s,
                    decision_payload = %s,
                    updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (
                    status,
                    reviewer,
                    notes,
                    Json(_json_ready(decision_payload or {})),
                    now,
                    review["id"],
                ),
            )
            review_row = cursor.fetchone()
        else:
            review_id = _new_id("review")
            cursor.execute(
                """
                INSERT INTO agent_reviews (
                    id, agent_run_id, proposal_id, status, reviewer, notes, decision_payload, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    review_id,
                    proposal["agent_run_id"],
                    proposal_id,
                    status,
                    reviewer,
                    notes,
                    Json(_json_ready(decision_payload or {})),
                    now,
                ),
            )
            review_row = cursor.fetchone()
        cursor.execute(
            """
            UPDATE agent_proposals
            SET review_state = %s,
                status = %s
            WHERE id = %s
            """,
            (status, proposal_status, proposal_id),
        )
    connection.commit()
    return _row_to_review_payload(review_row)


def mark_proposal_promoted(connection, proposal_id):
    now = _utc_now()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE agent_proposals
            SET status = %s,
                review_state = %s,
                promoted_at = %s
            WHERE id = %s
            """,
            (PROPOSAL_STATUS_PROMOTED, REVIEW_STATUS_APPROVED, now, proposal_id),
        )
    connection.commit()


class AgentRunTracker:
    def __init__(self, database_url, run_id):
        self.database_url = database_url
        self.run_id = run_id

    def _execute(self, fn):
        connection = get_connection(self.database_url)
        try:
            result = fn(connection)
            connection.commit()
            return result
        finally:
            connection.close()

    def start(self, step_name, state_payload=None, message="Agent run started."):
        now = _utc_now()

        def op(connection):
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE agent_runs
                    SET status = %s,
                        current_step = %s,
                        started_at = COALESCE(started_at, %s),
                        completed_at = NULL,
                        error_message = NULL,
                        state_payload = %s
                    WHERE id = %s
                    """,
                    (
                        RUN_STATUS_IN_PROGRESS,
                        step_name,
                        now,
                        Json(_json_ready(state_payload or {})),
                        self.run_id,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO agent_run_events (agent_run_id, step_name, status, message, payload)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        self.run_id,
                        step_name,
                        RUN_STATUS_IN_PROGRESS,
                        message,
                        Json(_json_ready(state_payload or {})),
                    ),
                )

        self._execute(op)

    def checkpoint(self, step_name, status, message, state_payload=None, current_step=None):
        def op(connection):
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE agent_runs
                    SET status = %s,
                        current_step = %s,
                        state_payload = %s
                    WHERE id = %s
                    """,
                    (
                        status,
                        current_step or step_name,
                        Json(_json_ready(state_payload or {})),
                        self.run_id,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO agent_run_events (agent_run_id, step_name, status, message, payload)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        self.run_id,
                        step_name,
                        status,
                        message,
                        Json(_json_ready(state_payload or {})),
                    ),
                )

        self._execute(op)

    def complete(self, state_payload=None, message="Agent run completed."):
        now = _utc_now()

        def op(connection):
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE agent_runs
                    SET status = %s,
                        current_step = %s,
                        completed_at = %s,
                        error_message = NULL,
                        state_payload = %s
                    WHERE id = %s
                    """,
                    (
                        RUN_STATUS_COMPLETED,
                        "completed",
                        now,
                        Json(_json_ready(state_payload or {})),
                        self.run_id,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO agent_run_events (agent_run_id, step_name, status, message, payload)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        self.run_id,
                        "completed",
                        RUN_STATUS_COMPLETED,
                        message,
                        Json(_json_ready(state_payload or {})),
                    ),
                )

        self._execute(op)

    def fail(self, error_message, step_name, state_payload=None):
        now = _utc_now()

        def op(connection):
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE agent_runs
                    SET status = %s,
                        current_step = %s,
                        completed_at = %s,
                        error_message = %s,
                        state_payload = %s
                    WHERE id = %s
                    """,
                    (
                        RUN_STATUS_FAILED,
                        step_name,
                        now,
                        error_message,
                        Json(_json_ready(state_payload or {})),
                        self.run_id,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO agent_run_events (agent_run_id, step_name, status, message, payload)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        self.run_id,
                        step_name,
                        RUN_STATUS_FAILED,
                        error_message,
                        Json(_json_ready(state_payload or {})),
                    ),
                )

        self._execute(op)

    def create_task(self, task_type, input_payload=None, parent_task_id=None, attempt=1):
        task_id = _new_id("task")
        now = _utc_now()

        def op(connection):
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO agent_tasks (
                        id,
                        agent_run_id,
                        parent_task_id,
                        task_type,
                        status,
                        attempt,
                        input_payload,
                        started_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        task_id,
                        self.run_id,
                        parent_task_id,
                        task_type,
                        TASK_STATUS_IN_PROGRESS,
                        attempt,
                        Json(_json_ready(input_payload or {})),
                        now,
                    ),
                )

        self._execute(op)
        return task_id

    def complete_task(self, task_id, output_payload=None, status=TASK_STATUS_COMPLETED):
        now = _utc_now()

        def op(connection):
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE agent_tasks
                    SET status = %s,
                        output_payload = %s,
                        completed_at = %s,
                        error_message = NULL
                    WHERE id = %s
                    """,
                    (status, Json(_json_ready(output_payload or {})), now, task_id),
                )

        self._execute(op)

    def fail_task(self, task_id, error_message, output_payload=None):
        now = _utc_now()

        def op(connection):
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE agent_tasks
                    SET status = %s,
                        output_payload = %s,
                        completed_at = %s,
                        error_message = %s
                    WHERE id = %s
                    """,
                    (
                        TASK_STATUS_FAILED,
                        Json(_json_ready(output_payload or {})),
                        now,
                        error_message,
                        task_id,
                    ),
                )

        self._execute(op)

    def create_artifact(self, artifact_type, payload, produced_by_task_id=None, source_document_id=None):
        artifact_id = _new_id("artifact")

        def op(connection):
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO agent_artifacts (
                        id,
                        agent_run_id,
                        produced_by_task_id,
                        artifact_type,
                        payload,
                        source_document_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        artifact_id,
                        self.run_id,
                        produced_by_task_id,
                        artifact_type,
                        Json(_json_ready(payload or {})),
                        source_document_id,
                    ),
                )

        self._execute(op)
        return artifact_id

    def create_proposal(
        self,
        proposal_type,
        payload,
        produced_by_task_id=None,
        decision=None,
        evidence_refs=None,
        review_state="not_required",
        status=PROPOSAL_STATUS_PROPOSED,
    ):
        proposal_id = _new_id("proposal")

        def op(connection):
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO agent_proposals (
                        id,
                        agent_run_id,
                        produced_by_task_id,
                        proposal_type,
                        status,
                        review_state,
                        decision,
                        payload,
                        evidence_refs
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        proposal_id,
                        self.run_id,
                        produced_by_task_id,
                        proposal_type,
                        status,
                        review_state,
                        decision,
                        Json(_json_ready(payload or {})),
                        Json(_json_ready(evidence_refs or [])),
                    ),
                )

        self._execute(op)
        return proposal_id

    def add_validation(self, proposal_id, validator_name, outcome, message, payload=None):
        validation_id = _new_id("validation")

        def op(connection):
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO agent_validations (
                        id,
                        agent_run_id,
                        proposal_id,
                        validator_name,
                        outcome,
                        message,
                        payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        validation_id,
                        self.run_id,
                        proposal_id,
                        validator_name,
                        outcome,
                        message,
                        Json(_json_ready(payload or {})),
                    ),
                )
                if outcome == VALIDATION_OUTCOME_FAILED:
                    cursor.execute(
                        """
                        UPDATE agent_proposals
                        SET status = %s
                        WHERE id = %s
                        """,
                        (PROPOSAL_STATUS_REJECTED, proposal_id),
                    )
                elif outcome == VALIDATION_OUTCOME_NEEDS_REVIEW:
                    cursor.execute(
                        """
                        UPDATE agent_proposals
                        SET status = %s,
                            review_state = %s
                        WHERE id = %s
                        """,
                        (PROPOSAL_STATUS_NEEDS_REVIEW, REVIEW_STATUS_PENDING, proposal_id),
                    )

        self._execute(op)
        return validation_id

    def mark_proposal_promoted(self, proposal_id):
        now = _utc_now()

        def op(connection):
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE agent_proposals
                    SET status = %s,
                        promoted_at = %s
                    WHERE id = %s
                    """,
                    (PROPOSAL_STATUS_PROMOTED, now, proposal_id),
                )

        self._execute(op)

    def ensure_review(self, proposal_id, notes=None):
        def op(connection):
            return get_or_create_review(connection, proposal_id, self.run_id, notes=notes)

        return self._execute(op)
