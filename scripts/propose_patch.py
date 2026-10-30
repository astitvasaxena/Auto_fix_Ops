"""
Propose (and validate) a remediation patch for one DIAGNOSED incident.

PROPOSAL ONLY: the patch is validated in a temporary sandbox and stored in the
patch_proposals table. It is never applied to the working tree, committed,
pushed, deployed or opened as a pull request. Human approval is Stage 3.

Usage:
    python scripts/propose_patch.py <incident_id> [--json]
    python scripts/propose_patch.py --show <incident_id> [--json]

Exit codes (propose):
    0 VALIDATED and stored      2 NOT_GENERATED      3 REJECTED      4 FAILED
    5 result could not be stored (whatever the outcome)
    6 another proposal for this incident is already running
    7 the model already decided for this diagnosis version (VALIDATED, REJECTED or declined);
      nothing new was generated (see --show)
    1 usage error / unknown incident      130 interrupted (Ctrl-C; the run is stored as FAILED/INTERRUPTED)
Exit codes (--show, read-only):
    0 a stored proposal was printed (whatever its status)
    1 nothing to show / proposals could not be read / usage error
--help prints usage and exits 0.
"""

import argparse
import json
import os
import sys
import uuid

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

BANNER = "PROPOSAL ONLY - not applied, not committed, requires human approval (Stage 3)"
EXIT_USAGE = 1
EXIT_INTERRUPTED = 130


class _UsageError(Exception):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    """argparse exits 2 on bad arguments, which would read as NOT_GENERATED; use 1 instead."""

    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise _UsageError(message)


def _default_session_factory(create_table: bool = True):
    """Real database: load .env, then (unless read-only) create only the Stage 2 tables if missing."""
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(REPO_ROOT, ".env"))
    except ImportError:
        pass
    from sqlalchemy import inspect

    from api.database import SessionLocal, engine
    from api.models import PatchProposal, PatchRoutingDecision

    if create_table:
        for model in (PatchProposal, PatchRoutingDecision):   # routing rows reference proposals
            try:
                model.__table__.create(bind=engine, checkfirst=True)
            except Exception:
                # Another run may have created it between the check and the CREATE.
                if not inspect(engine).has_table(model.__tablename__):
                    raise
    return SessionLocal


def _print_human(result: dict, out):
    rec = result.get("record") or {}
    print(BANNER, file=out)
    print(f"status:     {result['status']}" + (f" / {result['reason_code']}" if result.get("reason_code") else ""), file=out)
    if result.get("reason_detail"):
        print(f"detail:     {result['reason_detail']}", file=out)
    print(f"stored:     {'yes' if result['persisted'] else 'NO'}"
          + (f" (proposal {result['proposal_id']})" if result.get("proposal_id") else ""), file=out)
    if not rec:
        return
    info = rec.get("model_info") or {}
    if info.get("provider"):
        print(f"model:      {info.get('provider')} {info.get('model')} ({info.get('latency_ms', '?')} ms)", file=out)
    print("checks:", file=out)
    for step in (rec.get("validation") or {}).get("steps", []):
        code = f" [{step['code']}]" if step.get("code") else ""
        print(f"  {step['status']:<8} {step['check']}{code} {step.get('detail') or ''}".rstrip(), file=out)
    for note in rec.get("review_notes") or []:
        print(f"note:       {note}", file=out)
    if rec.get("rationale"):
        print(f"rationale:  {rec['rationale']}  (AI inference)", file=out)
    if result["status"] == "VALIDATED" and result["persisted"]:
        edit = rec.get("proposed_values") or {}
        print(f"edit:       {rec.get('target_file')}: {edit.get('field_path')} "
              f"{edit.get('old_value')} -> {edit.get('new_value')}", file=out)
        print("diff:", file=out)
        print(rec.get("unified_diff") or "", end="", file=out)


def _show(session_factory, incident_uuid, as_json: bool, out) -> int:
    from engine.patch_proposer import get_latest_proposal, serialise_proposal

    db = session_factory()
    try:
        row, status, reason = get_latest_proposal(db, incident_uuid)
        if row is None:
            print("no patch proposal recorded for this incident", file=out)
            return 1
        data = serialise_proposal(row, status, reason)
    except Exception as exc:  # noqa: BLE001 - e.g. the table does not exist yet
        print(f"could not read stored proposals: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        db.close()
    if as_json:
        print(json.dumps(data, indent=2), file=out)
    else:
        print(BANNER, file=out)
        print(f"proposal:   {data['id']} created {data['created_at']} (UTC)", file=out)
        print(f"status:     {data['effective_status']}"
              + (f" / {data['effective_reason_code']}" if data.get("effective_reason_code") else ""), file=out)
        if data["effective_status"] != data["status"]:
            print(f"stored as:  {data['status']}" + (f" / {data['reason_code']}" if data.get("reason_code") else ""),
                  file=out)
        if data["effective_status"] == "VALIDATED" and data.get("unified_diff"):
            print(data["unified_diff"], end="", file=out)
    return 0


def main(argv=None, session_factory=None, llm=None, repo_root=None, env=None, out=None) -> int:
    out = out or sys.stdout
    parser = _ArgumentParser(description="Propose and validate a remediation patch (never applied).",
                             allow_abbrev=False)
    parser.add_argument("incident_id", nargs="?", help="incident UUID to propose a patch for")
    parser.add_argument("--show", metavar="INCIDENT_ID", help="print the latest stored proposal (read-only)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    try:
        args = parser.parse_args(argv)
    except _UsageError:
        return EXIT_USAGE

    target = args.show or args.incident_id
    if not target or (args.show and args.incident_id):
        parser.print_usage(sys.stderr)
        return EXIT_USAGE
    try:
        incident_uuid = uuid.UUID(target)
    except ValueError:
        print(f"not a valid incident id: {target!r}", file=sys.stderr)
        return EXIT_USAGE

    if session_factory is None:
        try:
            session_factory = _default_session_factory(create_table=not args.show)
        except Exception as exc:  # noqa: BLE001
            print(f"database unavailable: {type(exc).__name__}", file=sys.stderr)
            return 1 if args.show else 5

    if args.show:
        return _show(session_factory, incident_uuid, args.json, out)

    from engine.patch_proposer import run_for_incident

    if not args.json:
        print("checking gates; if they pass, the local model is asked to choose a value "
              "(this can take a few minutes on CPU)...", file=sys.stderr)
    try:
        result = run_for_incident(session_factory, incident_uuid, repo_root or REPO_ROOT, llm=llm, env=env)
    except KeyboardInterrupt:
        print("interrupted: the run was recorded as FAILED/INTERRUPTED (if the database was reachable); "
              "nothing was applied", file=sys.stderr)
        return EXIT_INTERRUPTED
    if args.json:
        payload = {k: v for k, v in result.items() if k != "record"}
        payload.update({"applied": False, "requires_human_approval": True, "stage": "PROPOSAL_ONLY",
                        "record": result.get("record")})
        print(json.dumps(payload, indent=2, default=str), file=out)
    else:
        if result["exit_code"] == 1:
            print("unknown incident", file=out)
        elif result["exit_code"] == 6:
            print("another patch proposal for this incident is already running", file=out)
        elif result["exit_code"] == 7:
            print(f"{result['reason_detail']} (use --show)", file=out)
        else:
            _print_human(result, out)
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
