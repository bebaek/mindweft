from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from minigent_client.api_client import MindweftAPIClient
from minigent_client.errors import MindweftAPIError
from minigent_client.output import format_message, print_json


def _json_object_from_arg(raw: str | None, label: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MindweftAPIError(
            f"{label} must be valid JSON.",
            category="invalid_request",
            detail=str(exc),
        ) from exc
    if not isinstance(parsed, dict):
        raise MindweftAPIError(
            f"{label} must be a JSON object.",
            category="invalid_request",
            detail=raw,
        )
    return cast(dict[str, Any], parsed)


def _metadata_from_arg(raw: str | None) -> dict[str, Any] | None:
    return _json_object_from_arg(raw, "--metadata-json")


def _tenant_payload(args: argparse.Namespace, *, create: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if create and args.new_tenant_id is not None:
        payload["id"] = args.new_tenant_id
    for key in ["slug", "name", "status", "plan", "region"]:
        value = getattr(args, key, None)
        if value is not None:
            payload[key] = value
    if create and getattr(args, "provisioning_profile", None) is not None:
        payload["provisioning_profile"] = args.provisioning_profile
    metadata = _metadata_from_arg(args.metadata_json)
    if metadata is not None:
        payload["metadata"] = metadata
    return payload


def run_admin_tenants_list(
    args: argparse.Namespace,
    client: MindweftAPIClient,
    trace_id: str | None,
) -> int:
    response = client.list_admin_tenants(
        limit=args.limit,
        offset=args.offset,
        status=args.status,
        plan=args.plan,
        slug=args.slug,
    )
    if args.json:
        output: dict[str, Any] = dict(response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(
        " ".join(
            [
                f"total={response.get('total')}",
                f"limit={response.get('limit')}",
                f"offset={response.get('offset')}",
                f"next_offset={response.get('next_offset')}",
            ]
        )
    )
    for tenant in response.get("tenants", []):
        if not isinstance(tenant, dict):
            continue
        print(_format_tenant_line(tenant))
    return 0


def run_admin_tenants_create(
    args: argparse.Namespace,
    client: MindweftAPIClient,
    trace_id: str | None,
) -> int:
    response = client.create_admin_tenant(_tenant_payload(args, create=True))
    return _print_admin_tenant_response(args, response, trace_id)


def run_admin_tenants_show(
    args: argparse.Namespace,
    client: MindweftAPIClient,
    trace_id: str | None,
) -> int:
    response = client.get_admin_tenant(args.tenant_id)
    return _print_admin_tenant_response(args, response, trace_id)


def run_admin_tenants_update(
    args: argparse.Namespace,
    client: MindweftAPIClient,
    trace_id: str | None,
) -> int:
    payload = _tenant_payload(args, create=False)
    response = client.update_admin_tenant(args.tenant_id, payload)
    return _print_admin_tenant_response(args, response, trace_id)


def run_admin_tenants_transition(
    args: argparse.Namespace,
    client: MindweftAPIClient,
    trace_id: str | None,
) -> int:
    command = args.admin_tenants_command
    if command == "delete":
        response = client.delete_admin_tenant(args.tenant_id)
    else:
        response = client.transition_admin_tenant(args.tenant_id, command)
    return _print_admin_tenant_response(args, response, trace_id)


def _parse_seed_slug_overrides(values: list[str] | None) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for value in values or []:
        tenant_id, separator, slug = value.partition("=")
        if not separator or not tenant_id or not slug or tenant_id in overrides:
            raise MindweftAPIError(
                "Slug overrides must use a unique TENANT_ID=SLUG format.",
                category="invalid_request",
            )
        overrides[tenant_id] = slug
    return overrides


def run_admin_tenants_seed(
    args: argparse.Namespace,
    client: MindweftAPIClient,
    trace_id: str | None,
) -> int:
    payload: dict[str, Any] = {
        "source": args.seed_source,
        "status": args.status,
        "dry_run": args.dry_run,
        "conflict_policy": args.conflict_policy,
    }
    if args.plan is not None:
        payload["plan"] = args.plan
    if args.region is not None:
        payload["region"] = args.region
    if args.seed_tenants is not None:
        payload["tenant_ids"] = args.seed_tenants
    slug_overrides = _parse_seed_slug_overrides(args.seed_slug_overrides)
    if slug_overrides:
        payload["slug_overrides"] = slug_overrides
    response = client.seed_admin_tenants(payload)
    if args.json:
        output: dict[str, Any] = dict(response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(
        " ".join(
            [
                f"source={response.get('source')}",
                f"discovered={response.get('discovered')}",
                f"existing={response.get('existing')}",
                f"created={response.get('created')}",
                f"skipped={response.get('skipped', 0)}",
                f"conflicts={response.get('conflicts')}",
                f"policy={response.get('conflict_policy', 'suffix')}",
                f"missing={len(response.get('missing_tenant_ids', []))}",
                f"dry_run={response.get('dry_run')}",
            ]
        )
    )
    for tenant in response.get("tenants", []):
        if not isinstance(tenant, dict):
            continue
        print(
            " ".join(
                [
                    str(tenant.get("id", "")),
                    f"slug={tenant.get('slug')}",
                    f"requested_slug={tenant.get('requested_slug', tenant.get('slug'))}",
                    f"status={tenant.get('status')}",
                    f"action={tenant.get('action')}",
                    f"conflict={tenant.get('conflict') or 'none'}",
                    f"config_source={tenant.get('execution_config_source', 'unknown')}",
                ]
            )
        )
    return 0


def _load_execution_config_file(path_text: str) -> dict[str, dict[str, Any]]:
    path = Path(path_text).expanduser()
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MindweftAPIError(
            f"Execution-config file not found: {path_text}",
            category="invalid_request",
            detail=str(exc),
        ) from exc
    except json.JSONDecodeError as exc:
        raise MindweftAPIError(
            "Execution-config file must be valid JSON.",
            category="invalid_request",
            detail=str(exc),
        ) from exc
    if not isinstance(parsed, dict):
        raise MindweftAPIError(
            "Execution-config file must contain a JSON object.",
            category="invalid_request",
        )
    raw_configs = parsed.get("execution_configs") if "execution_configs" in parsed else parsed
    if not isinstance(raw_configs, dict):
        raise MindweftAPIError(
            "execution_configs must be a JSON object when present.",
            category="invalid_request",
        )
    configs: dict[str, dict[str, Any]] = {}
    for tenant_id, config in raw_configs.items():
        if not isinstance(tenant_id, str) or not tenant_id:
            raise MindweftAPIError(
                "Execution-config tenant IDs must be non-empty strings.",
                category="invalid_request",
            )
        if not isinstance(config, dict):
            raise MindweftAPIError(
                f"Execution config for tenant '{tenant_id}' must be a JSON object.",
                category="invalid_request",
            )
        configs[tenant_id] = cast(dict[str, Any], config)
    return configs


def _select_execution_configs(
    configs: dict[str, dict[str, Any]], tenant_id: str | None
) -> dict[str, dict[str, Any]]:
    if tenant_id is None:
        return configs
    config = configs.get(tenant_id)
    if config is None:
        raise MindweftAPIError(
            f"Execution-config file has no tenant '{tenant_id}'.",
            category="invalid_request",
        )
    return {tenant_id: config}


def _validation_ok(report: dict[str, Any]) -> bool:
    valid = report.get("valid")
    if isinstance(valid, bool):
        return valid
    status = report.get("status")
    if isinstance(status, str):
        return status.lower() in {"ok", "valid"}
    errors = report.get("errors")
    return not (isinstance(errors, list) and errors)


def _validation_summary(report: dict[str, Any]) -> str:
    if _validation_ok(report):
        return "valid"
    errors = report.get("errors")
    if isinstance(errors, list) and errors:
        return f"invalid errors={len(errors)}"
    return "invalid"


def _validate_execution_configs(
    client: MindweftAPIClient, configs: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    return {
        tenant_id: client.validate_admin_tenant_execution_config(tenant_id, config)
        for tenant_id, config in configs.items()
    }


def run_admin_execution_config_validate_file(
    args: argparse.Namespace,
    client: MindweftAPIClient,
    trace_id: str | None,
) -> int:
    configs = _select_execution_configs(
        _load_execution_config_file(args.file), args.validate_tenant_id
    )
    reports = _validate_execution_configs(client, configs)
    ok = all(_validation_ok(report) for report in reports.values())
    output: dict[str, Any] = {
        "valid": ok,
        "tenant_count": len(configs),
        "tenants": [
            {
                "tenant_id": tenant_id,
                "valid": _validation_ok(report),
                "report": report,
            }
            for tenant_id, report in reports.items()
        ],
    }
    if trace_id is not None:
        output["trace_id"] = trace_id
    if args.json:
        print_json(output)
        return 0 if ok else 1
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(f"tenant_count={len(configs)} valid={ok}")
    for tenant_id, report in reports.items():
        print(f"{tenant_id} {_validation_summary(report)}")
    return 0 if ok else 1


def run_admin_execution_config_import(
    args: argparse.Namespace,
    client: MindweftAPIClient,
    trace_id: str | None,
) -> int:
    if not args.dry_run and not args.upsert:
        raise MindweftAPIError(
            "Import requires --dry-run or --upsert.",
            category="invalid_request",
        )
    configs = _select_execution_configs(
        _load_execution_config_file(args.file), args.import_tenant_id
    )
    reports = _validate_execution_configs(client, configs)
    valid_tenant_ids = [
        tenant_id for tenant_id, report in reports.items() if _validation_ok(report)
    ]
    invalid_tenant_ids = [
        tenant_id for tenant_id, report in reports.items() if not _validation_ok(report)
    ]
    written: list[str] = []
    if not args.dry_run:
        if invalid_tenant_ids:
            raise MindweftAPIError(
                "Import validation failed; no configs were written.",
                category="invalid_request",
                detail=", ".join(invalid_tenant_ids),
            )
        for tenant_id in valid_tenant_ids:
            client.put_admin_tenant_execution_config(tenant_id, configs[tenant_id])
            written.append(tenant_id)
    seed_response: dict[str, Any] | None = None
    if args.seed_tenants and not args.dry_run:
        payload: dict[str, Any] = {
            "source": "execution-configs",
            "status": args.status,
            "dry_run": False,
        }
        if args.plan is not None:
            payload["plan"] = args.plan
        if args.region is not None:
            payload["region"] = args.region
        seed_response = client.seed_admin_tenants(payload)
    output: dict[str, Any] = {
        "dry_run": args.dry_run,
        "tenant_count": len(configs),
        "valid": len(valid_tenant_ids),
        "invalid": len(invalid_tenant_ids),
        "written": written,
        "tenants": [
            {
                "tenant_id": tenant_id,
                "valid": _validation_ok(report),
                "written": tenant_id in written,
                "report": report,
            }
            for tenant_id, report in reports.items()
        ],
    }
    if seed_response is not None:
        output["seed"] = seed_response
    if trace_id is not None:
        output["trace_id"] = trace_id
    if args.json:
        print_json(output)
        return 0 if not invalid_tenant_ids else 1
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(
        " ".join(
            [
                f"tenant_count={len(configs)}",
                f"valid={len(valid_tenant_ids)}",
                f"invalid={len(invalid_tenant_ids)}",
                f"written={len(written)}",
                f"dry_run={args.dry_run}",
            ]
        )
    )
    for tenant in output["tenants"]:
        if not isinstance(tenant, dict):
            continue
        print(
            f"{tenant.get('tenant_id')} valid={tenant.get('valid')} written={tenant.get('written')}"
        )
    if seed_response is not None:
        print(
            "seed "
            + " ".join(
                [
                    f"created={seed_response.get('created')}",
                    f"existing={seed_response.get('existing')}",
                    f"conflicts={seed_response.get('conflicts')}",
                ]
            )
        )
    return 0 if not invalid_tenant_ids else 1


def run_admin_execution_config_export(
    args: argparse.Namespace,
    client: MindweftAPIClient,
    trace_id: str | None,
) -> int:
    tenant_ids = (
        [args.export_tenant_id]
        if args.export_tenant_id is not None
        else client.list_admin_execution_config_tenants()
    )
    configs: dict[str, Any] = {}
    for tenant_id in tenant_ids:
        response = client.get_admin_tenant_execution_config(tenant_id)
        config = response.get("config")
        if not isinstance(config, dict):
            raise RuntimeError(
                f"Mindweft admin execution-config response for tenant '{tenant_id}' must include config"
            )
        configs[tenant_id] = config
    output: dict[str, Any] = configs
    if trace_id is not None and args.json:
        output = {"execution_configs": configs, "trace_id": trace_id}
    text = json.dumps(output, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        if not args.json:
            print(f"Wrote execution configs for {len(configs)} tenant(s) to {args.output}")
    else:
        print(text, end="")
    return 0


def _tenant_user_payload(args: argparse.Namespace, *, create: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if create:
        payload["user_id"] = args.user_id
    for key in ["email", "display_name", "role", "status"]:
        value = getattr(args, key, None)
        if value is not None:
            payload[key] = value
    metadata = _metadata_from_arg(args.metadata_json)
    if metadata is not None:
        payload["metadata"] = metadata
    return payload


def _format_tenant_user_line(user: dict[str, Any]) -> str:
    return " ".join(
        [
            str(user.get("id") or ""),
            f"tenant_id={user.get('tenant_id')}",
            f"user_id={user.get('user_id')}",
            f"email={user.get('email')}",
            f"display_name={user.get('display_name')}",
            f"role={user.get('role')}",
            f"status={user.get('status')}",
            f"updated_at={user.get('updated_at')}",
        ]
    )


def _print_admin_tenant_user_response(
    args: argparse.Namespace,
    response: dict[str, Any],
    trace_id: str | None,
) -> int:
    if args.json:
        output: dict[str, Any] = dict(response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(_format_tenant_user_line(response))
    metadata = response.get("metadata")
    if isinstance(metadata, dict) and metadata:
        print("metadata=" + json.dumps(metadata, ensure_ascii=True, sort_keys=True))
    return 0


def run_admin_tenant_users(
    args: argparse.Namespace,
    client: MindweftAPIClient,
    trace_id: str | None,
) -> int:
    command = args.admin_tenant_users_command
    if command == "list":
        response = client.list_admin_tenant_users(
            args.tenant_id,
            limit=args.limit,
            offset=args.offset,
            status=args.status,
            role=args.role,
            email=args.email,
        )
        if args.json:
            output: dict[str, Any] = dict(response)
            if trace_id is not None:
                output["trace_id"] = trace_id
            print_json(output)
            return 0
        if trace_id is not None:
            print(f"trace_id={trace_id}")
        print(
            " ".join(
                [
                    f"tenant_id={response.get('tenant_id')}",
                    f"total={response.get('total')}",
                    f"limit={response.get('limit')}",
                    f"offset={response.get('offset')}",
                    f"next_offset={response.get('next_offset')}",
                ]
            )
        )
        for user in response.get("users", []):
            if isinstance(user, dict):
                print(_format_tenant_user_line(user))
        return 0
    if command == "create":
        response = client.create_admin_tenant_user(
            args.tenant_id,
            _tenant_user_payload(args, create=True),
        )
        return _print_admin_tenant_user_response(args, response, trace_id)
    if command == "show":
        response = client.get_admin_tenant_user(args.tenant_id, args.user_record_id)
        return _print_admin_tenant_user_response(args, response, trace_id)
    if command == "update":
        response = client.update_admin_tenant_user(
            args.tenant_id,
            args.user_record_id,
            _tenant_user_payload(args, create=False),
        )
        return _print_admin_tenant_user_response(args, response, trace_id)
    if command in {"activate", "suspend"}:
        response = client.transition_admin_tenant_user(
            args.tenant_id,
            args.user_record_id,
            command,
        )
        return _print_admin_tenant_user_response(args, response, trace_id)
    if command == "delete":
        response = client.delete_admin_tenant_user(args.tenant_id, args.user_record_id)
        if args.json:
            output = dict(response)
            if trace_id is not None:
                output["trace_id"] = trace_id
            print_json(output)
            return 0
        if trace_id is not None:
            print(f"trace_id={trace_id}")
        print(
            " ".join(
                [
                    "deleted=True",
                    f"tenant_id={response.get('tenant_id')}",
                    f"id={response.get('id')}",
                    f"status={response.get('status')}",
                ]
            )
        )
        return 0
    raise RuntimeError(f"Unhandled tenant users command: {command}")


def _entitlements_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "features": _json_object_from_arg(args.features_json, "--features-json") or {},
        "limits": _json_object_from_arg(args.limits_json, "--limits-json") or {},
    }


def run_admin_tenant_entitlements(
    args: argparse.Namespace,
    client: MindweftAPIClient,
    trace_id: str | None,
) -> int:
    command = args.admin_tenant_entitlements_command
    if command == "show":
        response = client.get_admin_tenant_entitlements(args.tenant_id)
    elif command == "set":
        response = client.put_admin_tenant_entitlements(args.tenant_id, _entitlements_payload(args))
    elif command == "validate":
        response = client.validate_admin_tenant_entitlements(
            args.tenant_id, _entitlements_payload(args)
        )
    elif command == "delete":
        client.delete_admin_tenant_entitlements(args.tenant_id)
        response = {"deleted": True, "tenant_id": args.tenant_id}
    else:  # pragma: no cover - argparse prevents this
        raise RuntimeError(f"Unhandled entitlements command: {command}")
    if args.json:
        output: dict[str, Any] = dict(response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    if command == "delete":
        print(f"deleted tenant_id={args.tenant_id}")
        return 0
    print(
        " ".join(
            [
                f"tenant_id={response.get('tenant_id')}",
                f"version={response.get('version')}",
                f"updated_at={response.get('updated_at')}",
            ]
        )
    )
    if "valid" in response:
        print(f"valid={response.get('valid')}")
    features = response.get("features")
    limits = response.get("limits")
    if isinstance(features, dict):
        print("features=" + json.dumps(features, ensure_ascii=True, sort_keys=True))
    if isinstance(limits, dict):
        print("limits=" + json.dumps(limits, ensure_ascii=True, sort_keys=True))
    return 0


def _print_admin_tenant_response(
    args: argparse.Namespace,
    response: dict[str, Any],
    trace_id: str | None,
) -> int:
    if args.json:
        output: dict[str, Any] = dict(response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(_format_tenant_line(response))
    metadata = response.get("metadata")
    if isinstance(metadata, dict) and metadata:
        print("metadata=" + json.dumps(metadata, ensure_ascii=True, sort_keys=True))
    return 0


def _format_tenant_line(tenant: dict[str, Any]) -> str:
    return " ".join(
        [
            str(tenant.get("id") or tenant.get("tenant_id") or ""),
            f"slug={tenant.get('slug')}",
            f"name={tenant.get('name')}",
            f"status={tenant.get('status')}",
            f"plan={tenant.get('plan')}",
            f"region={tenant.get('region')}",
            f"updated_at={tenant.get('updated_at')}",
        ]
    )


def run_admin_threads_list(
    args: argparse.Namespace,
    client: MindweftAPIClient,
    trace_id: str | None,
) -> int:
    response = client.list_admin_threads(
        args.admin_tenant_id,
        limit=args.limit,
        offset=args.offset,
        status=args.status,
        profile=args.profile,
        skill=args.skill,
        created_after=args.created_after,
        updated_after=args.updated_after,
    )
    if args.json:
        output: dict[str, Any] = dict(response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(
        " ".join(
            [
                f"tenant_id={response.get('tenant_id')}",
                f"total={response.get('total')}",
                f"limit={response.get('limit')}",
                f"offset={response.get('offset')}",
                f"next_offset={response.get('next_offset')}",
            ]
        )
    )
    for thread in response.get("threads", []):
        if not isinstance(thread, dict):
            continue
        print(
            " ".join(
                [
                    str(thread.get("thread_id", "")),
                    f"status={thread.get('status')}",
                    f"messages={thread.get('message_count')}",
                    f"updated_at={thread.get('updated_at')}",
                ]
            )
        )
    return 0


def run_admin_threads_delete(
    args: argparse.Namespace,
    client: MindweftAPIClient,
    trace_id: str | None,
) -> int:
    response = client.delete_admin_thread(args.admin_tenant_id, args.thread_id)
    if args.json:
        output: dict[str, Any] = dict(response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(f"deleted thread_id={response.get('thread_id')} tenant_id={response.get('tenant_id')}")
    return 0


def run_admin_threads_prune(
    args: argparse.Namespace,
    client: MindweftAPIClient,
    trace_id: str | None,
) -> int:
    response = client.prune_admin_threads(
        args.admin_tenant_id,
        updated_before=args.updated_before,
        status=args.status,
        profile=args.profile,
        skill=args.skill,
        dry_run=args.dry_run,
    )
    if args.json:
        output: dict[str, Any] = dict(response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(
        " ".join(
            [
                f"tenant_id={response.get('tenant_id')}",
                f"deleted_count={response.get('deleted_count')}",
                f"dry_run={response.get('dry_run')}",
                f"candidate_count={len(response.get('candidate_thread_ids', []))}",
                f"updated_before={response.get('updated_before')}",
            ]
        )
    )
    return 0


def run_admin_audit_list(
    args: argparse.Namespace,
    client: MindweftAPIClient,
    trace_id: str | None,
) -> int:
    response = client.list_admin_audit_records(
        args.admin_tenant_id,
        limit=args.limit,
        offset=args.offset,
        action=args.action,
        actor=args.actor,
        created_after=args.created_after,
        created_before=args.created_before,
    )
    if args.json:
        output: dict[str, Any] = dict(response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(
        " ".join(
            [
                f"tenant_id={response.get('tenant_id')}",
                f"limit={response.get('limit')}",
                f"offset={response.get('offset')}",
                f"total={response.get('total')}",
                f"next_offset={response.get('next_offset')}",
            ]
        )
    )
    for record in response.get("audit_records", []):
        if not isinstance(record, dict):
            continue
        print(
            " ".join(
                [
                    str(record.get("audit_id", "")),
                    f"action={record.get('action')}",
                    f"actor={record.get('actor_user_id')}",
                    f"affected_count={record.get('affected_count')}",
                    f"created_at={record.get('created_at')}",
                ]
            )
        )
    return 0


def run_admin_threads_show(
    args: argparse.Namespace,
    client: MindweftAPIClient,
    trace_id: str | None,
) -> int:
    response = client.get_admin_thread(args.admin_tenant_id, args.thread_id)
    if args.json:
        output: dict[str, Any] = dict(response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(f"thread_id={response.get('thread_id')}")
    print(f"tenant_id={response.get('tenant_id')}")
    print(f"status={response.get('status')}")
    print(f"message_count={response.get('message_count')}")
    print(f"created_at={response.get('created_at')}")
    print(f"updated_at={response.get('updated_at')}")
    skill_name = response.get("skill_name")
    if skill_name is not None:
        print(f"skill_name={skill_name}")
    skill_names = response.get("skill_names")
    if skill_names is not None:
        print(f"skill_names={skill_names}")
    capability_profile = response.get("capability_profile")
    if capability_profile is not None:
        print(f"capability_profile={capability_profile}")
    context = response.get("context")
    if isinstance(context, dict):
        print(
            "context="
            f"summarized_message_count={context.get('summarized_message_count')} "
            f"updated_at={context.get('updated_at')}"
        )
        summary = context.get("summary")
        if isinstance(summary, str) and summary:
            print(f"summary={summary}")
    messages = response.get("messages", [])
    if isinstance(messages, list) and messages:
        print("messages:")
        for message in messages:
            if isinstance(message, dict):
                print(format_message(message))
    return 0
