from __future__ import annotations

from app.ownership import (
    CONFIDENCE_PROBABLE,
    OWNERSHIP_PARSER_VERSION,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_NEEDS_REVIEW,
    _ensure_exact_named_organization,
    _ensure_organization,
    _ensure_recorder_document,
    _evaluate_ownership_qc,
    _fetch_parcel,
    _mark_parcel_status,
    _replace_organization_roles,
    _slugify,
    _upsert_financing_claim,
    _upsert_claim,
    _upsert_source_document,
    _utc_now,
    extract_financing_claims,
)


def load_parcel_context(connection, market_id, parcel_id):
    with connection.cursor() as cursor:
        parcel = _fetch_parcel(cursor, market_id, parcel_id)
    if not parcel:
        return None
    return dict(parcel)


def mark_parcel_in_progress(connection, market_id, parcel_id, note="Ownership lookup in progress"):
    now = _utc_now()
    with connection.cursor() as cursor:
        parcel = _fetch_parcel(cursor, market_id, parcel_id)
        if not parcel:
            return None
        _mark_parcel_status(cursor, parcel_id, STATUS_IN_PROGRESS, now, note, {})
    connection.commit()
    return dict(parcel)


def mark_parcel_failed(connection, market_id, parcel_id, error_message):
    now = _utc_now()
    with connection.cursor() as cursor:
        _mark_parcel_status(
            cursor,
            parcel_id,
            STATUS_FAILED,
            now,
            "Ownership enrichment failed: %s" % error_message,
            {
                "matched_entity": False,
                "enrichment_error": {
                    "message": error_message,
                    "code": "ownership_enrichment_exception",
                },
                "qa": {
                    "issues": [
                        {
                            "status": "failed",
                            "message": "ownership enrichment failed",
                            "code": "enrichment_exception",
                            "severity": "error",
                            "details": {},
                        }
                    ],
                    "warnings": [],
                    "active_claim_count": 0,
                    "active_claim_ids": [],
                },
                "review_reasons": [
                    "ownership enrichment failed during extraction and should be retried"
                ],
            },
        )
    connection.commit()


def mark_parcel_review_pending(connection, market_id, parcel_id, note, metadata=None):
    now = _utc_now()
    with connection.cursor() as cursor:
        parcel = _fetch_parcel(cursor, market_id, parcel_id)
        if not parcel:
            return None
        payload = {
            "matched_entity": False,
            "qa": {
                "issues": [],
                "warnings": [],
                "active_claim_count": 0,
                "active_claim_ids": [],
            },
            "review_reasons": [],
        }
        if metadata:
            payload.update(metadata)
        _mark_parcel_status(cursor, parcel_id, STATUS_NEEDS_REVIEW, now, note, payload)
    connection.commit()
    return {"parcel_id": parcel_id, "enrichment_status": STATUS_NEEDS_REVIEW}


def persist_ownership_result(connection, market_id, parcel_id, recorder_result, entity_result=None):
    now = _utc_now()
    latest_deed = recorder_result["latest_deed"]
    grouped_records = recorder_result["grouped_records"]
    financing_claims = extract_financing_claims(grouped_records)
    recorder_source_document = {
        "id": "source-%s-recorder-%s" % (_slugify(parcel_id), now.strftime("%Y%m%d%H%M%S")),
        "source_name": recorder_result["source_name"],
        "title": "Essex County recorder search for block %s lot %s"
        % (recorder_result["block"], recorder_result["lot"]),
        "source_url": recorder_result["source_url"],
        "access_date": recorder_result["access_date"],
        "document_type": "recorder_search_results",
        "parser_version": OWNERSHIP_PARSER_VERSION,
    }

    with connection.cursor() as cursor:
        _upsert_source_document(cursor, recorder_source_document)

        recorder_document_ids = {}
        latest_recorder_document_id = None
        for grouped_record in grouped_records or []:
            recorder_document_id = _ensure_recorder_document(
                cursor,
                parcel_id,
                grouped_record,
                recorder_source_document["id"],
                now,
            )
            recorder_document_ids[
                (
                    grouped_record["document_number"],
                    grouped_record["recorded_at"],
                    grouped_record["document_type"],
                )
            ] = recorder_document_id
            if latest_deed and (
                grouped_record["document_number"] == latest_deed["document_number"]
                and grouped_record["recorded_at"] == latest_deed["recorded_at"]
                and grouped_record["document_type"] == latest_deed["document_type"]
            ):
                latest_recorder_document_id = recorder_document_id

        financing_count = 0
        for financing_claim in financing_claims:
            recorder_document_id = recorder_document_ids.get(
                (
                    financing_claim["document_number"],
                    financing_claim["recorded_at"],
                    financing_claim["document_type"],
                )
            )
            if not recorder_document_id:
                continue
            financing_organization_id = _ensure_exact_named_organization(
                cursor,
                market_id,
                financing_claim["lender_name"],
                recorder_source_document["id"],
                now,
            )
            _upsert_financing_claim(
                cursor,
                parcel_id,
                financing_organization_id,
                financing_claim["lender_name"],
                recorder_document_id,
                financing_claim["document_category"],
                financing_claim["consideration"],
                recorder_source_document["id"],
                "Recorder filing-backed financing history extracted from Essex online search results.",
                now,
                financing_claim["recorded_at"],
            )
            financing_count += 1

        if not latest_deed:
            no_deed_metadata = {
                "matched_entity": False,
                "no_deed_results": True,
                "recorder_document_count": len(grouped_records),
                "financing_claim_count": financing_count,
                "qa": {
                    "issues": [],
                    "warnings": [],
                    "active_claim_count": 0,
                    "active_claim_ids": [],
                },
                "review_reasons": [
                    "no deed results for the selected parcel/block-lot in Essex lookup"
                ],
            }
            _mark_parcel_status(
                cursor,
                parcel_id,
                STATUS_COMPLETED,
                now,
                (
                    "Recorder history was stored, but no deed results were found in the Essex County recorder search."
                    if grouped_records
                    else "No deed results were found in the Essex County recorder search."
                ),
                no_deed_metadata,
            )
            connection.commit()
            return {
                "parcel_id": parcel_id,
                "enrichment_status": STATUS_COMPLETED,
                "review_reasons": no_deed_metadata["review_reasons"],
                "qa": no_deed_metadata["qa"],
            }
        if latest_recorder_document_id is None:
            latest_recorder_document_id = _ensure_recorder_document(
                cursor,
                parcel_id,
                latest_deed,
                recorder_source_document["id"],
                now,
            )

        owner_name = latest_deed["primary_grantee"] or "; ".join(latest_deed["grantees"]) or "Unknown owner"
        organization_id = None
        note = "Latest deed owner captured from Essex County recorder search results."
        status = STATUS_COMPLETED
        review_reasons = []
        metadata = {"matched_entity": False, "search_candidates": []}

        if entity_result is not None:
            nj_source_document = {
                "id": "source-%s-njbgs-%s" % (_slugify(parcel_id), now.strftime("%Y%m%d%H%M%S")),
                "source_name": entity_result["source_name"],
                "title": "NJ business search for %s" % owner_name,
                "source_url": entity_result["source_url"],
                "access_date": entity_result["access_date"],
                "document_type": "business_name_search_results",
                "parser_version": OWNERSHIP_PARSER_VERSION,
            }
            _upsert_source_document(cursor, nj_source_document)

            if entity_result["match_status"] == "matched":
                organization_id = _ensure_organization(
                    cursor,
                    market_id,
                    entity_result["matched_entity"],
                    entity_result["details"],
                    nj_source_document["id"],
                    now,
                )
                if entity_result["details"]["officers"]:
                    _replace_organization_roles(
                        cursor,
                        organization_id,
                        entity_result["details"]["officers"],
                        nj_source_document["id"],
                        now,
                    )
                metadata = {
                    "matched_entity": True,
                    "entity_id": entity_result["matched_entity"]["entity_id"],
                    "match_score": entity_result["matched_entity"]["match_score"],
                    "search_candidates": entity_result["candidates"],
                }
                if (
                    not entity_result["details"]["status"]
                    and not entity_result["details"]["registered_agent"]
                    and not entity_result["details"]["officers"]
                ):
                    note = (
                        "Owner matched to the public NJ business search summary. "
                        "The current public response did not expose registered agent or officer detail."
                    )
                review_reasons.append("owner matched with one high-confidence NJ entity result")
            elif entity_result["match_status"] == "needs_review":
                status = STATUS_NEEDS_REVIEW
                review_reasons.append(
                    "owner had ambiguous NJ entity matches and requires analyst review"
                )
                metadata = {
                    "matched_entity": False,
                    "search_candidates": entity_result["candidates"],
                }
                note = "The owner matched multiple NJ business candidates and needs analyst review."
            else:
                note = (
                    "No NJ business entity match was found for the deed grantee in the public name search."
                )
                review_reasons.append(
                    "no NJ entity match was found in public name search for this owner"
                )

        _upsert_claim(
            cursor,
            parcel_id,
            owner_name,
            organization_id,
            latest_recorder_document_id,
            recorder_source_document["id"],
            note,
            now,
            latest_deed["recorded_at"],
        )
        qa_result = _evaluate_ownership_qc(
            cursor,
            parcel_id,
            owner_name,
            latest_deed["recorded_at"],
            CONFIDENCE_PROBABLE,
            now,
        )
        if qa_result["status"] == STATUS_FAILED:
            status = STATUS_FAILED
        elif qa_result["status"] == STATUS_NEEDS_REVIEW and status != STATUS_FAILED:
            status = STATUS_NEEDS_REVIEW

        metadata.update(
            {
                "qa": qa_result["qa"],
                "review_reasons": qa_result["review_reasons"] + review_reasons,
                "match_confidence": CONFIDENCE_PROBABLE,
                "match_status": entity_result["match_status"] if entity_result else "not_found",
                "search_candidates": metadata.get("search_candidates", []),
            }
        )
        if review_reasons:
            metadata["needs_review_reasons"] = review_reasons
        if status == STATUS_FAILED:
            metadata["enrichment_error"] = {
                "message": note,
                "code": "ownership_qa_failed",
            }
        metadata["recorder_document_count"] = len(grouped_records)
        metadata["financing_claim_count"] = financing_count

        _mark_parcel_status(cursor, parcel_id, status, now, note, metadata)
    connection.commit()
    return {
        "parcel_id": parcel_id,
        "enrichment_status": status,
        "review_reasons": metadata.get("review_reasons", []),
        "qa": metadata.get("qa", {}),
    }
