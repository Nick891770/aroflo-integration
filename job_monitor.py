"""
Job Completion Monitor (Module 1)

Polls AroFlo for completed tasks, proofreads them, marks Ready to Invoice,
and adds an audit note. Uses SQLite to track processed tasks so nothing
gets re-processed.

Usage:
    python job_monitor.py                    # One-shot: process new tasks, then exit
    python job_monitor.py --apply            # One-shot with changes applied
    python job_monitor.py --poll             # Continuous polling (for Raspberry Pi)
    python job_monitor.py --poll --apply     # Continuous polling with auto-apply
    python job_monitor.py --poll --interval 900  # Poll every 15 minutes
    python job_monitor.py --stats            # Show processing statistics
    python job_monitor.py --history          # Show recent poll run history
"""

import argparse
import difflib
import signal
import sys
import time
from datetime import datetime, timezone

import database as db
from aroflo_connector import create_connector
from proofreader import Proofreader


# Graceful shutdown flag
_shutdown = False


def _signal_handler(signum, frame):
    global _shutdown
    print("\nShutdown requested, finishing current run...")
    _shutdown = True


def get_completed_tasks(connector):
    """Fetch all tasks with status = Completed."""
    response = connector.request("tasks", {"where": "and|status|=|Completed"})
    zr = response.get("zoneresponse", response)
    tasks = zr.get("tasks", [])
    if not isinstance(tasks, list):
        tasks = [tasks] if tasks else []
    return tasks


def get_timesheets_by_job(connector):
    """Get all timesheets grouped by job number."""
    response = connector.request("timesheets", {"page": 1})
    zr = response.get("zoneresponse", {})
    timesheets = zr.get("timesheets", [])
    if not isinstance(timesheets, list):
        timesheets = [timesheets] if timesheets else []

    by_job = {}
    for ts in timesheets:
        task_info = ts.get("task", {})
        job_no = task_info.get("jobnumber", "") if isinstance(task_info, dict) else ""
        task_id = task_info.get("taskid", "") if isinstance(task_info, dict) else ""
        if job_no:
            if job_no not in by_job:
                by_job[job_no] = []
            user_info = ts.get("user", {})
            user_name = ""
            if isinstance(user_info, dict):
                given = user_info.get("givennames", "")
                surname = user_info.get("surname", "")
                user_name = f"{given} {surname}".strip()
            by_job[job_no].append({
                "timesheetid": ts.get("timesheetid"),
                "task_id": task_id,
                "note": ts.get("note", ""),
                "user": user_name,
                "workdate": ts.get("workdate", ""),
                "starttime": ts.get("startdatetime", ""),
            })
    return by_job


def process_single_task(task, proofreader, connector, timesheets_by_job, substatus_id, apply, run_id, conn):
    """Process one completed task: proofread, fix, mark ready, add note."""
    task_id = task.get("taskid")
    task_name = task.get("taskname", "Unnamed")
    job_no = task.get("jobnumber", "")
    description = task.get("description", "")

    # Get timesheet notes for this job
    timesheet_entries = timesheets_by_job.get(job_no, [])
    labour_notes = "\n\n".join([ts["note"] for ts in timesheet_entries if ts["note"]])

    # Combine text for proofreading
    all_text = []
    if description:
        all_text.append(description)
    if labour_notes:
        all_text.append(labour_notes)
    combined_text = "\n\n".join(all_text)

    # Proofread
    errors_found = 0
    description_fixed = False
    corrected_description = description
    manual_corrections = []

    if combined_text.strip():
        try:
            # Proofread description separately
            if description:
                corrected_description, desc_errors = proofreader._check_text(description)
                if description != corrected_description:
                    description_fixed = True
                    errors_found += len(desc_errors)

            # Proofread timesheet notes separately (can only flag, not fix)
            for ts in timesheet_entries:
                if ts["note"]:
                    try:
                        corrected_note, note_errors = proofreader._check_text(ts["note"])
                        if ts["note"] != corrected_note:
                            errors_found += len(note_errors)
                            manual_corrections.append({
                                "user": ts.get("user", "Unknown"),
                                "workdate": ts.get("workdate", ""),
                                "starttime": ts.get("starttime", ""),
                                "before": ts["note"],
                                "after": corrected_note,
                            })
                    except Exception:
                        pass

        except Exception as e:
            print(f"     Error proofreading: {e}")

    # Report findings
    status_parts = []
    if errors_found == 0:
        status_parts.append("clean")
    else:
        status_parts.append(f"{errors_found} error(s)")
    if description_fixed:
        status_parts.append("desc fix")
    if manual_corrections:
        status_parts.append(f"{len(manual_corrections)} note fix(es) needed")

    print(f"   [{', '.join(status_parts)}] {task_name[:50]}")

    if not apply:
        # Record as processed even in dry run so we can see what would happen
        # but DON'T actually record — we want to re-process on next real run
        return {
            "task_id": task_id,
            "task_name": task_name,
            "job_no": job_no,
            "errors_found": errors_found,
            "description_fixed": False,
            "manual_corrections": manual_corrections,
            "marked_ready": False,
            "note_added": False,
        }

    # Apply description fix
    if description_fixed and corrected_description:
        try:
            connector.update_task_description(task_id, corrected_description)
            print(f"     [OK] Description fixed")
        except Exception as e:
            print(f"     [FAIL] Description fix: {e}")
            description_fixed = False

    # Mark Ready to Invoice
    marked_ready = False
    substatus = task.get("substatus", {})
    current_substatus = substatus.get("substatus", "") if isinstance(substatus, dict) else ""
    if current_substatus != "Ready to Invoice":
        try:
            connector.update_task_substatus(task_id, substatus_id)
            marked_ready = True
            print(f"     [OK] Marked Ready to Invoice")
        except Exception as e:
            print(f"     [FAIL] Mark ready: {e}")

    # Add audit note to task
    note_added = False
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    note_parts = [f"Auto-proofread and marked Ready to Invoice on {now_str}."]
    if description_fixed:
        note_parts.append("Description spelling/grammar corrected.")
    if manual_corrections:
        note_parts.append(f"{len(manual_corrections)} timesheet note(s) need manual correction.")
    if errors_found == 0:
        note_parts = [f"Auto-proofread on {now_str} — no errors found. Marked Ready to Invoice."]

    try:
        connector.insert_task_note(task_id, " ".join(note_parts))
        note_added = True
        print(f"     [OK] Audit note added")
    except Exception as e:
        print(f"     [FAIL] Add note: {e}")

    # Record in SQLite
    db.mark_processed(
        conn,
        task_id=task_id,
        task_name=task_name,
        job_number=job_no,
        description_fixed=description_fixed,
        notes_needing_fix=len(manual_corrections),
        errors_found=errors_found,
        marked_ready=marked_ready,
        note_added=note_added,
        run_id=run_id,
    )

    return {
        "task_id": task_id,
        "task_name": task_name,
        "job_no": job_no,
        "errors_found": errors_found,
        "description_fixed": description_fixed,
        "manual_corrections": manual_corrections,
        "marked_ready": marked_ready,
        "note_added": note_added,
    }


def run_once(apply: bool = False) -> dict:
    """
    Run a single poll cycle. Returns summary stats.
    """
    conn = db.get_connection()
    mode = "one-shot"
    run_id = db.start_run(conn, mode)

    print(f"Job Completion Monitor — Run #{run_id}")
    print("=" * 60)
    if not apply:
        print("DRY RUN — no changes will be made")
        print("Run with --apply to apply changes\n")

    connector = create_connector()
    proofreader = Proofreader(connector)

    # Step 1: Get Ready to Invoice substatus ID
    print("1. Getting substatus ID...")
    substatus_id = connector.get_substatus_id("Ready to Invoice")
    if not substatus_id:
        print("   ERROR: Could not find 'Ready to Invoice' substatus")
        db.complete_run(conn, run_id, errors="Missing Ready to Invoice substatus")
        conn.close()
        return {"error": "Missing substatus"}
    print(f"   Found: Ready to Invoice")

    # Step 2: Fetch completed tasks
    print("\n2. Fetching completed tasks...")
    tasks = get_completed_tasks(connector)
    print(f"   Found {len(tasks)} completed tasks")

    if not tasks:
        db.complete_run(conn, run_id, tasks_found=0)
        conn.close()
        print("\n   Nothing to process.")
        return {"tasks_found": 0}

    # Step 3: Filter out already-processed tasks
    new_tasks = []
    skipped = 0
    for task in tasks:
        tid = task.get("taskid")
        if db.is_processed(conn, tid):
            skipped += 1
        else:
            new_tasks.append(task)

    print(f"   Already processed: {skipped}")
    print(f"   New tasks to process: {len(new_tasks)}")

    if not new_tasks:
        db.complete_run(conn, run_id, tasks_found=len(tasks), tasks_skipped=skipped)
        conn.close()
        print("\n   All tasks already processed.")
        return {"tasks_found": len(tasks), "tasks_new": 0}

    # Step 4: Fetch timesheets
    print("\n3. Fetching timesheets...")
    timesheets_by_job = get_timesheets_by_job(connector)
    print(f"   Found timesheets for {len(timesheets_by_job)} jobs")

    # Step 5: Process each new task
    print(f"\n4. Processing {len(new_tasks)} task(s)...\n")
    results = []
    all_manual_corrections = []

    for task in new_tasks:
        result = process_single_task(
            task, proofreader, connector, timesheets_by_job,
            substatus_id, apply, run_id, conn,
        )
        results.append(result)
        all_manual_corrections.extend(result.get("manual_corrections", []))

    # Step 6: Print manual corrections summary
    if all_manual_corrections:
        print(f"\n{'=' * 60}")
        print(f"MANUAL CORRECTIONS NEEDED ({len(all_manual_corrections)} timesheet notes)")
        print("(AroFlo API does not support updating timesheet notes)")
        print("-" * 60)

        for i, fix in enumerate(all_manual_corrections, 1):
            date_str = fix["workdate"]
            time_str = ""
            if fix["starttime"]:
                parts = fix["starttime"].split(" ")
                if len(parts) == 2:
                    time_str = parts[1][:5]

            print(f"\n   [{i}] Employee: {fix['user']}")
            print(f"       Date: {date_str}  Start: {time_str}")

            old_words = fix["before"].split()
            new_words = fix["after"].split()
            sm = difflib.SequenceMatcher(None, old_words, new_words)
            changes = []
            for op, i1, i2, j1, j2 in sm.get_opcodes():
                if op == "equal":
                    continue
                old_phrase = " ".join(old_words[i1:i2])
                new_phrase = " ".join(new_words[j1:j2])
                ctx_before = old_words[max(0, i1-1):i1]
                ctx_after = old_words[i2:min(len(old_words), i2+1)]
                ctx = ""
                if ctx_before:
                    ctx += f"...{ctx_before[0]} "
                ctx += f'"{old_phrase}" -> "{new_phrase}"'
                if ctx_after:
                    ctx += f" {ctx_after[0]}..."
                changes.append(ctx)

            if changes:
                for change in changes:
                    print(f"       Fix: {change}")

        print("\n" + "-" * 60)

    # Summary
    processed_count = len(results)
    errors_total = sum(r["errors_found"] for r in results)
    desc_fixed = sum(1 for r in results if r["description_fixed"])
    marked = sum(1 for r in results if r["marked_ready"])
    notes_added = sum(1 for r in results if r["note_added"])

    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"  Tasks found:           {len(tasks)}")
    print(f"  Already processed:     {skipped}")
    print(f"  New tasks processed:   {processed_count}")
    print(f"  Errors found:          {errors_total}")
    if apply:
        print(f"  Descriptions fixed:    {desc_fixed}")
        print(f"  Marked Ready:          {marked}")
        print(f"  Audit notes added:     {notes_added}")
        print(f"  Manual note fixes:     {len(all_manual_corrections)}")
    else:
        print(f"  (Dry run — no changes applied)")

    db_total = db.get_processed_count(conn)
    print(f"\n  Total in database:     {db_total}")

    db.complete_run(
        conn, run_id,
        tasks_found=len(tasks),
        tasks_new=len(new_tasks),
        tasks_processed=processed_count,
        tasks_skipped=skipped,
    )
    conn.close()

    return {
        "tasks_found": len(tasks),
        "tasks_new": len(new_tasks),
        "tasks_processed": processed_count,
        "errors_total": errors_total,
        "manual_corrections": len(all_manual_corrections),
    }


def run_polling(apply: bool = False, interval: int = 900):
    """
    Continuous polling mode for Raspberry Pi.

    Args:
        apply: Whether to apply changes
        interval: Seconds between polls (default 900 = 15 minutes)
    """
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    print(f"Starting continuous polling (every {interval}s)")
    print(f"Apply mode: {'ON' if apply else 'OFF (dry run)'}")
    print(f"Press Ctrl+C to stop\n")

    while not _shutdown:
        try:
            run_once(apply=apply)
        except Exception as e:
            print(f"\n[ERROR] Poll failed: {e}")
            # Log the error but keep polling
            conn = db.get_connection()
            run_id = db.start_run(conn, "poll-error")
            db.complete_run(conn, run_id, errors=str(e))
            conn.close()

        if _shutdown:
            break

        print(f"\nNext poll in {interval}s ({interval // 60}m)...")
        # Sleep in small increments so we can catch shutdown signals
        for _ in range(interval):
            if _shutdown:
                break
            time.sleep(1)

    print("\nMonitor stopped.")


def show_stats():
    """Display processing statistics."""
    conn = db.get_connection()
    stats = db.get_stats(conn)
    conn.close()

    print("Job Monitor Statistics")
    print("=" * 40)
    print(f"  Total tasks processed:     {stats['total_processed']}")
    print(f"  Tasks with errors:         {stats['with_errors']}")
    print(f"  Descriptions auto-fixed:   {stats['descriptions_fixed']}")
    print(f"  Timesheet notes pending:   {stats['timesheet_notes_pending']}")
    print(f"  Total poll runs:           {stats['total_runs']}")


def show_history():
    """Display recent poll run history."""
    conn = db.get_connection()
    runs = db.get_recent_runs(conn, limit=20)
    conn.close()

    if not runs:
        print("No poll runs recorded yet.")
        return

    print("Recent Poll Runs")
    print("=" * 80)
    print(f"{'Run':>4}  {'Started':>20}  {'Found':>5}  {'New':>4}  {'Done':>4}  {'Skip':>4}  {'Errors'}")
    print("-" * 80)

    for run in runs:
        started = run["started_at"][:19].replace("T", " ") if run["started_at"] else "?"
        errors_str = run["errors"][:30] if run["errors"] else ""
        print(
            f"{run['run_id']:>4}  {started:>20}  "
            f"{run['tasks_found'] or 0:>5}  {run['tasks_new'] or 0:>4}  "
            f"{run['tasks_processed'] or 0:>4}  {run['tasks_skipped'] or 0:>4}  "
            f"{errors_str}"
        )


def main():
    parser = argparse.ArgumentParser(
        description="Job Completion Monitor — polls AroFlo for completed tasks",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Apply changes (default is dry run)",
    )
    parser.add_argument(
        "--poll", action="store_true",
        help="Continuous polling mode (for Raspberry Pi)",
    )
    parser.add_argument(
        "--interval", type=int, default=900,
        help="Poll interval in seconds (default: 900 = 15 min)",
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Show processing statistics",
    )
    parser.add_argument(
        "--history", action="store_true",
        help="Show recent poll run history",
    )

    args = parser.parse_args()

    if args.stats:
        show_stats()
        return 0

    if args.history:
        show_history()
        return 0

    if args.poll:
        run_polling(apply=args.apply, interval=args.interval)
        return 0

    result = run_once(apply=args.apply)
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    sys.exit(main())
